"""Small tests for the risky core logic. Run: python test_core.py (or pytest)."""
import json
import math
import os
import tempfile
import threading
import time
import urllib.error
from types import SimpleNamespace

import coverglow as s


def test_import_dedupes_both_export_formats():
    d = tempfile.mkdtemp()
    ext = [  # extended history (ts with seconds, UTC)
        {"ts": "2025-06-01T12:00:41Z", "ms_played": 200000,
         "master_metadata_track_name": "Song", "master_metadata_album_artist_name": "Artist"},
        {"ts": "2025-06-01T12:10:00Z", "ms_played": 10000,  # under 30 s: time counts, play does not
         "master_metadata_track_name": "Short", "master_metadata_album_artist_name": "Artist"},
        {"ts": "2025-06-01T12:20:00Z", "ms_played": 90000,  # podcast: skipped
         "master_metadata_track_name": None, "master_metadata_album_artist_name": None},
    ]
    acc = [  # account data: same listen at minute precision
        {"endTime": "2025-06-01 12:00", "msPlayed": 200000, "trackName": "Song", "artistName": "Artist"},
    ]
    p1, p2 = os.path.join(d, "a.json"), os.path.join(d, "b.json")
    json.dump(ext, open(p1, "w", encoding="utf-8"))
    json.dump(acc, open(p2, "w", encoding="utf-8"))
    s.STATS_IMPORT_PATH = os.path.join(d, "stats_import.json")
    res = s.import_spotify_history([p1, p2])
    assert res["n"] == 2, res  # Song once + Short
    day = next(iter(s.load_imported_history().values()))
    assert day["plays"] == 1 and day["sec"] == 210.0, day
    assert day["counts"] == {"Song\tArtist": 1}, day


def test_stat_get_takes_larger_day():
    app = SimpleNamespace(stats={"days": {"d1": {"sec": 50}, "d2": {"sec": 500}}},
                          imported={"d1": {"sec": 100}, "d2": {"sec": 100}, "d3": {"sec": 7}})
    assert s.App._stat_get(app, "d1")["sec"] == 100
    assert s.App._stat_get(app, "d2")["sec"] == 500
    assert s.App._stat_get(app, "d3")["sec"] == 7
    assert s.App._stat_get(app, "missing") is None


def test_lyrics_parse():
    out = s.Lyrics.parse("[00:01.50]a\n[01:00.00][00:30.00]b\nplain line")
    assert out == [(1.5, "a"), (30.0, "b"), (60.0, "b")], out


def test_tick_keeps_running_after_error():
    calls = []

    def boom():
        raise RuntimeError("draw error")
    app = SimpleNamespace(_tick_body=boom, _tick=None,
                          root=SimpleNamespace(after=lambda ms, fn: calls.append(ms)))
    try:
        s.App._tick(app)
    except RuntimeError:
        pass
    assert len(calls) == 1, "next frame not scheduled after an error"


def test_config_encrypts_token_and_migrates_plaintext():
    s.CONFIG_PATH = os.path.join(tempfile.mkdtemp(), "config.json")
    json.dump({"client_id": "x", "refresh_token": "secret-rt"}, open(s.CONFIG_PATH, "w"))
    c = s.Config()  # old plain-text file: encrypted on load
    assert c["refresh_token"] == "secret-rt"
    raw = open(s.CONFIG_PATH, encoding="utf-8").read()
    assert "secret-rt" not in raw and "refresh_token_enc" in raw, raw
    assert s.Config()["refresh_token"] == "secret-rt"  # round trip


def test_corrupt_config_is_set_aside_not_overwritten():
    s.CONFIG_PATH = os.path.join(tempfile.mkdtemp(), "config.json")
    open(s.CONFIG_PATH, "w").write("")  # e.g. empty after a power cut
    c = s.Config()
    c["x"] = 1
    c.save()
    assert open(s.CONFIG_PATH + ".bad").read() == ""
    assert json.load(open(s.CONFIG_PATH))["x"] == 1


def _web(rt, post):
    saved = []
    cfg = {"refresh_token": rt, "client_id": "c"}
    cfg = type("C", (dict,), {"save": lambda self: saved.append(dict(self))})(cfg)
    web = SimpleNamespace(cfg=cfg, access=None, expires=0.0, token_lock=threading.Lock(),
                          _post_token=post, _set=lambda *a: None)
    web._refresh_token = lambda: s.SpotifyWeb._refresh_token(web)
    return web


