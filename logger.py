"""统一日志格式 — 让 ./serve.sh logs 一眼看出谁在干什么

格式: HH:MM:SS.mmm  Model-Tag       动作 内容

用法:
    from logger import log, ok, err
    log("qwen-asr", "→ connect", "lang=zh")
    ok("deepseek", f"← {result!r} ({elapsed:.0f}ms)")
    err("openai", "ws closed unexpectedly")
"""
import sys
import time

_C = {"r": "31", "g": "32", "y": "33", "b": "34", "m": "35", "c": "36", "gray": "90"}


def _c(color, s):
    return f"\033[{_C[color]}m{s}\033[0m"


# 模型标签 — 等宽 16 字符对齐
TAGS = {
    "qwen-asr":  _c("c", "Qwen3-ASR     ".ljust(14)),
    "qwen-tts":  _c("m", "Qwen3-TTS     ".ljust(14)),
    "deepseek":  _c("y", "DeepSeek-V4   ".ljust(14)),
    "openai":    _c("g", "OpenAI-RT-Tx  ".ljust(14)),
    "router":    _c("gray", "Router        ".ljust(14)),
    "ws":        _c("gray", "WS-Server     ".ljust(14)),
}


def _ts():
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t - int(t)) * 1000):03d}"


def _emit(tag, marker, parts):
    label = TAGS.get(tag) or _c("gray", tag.ljust(14))
    msg = " ".join(str(p) for p in parts)
    line = f"{_c('gray', _ts())}  {label}  {marker}{msg}\n"
    sys.stdout.write(line)
    sys.stdout.flush()


def log(tag, *parts):
    _emit(tag, "", parts)


def ok(tag, *parts):
    _emit(tag, _c("g", "✓ "), parts)


def err(tag, *parts):
    _emit(tag, _c("r", "✗ "), parts)


def warn(tag, *parts):
    _emit(tag, _c("y", "⚠ "), parts)
