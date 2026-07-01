"""Generates a simple square logo (512x512) for directory listings that
require one (Smol Launch etc.) — not part of the package, one-off asset
generator, run manually: python assets/make_logo.py
"""

from PIL import Image, ImageDraw, ImageFont

FONT_BOLD = r"C:\Windows\Fonts\consolab.ttf"

BG = (13, 17, 23)
GREEN = (63, 185, 80)
RED = (248, 81, 73)
FG = (201, 209, 217)

SIZE = 512
img = Image.new("RGB", (SIZE, SIZE), BG)
draw = ImageDraw.Draw(img)

# ">_" prompt glyph, centered, as the mark
font_prompt = ImageFont.truetype(FONT_BOLD, 220)
prompt = ">_"
bbox = draw.textbbox((0, 0), prompt, font=font_prompt)
w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
draw.text(((SIZE - w) / 2 - bbox[0], (SIZE - h) / 2 - bbox[1] - 40), prompt, font=font_prompt, fill=GREEN)

# small red/green status dots under it, echoing the CLI's own OPEN/CLAIMED verdicts
font_small = ImageFont.truetype(FONT_BOLD, 34)
label = "bounty-check"
bbox2 = draw.textbbox((0, 0), label, font=font_small)
w2 = bbox2[2] - bbox2[0]
draw.text(((SIZE - w2) / 2, SIZE - 100), label, font=font_small, fill=FG)

out_path = r"C:\Users\beres\HermesHQ\projects\bounty-check\assets\logo-512.png"
img.save(out_path)
print("saved:", out_path)