def test_parallel_token_refresh_keeps_the_rotated_token():
    calls = []

    def post(data):  # Spotify rotates: the old token works once
        calls.append(data["refresh_token"])
        time.sleep(0.05)
        if data["refresh_token"] != "rt1":
            raise urllib.error.HTTPError("u", 400, "invalid_grant", {}, None)
        return {"access_token": "a", "expires_in": 3600, "refresh_token": "rt2"}
    web = _web("rt1", post)
    ths = [threading.Thread(target=s.SpotifyWeb._ensure_token, args=(web,)) for _ in range(3)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert calls == ["rt1"] and web.cfg["refresh_token"] == "rt2", (calls, web.cfg)


def test_refused_token_is_not_deleted_if_a_newer_one_was_saved():
    def post(data):  # a reconnect saves a new token while this refresh is in flight
        web.cfg["refresh_token"] = "new"
        raise urllib.error.HTTPError("u", 400, "invalid_grant", {}, None)
    web = _web("old", post)
    try:
        s.SpotifyWeb._ensure_token(web)
    except urllib.error.HTTPError:
        pass
    assert web.cfg["refresh_token"] == "new"


def test_volume_left_low_by_a_killed_run_is_restored():
    vol = SimpleNamespace(v=0.02, get=lambda: vol.v, set=lambda x: setattr(vol, "v", x))
    cfg = type("C", (dict,), {"save": lambda self: None})(vol_restore=0.7, react=False)
    app = SimpleNamespace(cfg=cfg, volume=vol, _vol_pending=0.7, level=0.0, _lvl_lo=None,
                          _duck=None, _duck_fade=None)
    app._vol_mark = lambda v: s.App._vol_mark(app, v)
    s.App._audio_tick(app, 0.0, {"ok": True}, False)
    assert vol.v == 0.7 and "vol_restore" not in cfg and app._vol_pending is None


def test_neumorphic_theme_follows_the_cover():
    assert s.pick_neu((0.58, 0.6), True) == "buz"      # blue cover, light
    assert s.pick_neu((0.78, 0.6), False) == "erik"    # purple cover, dark
    assert s.pick_neu((0.1, 0.05), True) == "" and s.pick_neu((0.1, 0.05), False) == "grafit"
    p = s.build_neu(True, (0.58, 0.6))
    assert p["surface"] == (0xe4, 0xeb, 0xf2) and p["hi"] == (0xfb, 0xfd, 0xff), p


def test_shadcn_from_cover_uses_the_nearest_shadcn_theme():
    assert s.pick_shadcn((0.6, 0.65)) == "blue" and s.pick_shadcn((0.0, 0.7)) == "red"
    assert s.pick_shadcn((0.3, 0.05)) is None   # grey cover: plain zinc
    p = s.build_shadcn(False, theme="blue")
    assert p["primary"] == s.oklch(0.424, 0.199, 265.638), p["primary"]


def test_accent_text_is_made_readable():
    bg, tx = s.hexc("#e9eaec"), s.hexc("#4a4d55")
    c = s.readable(s.hexc("#8fb996"), bg, tx)   # RYOCS green on its light card: 1.8:1 as is
    lc, lb = s._lum(c), s._lum(bg)
    assert (max(lc, lb) + .05) / (min(lc, lb) + .05) >= 4.5 and c != tx, c
    dark = s.hexc("#28342a")
    assert s.readable(dark, bg, tx) == dark   # already readable: unchanged


def test_toast_goes_to_the_chosen_corner():
    area = SimpleNamespace(left=1920, top=0, right=3840, bottom=1040)   # second monitor, taskbar 40
    assert s.toast_xy(area, 300, 80, "br", 16) == (3524, 944)
    assert s.toast_xy(area, 300, 80, "tl", 16) == (1936, 16)
    assert s.toast_xy(area, 300, 80, "tr", 16) == (3524, 16)
    assert s.toast_xy(area, 300, 80, "bl", 16) == (1936, 944)


def test_frame_pacing_keeps_the_chosen_rate_despite_late_timers():
    due = 0.0
    for i in range(1, 301):   # Windows wakes up on a 15.6 ms grid, never early
        woke = math.ceil(due / 0.015625) * 0.015625
        due = s.next_due(due, woke, 1000 / 30)
    assert abs(due - 10.0) < 0.05, due            # 300 frames at 30 fps = 10 s, not ~14 s
    assert s.next_due(1.0, 5.0, 1000 / 60) == 5.0  # far behind (sleep): no catch-up burst
    assert abs(s.ease(0.3, 32) - (1 - 0.7 ** 2)) < 1e-9   # one 30 fps frame = two 60 fps frames


def test_update_takes_only_a_newer_installer_from_this_repo():
    good = {"name": "CoverglowSetup.exe", "size": 3, "digest": "sha256:ABC",
            "browser_download_url": s.UPDATE_PREFIX + "v99.0.0/CoverglowSetup.exe"}
    u = s.pick_update({"tag_name": "v99.0.0", "assets": [good]})
    assert u == {"tag": "v99.0.0", "url": good["browser_download_url"], "size": 3, "sha256": "abc"}
    assert s.pick_update({"tag_name": "v" + s.APP_VERSION, "assets": [good]}) is None  # not newer
    evil = dict(good, browser_download_url="https://example.com/CoverglowSetup.exe")
    assert s.pick_update({"tag_name": "v99.0.0", "assets": [evil]}) is None   # other host


def test_downloaded_update_must_match_the_listed_digest():
    import hashlib
    from pathlib import Path
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src.exe")
    open(src, "wb").write(b"installer")
    sha = hashlib.sha256(b"installer").hexdigest()
    upd = {"tag": "v99.0.0", "url": Path(src).as_uri(), "size": 9, "sha256": sha}
    path = s.download_update(upd, os.path.join(d, "out"))
    assert open(path, "rb").read() == b"installer"
    try:
        s.download_update(dict(upd, sha256="0" * 64), os.path.join(d, "bad"))
        raise AssertionError("a file with the wrong digest was accepted")
    except ValueError:
        pass
    assert os.listdir(os.path.join(d, "bad")) == []   # nothing left behind


def test_cached_text_matches_direct_drawing():
    from PIL import Image, ImageChops, ImageDraw
    f = s.TextFont("segoeui.ttf", 13, 1.25)
    for xy, anchor in (((10.3, 12.6), "la"), ((100.5, 30.2), "mm"), ((190.7, 50.4), "rs"),
                       ((1, 1), "lm")):
        a = Image.new("RGBA", (200, 60), (30, 32, 36, 255))
        b = a.copy()
        f.draw(ImageDraw.Draw(a), xy, "Şarkı İçin ğü", (240, 240, 240, 255), anchor)
        f.paste(b, xy, "Şarkı İçin ğü", (240, 240, 240, 255), anchor)
        assert ImageChops.difference(a, b).getbbox() is None, (xy, anchor)


def test_update_check_starts_before_the_window_exists():
    seen = []
    app = SimpleNamespace(_check_update=lambda: seen.append(1),   # no .root yet, as in __init__
                          _recheck_update=lambda: None)
    s.App._recheck_update(app)   # 1.2.7 / 1.2.8 crashed here at startup
    time.sleep(0.2)
    assert seen == [1]


def test_silent_install_keeps_the_folder_and_scope():
    local = r"C:\Users\Ada\AppData\Local"
    mine = r"c:\users\ada\appdata\local\Programs\Coverglow"   # other letter case than LOCALAPPDATA
    per_user = s.installer_args("setup.exe", mine, local)
    assert "/CURRENTUSER" in per_user and "/DIR=" + mine in per_user
    shared = r"C:\Program Files\Coverglow"
    all_users = s.installer_args("setup.exe", shared, local)
    assert "/ALLUSERS" in all_users and "/DIR=" + shared in all_users
    assert all_users[0] == "setup.exe" and "/VERYSILENT" in all_users and "/RELAUNCH=1" in all_users
    assert "/ALLUSERS" in s.installer_args("setup.exe", r"C:\Coverglow", None)   # no LOCALAPPDATA


def test_version_compare_and_installer_in_sync():
    assert s.version_tuple("v1.10.0") > s.version_tuple("1.9.3")
    assert s.version_tuple("v1.2.10") == s.version_tuple(s.APP_VERSION)
    iss = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "Coverglow.iss"),
               encoding="utf-8").read()
    assert f'#define AppVersion "{s.APP_VERSION}"' in iss, "bump AppVersion in Coverglow.iss"


