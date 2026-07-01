"""Generates a terminal-style preview PNG for bounty-check listings
(Terminal Trove, README, etc.) from real, actually-run CLI output.

Not part of the package itself — a one-off asset generator, run manually:
    python assets/make_preview.py
"""

from PIL import Image, ImageDraw, ImageFont

FONT_REGULAR = r"C:\Windows\Fonts\CascadiaMono.ttf"
FONT_BOLD = r"C:\Windows\Fonts\consolab.ttf"

BG = (13, 17, 23)          # GitHub-dark-ish background
TITLEBAR = (22, 27, 34)
FG = (201, 209, 217)       # default text
GREEN = (63, 185, 80)
RED = (248, 81, 73)
YELLOW = (210, 153, 34)
GRAY = (110, 118, 129)
CYAN = (121, 192, 255)

W, H = 1280, 720
PAD = 32
LINE_H = 30

lines = [
    (GRAY, "$ bounty-check rustdesk/rustdesk#3762"),
    (FG, ""),
    (RED, "0/1 actually open and claimable."),
    (FG, "rustdesk/rustdesk#3762"),
    (RED, "  verdict: HAS_OPEN_PR"),
    (FG, "  title:   Audio no sound (Add asio support)"),
    (GRAY, "  note:    1 open PR(s) already reference this issue -"),
    (GRAY, "           someone's ahead of you: .../pull/15232"),
    (FG, ""),
    (GRAY, "$ bounty-check zed-industries/zed#4642"),
    (FG, ""),
    (RED, "0/1 actually open and claimable."),
    (FG, "zed-industries/zed#4642"),
    (YELLOW, "  verdict: CLOSED"),
    (FG, "  title:   Helix keymap"),
    (GRAY, "  note:    Issue is already closed - the bounty is"),
    (GRAY, "           very likely already claimed."),
    (FG, ""),
    (GREEN, "# checks GitHub issue state + linked PRs directly,"),
    (GREEN, "# not just what a bounty aggregator's dashboard claims."),
]

img = Image.new("RGB", (W, H), BG)
draw = ImageDraw.Draw(img)

# Title bar
draw.rectangle([0, 0, W, 44], fill=TITLEBAR)
for i, color in enumerate([RED, YELLOW, GREEN]):
    draw.ellipse([PAD + i * 28, 16, PAD + i * 28 + 14, 30], fill=color)

font_title = ImageFont.truetype(FONT_BOLD, 16)
title = "bounty-check"
tw = draw.textlength(title, font=font_title)
draw.text(((W - tw) / 2, 14), title, font=font_title, fill=GRAY)

font = ImageFont.truetype(FONT_REGULAR, 20)

y = 44 + 24
for color, text in lines:
    draw.text((PAD, y), text, font=font, fill=color)
    y += LINE_H

out_path = r"C:\Users\beres\HermesHQ\projects\bounty-check\assets\terminal-trove-preview.png"
img.save(out_path)
print("saved:", out_path)
