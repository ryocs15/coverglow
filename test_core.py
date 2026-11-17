"""Small tests for the risky core logic. Run: python test_core.py (or pytest)."""
import json
import os
import tempfile
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
