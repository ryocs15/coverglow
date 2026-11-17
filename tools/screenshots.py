"""Render the README screenshots headless into docs/.

Runs a copy of the app from a temp folder, so your real config, token, history and stats are never
loaded. Tracks, artists and covers are made up. Run: python tools/screenshots.py
"""
import json
import math
import os
import random
import shutil
import sys
import tempfile
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs")
TMP = tempfile.mkdtemp()
shutil.copy(os.path.join(ROOT, "coverglow.py"), TMP)
shutil.copytree(os.path.join(ROOT, "fonts"), os.path.join(TMP, "fonts"))
with open(os.path.join(TMP, "config.json"), "w", encoding="utf-8") as f:
    json.dump({"lang": "en", "startup": False, "memory": False, "toast": False}, f)
sys.path.insert(0, TMP)
import coverglow as s  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402

random.seed(7)


def cover(c1, c2, c3):
    """Abstract generated album art."""
    n = 600
    img = Image.new("RGB", (n, n), c1)
    d = ImageDraw.Draw(img)
    for i in range(n):
        t = i / n
        d.line((0, i, n, i), fill=tuple(round(a + (b - a) * t) for a, b in zip(c1, c2)))
    for _ in range(7):
        r = random.randint(60, 220)
        x, y = random.randint(0, n), random.randint(0, n)
        d.ellipse((x - r, y - r, x + r, y + r), fill=c3 if random.random() < .5 else c2)
    return img.filter(ImageFilter.GaussianBlur(18))


COVERS = {
    "sunset": cover((255, 140, 66), (120, 30, 80), (255, 210, 120)),
    "ocean": cover((20, 70, 140), (90, 200, 220), (230, 240, 255)),
    "violet": cover((70, 30, 120), (200, 90, 200), (30, 10, 60)),
    "forest": cover((30, 90, 60), (170, 210, 120), (240, 230, 180)),
}
TRACKS = {
    "sunset": ("Golden Hour Drive", "The Paper Moons", "Coastline", 70, 214),
    "ocean": ("Tidal Lines", "Nova Harbor", "Blue Static", 95, 241),
    "violet": ("Night Signals", "Luma & The Echo", "After Dark", 128, 263),
    "forest": ("夜明けのうた", "Hoshino", "Morning Light", 40, 198),
}
QUEUE = [("Afterglow", "The Paper Moons", 201), ("Slow Satellites", "Nova Harbor", 236),
         ("夜明けのうた", "Hoshino", 198), ("Paper Planes", "Juniper Lane", 184)]
LYRICS = [(0.0, "Windows down, the city's falling behind"), (12.0, "Chasing the sun on the coastline"),
          (24.0, "Every mile a little more gold"), (36.0, "We don't need a place to go"),
          (60.0, "Golden hour, stay a little longer"), (72.0, "Golden hour, we're only getting stronger"),
          (84.0, "Hold the light before it's gone"), (96.0, "Hum along to the radio song")]

a = s.App()
a.cfg.save = lambda: None
a._save_stats = lambda: None
a.media.command = lambda *x, **k: None
qthumbs = list(COVERS.values())
queue = [{"id": str(i), "name": n, "sub": ar, "url": f"q{i}", "dur": d} for i, (n, ar, d) in enumerate(QUEUE)]
a.web.thumbs.update({f"q{i}": qthumbs[(i + 1) % 4] for i in range(len(QUEUE))})
a.web.snapshot = lambda: (True, queue, None, 1)

# made-up listening stats for the last 60 days
days = {}
artists = ["The Paper Moons", "Nova Harbor", "Hoshino", "Luma & The Echo", "Juniper Lane"]
for i in range(60):
    k = (date.today() - timedelta(days=i)).isoformat()
    sec = random.uniform(1800, 9000) * (0.6 + 0.4 * math.sin(i / 3))
    w = [random.random() ** 2 for _ in artists]
    days[k] = {"sec": sec, "plays": int(sec / 210), "artists": {ar: sec * x / sum(w) for ar, x in zip(artists, w)},
               "tracks": {f"{t[0]}\t{t[1]}": sec * random.uniform(.05, .2) for t in TRACKS.values()},
               "hours": {str(h): random.uniform(0, 900) * (1.5 if 18 <= h <= 23 else .5) for h in range(8, 24)}}
a.stats = {"days": days}
a.imported = {}

shots = {}


