#!/usr/bin/env python3
"""把 docs/screenshots/*.png 统一到 620×1320, 不足处用 paper 色 #fdfaf3 补全"""
from pathlib import Path

from PIL import Image


TARGET_W = 620
TARGET_H = 1320
BG = (253, 250, 243, 255)  # --paper #fdfaf3

ROOT = Path(__file__).parent.parent
shots = sorted((ROOT / "docs" / "screenshots").glob("*.png"))

for src in shots:
    im = Image.open(src).convert("RGBA")
    w, h = im.size
    if (w, h) == (TARGET_W, TARGET_H):
        print(f"= {src.name:14s} already {w}×{h}")
        continue
    canvas = Image.new("RGBA", (TARGET_W, TARGET_H), BG)
    # 居中粘贴 (顶部对齐, 底部留白)
    x = (TARGET_W - w) // 2
    y = 0  # 顶部对齐, 高度差留在底部
    canvas.paste(im, (x, y), im if im.mode == "RGBA" else None)
    canvas.convert("RGB").save(src, optimize=True)
    print(f"✓ {src.name:14s} {w}×{h} → {TARGET_W}×{TARGET_H}")
