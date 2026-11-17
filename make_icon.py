"""Builds the app icon (icon.ico)."""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "SegoeIcons.ttf")

N = 1024
img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((40, 40, N - 40, N - 40), radius=300, fill=(30, 215, 96, 255))
d.rounded_rectangle((150, 150, N - 150, N - 150), radius=200, fill=(18, 18, 18, 255))
f = ImageFont.truetype(FONT, 470)
d.text((N / 2, N / 2 + 10), "\uE8D6", font=f, fill=(30, 215, 96, 255), anchor="mm")

sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
img.save(os.path.join(HERE, "icon.ico"), sizes=sizes)
img.resize((256, 256), Image.Resampling.LANCZOS).save(os.path.join(HERE, "icon.png"))
print("icon.ico ready")