def render(name, ui="material", theme="dark", track="sunset", tab="queue", mini=False, bg="solid",
           show_queue=True):
    title, artist, album, pos, end = TRACKS[track]
    st = {"ok": True, "title": title, "artist": artist, "album": album, "playing": True, "pos": pos,
          "end": end, "shuffle": True, "repeat": 0}
    a.media.snapshot = lambda: dict(st)
    a.st = st
    a._position = lambda: (pos, end)
    a.cfg.update(theme=theme, accent="auto", bg=bg, list_tab=tab, queue_rows=4, show_queue=show_queue,
                 queue_collapsed=False, stats_period="30")
    a.light = theme == "light"
    a.mini = mini
    a._set_art(COVERS[track])
    a._set_ui(ui)
    with a.lyrics.lock:
        a.lyrics._lines = LYRICS
    a.lyrics.state, a.lyrics.synced, a.lyrics.key = "ok", True, (title, artist)
    a.lyrics.version += 1
    a.base_key = None
    a._render_all()
    img = a.base.copy()
    img.paste(a._render_strip(pos / end, pos, end), (0, a.sy0))
    if a.marq:
        img.paste(a._render_marquee(0), (a.marq["x"], a.marq["y0"]))
    shots[name] = img.crop((0, 0, a.WP, round(a.content_h * a.S)))
    return shots[name]


def menu_shot():
    s.MenuPanel._on_focus_out = lambda self, e: None
    s.MenuPanel._watch = lambda self: None
    cap = {}
    of = s.LayeredSurface.full

    def cf(self, img, h=None, pos=None):
        cap[id(self)] = img
        of(self, img, h, pos)
    s.LayeredSurface.full = cf
    s.MenuPanel.toggle(a, 600, 200)
    m = s.MenuPanel._inst
    m.pages = ["main", "look"]
    m.render()
    img = cap[id(m.surface)].crop((0, 0, m.WM, round((m.vh + 2 * m.M) * m.S)))
    m.close()
    s.LayeredSurface.full = of
    return img


def backdrop(w, h, c1, c2):
    """Soft blurred 'desktop' behind the widgets."""
    bg = cover(c1, c2, tuple(min(255, v + 60) for v in c1)).resize((w, h))
    return bg.filter(ImageFilter.GaussianBlur(40)).convert("RGBA")


def sheet(name, imgs, c1, c2, pad=36, gap=28, brand=False, size=None):
    """size: final (w, h); the layout is centered on a wider canvas of that aspect, then scaled."""
    head = 96 if brand else 0  # app icon + name above the widgets
    cw = sum(i.width for i in imgs) + gap * (len(imgs) - 1) + 2 * pad
    h = max(i.height for i in imgs) + 2 * pad + head
    w = max(cw, round(h * size[0] / size[1])) if size else cw
    out = backdrop(w, h, c1, c2)
    px = pad + (w - cw) // 2  # x start of the centered layout
    if brand:
        icon = Image.open(os.path.join(ROOT, "icon.png")).convert("RGBA").resize((80, 80), Image.LANCZOS)
        out.alpha_composite(icon, (px - 8, pad - 4))
        font = ImageFont.truetype(os.path.join(ROOT, "fonts", "Geist-SemiBold.ttf"), 40)
        sub = ImageFont.truetype(os.path.join(ROOT, "fonts", "Geist-Regular.ttf"), 19)
        d = ImageDraw.Draw(out)
        d.text((px + 84, pad + 16), "Coverglow", font=font, fill=(255, 255, 255), anchor="lm")
        d.text((px + 86, pad + 54), "A desktop widget for Spotify on Windows", font=sub,
               fill=(255, 255, 255, 200), anchor="lm")
    x = px
    for i in imgs:
        out.alpha_composite(i, (x, pad + head))
        x += i.width + gap
    if size:
        out = out.resize(size, Image.LANCZOS)
    out.convert("RGB").save(os.path.join(OUT, name), optimize=True)


def column(imgs, gap=20):
    w = max(i.width for i in imgs)
    out = Image.new("RGBA", (w, sum(i.height for i in imgs) + gap * (len(imgs) - 1)), (0, 0, 0, 0))
    y = 0
    for i in imgs:
        out.alpha_composite(i, (0, y))
        y += i.height + gap
    return out


os.makedirs(OUT, exist_ok=True)
sheet("hero.png", [render("q", track="sunset", tab="queue"),
                   render("l", theme="light", track="ocean", tab="lyrics"),
                   render("s", track="violet", tab="stats")], (40, 40, 60), (110, 70, 90), brand=True)
sheet("themes.png", [render("m", track="forest", show_queue=False),
                     render("sh", ui="shadcn", track="ocean", show_queue=False),
                     render("nl", ui="neu", theme="light", show_queue=False),
                     render("nd", ui="neu", theme="dark", show_queue=False)], (70, 90, 110), (40, 50, 70))
sheet("mini.png", [column([render("m1", track="sunset", mini=True),
                           render("m2", ui="shadcn", theme="light", track="ocean", mini=True),
                           render("m3", ui="neu", track="violet", mini=True)])], (60, 60, 80), (120, 90, 110))
# 1280x640 social preview (GitHub: Settings → Social preview)
sheet("social.png", [shots["q"], shots["l"], shots["s"]], (40, 40, 60), (110, 70, 90), brand=True,
      size=(1280, 640))
render("menu_bg", track="violet", show_queue=False)
sheet("menu.png", [menu_shot()], (60, 50, 90), (30, 30, 50))
a.root.destroy()
shutil.rmtree(TMP, ignore_errors=True)
print("saved to", OUT)
