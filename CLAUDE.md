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
  fallback fonts; `Drawer.text` on RGBA pastes a cached text layer, `TextFont.paste`), `fit_text()`, `wrap_text()`, `fmt_dur()`, `fmt_time()`.
- **Cache:** the main image is cached by the `base_key` tuple (`_render_all`). If you add new visual state,
  add it to `base_key`. The progress strip and the mini-mode scrolling title update partially via `region()`.
- **Loop:** `App._tick`: `frame_ms()` while animating (setting "Animation smoothness": ~64 or ~32 fps,
  whole Windows timer ticks), paced from the previous due time by `next_due()`; 200 ms while playing,
  450 ms idle, slow when the widget is fully covered. Drawing must stay cheap; cache expensive work.
- **Data sources:** `MediaWorker` (GSMTC, asyncio thread: current track, cover, commands),
  `SpotifyWeb` (PKCE OAuth, queue, like, search, devices), `AppVolume` (pycaw: Spotify volume, peak
  meters), `Lyrics` (lrclib.net), `Hotkeys` (RegisterHotKey).
- **Updates:** `latest_release()` → `pick_update()` (newer tag, CoverglowSetup.exe from this repo only);
  menu "Update to …" → `App._install_update`: `download_update()` checks size + GitHub's SHA-256, then runs
  the installer silently (`/VERYSILENT /DIR=<exe folder> /RELAUNCH=1`); `Coverglow.iss` restarts the app.
- **UI parts:** `_render_base` / `_render_mini` / `_render_queue` (tabs: queue, recent, lyrics, stats),
  `_render_stats`, `Toast` (track toast + short indicator via `show_message`; corner `cfg["toast_pos"]`, time
  `cfg["toast_secs"]`, shown on the foreground window's monitor, hover keeps it, next button / click opens Spotify), `MenuPanel` (right-click
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
  "From cover" in shadcn picks the nearest colour theme of the same themes.ts (`SHADCN_THEMES`,
  `pick_shadcn()`); grey covers stay zinc. On glass / translucent cards (`STYLE["see"]`) shadcn
  prev / next get a border + 60% fill. In glass mode the menu background is `_card(glass=...)` with the
  wallpaper behind the menu (`App._glass_at`).
  `neu` (Neumorphic): the RYOCS themes `NEU_THEMES` (kisisel_panel ui.html TEMALAR); `pick_neu()` takes the
  one whose accent hue is nearest to the cover → `build_neu()` (accent picker hidden; Poppins Light / Medium
  from `fonts/` like RYOCS). Shadow colours / glass state live in `STYLE` (hi, lo, glass, clear).
  Card is `_neu_card` (flat; glass in light mode = RYOCS glass look with `neu_glass()` sheen).
  Buttons are `neu_btn()` (raised by `_neu_shadows()`, `pressed=True` sinks in with `neu_inset()`; on glass /
  translucent cards the body shows the card through; `rr()` draws big circles this way, `plain=True`
  skips it). Menu segments slide like RYOCS (`MenuPanel._slide_pos`). The `neu_raise()` shadow is clipped to
  img's opaque pixels, so it never spills outside the card. `PillButton` is raised in neu too.
- **Language:** `STRINGS["tr"]` and `STRINGS["en"]`. Add every new string to both, use it via `T("key")`.
  Code comments, logs and docs are in English.
- **Settings:** `config.json` (give defaults with `cfg.setdefault` in `App.__init__`). The exe keeps its files
  in `%APPDATA%\Coverglow` (source runs: next to coverglow.py): config.json, stats.json (live stats), stats_import.json (imported Spotify
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
3. Before building run `python test_core.py` and `python tools/smoke_exe.py` (starts the app on the
   exe-only code paths; a startup crash there shipped in 1.2.7).
   Build: `cd D:\Calude\coverglow; Stop-Process -Name Coverglow;
   python -m PyInstaller --noconfirm Coverglow.spec` → start `dist\Coverglow.exe`,
   check `%APPDATA%\Coverglow\widget.log` for new errors. Installer: bump `AppVersion` in `Coverglow.iss`,
   then `& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" Coverglow.iss` → `dist\CoverglowSetup.exe`.
4. Commit and push (short English commit message).
5. At the end, state clearly what you changed, what you tested and what you couldn't try in real use.
