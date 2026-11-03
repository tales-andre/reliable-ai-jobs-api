"""Turn the recorded log of a chaos run (report/chaos.json) into a terminal GIF.

    pip install pillow
    python scripts/make_gif.py            # writes docs/chaos-run.gif

It replays the lines the run really printed, with their real timestamps, at 2x speed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
W, H, PAD, LINE_H, ROWS = 1100, 600, 18, 21, 23
BG, FG, DIM = (17, 24, 39), (226, 232, 240), (100, 116, 139)
COLORS = [
    ("KILL", (255, 255, 255), (185, 28, 28)),
    ("spawn", (250, 204, 21), None),
    ("expired", (103, 232, 249), None),
    ("not paid again", (45, 212, 191), None),
    ("refused", (196, 181, 253), None),
    ("PASS", (74, 222, 128), None),
    ("FAIL", (248, 113, 113), None),
    ("job failed", (248, 113, 113), None),
    ("retry in", (251, 191, 36), None),
    (": ok", (134, 239, 172), None),
    ("checks", (255, 255, 255), None),
]


def font(bold=False, size=15):
    for name in (["consolab.ttf", "DejaVuSansMono-Bold.ttf"] if bold else ["consola.ttf", "DejaVuSansMono.ttf"]):
        for base in ("C:/Windows/Fonts/", "/usr/share/fonts/truetype/dejavu/", ""):
            try:
                return ImageFont.truetype(base + name, size)
            except OSError:
                continue
    return ImageFont.load_default()


F, FB, FT = font(), font(True), font(True, 16)


def style(line):
    for needle, fg, bg in COLORS:
        if needle in line:
            return fg, bg
    return FG, None


def frame(lines, title, caption=None):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 34], fill=(30, 41, 59))
    for i, c in enumerate([(248, 113, 113), (250, 204, 21), (74, 222, 128)]):
        d.ellipse([14 + i * 20, 11, 26 + i * 20, 23], fill=c)
    d.text((84, 8), title, font=FT, fill=(203, 213, 225))
    y = 34 + PAD
    for t, text in lines[-ROWS:]:
        fg, bg = style(text)
        d.text((PAD, y), f"[{t:5.1f}s]", font=F, fill=DIM)
        if bg:
            d.rectangle([PAD + 88, y - 1, W - PAD, y + LINE_H - 3], fill=bg)
        d.text((PAD + 96, y), text[:104], font=FB if bg or "checks" in text else F, fill=fg)
        y += LINE_H
    if caption:
        d.rectangle([0, H - 46, W, H], fill=(30, 64, 175))
        d.text((PAD, H - 34), caption, font=FT, fill=(255, 255, 255))
    return img


def main(src=ROOT / "report" / "chaos.json", out=ROOT / "docs" / "chaos-run.gif"):
    data = json.loads(Path(src).read_text(encoding="utf-8"))
    res = data["results"]
    log = res["safe"]["log"]
    t_start = log[0][0]
    log = [(t - t_start, line) for t, line in log]
    title = f"python -m chaos.run --seed {data['params']['seed']}   (recorded log, replayed at 2x)"
    frames, durations = [], []
    step = 0.2
    t, shown = 0.0, 0
    end = max(t for t, _ in log)
    while t <= end + step:
        while shown < len(log) and log[shown][0] <= t:
            shown += 1
        visible = log[:shown]
        kill = any("KILL" in line for _, line in visible[-2:])
        frames.append(frame(visible, title))
        durations.append(900 if kill else 100)   # pause on each kill
        t += step
    caption = "With safeguards: " + f"{sum(c['ok'] for c in res['safe']['checks'])} of {len(res['safe']['checks'])} checks pass"
    if "naive" in res:
        caption += f"   |   without: {sum(c['ok'] for c in res['naive']['checks'])} of {len(res['naive']['checks'])}"
    frames.append(frame(log, title, caption))
    durations.append(5000)
    pal = [f.convert("P", palette=Image.Palette.ADAPTIVE, colors=64) for f in frames]
    pal[0].save(out, save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=True)
    print(f"wrote {out} ({len(frames)} frames, {Path(out).stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(*sys.argv[1:])
