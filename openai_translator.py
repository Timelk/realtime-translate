#!/usr/bin/env python3
"""OpenAI gpt-realtime-translate 客户端 — 端到端 speech → translated speech

协议(2026 版,经官方 reference 双重核对):
  客户端发: session.update / session.input_audio_buffer.append / session.close
  服务端发: session.{created,updated,closed} / session.{input,output}_transcript.delta
            / session.output_audio.delta / error

输入: 24kHz PCM16 mono (base64 in `audio` field)
输出: 24kHz PCM16 mono, 200ms 帧 (base64 in `delta` field, sample_rate 字段告知)
"""

import os
import json
import base64
import threading
import time
import websocket
from dotenv import load_dotenv
from logger import log, ok, err

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_URL = "wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate"

# 13 种目标语言 — TODO: 待 OpenAI Playground 抄实际名单替换
# (https://platform.openai.com/playground 选 gpt-realtime-translate 看下拉)
# 以下是常见 13 种推测,落不在此集合的目标语言会自动 fallback DashScope
OPENAI_LANGS = {
    "en", "zh", "ja", "ko", "es", "fr", "de", "it", "pt", "ru",
    "ar", "hi", "id",
}


class OpenAITranslator:
    def __init__(self, target_lang, on_partial_src, on_partial_tgt, on_audio, on_session_lost=None):
        self.target_lang = target_lang
        self.on_partial_src = on_partial_src
        self.on_partial_tgt = on_partial_tgt
        self.on_audio = on_audio
        self.on_session_lost = on_session_lost  # 被服务端 session.closed 或 ws 异常关时触发
        self.ws = None
        self.session_ready = threading.Event()
        self.done = threading.Event()
        self._closing = False  # 主动 close() 时不触发 on_session_lost
        # 流式日志聚合 — 每秒一次摘要,避免 token 级刷屏
        self._src_buf = ""
        self._tgt_buf = ""
        self._audio_bytes = 0
        self._last_flush = 0.0

    def _flush_streaming(self, force=False):
        now = time.time()
        if not force and now - self._last_flush < 1.0:
            return
        if self._src_buf:
            log("openai", f"src ← {self._src_buf!r}")
            self._src_buf = ""
        if self._tgt_buf:
            log("openai", f"tgt ← {self._tgt_buf!r}")
            self._tgt_buf = ""
        if self._audio_bytes:
            log("openai", f"audio ← {self._audio_bytes} bytes (last 1s)")
            self._audio_bytes = 0
        self._last_flush = now

    def _on_message(self, _, raw):
        try:
            ev = json.loads(raw)
        except Exception:
            return
        t = ev.get("type")

        # 调试: 所有非已知-流式事件 + 每个事件至少 log 一次类型
        # (流式 delta 由 _flush_streaming 聚合, 这里只 log 非流式)
        if t not in (
            "session.input_transcript.delta",
            "session.output_transcript.delta",
            "session.output_audio.delta",
        ):
            log("openai", "←", t, json.dumps({k: v for k, v in ev.items() if k != "type"}, ensure_ascii=False)[:200])

        if t == "session.created":
            sid = ev.get("session", {}).get("id", "?")
            log("openai", f"← session.created id={sid}")
            self.ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "audio": {
                        "input": {
                            "transcription": {"model": "gpt-realtime-whisper"},
                            "noise_reduction": {"type": "near_field"},
                        },
                        "output": {"language": self.target_lang},
                    },
                },
            }))
        elif t == "session.updated":
            ok("openai", f"session ready target={self.target_lang}")
            self.session_ready.set()
        elif t == "session.input_transcript.delta":
            d = ev.get("delta", "")
            if d:
                self._src_buf += d
                if self.on_partial_src:
                    self.on_partial_src(d)
                self._flush_streaming()
        elif t == "session.output_transcript.delta":
            d = ev.get("delta", "")
            if d:
                self._tgt_buf += d
                if self.on_partial_tgt:
                    self.on_partial_tgt(d)
                self._flush_streaming()
        elif t == "session.output_audio.delta":
            try:
                pcm = base64.b64decode(ev["delta"])
                self._audio_bytes += len(pcm)
                if self.on_audio:
                    self.on_audio(pcm, ev.get("sample_rate", 24000))
                self._flush_streaming()
            except Exception as e:
                err("openai", f"audio decode: {e}")
        elif t == "session.closed":
            self._flush_streaming(force=True)
            log("openai", "← session.closed")
            self.done.set()
            if self.on_session_lost and not self._closing:
                try: self.on_session_lost()
                except Exception as e:
                    log("openai", f"on_session_lost: {e}")
        elif t == "error":
            err("openai", json.dumps(ev.get("error", ev), ensure_ascii=False))
            self.done.set()
        else:
            log("openai", "←", t)

    def _on_error(self, _, e):
        err("openai", f"ws: {e}")
        self.done.set()

    def _on_close(self, *_):
        self.done.set()
        if self.on_session_lost and not self._closing:
            try: self.on_session_lost()
            except Exception as e:
                log("openai", f"on_session_lost(ws close): {e}")

    def connect(self):
        if not OPENAI_API_KEY:
            err("openai", "OPENAI_API_KEY 未设置 — export OPENAI_API_KEY=sk-...")
            return False
        h = [f"Authorization: Bearer {OPENAI_API_KEY}"]
        self.ws = websocket.WebSocketApp(
            OPENAI_URL, header=h,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        threading.Thread(target=self.ws.run_forever, daemon=True).start()
        return self.session_ready.wait(15)

    def send_audio(self, pcm_24k):
        try:
            self.ws.send(json.dumps({
                "type": "session.input_audio_buffer.append",
                "audio": base64.b64encode(pcm_24k).decode(),
            }))
        except Exception as e:
            err("openai", f"send: {e}")

    def update_target_lang(self, lang):
        """运行时切换目标语言 — 通过再发一次 session.update"""
        if not self.ws or not self.session_ready.is_set():
            return
        self.target_lang = lang
        try:
            self.ws.send(json.dumps({
                "type": "session.update",
                "session": {"audio": {"output": {"language": lang}}},
            }))
            log("openai", f"→ session.update target={lang}")
        except Exception as e:
            err("openai", f"update_target: {e}")

    def close(self):
        """主动关闭 ws — 发 session.close 协议消息 + 真正 close socket"""
        self._closing = True
        try:
            self.ws.send(json.dumps({"type": "session.close"}))
        except Exception:
            pass
        try:
            self.ws.close()
        except Exception:
            pass
