# Coverglow

Coverglow: a Material You style Spotify desktop widget for Windows, written in Python.
Reply to the user in the language they write in (the maintainer writes in Turkish).

## Project
- The whole app is one file: `coverglow.py` (~6700 lines).
- Python 3.15, Windows 11. Dependencies: Pillow, winrt (Windows.Media.Control etc.), pycaw/comtypes,
  fontTools, PyInstaller.
- Git repo: github.com/ryocs15/coverglow.
  git: `%LOCALAPPDATA%\Programs\MinGit\cmd\git.exe`, gh: `%LOCALAPPDATA%\Programs\GitHubCLI\bin\gh.exe`

## Architecture
- **Drawing:** tkinter is only used for events and dialogs. The widget is drawn as an RGBA image with Pillow
  and pushed to a layered window with `UpdateLayeredWindowIndirect` (`LayeredSurface`: `full()` / `region()`).
- **Coordinates** are logical px; physical = value × `self.S` (dpi × scale). The code uses the
  `X = lambda v: v * S` pattern.
- **Helpers:** `rr()` rounded rectangle, `rounded_image()`, `Drawer` + `TextFont` (Japanese/Korean/emoji
  fallback fonts), `fit_text()`, `wrap_text()`, `fmt_dur()`, `fmt_time()`.
- **Cache:** the main image is cached by the `base_key` tuple (`_render_all`). If you add new visual state,
  add it to `base_key`. The progress strip and the mini-mode scrolling title update partially via `region()`.
- **Loop:** `App._tick` (40 ms while animating, else 200 ms). Drawing must stay cheap; cache expensive work.
- **Data sources:** `MediaWorker` (GSMTC, asyncio thread: current track, cover, commands),
  `SpotifyWeb` (PKCE OAuth, queue, like, search, devices), `AppVolume` (pycaw: Spotify volume, peak
  meters), `Lyrics` (lrclib.net), `Hotkeys` (RegisterHotKey).
- **UI parts:** `_render_base` / `_render_mini` / `_render_queue` (tabs: queue, recent, lyrics, stats),
  `_render_stats`, `Toast` (track toast + short indicator via `show_message`), `MenuPanel` (right-click
  menu: defined by dicts in `items()`; kinds: quick, nav, toggle, seg, swatch, radio, action, info, header,
  sep, title), `_Dialog`-based windows (`SearchDialog`, `AlarmDialog`, `RecapWindow`), `ConnectDialog`.
- **Colors:** Material You palette from the album cover (`self.pal`). Keys: primary, on_primary,
  primary_container, on_primary_container, secondary_container, on_secondary_container, surface,
  surface_high, surface_highest, on_surface, on_surface_variant, outline. Don't hard-code colors.
- **Design language:** `cfg["ui"]` = `material` / `shadcn` / `neu`. Global `STYLE` + `set_style()`: in shadcn
  `rr()` caps corners at 10 px and turns button-sized circles into rounded squares; `ui_font()` uses
  Geist from `fonts/` (OFL, github.com/vercel/geist-font) instead of Segoe. shadcn colors come from
  `SHADCN_ZINC` (github.com/shadcn-ui/ui `apps/v4/registry/themes.ts`, OKLCH) → `build_shadcn()`.
  The shadcn palette also has a `border` key (defined in the Material palette too).
  `neu` (Neumorphic): fixed slate palette `NEU` → `build_neu()` (cover / accent colors unused, color
  picker hidden). Card is `_neu_card` (NEU_GRAD diagonal gradient; also in glass / translucent mode).
  Buttons are `neu_btn()` (soft UI: card colored, light shadow top left + dark bottom right; `rr()` draws
  big circles this way automatically, `plain=True` skips it). The `neu_raise()` shadow is clipped to
  img's opaque pixels, so it never spills outside the card. `PillButton` is raised in neu too.
- **Language:** `STRINGS["tr"]` and `STRINGS["en"]`. Add every new string to both, use it via `T("key")`.
  Code comments, logs and docs are in English.
- **Settings:** `config.json` (give defaults with `cfg.setdefault` in `App.__init__`). When running the exe
  the files live in `dist\`: config.json, stats.json (live stats), stats_import.json (imported Spotify
  history), history.json, last_track.json, widget.log.

## Rules
- The refresh_token in `config.json` is secret: never print, log or commit it. Personal data files must
  stay in `.gitignore`.
- Don't send commands that play/change the user's music (play, queue, transfer, seek) for testing on your
  own; ask first if needed.
- If you call the Spotify Web API from a script, stop the running exe first (`Stop-Process -Name
  Coverglow`) and restart it afterwards: two copies can clash when the token refreshes.
- Spotify notes: the old `/me/tracks` endpoint returns 403, `/me/library` is used. Playback control needs
  Premium. If you add a new scope the user must reconnect their account; say so.
- Don't break existing features: mini mode, growing upward at the bottom of the screen, toast over
  fullscreen apps, glass background, Discord ducking, stats caches, the menu keeping its bottom edge when
  it opens upward.

## Workflow
1. Read the relevant code, then change it. Use existing helpers and patterns.
2. Test visual changes headless: create `App()` in a script, stub out `a.cfg.save` and `a.media.command`,
   set the state, call `a.base_key = None; a._render_all()`, save `a.base` as PNG and inspect it. Check
   both languages and mini/normal mode. `tools/screenshots.py` renders the README images.
3. Build: `cd D:\Calude\coverglow; Stop-Process -Name Coverglow;
   python -m PyInstaller --noconfirm Coverglow.spec` → start `dist\Coverglow.exe`,
   check `dist\widget.log` for new errors.
4. Commit and push (short English commit message).
5. At the end, state clearly what you changed, what you tested and what you couldn't try in real use.