def test_duck_never_ratchets_the_volume_down():
    vol = SimpleNamespace(v=0.8, talk=0.0)
    vol.get, vol.set = (lambda: vol.v), (lambda x: setattr(vol, "v", x))
    vol.peak = lambda names: vol.talk
    cfg = type("C", (dict,), {"save": lambda self: None})(react=False, duck=True, duck_apps=["discord.exe"])
    app = SimpleNamespace(cfg=cfg, volume=vol, _vol_pending=None, level=0.0, _lvl_lo=None,
                          _duck=None, _duck_fade=None, sleep_fade=None, _alarm_fade=None)
    app._vol_mark = lambda v: s.App._vol_mark(app, v)
    app._duck_restore = lambda: s.App._duck_restore(app)
    tick = lambda now: s.App._audio_tick(app, now, {"ok": True}, True)
    t0 = time.monotonic()
    for _ in range(5):            # talk resumes halfway through every restore fade
        vol.talk = 0.5; tick(t0)
        vol.talk = 0.0; tick(t0 + 1.3); tick(t0 + 1.3 + 0.4)
        t0 = time.monotonic()
    vol.talk = 0.5; tick(t0)
    vol.v *= 0.2                  # Discord's own attenuation lowers it further
    vol.talk = 0.0; tick(t0 + 1.3); tick(time.monotonic() + 1.0)
    assert abs(vol.v - 0.8) < 1e-6 and "vol_restore" not in cfg, vol.v
    vol.talk = 0.5; tick(t0)      # ducked to 0.24, user turns it down with the volume key
    app.vol_val = app.vol_until = None
    s.App._change_volume(app, -0.14)
    vol.talk = 0.0; tick(t0 + 1.3); tick(time.monotonic() + 1.0)
    assert abs(vol.v - 0.1) < 1e-6 and "vol_restore" not in cfg, vol.v


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
