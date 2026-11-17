"""Builds the Coverglow app icon (icon.ico + icon.png): a glowing album cover with a sound wave."""
import math
import os
from PIL import Image, ImageChops, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
N = 1024
C1, C2, C3 = (255, 170, 80), (240, 80, 140), (130, 70, 230)  # amber → pink → violet


def gradient(n):
    """Diagonal three-stop gradient, top left to bottom right."""
    img = Image.new("RGB", (n, n))
    px = img.load()
    for y in range(n):
        for x in range(n):
            t = (x + y) / (2 * n - 2)
            a, b, u = (C1, C2, t * 2) if t < .5 else (C2, C3, t * 2 - 1)
            px[x, y] = tuple(round(p + (q - p) * u) for p, q in zip(a, b))
    return img


def mask(box, r):
    m = Image.new("L", (N, N), 0)
    ImageDraw.Draw(m).rounded_rectangle(box, radius=r, fill=255)
    return m


grad = gradient(N)


def icon(small):
    """small: no glow, bigger cover and thicker wave so it reads at 16-32 px."""
    out = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    m = 40 if small else 150
    cover_box, cover_r = (m, m, N - m, N - m), 200 if small else 170
    if not small:  # glow: blurred cover behind the cover
        glow = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        glow.paste(grad, (0, 0), mask((100, 100, N - 100, N - 100), 240))
        out.alpha_composite(glow.filter(ImageFilter.GaussianBlur(50)))
    # the cover, with a light sheen at the top left
    cover = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    cover.paste(grad, (0, 0), mask(cover_box, cover_r))
    sheen = Image.new("L", (N, N), 0)
    ImageDraw.Draw(sheen).ellipse((-200, -260, 620, 520), fill=70)
    sheen = ImageChops.multiply(sheen.filter(ImageFilter.GaussianBlur(80)), mask(cover_box, cover_r))
    cover.alpha_composite(Image.merge("RGBA", (*Image.new("RGB", (N, N), "white").split(), sheen)))
    out.alpha_composite(cover)
    # sound wave across the cover (the widget's wavy progress bar), drawn as dense dots
    d = ImageDraw.Draw(out)
    pad, w = (m + 110, 60) if not small else (m + 120, 100)
    x0, x1, cy, amp = pad, N - pad, N / 2 + 10, 80 if small else 70
    for i in range(801):
        x = x0 + (x1 - x0) * i / 800
        y = cy + amp * math.sin((x - x0) / (x1 - x0) * 2.5 * math.pi)
        d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=(255, 255, 255, 255))
    return out


big, small = icon(False), icon(True)
imgs = {s: (small if s <= 32 else big).resize((s, s), Image.Resampling.LANCZOS)
        for s in (16, 24, 32, 48, 64, 128, 256)}
imgs[256].save(os.path.join(HERE, "icon.ico"), sizes=[(s, s) for s in imgs],
               append_images=[imgs[s] for s in imgs if s != 256])
imgs[256].save(os.path.join(HERE, "icon.png"))
print("icon.ico ready")
