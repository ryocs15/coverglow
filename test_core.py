"""Small tests for the risky core logic. Run: python test_core.py (or pytest)."""
import json
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


def test_version_compare_and_installer_in_sync():
    assert s.version_tuple("v1.10.0") > s.version_tuple("1.9.3")
    assert s.version_tuple("v1.2.2") == s.version_tuple(s.APP_VERSION)
    iss = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "Coverglow.iss"),
               encoding="utf-8").read()
    assert f'#define AppVersion "{s.APP_VERSION}"' in iss, "bump AppVersion in Coverglow.iss"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
