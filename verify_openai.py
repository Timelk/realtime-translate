#!/usr/bin/env python3
"""验证 OpenAI gpt-realtime-translate 端到端连通 + 延迟

用法:
  export OPENAI_API_KEY=sk-...
  uv run verify_openai.py                   # 默认翻译到中文 (zh), 录 10 秒
  uv run verify_openai.py --target en       # 翻译到英文
  uv run verify_openai.py --target ja -d 15 # 日语, 15 秒
  uv run verify_openai.py --probe-langs     # 探测哪些 ISO 639-1 代码服务端接受

期望输出:
  [源] (实时识别原文增量)
  [译] (实时翻译增量)
  播放翻译后的音频
  [指标] 第一个 audio delta 到达时间 = ?ms
"""

import os
import sys
import time
import json
import base64
import threading
import argparse
import tempfile
import wave
import subprocess
import websocket
import pyaudio
from dotenv import load_dotenv

load_dotenv()

KEY = os.getenv("OPENAI_API_KEY", "")
URL = "wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate"


def run_session(target, duration):
    if not KEY:
        print("✗ 未设置 OPENAI_API_KEY")
        return False

    audio_chunks = []
    first_delta_at = [None]
    first_audio_at = [None]
    t_first_send = [None]
    errors = []
    session_ready = threading.Event()
    done = threading.Event()

    def on_msg(ws, raw):
        try:
            ev = json.loads(raw)
        except Exception:
            return
        t = ev.get("type", "")
        if t == "session.created":
            print(f"  ✓ session.created (id={ev.get('session', {}).get('id', '?')})")
            ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "audio": {
                        "input": {
                            "transcription": {"model": "gpt-realtime-whisper"},
                            "noise_reduction": {"type": "near_field"},
                        },
                        "output": {"language": target},
                    },
                },
            }))
        elif t == "session.updated":
            print(f"  ✓ session.updated, target={target}")
            session_ready.set()
        elif t == "session.input_transcript.delta":
            if first_delta_at[0] is None and t_first_send[0]:
                first_delta_at[0] = (time.time() - t_first_send[0]) * 1000
            sys.stdout.write(f"\033[36m{ev.get('delta', '')}\033[0m")
            sys.stdout.flush()
        elif t == "session.output_transcript.delta":
            sys.stdout.write(f"\033[33m{ev.get('delta', '')}\033[0m")
            sys.stdout.flush()
        elif t == "session.output_audio.delta":
            if first_audio_at[0] is None and t_first_send[0]:
                first_audio_at[0] = (time.time() - t_first_send[0]) * 1000
            audio_chunks.append(base64.b64decode(ev["delta"]))
        elif t == "session.closed":
            done.set()
        elif t == "error":
            err = ev.get("error", ev)
            print(f"\n✗ {json.dumps(err, ensure_ascii=False)}")
            errors.append(err)
            done.set()

    def on_err(ws, err):
        print(f"\n✗ ws error: {err}")
        errors.append(str(err))
        done.set()

    def on_close(*_):
        done.set()

    print(f"→ 连接 {URL}")
    ws = websocket.WebSocketApp(
        URL,
        header=[f"Authorization: Bearer {KEY}"],
        on_message=on_msg, on_error=on_err, on_close=on_close,
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

    if not session_ready.wait(15):
        print("✗ session 15 秒内未就绪")
        return False

    p = pyaudio.PyAudio()
    s = p.open(format=pyaudio.paInt16, channels=1, rate=24000,
               input=True, frames_per_buffer=2400)
    print(f"\n🎤 说话 {duration} 秒...\n")

    t0 = time.time()
    try:
        while time.time() - t0 < duration and not done.is_set():
            pcm = s.read(2400, exception_on_overflow=False)
            if t_first_send[0] is None:
                t_first_send[0] = time.time()
            ws.send(json.dumps({
                "type": "session.input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode(),
            }))
    except KeyboardInterrupt:
        pass
    s.stop_stream()
    s.close()
    p.terminate()

    print("\n\n→ 等待最后的翻译 (3 秒)...")
    time.sleep(3)
    ws.send(json.dumps({"type": "session.close"}))
    done.wait(3)

    print("\n[指标]")
    print(f"  首个源 transcript delta: {first_delta_at[0]:.0f}ms" if first_delta_at[0] else "  首个源 transcript: 无")
    print(f"  首个译音 audio delta:    {first_audio_at[0]:.0f}ms" if first_audio_at[0] else "  首个译音: 无")
    print(f"  累积音频块: {len(audio_chunks)} 个 ({sum(len(c) for c in audio_chunks)} bytes)")
    if errors:
        print(f"  错误: {errors}")
        return False

    if audio_chunks:
        path = os.path.join(tempfile.gettempdir(), "openai_translate_out.wav")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(b"".join(audio_chunks))
        print(f"\n→ 播放: {path}")
        subprocess.run(["afplay", path], check=False)
    return True


def probe_languages():
    """探测哪些 ISO 639-1 代码服务端接受 — 通过 session.update 试错"""
    if not KEY:
        print("✗ 未设置 OPENAI_API_KEY")
        return
    candidates = [
        "en", "zh", "zh-CN", "zh-TW", "ja", "ko", "es", "fr", "de", "it",
        "pt", "ru", "ar", "hi", "id", "th", "tr", "vi", "nl", "sv", "da",
        "fi", "pl", "cs", "ms", "no", "fil", "tl", "he", "uk", "ro", "el",
    ]
    accepted, rejected = [], []

    for code in candidates:
        result = {"ok": None, "err": None}
        ev_done = threading.Event()

        def on_msg(ws, raw):
            ev = json.loads(raw)
            t = ev.get("type")
            if t == "session.created":
                ws.send(json.dumps({
                    "type": "session.update",
                    "session": {"audio": {"output": {"language": code}}},
                }))
            elif t == "session.updated":
                result["ok"] = True
                ev_done.set()
            elif t == "error":
                result["err"] = ev.get("error", {}).get("message", str(ev))[:80]
                ev_done.set()

        ws = websocket.WebSocketApp(
            URL,
            header=[f"Authorization: Bearer {KEY}"],
            on_message=on_msg,
            on_error=lambda *_: ev_done.set(),
            on_close=lambda *_: ev_done.set(),
        )
        threading.Thread(target=ws.run_forever, daemon=True).start()
        ev_done.wait(8)
        try:
            ws.close()
        except Exception:
            pass

        if result["ok"]:
            accepted.append(code)
            print(f"  ✓ {code}")
        else:
            rejected.append((code, result["err"]))
            print(f"  ✗ {code}  ({result['err']})")
        time.sleep(0.3)

    print(f"\n=== 接受 ({len(accepted)}) ===")
    print(", ".join(accepted))
    print(f"\n=== 拒绝 ({len(rejected)}) ===")
    for c, e in rejected:
        print(f"  {c}: {e}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="zh", help="目标语言代码 (zh/en/ja/...)")
    p.add_argument("-d", "--duration", type=int, default=10, help="录音时长(秒)")
    p.add_argument("--probe-langs", action="store_true", help="探测支持的语言名单")
    args = p.parse_args()

    if args.probe_langs:
        probe_languages()
    else:
        run_session(args.target, args.duration)


if __name__ == "__main__":
    main()
