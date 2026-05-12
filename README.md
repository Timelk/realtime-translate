# 实时语音转译系统 v0.4

```
              ┌─ ⚡ OpenAI gpt-realtime-translate (国外, 端到端流式)
语音输入 ──┤
              └─ 🔁 Qwen3-ASR + DeepSeek-V4 + Qwen3-TTS (国内, 三跳)
```

双引擎实时语音翻译,**单人字幕模式** + **群组会议**,自带用户登录 / 历史录音 / Markdown 导出。
移动端 Web UI 优先,FastAPI + SQLite 后端,无外部数据库依赖。

---

## 目录

1. [设计要点](#1-设计要点)
2. [系统架构](#2-系统架构)
3. [快速开始](#3-快速开始)
4. [用户管理 (admin_cli)](#4-用户管理-admin_cli)
5. [前端功能](#5-前端功能)
6. [双引擎对比](#6-双引擎对比)
7. [群组会议协议](#7-群组会议协议)
8. [WebSocket 命令协议](#8-websocket-命令协议)
9. [REST 端点](#9-rest-端点)
10. [SQLite Schema](#10-sqlite-schema)
11. [文件结构](#11-文件结构)
12. [运维 (serve.sh)](#12-运维-servesh)
13. [VS Code 调试](#13-vs-code-调试)
14. [故障排查](#14-故障排查)
15. [已知限制](#15-已知限制)

---

## 1. 设计要点

- **双引擎热切换**:OpenAI(国外,流式 speech-to-speech)/ Qwen+DP4(国内,ASR+翻译+TTS 三跳),根据用户语言/网络/目标语言自动路由
- **网络预检**:启动时探测 `gstatic.com` 和 `baidu.com`,不可达的引擎 tab 自动禁用,两边都不通显示全屏遮罩
- **登录强制**:所有数据 API + WebSocket 都要 cookie 认证。**未登录无法使用任何功能**。注册关闭,账号由管理员通过 `admin_cli.py` 创建
- **群组持久化**:每场会议在 SQLite 留 `rooms` + `room_members` + `room_messages` 三张表,服务重启不丢,邀请码长期有效直到 host 关闭
- **录音历史**:单人翻译每次会话存 `recordings` 表,每个用户隔离,可下载 Markdown
- **字幕模式 UI**:单人模式只展示译文,连续滚动,turn 边界(1.5s 静默)落库,体感像同传字幕

---

## 2. 系统架构

```
浏览器 (移动端优先)                       FastAPI :8800
─────────────────                       ──────────────────────────────
viewAuth (登录) ──cookie── ws/HTTP ───→ session check (rt_session)
                                         │
viewLanding                              ├── /auth/* (login/logout/me)
  ├── 单人翻译 ──────WS────────────────→  ├── /ws (WebSocket)
  ├── 创建会议                            │     ├── ASRClient (Qwen3-ASR wss)
  └── 加入会议                            │     ├── OpenAITranslator (gpt-realtime-translate wss)
                                         │     ├── TTSClient (Qwen3-TTS wss, 仅 Qwen 模式)
viewSolo (字幕)  ←──── transcript ──→    │     └── translate() (DeepSeek HTTP)
viewRoom (多人) ←─── room_message ──→    │
                                         ├── /recordings (db)
                                         ├── /rooms (db)
                                         └── /export?code=XXX (markdown)

                            ┌─────────── app.db (SQLite WAL) ───────────┐
                            │  users / sessions / recordings             │
                            │  rooms / room_members / room_messages      │
                            └────────────────────────────────────────────┘
```

### 2.1 引擎路由

```python
# server.py:pick_backend
def pick_backend(config):
    if config['engine'] in ('openai', 'dashscope'):
        return config['engine']           # 用户显式选
    # auto:同时满足 translate + target ∈ 13 种 → openai
    if config['translate'] and config['target'] in OPENAI_LANGS:
        return 'openai'
    return 'dashscope'
```

前端 `selectEngine()` 在切到 OpenAI 但当前 target 不在 13 种内时,**自动切到中文 + toast 提示**。

---

## 3. 快速开始

### 3.1 依赖

```bash
uv sync                  # Python 3.14, fastapi, dotenv, python-dotenv, ...
cp .env.example .env     # 填三个 API Key
# DASHSCOPE_API_KEY=sk-...
# DEEPSEEK_API_KEY=sk-...
# OPENAI_API_KEY=sk-...
```

### 3.2 创建首个管理员账号

```bash
uv run admin_cli.py create admin@local.test Admin
# 提示输入密码 (≥6 位), 两次
```

### 3.3 启动 + 访问

```bash
./serve.sh start                # 后台 + reload + 健康检查, 输出 http://localhost:8800
./serve.sh logs                 # tail 日志
./serve.sh status               # PID + 端口 + 健康检查
./serve.sh stop                 # 优雅停止
```

浏览器开 `http://localhost:8800` → 用刚创建的账号登录。

---

## 4. 用户管理 (admin_cli)

注册接口已关闭(`/auth/register` 返回 403)。所有账号由管理员命令行创建:

```bash
uv run admin_cli.py create <email> <nickname>    # 创建用户 (提示密码两次)
uv run admin_cli.py list                          # 列所有用户
uv run admin_cli.py passwd <email>                # 改密 + 撤销该用户所有 session
uv run admin_cli.py delete <email>                # 删除用户 + 级联删 recordings/rooms (需 y 确认)
```

密码用 `hashlib.scrypt(n=2^14, r=8, p=1)` 哈希,Session token 是 `secrets.token_urlsafe(32)`,TTL 30 天,存 `sessions` 表,通过 cookie `rt_session`(`httponly + samesite=lax`)下发。

---

## 5. 前端功能

### 5.1 视图切换

| 视图 | id | 说明 |
|---|---|---|
| 登录 | `viewAuth` | 默认入口,未登录强制显示 |
| 首页 | `viewLanding` | 3 卡片:单人 / 创建会议 / 加入会议 |
| 单人 | `viewSolo` | 字幕模式 + 引擎切换 + 录音历史 |
| 群组 | `viewRoom` | 多说话人 + 邀请码 + 持久化 + 导出 |

### 5.2 单人字幕模式

- **只显示译文**(不展示原文),turn 间 1.5 秒静默断句
- 翻译延迟时灰色斜体显示**临时占位**(让用户感知到在工作)
- turn 完成自动 POST 给 server 落 `recordings` 表
- dock 上 `↓` 下载本次 `.md`,`📜` 弹出历史列表

### 5.3 群组会议

- **创建**:输昵称 + 目标语言 → 拿到 6 位邀请码(`A-Z2-9` 排除 O/0/I/1)
- **加入**:URL `?room=CODE` 自动弹加入框,或在首页点"加入会议"输码
- **多说话人**:头像呼吸 + 不同色,正在发言的成员头像有橘色脉动光环
- **导出**:`↓` 按钮下载完整对话 Markdown(含所有 target 译文)
- **关闭**:全员离开 → 房间自动 close,邀请码失效;**消息记录永久保留**

### 5.4 网络预检

启动并行 `fetch(no-cors, abort 4s)`:

| google 通 | baidu 通 | 表现 |
|---|---|---|
| ✓ | ✓ | 双引擎都可点 |
| ✗ | ✓ | OpenAI 灰显 + hover 提示;auto 自动 fallback Qwen |
| ✓ | ✗ | Qwen 灰显;auto fallback OpenAI |
| ✗ | ✗ | 全屏遮罩 + 重试按钮 |

### 5.5 语言 picker

- 26 种语言(Qwen 全集)
- 国旗 + 中文名 + ISO 代码 + ✓ 当前选中
- 搜索:中文 / 英文 / 原文 / 代码 都能匹配(`thai` / `泰` / `th` 都找到)
- OpenAI 引擎下 target 自动过滤为 13 种;auto 引擎下显示全 26 种 + 标 ⚡ OpenAI 直译可用

---

## 6. 双引擎对比

| 维度 | ⚡ OpenAI (国外) | 🔁 Qwen+DP4 (国内) |
|---|---|---|
| 协议 | `wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate` | DashScope ASR wss + DeepSeek HTTP + DashScope TTS wss |
| 端点数 | **1** | **3** |
| 输入采样率 | 24kHz PCM16 | 16kHz PCM16(server 端把 24k 降到 16k) |
| 源语言数 | 70+ 自动检测 | 26 种(可手动 / auto) |
| 目标语言数 | **13** | DeepSeek 翻译不限 / TTS 仅 10 种声学模型 |
| 端到端延迟 | ~500-800ms 流式 | ~2-4s 句子级(VAD 断句 + DeepSeek + TTS 重连) |
| 音色 | dynamic (跟随说话人) | 8 选 1 |
| 自定义 prompt | ❌ | ✓ 内置"严格翻译机器"提示词 |
| 计费 | $0.034/min(按音频时长) | ¥0.0028/min ASR + DeepSeek + ¥0.0008/千字 TTS |
| 已知 quirk | session.closed 频繁触发(server 已实现自动 reconnect) | TTS 逐句重连(单 ws 一句) |

### 6.1 OpenAI 自动重连

OpenAI 实际行为偏离文档:**session 经常被服务端主动关**(~每句话一次)。`OpenAITranslator.on_session_lost` 回调反向调度 asyncio 重连协程,从 `_reconnect_openai` 在 thread pool 跑阻塞的 `connect()`,前端无感。

---

## 7. 群组会议协议

### 7.1 服务端流程(DashScope 路径,默认)

```
speaker.audio (24k) ──┐
                       ├──→ server downsample → Qwen3-ASR (16k)
                       │     ↓ on_transcript_room
                       │     for each member.target in room:
                       │       translate(text, target) ──→ DeepSeek
                       │     ↓
                       └──→ broadcast room_message + room_translation 给所有 member
                            ↓
                            db.room_add_message(code, speaker, src, translations)
```

### 7.2 服务端流程(OpenAI 路径)

```
speaker.audio ──→ OpenAITranslator (target = speaker 自己的 target)
                    ↓ on_room_openai_src/tgt/audio
                    broadcast 给所有 member (共享 speaker target, listener 看同一译文)
                    speak_stop → 累积 src+tgt → db.room_add_message
```

**注意**:OpenAI 群组下 listener 看的是 **speaker 的 target**,不是 listener 自己的 target。多 target 个性化是 v2。

### 7.3 持久化

- `rooms`:邀请码 + 名称 + host_user_id + closed_at
- `room_members`:每个 user 一行 + joined_at / left_at,**user 重新 join 会更新而不是插新**(ON CONFLICT)
- `room_messages`:所有 speaker 说的话 + 各 target 的翻译(`translations_json` 是 `{lang: text}`)

服务重启,内存 `RoomManager` 为空。下一次 `join_room` 命令触发 `RoomManager.get(code)`:**内存优先,db fallback**(自动从 db 加载未 closed 的房间到内存)。

---

## 8. WebSocket 命令协议

`wss://host/ws` 要求 cookie `rt_session`,未登录立即 close。

### 8.1 客户端 → 服务端

| command | 字段 | 说明 |
|---|---|---|
| `start` | lang, target, translate, tts, voice, engine | 单人翻译启动后端 |
| `audio` | data (base64 PCM16 24kHz) | 音频块 |
| `stop` | — | 停止单人录音 |
| `update_config` | lang/target/translate/tts/voice/engine 任一 | 运行时改配置 → server 检测到 target 变化自动调 OpenAI `session.update` 热切语言 |
| `tts` | text | 手动 TTS(单人) |
| `record_entry` | src, tgt, lang | 单人 turn 完成上报落 recordings |
| `create_room` | room_name, name, target | 创建群组 |
| `join_room` | code, name, target | 加入群组 |
| `leave_room` | — | 离开 |
| `speak_start` | — | 房间内开始发言(启动 ASR 或 OpenAITranslator) |
| `speak_stop` | — | 停止发言(并 flush OpenAI 累积到 db) |
| `ping` | — | 心跳 |

### 8.2 服务端 → 客户端

| type | 字段 | 说明 |
|---|---|---|
| `welcome` | recording_id | ws connect 后立即推 |
| `ready` | engine, recording_id | start / speak_start 后端就绪 |
| `transcript` | text, lang, incremental | 单人原文 |
| `translation` | text, incremental | 单人译文 |
| `audio` | data, sample_rate | TTS 音频 |
| `stopped` | — | 单人 stop ACK |
| `config_updated` | translate, tts | update_config ACK |
| `room_joined` | code, room_name, members, you, your_target | 加入房间 |
| `member_joined` | member | 广播新成员 |
| `member_left` | id | 广播成员离开 |
| `speaking` | id, speaking | 头像呼吸状态广播 |
| `room_message` | turn_id, speaker_id, speaker_name, src_lang, text, incremental, ts | 房间原文 |
| `room_translation` | turn_id, speaker_id, text, target_lang, incremental, final, ts | 房间译文 |
| `error` | message, code | 错误(含 `auth_required`) |

---

## 9. REST 端点

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/` | — | static index.html |
| GET | `/langs` | — | 语言名称 dict |
| GET | `/voices` | — | TTS 音色列表 |
| POST | `/auth/login` | — | body `{email, password}` → set cookie |
| POST | `/auth/logout` | — | 撤销当前 token,清 cookie |
| POST | `/auth/register` | — | **403 关闭**(管理员 CLI 创建) |
| GET | `/auth/me` | ✓ | 当前用户 |
| GET | `/recordings` | ✓ | 当前用户的单人录音列表 |
| GET | `/recordings/{id}.md` | ✓ | 下载 Markdown attachment |
| DELETE | `/recordings/{id}` | ✓ | 删除录音 |
| GET | `/rooms` | ✓ | 当前用户参与过的所有房间 |
| GET | `/export?code=XXX` | ✓ | 房间完整对话 Markdown |
| WS | `/ws` | ✓ (cookie) | 主要业务通道 |

---

## 10. SQLite Schema

`app.db` 在项目根,WAL 模式,无 Python 层全局锁(`per-call connection` + `@contextmanager`)。

```sql
CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  nickname TEXT NOT NULL,
  password_hash TEXT NOT NULL,    -- "salt_hex:hash_hex"
  created_at REAL NOT NULL
);

CREATE TABLE sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE recordings (
  id TEXT PRIMARY KEY,             -- "20260511-150432-089b"
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL,              -- 'solo' | 'room'
  name TEXT NOT NULL,
  created_at REAL NOT NULL,
  entries_json TEXT NOT NULL DEFAULT '[]',
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE rooms (
  code TEXT PRIMARY KEY,           -- "Q3PLAN" 6 位
  name TEXT NOT NULL,
  host_user_id INTEGER NOT NULL,
  created_at REAL NOT NULL,
  closed_at REAL,                  -- NULL = 还活着
  FOREIGN KEY (host_user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE room_members (
  room_code TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  nickname TEXT NOT NULL,
  target_lang TEXT NOT NULL,
  color INTEGER NOT NULL DEFAULT 0,
  joined_at REAL NOT NULL,
  left_at REAL,
  PRIMARY KEY (room_code, user_id)
);

CREATE TABLE room_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_code TEXT NOT NULL,
  speaker_user_id INTEGER NOT NULL,
  speaker_name TEXT NOT NULL,
  src_lang TEXT,
  src TEXT,
  translations_json TEXT NOT NULL DEFAULT '{}',
  ts REAL NOT NULL
);
```

---

## 11. 文件结构

```
qwen3-asr-flash-realtime/
├── server.py                # FastAPI 主服务 (1100+ 行)
├── db.py                    # SQLite + auth + recording + room helpers
├── openai_translator.py     # gpt-realtime-translate 客户端
├── logger.py                # 统一日志格式 (彩色 + 时间戳 + 模型 tag)
├── admin_cli.py             # 管理员 CLI (create/list/passwd/delete)
├── verify_openai.py         # OpenAI 端到端连通验证脚本
├── test.py                  # 原 CLI 工具 (DashScope 单机版)
├── serve.sh                 # 启停脚本 (start/stop/restart/status/logs/fg)
├── static/index.html        # 单页 Web UI (~ 1700 行)
├── .vscode/launch.json      # 5 个调试入口
├── .env / .env.example      # 三个 API Key
├── design-mockups/          # 9 个 UI 风格 mockup (HTML)
├── pyproject.toml           # 依赖
└── app.db                   # SQLite (运行时生成, .gitignore)
```

---

## 12. 运维 (serve.sh)

```bash
./serve.sh start    # 后台启动 + curl 健康轮询 (6s 内就绪)
./serve.sh stop     # 杀 PIDFILE + lsof 兜底所有占端口的子进程
./serve.sh restart  # stop + start
./serve.sh status   # PID + 端口 + HTTP 健康检查
./serve.sh logs     # tail -f .server.log
./serve.sh fg       # 前台 (Ctrl+C 退)
PORT=9000 ./serve.sh start    # 自定义端口
```

uvicorn `--reload` 子进程问题:`stop` 用 `lsof -ti:8800` 一锅端 reloader + worker。

---

## 13. VS Code 调试

`.vscode/launch.json` 5 个配置:

- **Web: FastAPI server (uvicorn)** — 调试主服务(无 reload,断点稳定)
- **CLI: ASR stream + 翻译 + 朗读** — `test.py --stream -t`
- **CLI: TTS 单句** — `test.py --tts -t "..."`
- **Verify: OpenAI 端到端 (10s)** — `verify_openai.py`
- **Verify: 探测 OpenAI 支持的目标语言** — `verify_openai.py --probe-langs`

全部 `envFile: ${workspaceFolder}/.env`,`python: ${workspaceFolder}/.venv/bin/python`,无需 VS Code 全局解释器配置。

---

## 14. 故障排查

### 14.1 POST 请求挂死,GET 工作
**已修复**。旧版 `db.py` 用 `threading.Lock + module-level _CONN`,async 上下文持锁阻塞 event loop。现在 `per-call connection` + WAL 自带并发。

### 14.2 OpenAI 切换目标语言不生效
关键日志(`tail .server.log`):

```
Router · update_config target zh→en engine auto→auto backend=openai oai_client=yes
OpenAI-RT-Tx · update_target_lang → en
```

如果第一行有但第二行没,说明 `current_backend != ENGINE_OPENAI` 或 `openai_client is None`(正在重连)。

### 14.3 OpenAI session.closed 第一句后就停
**已修复**。`OpenAITranslator.on_session_lost` 回调反向 schedule asyncio reconnect,旧 session close 在新 session ready 后才做。前端无感。

### 14.4 OpenAI src=tgt 时不发译文
模型行为(中文 → 中文 = echo,无翻译)。前端 `selectEngine('openai')` 时检测 `target == 'zh' && lang == 'auto'`(默认场景)→ toast 提示用户切目标。

### 14.5 房间内 OpenAI 不可用
**已实现**。`speak_start` 检测 `pick_backend(config)`,选 OpenAI 启动 `OpenAITranslator`,流式 delta 共享 `speak_turn_id` 广播给所有成员。**注意**:room 模式下 listener 看到的是 speaker 的 target,不是自己的。

### 14.6 未登录访问应用
所有数据 API + WebSocket 都要 cookie。前端 init 时 `fetch /auth/me`:200 → 进 landing;401 → 强制显示 viewAuth。

---

## 15. 已知限制

| 限制 | 严重度 | v2 计划 |
|---|---|---|
| 群组 OpenAI 模式所有 listener 共享 speaker target | 🟡 中 | 每 listener 独立 OpenAI session(成本 N×$0.034/min) |
| 群组听众无 TTS 朗读(Qwen+DP4 路径下) | 🟡 中 | OpenAI 已转发,Qwen 路径下仅字幕 |
| 房间历史无搜索 / 分页 | 🟢 低 | room_messages 表已有 ts 索引,前端加搜索 |
| Markdown 导出无富格式 | 🟢 低 | 可选 PDF / DOCX |
| 移动端 Safari 长时间录音 audioCtx suspend | 🟢 低 | AudioWorkletNode 替代 ScriptProcessorNode |
| 群组同 user 多 ws tab 用同色头像 | 🟢 低 | Room.members dict 改用 user_id 作 key |
| OpenAI API 在中国大陆需代理 | 🔴 高 | 用户网络预检会自动 fallback Qwen |
| 单机 SQLite | 🟢 低 | 量上去后迁 PostgreSQL |

---

## 附录

### A. 启动检查清单

```bash
uv sync                                           # 1. 依赖
cp .env.example .env                              # 2. 填 3 个 API Key
uv run admin_cli.py create admin@local Admin      # 3. 创首位管理员
uv run verify_openai.py                           # 4. (可选) 验证 OpenAI 连通
./serve.sh start                                  # 5. 启动
open http://localhost:8800                        # 6. 浏览器
```

### B. 重置整个项目数据

```bash
./serve.sh stop
rm -f app.db app.db-wal app.db-shm
./serve.sh start
uv run admin_cli.py create admin@local Admin
```

### C. 安全说明

- 不登录无法访问任何业务功能(`/auth/*` 外的所有端点 + WebSocket 都要 cookie)
- 注册接口已关闭,只能通过 `admin_cli.py` 在服务器命令行创建账号
- 密码 `hashlib.scrypt` 哈希 + 16 字节 salt,无明文存储
- Session token 32 字节 url-safe random,30 天 TTL,改密时自动撤销所有该用户的 session
- `recordings` / `rooms` 数据按 `user_id` 隔离,SQL 层强制 `WHERE user_id = ?`,跨用户访问返回 404
