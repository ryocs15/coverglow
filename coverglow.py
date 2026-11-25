"""
Coverglow — a Material You desktop widget for Spotify (Windows 10/11)

- Sticks to the desktop like Rainmeter: stays below windows, survives Win+D,
  hidden from the taskbar / Alt+Tab.
- Dynamic Material You palette from the album art (dark / light / system).
- Wavy progress bar: click / drag / scroll to seek.
- Shuffle, repeat (off / context / track), previous / play / next.
- Up-next queue (with an optional Spotify account link); click a track to jump to it.
- Click the cover -> open Spotify. Right click -> settings menu.

Playback info and controls come from the Windows media API (GSMTC); no API key needed.
The up-next queue uses the Spotify Web API (PKCE, no client secret needed).
"""

import asyncio
import base64
import colorsys
import ctypes
import hashlib
import io
import json
import math
import os
import secrets
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import winreg
import tkinter as tk
from ctypes import wintypes
from datetime import date, datetime, timedelta, timezone
import http.client
import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont

from winrt.windows.media import MediaPlaybackAutoRepeatMode as RepeatMode
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winrt.windows.storage.streams import DataReader

FROZEN = getattr(sys, "frozen", False)  # PyInstaller .exe?
# exe: settings and stats in %APPDATA%\Coverglow, so the install folder stays read-only and
# survives reinstalls. Source runs keep them next to the script (tests rely on that).
APP_DIR = (os.path.join(os.environ["APPDATA"], "Coverglow") if FROZEN
           else os.path.dirname(os.path.abspath(__file__)))
os.makedirs(APP_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LAST_PATH = os.path.join(APP_DIR, "last_track.json")
LAST_ART_PATH = os.path.join(APP_DIR, "last_art.img")
STATS_PATH = os.path.join(APP_DIR, "stats.json")
# animation frame interval at the "smooth" setting (~64 fps; saver: 2x). Whole Windows timer
# ticks (15.625 ms): with 1000 / 60 the planned frames drift against the wake-ups and ~7 frames
# a second wait a whole extra tick (a visible hitch)
FRAME_MS = 1000 / 64


def next_due(due, now, interval):
    """Wake-up time (s) of the next tick. Windows timers wake up to ~15.6 ms late, so counting
    from the previous due time (not from now) keeps the average at 1000 / interval per second;
    counting from now turned 33 ms into ~46 ms (30 fps into ~22 fps)."""
    nxt = due + interval / 1000
    return nxt if nxt >= now else now   # fell behind (slow frame, sleep): no catch-up burst


def ease(speed, ms):
    """Per-frame step of an exponential approach tuned at 16 ms, for a frame of ms: the same
    duration at 30 or 60 fps."""
    return 1 - (1 - speed) ** (ms / 16)
LOG_PATH = os.path.join(APP_DIR, "widget.log")
FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

# no stderr under pythonw: write errors to a file
if sys.stderr is None or sys.stdout is None:
    _big = os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000
    _f = open(LOG_PATH, "w" if _big else "a", encoding="utf-8", buffering=1)  # reset past 1 MB
    sys.stderr = sys.stdout = _f


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, file=sys.stderr, flush=True)


def save_json(path, data, **kw):
    """Write to a temp file first, then swap it in: an interrupted write leaves the old file intact."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, **kw)
        f.flush()
        os.fsync(f.fileno())  # data on disk before the swap: a power cut can't leave an empty file
    os.replace(tmp, path)


def set_aside(path, e):
    """An unreadable data file is renamed to .bad, not overwritten by the fresh defaults."""
    log("could not read", os.path.basename(path), "- kept as .bad:", repr(e))
    try:
        os.replace(path, path + ".bad")
    except OSError as e2:
        log("could not set it aside:", repr(e2))


# ------------------------------------------------------------------ layout
W = 380            # logical pixels (96 DPI)
MAIN_H = 268
GAP = 8
Q_HEAD = 52
Q_ROW = 56
Q_ROWS = 5
Q_PAD = 10
TOTAL_H = MAIN_H + GAP + Q_HEAD + Q_ROWS * Q_ROW + Q_PAD
STATS_H = 286  # stats tab body height (at most Q_ROWS * Q_ROW + Q_PAD)

# size presets (multiplier on top of DPI scale); Ctrl + wheel for values in between
SIZES = (("size_s", 0.75), ("size_m", 0.85), ("size_n", 1.0), ("size_l", 1.15))
SCALE_MIN, SCALE_MAX, SCALE_STEP = 0.6, 1.2, 0.05

MINI_H = 80               # mini mode height
MINI_BAR_Y = 73           # thin progress line in mini mode
MINI_ART = 56
BAR_X0, BAR_X1, BAR_CY = 24, W - 24, 160
STRIP_Y0, STRIP_Y1 = 144, 194
LAM, AMP = 36.0, 3.0          # wavelength / amplitude
CTRL_CY = 228
CX = W / 2

# Segoe Fluent Icons
IC_PLAY, IC_PAUSE = "\uF5B0", "\uE769"
IC_PREV, IC_NEXT = "\uE892", "\uE893"
IC_SHUFFLE, IC_REPEAT, IC_REPEAT_ONE = "\uE8B1", "\uE8EE", "\uE8ED"
IC_MUSIC, IC_OPEN, IC_INFO = "\uE8D6", "\uE8A7", "\uE946"
IC_DOWN, IC_UP = "\uE70D", "\uE70E"
IC_CAL, IC_VOL, IC_MUTE, IC_ALARM = "\uE787", "\uE767", "\uE74F", "\uEA8F"

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = ("user-read-playback-state user-read-currently-playing "
          "user-library-read user-library-modify user-modify-playback-state")
APP_VERSION = "1.2.10"  # keep in sync with AppVersion in Coverglow.iss (test_core checks it)
UA = f"Coverglow/{APP_VERSION} (+https://github.com/ryocs15/coverglow)"


def version_tuple(v):
    """'v1.10.0' -> (1, 10, 0), so versions compare as numbers, not text."""
    return tuple(int(x) for x in v.strip().lstrip("vV").split(".") if x.isdigit())


UPDATE_PREFIX = "https://github.com/ryocs15/coverglow/releases/download/"


def pick_update(release):
    """From a GitHub 'latest release' answer: {tag, url, size, sha256} of CoverglowSetup.exe if
    the release is newer than this build and the file really comes from this repo, else None."""
    tag = release.get("tag_name") or ""
    if version_tuple(tag) <= version_tuple(APP_VERSION):
        return None
    for a in release.get("assets") or []:
        url = a.get("browser_download_url") or ""
        if a.get("name") == "CoverglowSetup.exe" and url.startswith(UPDATE_PREFIX):
            digest = a.get("digest") or ""
            return {"tag": tag, "url": url, "size": int(a.get("size") or 0),
                    "sha256": digest[7:].lower() if digest.startswith("sha256:") else None}
    return None


def latest_release():
    """The newer release's installer (see pick_update), else None."""
    req = urllib.request.Request(
        "https://api.github.com/repos/ryocs15/coverglow/releases/latest",
        headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    return pick_update(json.loads(http_read(req, timeout=10)))


def installer_args(path, app_dir, local_appdata):
    """Silent install over this copy: same folder, same scope (per-user installs live under
    %LOCALAPPDATA%; anything else is an all-users install and asks for admin rights as before)."""
    local = os.path.normcase(local_appdata or "")
    scope = "/CURRENTUSER" if local and os.path.normcase(app_dir).startswith(local) else "/ALLUSERS"
    return [path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", scope,
            f"/DIR={app_dir}", "/RELAUNCH=1"]


def download_update(upd, folder):
    """Download the installer to folder and check its size and SHA-256 (GitHub's asset digest).
    Returns the file path; raises ValueError if the file is not the one GitHub listed."""
    os.makedirs(folder, exist_ok=True)
    for old in os.listdir(folder):   # earlier installers / broken downloads (~22 MB each)
        try:
            os.remove(os.path.join(folder, old))
        except OSError:
            pass
    path = os.path.join(folder, f"CoverglowSetup-{upd['tag']}.exe")
    h, n = hashlib.sha256(), 0
    req = urllib.request.Request(upd["url"], headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r, open(path + ".part", "wb") as f:
        while chunk := r.read(1 << 16):
            h.update(chunk)
            n += len(chunk)
            f.write(chunk)
    if (upd["size"] and n != upd["size"]) or (upd["sha256"] and h.hexdigest() != upd["sha256"]):
        os.remove(path + ".part")
        raise ValueError(f"download check failed: {n} bytes")
    os.replace(path + ".part", path)
    return path


# ------------------------------------------------------------------ language
STRINGS = {
    "tr": {
        "playing": "ŞİMDİ ÇALIYOR", "paused": "DURAKLATILDI",
        "sleep_status": " · ☾ {m} DK", "sleep_sub": "☾ {m} dk · ",
        "offline_chip": "SPOTIFY KAPALI · AÇMAK İÇİN TIKLA",
        "waiting_title": "Spotify bekleniyor", "waiting_sub": "Spotify'ı aç ve bir şarkı çal",
        "mini_waiting_sub": "Spotify'ı aç", "mini_offline": "Spotify kapalı · aç",
        "unknown": "Bilinmeyen",
        "tab_queue": "Sıradaki", "tab_recent": "Geçmiş",
        "connect_line1": "Sıradaki şarkıları görmek için", "connect_line2": "Spotify hesabını bağla.",
        "connect_btn": "Bağla", "loading": "Yükleniyor…", "queue_empty": "Sırada şarkı yok",
        "recent_empty": "Henüz çalınan şarkı yok",
        "tab_stats": "İstatistik", "st_today": "Bugün", "st_week": "Bu hafta",
        "st_songs": "Şarkı · bugün", "st_top": "EN ÇOK DİNLENENLER · 7 GÜN",
        "st_empty": "Dinledikçe burada görünecek", "dur_min": "{m} dk", "dur_hm": "{h} sa {m} dk",
        "days": "Pt Sa Ça Pe Cu Ct Pz", "dur_h": "{h} sa",
        "months": "Oca Şub Mar Nis May Haz Tem Ağu Eyl Eki Kas Ara",
        "sp_7": "7 gün", "sp_30": "30 gün", "sp_365": "1 yıl", "sp_all": "Tümü",
        "st_total": "Toplam", "st_plays": "Çalma", "st_avg": "Günlük ort.",
        "st_top_a": "EN ÇOK DİNLENEN SANATÇILAR", "st_top_t": "EN ÇOK DİNLENEN ŞARKILAR",
        "sl_artists": "Sanatçı", "sl_tracks": "Şarkı",
        "m_import": "Spotify verisini içe aktar…",
        "imp_title": "Spotify verisi", "imp_busy": "İçe aktarılıyor…",
        "imp_ok": "{n} dinleme içe aktarıldı ({h} saat, {a} – {b}).",
        "imp_none": "Dosyada dinleme geçmişi bulunamadı.",
        "imp_fail": "İçe aktarılamadı: {e}",
        "imp_hint": ("Spotify > Hesap > Gizlilik ayarları > \"Verilerini indir\" ile aldığın "
                     ".zip dosyasını ya da içindeki StreamingHistory .json dosyalarını seç."),
        # v8
        "tab_lyrics": "Sözler", "ly_loading": "Sözler aranıyor…", "ly_none": "Sözler bulunamadı",
        "ly_instr": "♪ Enstrümantal", "ly_plain": "Senkronize değil", "ly_err": "Sözler alınamadı",
        "sl_hours": "Saat", "st_hours": "DİNLEME SAATLERİ", "st_streak": "Rekor: {n} gün",
        "st_days": "{n} gün", "st_streak_v": "{n} gün seri", "since": "{d}'den beri",
        "months_full": "Ocak Şubat Mart Nisan Mayıs Haziran Temmuz Ağustos Eylül Ekim Kasım Aralık",
        "lastyear": "Geçen yıl bugün", "wrap_btn": "★ Özet", "ly_short": "Geçen yıl bugün",
        "ly_toast": "Geçen yıl bugün: {t} · {a}", "m_memory": "Günlük \"geçen yıl bugün\" hatırlatması",
        "wr_title": "{y} Özeti", "wr_total": "toplam dinleme", "wr_plays": "çalma",
        "wr_artists": "EN ÇOK DİNLENEN SANATÇILAR", "wr_tracks": "EN ÇOK DİNLENEN ŞARKILAR",
        "wr_month": "En yoğun ay", "wr_day": "En sevdiğin gün", "wr_hour": "En sevdiğin saat",
        "wr_streak": "En uzun seri", "wr_copy": "Panoya kopyala", "wr_save": "Kaydet…",
        "wr_copied": "Görsel panoya kopyalandı",
        "weekdays": "Pazartesi Salı Çarşamba Perşembe Cuma Cumartesi Pazar",
        "m_react": "Müziğe tepki veren dalga", "m_duck": "Discord'da konuşulunca Spotify'ı kıs",
        "m_osd": "Kısayolda ekran göstergesi",
        "m_tpos": "Bildirim yeri", "m_tsecs": "Bildirim süresi", "m_tprev": "Bildirimi önizle",
        "sec": "sn",
        "osd_play": "Çalıyor", "osd_pause": "Duraklatıldı", "osd_vol": "Ses",
        "m_search": "Şarkı ara…  (Ctrl+Alt+S)", "hk_search": "Şarkı ara",
        "hk_like": "Beğen / beğeniyi kaldır", "osd_liked": "Beğenildi",
        "osd_unliked": "Beğeni kaldırıldı", "like_wait": "Şarkı henüz tanınmadı, birazdan tekrar dene",
        "hs_title": "Dinleme geçmişinde ara", "hs_hint": "Şarkı ya da sanatçı adı yaz",
        "hs_song": "Şarkı", "hs_artist": "Sanatçı", "hs_time": "Süre", "hs_plays": "Çalma",
        "hs_days": "Gün", "hs_first": "İlk", "hs_last": "Son", "hs_none": "Eşleşen kayıt yok",
        "hs_count": "{n} sonuç", "hs_open": "Spotify'da aç", "hs_menu": "Geçmişte ara…",
        "hs_detail": ("{name}\nToplam {dur} · {plays} çalma · {days} farklı gün\n"
                      "İlk: {first}   ·   Son: {last}   ·   En çok: {year}"),
        "hs_empty": "Önce Spotify verini içe aktar (menü → Spotify hesabı).",
        "s_title": "Şarkı ara", "s_hint": "Şarkı, sanatçı ya da albüm yaz ve Enter'a bas",
        "s_play": "Çal", "s_queue": "Sıraya ekle", "s_none": "Sonuç yok", "s_queued": "Sıraya eklendi ✓",
        "s_need": "Bu özellik için Spotify hesabını bağla.",
        "s_premium": "Spotify bunun için Premium istiyor; şarkı Spotify'da açıldı.",
        "m_devices": "Cihaza aktar", "dev_none": "Açık cihaz yok", "dev_err": "Cihazlar alınamadı",
        "m_share": "Şarkıyı paylaş", "sh_link": "Bağlantıyı kopyala", "sh_card": "Paylaşım kartını kopyala",
        "sh_link_ok": "Bağlantı panoya kopyalandı", "sh_card_ok": "Kart panoya kopyalandı ve kaydedildi",
        "m_alarm": "Alarm", "al_on": "Alarm açık", "al_set": "Saati ayarla…  ({t})",
        "al_title": "Alarm", "al_text": "Bu saatte Spotify açılır ve ses yavaşça açılarak çalmaya başlar.\n"
                                         "(Bilgisayar açık ve widget çalışıyor olmalı.)",
        "al_save": "Kaydet", "al_fired": "Günaydın! Alarm çalıyor",
        "need_scope": "Bu özellik için hesabı bir kez yeniden bağla (yeni izin gerekiyor).",
        # yeni menü
        "mn_appearance": "Görünüm", "mn_transparency": "Şeffaflık", "mn_widget": "Widget",
        "mn_notif": "Bildirim ve kısayollar", "mn_account": "Spotify hesabı",
        "mn_language": "Dil", "mn_share": "Şarkıyı paylaş", "acc_on": "Bağlı",
        "acc_off": "Bağlı değil", "q_search": "Ara", "q_mini": "Mini", "q_lock": "Kilitle",
        "q_share": "Paylaş", "q_open": "Spotify", "off": "Kapalı", "on": "Açık",
        "sleep_left": "{m} dk kaldı", "seg_desktop": "Masaüstü", "seg_top": "Her zaman üstte",
        "m_rows_s": "Listedeki satır", "m_idle_s": "Fare uzaktayken", "m_reconnect": "Yeniden bağla",
        "hk_list": "KISAYOLLAR", "m_fps": "Animasyon akıcılığı", "m_ui": "Tasarım",
        "sd_dur": "Süre", "sd_liked": "Beğeni", "sd_yours": "SENİN DİNLEMEN",
        "sd_open": "Spotify'da aç", "sd_first": "Bu şarkıyı ilk kez dinliyorsun 🎧",
        "sd_album": "Albüm", "sd_single": "Single", "sd_comp": "Derleme", "sd_type": "Tür",
        "sd_release": "Çıkış", "sd_track": "Sıra", "sd_disc": "Disk", "sd_explicit": "Açık içerik",
        "fps_30": "30 fps · tasarruflu", "fps_60": "60 fps · akıcı",
        "m_accent": "Renk", "ac_auto": "Albüm kapağından", "ac_green": "Yeşil",
        "ac_blue": "Mavi", "ac_purple": "Mor", "ac_pink": "Pembe", "ac_red": "Kırmızı",
        "ac_orange": "Turuncu", "ac_teal": "Turkuaz", "ac_mono": "Gri",
        "m_bg": "Arka plan", "bg_solid": "Düz", "bg_glass": "Cam (bulanık duvar kağıdı)",
        "bg_clear": "Yarı şeffaf",
        "ago_now": "şimdi", "ago_min": "{n} dk önce", "ago_hour": "{n} sa önce",
        "ago_day": "{n} gün önce",
        "err_expired": "Bağlantının süresi doldu — tekrar bağla",
        "err_denied": "Erişim reddedildi (uygulama ayarlarını kontrol et)",
        "err_http": "Spotify hatası ({code})",
        "auth_wait": "Tarayıcıda Spotify'a izin ver…",
        "auth_page_ok": "Bağlandı! Bu sekmeyi kapatabilirsin.",
        "auth_page_fail": "Bağlantı başarısız oldu.",
        "auth_port": "Hata: 8888 portu kullanımda.",
        "auth_denied": "Hata: izin alınamadı / zaman aşımı",
        "auth_token": "Hata: token alınamadı ({e})",
        # menü
        "m_layer": "Katman", "m_desktop": "Masaüstünde (pencerelerin altında)",
        "m_top": "Her zaman üstte", "m_lock": "Konumu kilitle", "m_theme": "Tema",
        "th_system": "Sistem", "th_dark": "Koyu", "th_light": "Açık",
        "m_opacity": "Opaklık", "m_size": "Boyut",
        "mh_custom": "Kişiselleştir", "mh_play": "Çalma", "mh_app": "Uygulama",
        "size_s": "Küçük", "size_m": "Orta", "size_n": "Normal", "size_l": "Büyük",
        "size_hint": "İnce ayar: widget üstünde Ctrl + tekerlek",
        "m_rows": "Listedeki şarkı sayısı", "rows_n": "{n} şarkı",
        "m_mini": "Mini mod (boş yere çift tıkla)",
        "m_sleep": "Uyku zamanlayıcısı", "sleep_off": "Kapalı", "sleep_n": "{n} dakika",
        "m_hotkeys": "Klavye kısayolları", "hk_enable": "Kısayolları etkinleştir",
        "hk_play": "Oynat / duraklat", "hk_next": "Sonraki şarkı", "hk_prev": "Önceki şarkı",
        "hk_volup": "Sesi aç", "hk_voldown": "Sesi kıs",
        "m_fade": "Fare uzaktayken soluklaştır", "m_idle": "Fare uzaktayken opaklık",
        "m_wave": "Dalgalı ilerleme çubuğu",
        "m_showlist": "Sıradaki / son çalınanlar kartını göster",
        "m_notify": "Şarkı değişince bildirim göster",
        "m_connect": "Spotify hesabını bağla…", "m_disconnect": "Spotify bağlantısını kaldır",
        "m_open": "Spotify'ı aç", "m_startup": "Windows açılışında başlat", "m_close": "Kapat", "upd_new": "Güncelle: {v}",
        "upd_dl": "Güncelleme indiriliyor…", "upd_inst": "Güncelleme kuruluyor…",
        "upd_fail": "Güncelleme indirilemedi", "upd_src": "Kaynak koddan çalışıyor: git pull ile güncelle",
        # bağlantı penceresi
        "dlg_title": "Spotify Hesabını Bağla", "dlg_head": "Spotify hesabını bağla",
        "dlg_steps": ("Sıradaki listesi ve Beğen butonu Spotify Web API ile çalışır. "
                      "Bir kerelik kurulum:\n\n"
                      "1. Aşağıdaki butonla Spotify Developer panelini aç ve \"Create app\" de.\n"
                      "2. \"Redirect URI\" olarak şu adresi ekle (kopyala butonu var):\n"
                      "      {uri}\n"
                      "3. \"Web API\" kutusunu işaretle ve kaydet.\n"
                      "4. Uygulamanın \"Client ID\" değerini aşağıya yapıştır ve Bağla'ya bas."),
        "dlg_reauth": "Yeni özellikler için hesabı bir kez yeniden bağla (yeni izin gerekiyor).",
        "dlg_dev": "Developer panelini aç", "dlg_copy": "Redirect URI'yi kopyala",
        "dlg_copied": "Redirect URI panoya kopyalandı.",
        "dlg_invalid": "Geçerli bir Client ID yapıştır.", "dlg_ok": "Bağlandı ✓",
        "dlg_connect": "Bağla", "dlg_close": "Kapat",
    },
    "en": {
        "playing": "NOW PLAYING", "paused": "PAUSED",
        "sleep_status": " · ☾ {m} MIN", "sleep_sub": "☾ {m} min · ",
        "offline_chip": "SPOTIFY CLOSED · CLICK TO OPEN",
        "waiting_title": "Waiting for Spotify", "waiting_sub": "Open Spotify and play a song",
        "mini_waiting_sub": "Open Spotify", "mini_offline": "Spotify closed · open",
        "unknown": "Unknown",
        "tab_queue": "Up next", "tab_recent": "History",
        "connect_line1": "Connect your Spotify account", "connect_line2": "to see what's up next.",
        "connect_btn": "Connect", "loading": "Loading…", "queue_empty": "Queue is empty",
        "recent_empty": "Nothing played yet",
        "tab_stats": "Stats", "st_today": "Today", "st_week": "This week",
        "st_songs": "Songs · today", "st_top": "TOP ARTISTS · 7 DAYS",
        "st_empty": "Your listening will show up here", "dur_min": "{m} min",
        "dur_hm": "{h}h {m}m", "days": "Mo Tu We Th Fr Sa Su", "dur_h": "{h}h",
        "months": "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec",
        "sp_7": "7 days", "sp_30": "30 days", "sp_365": "1 year", "sp_all": "All time",
        "st_total": "Total", "st_plays": "Plays", "st_avg": "Daily avg.",
        "st_top_a": "TOP ARTISTS", "st_top_t": "TOP SONGS",
        "sl_artists": "Artists", "sl_tracks": "Songs",
        "m_import": "Import Spotify data…",
        "imp_title": "Spotify data", "imp_busy": "Importing…",
        "imp_ok": "Imported {n} plays ({h} hours, {a} – {b}).",
        "imp_none": "No listening history found in the file.",
        "imp_fail": "Import failed: {e}",
        "imp_hint": ("Pick the .zip from Spotify > Account > Privacy settings > "
                     "\"Download your data\", or the StreamingHistory .json files inside it."),
        "tab_lyrics": "Lyrics", "ly_loading": "Looking for lyrics…", "ly_none": "No lyrics found",
        "ly_instr": "♪ Instrumental", "ly_plain": "Not synced", "ly_err": "Couldn't load lyrics",
        "sl_hours": "Hours", "st_hours": "LISTENING HOURS", "st_streak": "Best: {n} days",
        "st_days": "{n} days", "st_streak_v": "{n}-day streak", "since": "since {d}",
        "months_full": ("January February March April May June July August September October "
                        "November December"),
        "lastyear": "On this day last year", "wrap_btn": "★ Recap", "ly_short": "Last year",
        "ly_toast": "A year ago today: {t} · {a}", "m_memory": "Daily \"a year ago today\" reminder",
        "wr_title": "{y} Recap", "wr_total": "total listening", "wr_plays": "plays",
        "wr_artists": "TOP ARTISTS", "wr_tracks": "TOP SONGS",
        "wr_month": "Busiest month", "wr_day": "Favorite day", "wr_hour": "Favorite hour",
        "wr_streak": "Longest streak", "wr_copy": "Copy to clipboard", "wr_save": "Save…",
        "wr_copied": "Image copied to clipboard",
        "weekdays": "Monday Tuesday Wednesday Thursday Friday Saturday Sunday",
        "m_react": "Wave reacts to music", "m_duck": "Lower Spotify when someone talks on Discord",
        "m_osd": "On-screen display for shortcuts",
        "m_tpos": "Notification position", "m_tsecs": "Notification time",
        "m_tprev": "Preview notification",
        "sec": "s",
        "osd_play": "Playing", "osd_pause": "Paused", "osd_vol": "Volume",
        "m_search": "Search songs…  (Ctrl+Alt+S)", "hk_search": "Search songs",
        "hk_like": "Like / unlike", "osd_liked": "Liked", "osd_unliked": "Removed from Liked",
        "like_wait": "Song not recognized yet, try again in a moment",
        "hs_title": "Search listening history", "hs_hint": "Type a song or artist name",
        "hs_song": "Song", "hs_artist": "Artist", "hs_time": "Time", "hs_plays": "Plays",
        "hs_days": "Days", "hs_first": "First", "hs_last": "Last", "hs_none": "No matches",
        "hs_count": "{n} results", "hs_open": "Open in Spotify", "hs_menu": "Search history…",
        "hs_detail": ("{name}\nTotal {dur} · {plays} plays · {days} different days\n"
                      "First: {first}   ·   Last: {last}   ·   Most: {year}"),
        "hs_empty": "Import your Spotify data first (menu → Spotify account).",
        "s_title": "Search songs", "s_hint": "Type a song, artist or album and press Enter",
        "s_play": "Play", "s_queue": "Add to queue", "s_none": "No results", "s_queued": "Added to queue ✓",
        "s_need": "Connect your Spotify account to use this.",
        "s_premium": "Spotify requires Premium for this; opened the song in Spotify.",
        "m_devices": "Transfer to device", "dev_none": "No active devices",
        "dev_err": "Couldn't load devices",
        "m_share": "Share song", "sh_link": "Copy link", "sh_card": "Copy share card",
        "sh_link_ok": "Link copied to clipboard", "sh_card_ok": "Card copied to clipboard and saved",
        "m_alarm": "Alarm", "al_on": "Alarm on", "al_set": "Set time…  ({t})",
        "al_title": "Alarm", "al_text": "At this time Spotify opens and starts playing, fading the volume in.\n"
                                         "(The PC must be on and the widget running.)",
        "al_save": "Save", "al_fired": "Good morning! Alarm playing",
        "need_scope": "Reconnect your account once to use this (needs a new permission).",
        "mn_appearance": "Appearance", "mn_transparency": "Transparency", "mn_widget": "Widget",
        "mn_notif": "Notifications & shortcuts", "mn_account": "Spotify account",
        "mn_language": "Language", "mn_share": "Share song", "acc_on": "Connected",
        "acc_off": "Not connected", "q_search": "Search", "q_mini": "Mini", "q_lock": "Lock",
        "q_share": "Share", "q_open": "Spotify", "off": "Off", "on": "On",
        "sleep_left": "{m} min left", "seg_desktop": "Desktop", "seg_top": "Always on top",
        "m_rows_s": "Rows in list", "m_idle_s": "When mouse is away", "m_reconnect": "Reconnect",
        "hk_list": "SHORTCUTS", "m_fps": "Animation smoothness", "m_ui": "Design",
        "sd_dur": "Duration", "sd_liked": "Liked", "sd_yours": "YOUR LISTENING",
        "sd_open": "Open in Spotify", "sd_first": "First time listening to this one 🎧",
        "sd_album": "Album", "sd_single": "Single", "sd_comp": "Compilation", "sd_type": "Type",
        "sd_release": "Released", "sd_track": "Track", "sd_disc": "Disc", "sd_explicit": "Explicit",
        "fps_30": "30 fps · saver", "fps_60": "60 fps · smooth",
        "m_accent": "Color", "ac_auto": "From album art", "ac_green": "Green",
        "ac_blue": "Blue", "ac_purple": "Purple", "ac_pink": "Pink", "ac_red": "Red",
        "ac_orange": "Orange", "ac_teal": "Teal", "ac_mono": "Gray",
        "m_bg": "Background", "bg_solid": "Solid", "bg_glass": "Glass (blurred wallpaper)",
        "bg_clear": "Translucent",
        "ago_now": "now", "ago_min": "{n} min ago", "ago_hour": "{n} h ago",
        "ago_day": "{n} d ago",
        "err_expired": "Connection expired — reconnect",
        "err_denied": "Access denied (check your app settings)",
        "err_http": "Spotify error ({code})",
        "auth_wait": "Allow access to Spotify in your browser…",
        "auth_page_ok": "Connected! You can close this tab.",
        "auth_page_fail": "Connection failed.",
        "auth_port": "Error: port 8888 is in use.",
        "auth_denied": "Error: permission denied / timed out",
        "auth_token": "Error: could not get token ({e})",
        "m_layer": "Layer", "m_desktop": "On desktop (below windows)",
        "m_top": "Always on top", "m_lock": "Lock position", "m_theme": "Theme",
        "th_system": "System", "th_dark": "Dark", "th_light": "Light",
        "m_opacity": "Opacity", "m_size": "Size",
        "mh_custom": "Personalize", "mh_play": "Playback", "mh_app": "App",
        "size_s": "Small", "size_m": "Medium", "size_n": "Normal", "size_l": "Large",
        "size_hint": "Fine-tune: Ctrl + mouse wheel on the widget",
        "m_rows": "Songs in list", "rows_n": "{n} songs",
        "m_mini": "Mini mode (double-click empty area)",
        "m_sleep": "Sleep timer", "sleep_off": "Off", "sleep_n": "{n} minutes",
        "m_hotkeys": "Keyboard shortcuts", "hk_enable": "Enable shortcuts",
        "hk_play": "Play / pause", "hk_next": "Next track", "hk_prev": "Previous track",
        "hk_volup": "Volume up", "hk_voldown": "Volume down",
        "m_fade": "Fade when mouse is away", "m_idle": "Opacity when mouse is away",
        "m_wave": "Wavy progress bar",
        "m_showlist": "Show Up next / Recent card",
        "m_notify": "Notify on song change",
        "m_connect": "Connect Spotify account…", "m_disconnect": "Disconnect Spotify",
        "m_open": "Open Spotify", "m_startup": "Start with Windows", "m_close": "Close", "upd_new": "Update to {v}",
        "upd_dl": "Downloading the update…", "upd_inst": "Installing the update…",
        "upd_fail": "Could not download the update", "upd_src": "Running from source: update with git pull",
        "dlg_title": "Connect Spotify Account", "dlg_head": "Connect your Spotify account",
        "dlg_steps": ("Up next and the Like button use the Spotify Web API. "
                      "One-time setup:\n\n"
                      "1. Open the Spotify Developer dashboard below and click \"Create app\".\n"
                      "2. Add this \"Redirect URI\" (there's a copy button):\n"
                      "      {uri}\n"
                      "3. Tick \"Web API\" and save.\n"
                      "4. Paste the app's \"Client ID\" below and click Connect."),
        "dlg_reauth": "Reconnect once to use the new features (needs a new permission).",
        "dlg_dev": "Open Developer dashboard", "dlg_copy": "Copy Redirect URI",
        "dlg_copied": "Redirect URI copied to clipboard.",
        "dlg_invalid": "Paste a valid Client ID.", "dlg_ok": "Connected ✓",
        "dlg_connect": "Connect", "dlg_close": "Close",
    },
}
LANG = "tr"


def T(key, **kw):
    s = STRINGS.get(LANG, STRINGS["tr"]).get(key) or STRINGS["tr"].get(key, key)
    return s.format(**kw) if kw else s


def default_lang():
    try:  # Windows UI language Turkish -> tr, otherwise en
        return "tr" if (ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF) == 0x1F \
            else "en"
    except Exception:  # noqa: BLE001
        return "en"


def fmt_dur(sec):
    m = int(sec // 60)
    if m >= 6000:  # over 100 hours: hours only
        return T("dur_h", h=f"{m // 60:,}".replace(",", "."))
    return T("dur_min", m=m) if m < 60 else T("dur_hm", h=m // 60, m=m % 60)


STATS_IMPORT_PATH = os.path.join(APP_DIR, "stats_import.json")


def import_spotify_history(paths):
    """Convert a Spotify 'Download your data' export (zip or json) into daily summaries.
    Supports both extended streaming history (ts, ms_played, master_metadata_*) and account data
    (endTime, msPlayed, artistName, trackName). Only music is counted."""
    import zipfile

    def records():
        for p in paths:
            if p.lower().endswith(".zip"):
                with zipfile.ZipFile(p) as z:
                    for name in z.namelist():
                        base = os.path.basename(name)
                        if base.endswith(".json") and (base.startswith("Streaming_History_Audio")
                                                       or base.startswith("StreamingHistory")
                                                       or base.startswith("endsong")):
                            yield from json.loads(z.read(name))
            else:
                with open(p, "r", encoding="utf-8") as f:
                    yield from json.load(f)

    artists, a_idx, tracks, t_idx, days = [], {}, [], {}, {}
    n, total, first, last, seen = 0, 0.0, None, None, set()
    for r in records():
        if not isinstance(r, dict):
            continue
        if "ts" in r:
            title = r.get("master_metadata_track_name")
            artist = r.get("master_metadata_album_artist_name")
            ms, stamp = r.get("ms_played") or 0, r.get("ts")
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None
        else:
            title, artist = r.get("trackName"), r.get("artistName")
            ms, stamp = r.get("msPlayed") or 0, r.get("endTime")
            when = (datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                    if stamp else None)
            if r.get("podcastName") or r.get("episodeName"):
                title = None
        if not title or not artist or when is None or ms <= 0:
            continue  # podcast, audiobook, broken record
        # if account data + extended history are picked together the same play arrives twice
        dup = (when.strftime("%Y-%m-%d %H:%M"), title, artist)
        if dup in seen:
            continue
        seen.add(dup)
        local = when.astimezone()
        day = local.date().isoformat()
        sec = ms / 1000.0
        ai = a_idx.get(artist)
        if ai is None:
            ai = a_idx[artist] = len(artists)
            artists.append(artist)
        tk_ = (title, ai)
        ti = t_idx.get(tk_)
        if ti is None:
            ti = t_idx[tk_] = len(tracks)
            tracks.append([title, ai])
        d = days.setdefault(day, {"s": 0.0, "p": 0, "a": {}, "t": {}, "h": {}, "c": {}})
        d["s"] += sec
        hr = str(local.hour)
        d["h"][hr] = d["h"].get(hr, 0.0) + sec
        if ms >= 30000:  # Spotify counts 30 s as a play
            d["p"] += 1
            d["c"][ti] = d["c"].get(ti, 0) + 1
        d["a"][ai] = d["a"].get(ai, 0.0) + sec
        d["t"][ti] = d["t"].get(ti, 0.0) + sec
        n += 1
        total += sec
        first = day if first is None or day < first else first
        last = day if last is None or day > last else last
    if not n:
        return None
    for d in days.values():  # keep the file small
        d["s"] = round(d["s"], 1)
        d["a"] = {str(k): round(v, 1) for k, v in d["a"].items()}
        d["t"] = {str(k): round(v, 1) for k, v in d["t"].items()}
        d["h"] = {k: round(v, 1) for k, v in d["h"].items()}
        d["c"] = {str(k): v for k, v in d["c"].items()}
    data = {"version": 3, "first": first, "last": last, "artists": artists, "tracks": tracks,
            "days": days}
    save_json(STATS_IMPORT_PATH, data, ensure_ascii=False, separators=(",", ":"))
    return {"n": n, "hours": round(total / 3600), "first": first, "last": last}


def load_imported_history():
    """stats_import.json → {day: {"sec", "plays", "artists": {name: s}, "tracks": {name\\tartist: s}}}"""
    try:
        with open(STATS_IMPORT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        artists, tracks = data["artists"], data["tracks"]
        tnames = [f"{t}\t{artists[a]}" for t, a in tracks]
        out = {}
        for day, d in data["days"].items():
            out[day] = {"sec": d["s"], "plays": d["p"], "hours": d.get("h", {}),
                        "counts": {tnames[int(k)]: v for k, v in d.get("c", {}).items()},
                        "artists": {artists[int(k)]: v for k, v in d["a"].items()},
                        "tracks": {tnames[int(k)]: v for k, v in d["t"].items()}}
        return out
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        log("could not read imported history:", repr(e))
        return {}


def time_ago(ts):
    sec = max(0, time.time() - ts)
    if sec < 60:
        return T("ago_now")
    if sec < 3600:
        return T("ago_min", n=int(sec // 60))
    if sec < 86400:
        return T("ago_hour", n=int(sec // 3600))
    return T("ago_day", n=int(sec // 86400))


# ------------------------------------------------------------------ settings
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    """Windows DPAPI: only this Windows user on this PC can decrypt (other programs reading the file can't)."""
    buf = ctypes.create_string_buffer(data, len(data))
    src, out = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):  # UI_FORBIDDEN
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


class Config(dict):
    """config.json; the Spotify refresh token is stored DPAPI-encrypted as refresh_token_enc."""

    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                self.update(json.load(f))
        except FileNotFoundError:
            pass  # first run
        except Exception as e:  # noqa: BLE001
            set_aside(CONFIG_PATH, e)
        enc = self.pop("refresh_token_enc", None)
        if enc:
            try:
                self["refresh_token"] = _dpapi(base64.b64decode(enc), False).decode()
            except Exception as e:  # other user / PC: the user reconnects
                log("could not decrypt the saved token:", repr(e))
                self.pop("refresh_token", None)
        elif self.get("refresh_token"):
            self.save()  # older versions stored it in plain text: encrypt it now

    def save(self):
        with self.lock:
            try:
                d = dict(self)
                rt = d.pop("refresh_token", None)
                if rt:
                    d["refresh_token_enc"] = base64.b64encode(_dpapi(rt.encode(), True)).decode()
                save_json(CONFIG_PATH, d, indent=1)
            except Exception as e:
                log("could not save config:", e)


# ------------------------------------------------------------------ Win32
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

GWL_EXSTYLE = -20
GWLP_HWNDPARENT = -8
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_APPWINDOW = 0x00040000
WS_EX_TRANSPARENT = 0x00000020
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
HWND_BOTTOM, HWND_TOPMOST, HWND_NOTOPMOST = 1, -1, -2
ULW_ALPHA = 2


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class ULWINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hdcDst", wintypes.HDC),
                ("pptDst", ctypes.POINTER(wintypes.POINT)),
                ("psize", ctypes.POINTER(wintypes.SIZE)), ("hdcSrc", wintypes.HDC),
                ("pptSrc", ctypes.POINTER(wintypes.POINT)), ("crKey", wintypes.DWORD),
                ("pblend", ctypes.POINTER(BLENDFUNCTION)), ("dwFlags", wintypes.DWORD),
                ("prcDirty", ctypes.POINTER(wintypes.RECT))]


user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowPos.argtypes = [wintypes.HWND, ctypes.c_ssize_t, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.UpdateLayeredWindowIndirect.argtypes = [wintypes.HWND, ctypes.POINTER(ULWINFO)]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE,
                                   wintypes.DWORD]
gdi32.CreateDIBSection.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [wintypes.HDC, ctypes.c_void_p]
gdi32.SelectObject.restype = ctypes.c_void_p


def set_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        return user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


class LayeredSurface:
    """Draws window content with per-pixel alpha (UpdateLayeredWindowIndirect)."""

    def __init__(self, hwnd, w, h):
        self.hwnd, self.w, self.h = hwnd, w, h
        self.alpha = 255
        self.screen_dc = user32.GetDC(None)
        self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        self.bits = ctypes.c_void_p()
        self.hbmp = gdi32.CreateDIBSection(self.mem_dc, ctypes.byref(bmi), 0,
                                           ctypes.byref(self.bits), None, 0)
        gdi32.SelectObject(self.mem_dc, self.hbmp)
        self._warned = False

    @staticmethod
    def to_bgra(img):
        r, g, b, a = img.convert("RGBa").split()  # premultiplied alpha
        return Image.merge("RGBA", (b, g, r, a)).tobytes()

    def full(self, img, h=None, pos=None):
        """Update the whole window. If h is given only the top h rows are converted (when content
        is small, e.g. mini mode, to avoid processing the whole window); the rest is cleared.
        If pos is given the window is moved in the same call (move + draw in the same frame)."""
        h = self.h if h is None else max(1, min(self.h, h))
        data = self.to_bgra(img if h == self.h else img.crop((0, 0, self.w, h)))
        ctypes.memmove(self.bits, data, len(data))
        prev = getattr(self, "_used_h", self.h)
        if prev > h:  # clear rows left over from taller previous content
            ctypes.memset(self.bits.value + h * self.w * 4, 0, (prev - h) * self.w * 4)
        self._used_h = h
        self._commit(None, pos)

    def region(self, img, x, y):
        data = self.to_bgra(img)
        rw = img.width * 4
        base = self.bits.value
        for r in range(img.height):
            ctypes.memmove(base + ((y + r) * self.w + x) * 4, data[r * rw:(r + 1) * rw], rw)
        self._commit(wintypes.RECT(x, y, x + img.width, y + img.height))

    def _commit(self, rect, pos=None):
        size = wintypes.SIZE(self.w, self.h)
        src = wintypes.POINT(0, 0)
        blend = BLENDFUNCTION(0, 0, self.alpha, 1)
        info = ULWINFO()
        info.cbSize = ctypes.sizeof(ULWINFO)
        info.hdcDst = self.screen_dc
        info.pptDst = ctypes.pointer(wintypes.POINT(*pos)) if pos else None
        info.psize = ctypes.pointer(size)
        info.hdcSrc = self.mem_dc
        info.pptSrc = ctypes.pointer(src)
        info.pblend = ctypes.pointer(blend)
        info.dwFlags = ULW_ALPHA
        info.prcDirty = ctypes.pointer(rect) if rect is not None else None
        if not user32.UpdateLayeredWindowIndirect(self.hwnd, ctypes.byref(info)):
            if not self._warned:
                log("UpdateLayeredWindowIndirect failed:", ctypes.GetLastError())
                self._warned = True


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user32.GetForegroundWindow.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def window_class(hwnd):
    """Window class name ('' for no window); desktop / taskbar are in _SHELL_CLASSES."""
    cls = ctypes.create_unicode_buffer(64)
    if hwnd:
        user32.GetClassNameW(hwnd, cls, 64)
    return cls.value


def own_window_at_cursor():
    """Root window under the mouse if it belongs to this app (widget, menu, toast), else None."""
    pt, pid = wintypes.POINT(), wintypes.DWORD()
    user32.GetCursorPos(ctypes.byref(pt))
    h = user32.WindowFromPoint(pt)
    if not h:
        return None
    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    return user32.GetAncestor(h, 2) if pid.value == os.getpid() else None


def monitor_info(hwnd):
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi))  # NEAREST
    return mi


def fullscreen_info(own=()):
    """(monitor rect or None, reason). Counts as fullscreen:
    - if Windows says 'a fullscreen app is running' (QUNS_BUSY=2, D3D fullscreen=3, presentation=4),
    - or the foreground window covers at least 97% of the monitor (including borderless
      games that open 1 px smaller)."""
    try:
        state = ctypes.c_int(0)
        ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
        hwnd = user32.GetForegroundWindow()
        cls = window_class(hwnd)
        desc = f"on={cls or '-'} durum={state.value}"
        if not hwnd or hwnd in own or cls in _SHELL_CLASSES:
            # we / the desktop are in front; if Windows still says fullscreen use the primary monitor
            if state.value in (3, 4):
                return monitor_info(user32.GetDesktopWindow()).rcMonitor, desc + " (sistem)"
            return None, desc
        m = monitor_info(hwnd).rcMonitor
        if state.value in (2, 3, 4):
            return m, desc
        rc = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rc))
        ix = max(0, min(rc.right, m.right) - max(rc.left, m.left))
        iy = max(0, min(rc.bottom, m.bottom) - max(rc.top, m.top))
        area = (m.right - m.left) * (m.bottom - m.top)
        desc += f" kaplama=%{round(100 * ix * iy / max(1, area))}"
        if ix * iy >= 0.97 * area:
            return m, desc
        return None, desc
    except Exception as e:  # noqa: BLE001
        return None, f"hata {e!r}"


def covered_by_foreground(rect, own=()):
    """Is rect (screen px) fully behind the foreground window? Catches the common case (a big app
    window over the widget); a partial overlap counts as visible."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd or hwnd in own or user32.IsIconic(hwnd) or window_class(hwnd) in _SHELL_CLASSES:
        return False
    rc = wintypes.RECT()  # visible frame, without the invisible resize border
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rc), ctypes.sizeof(rc)):
        return False  # DWMWA_EXTENDED_FRAME_BOUNDS failed
    x0, y0, x1, y1 = rect
    return rc.left <= x0 and rc.top <= y0 and rc.right >= x1 and rc.bottom >= y1


def toast_xy(area, w, h, corner, m):
    """Top-left of a w x h toast in a corner ('br', 'tr', 'bl', 'tl') of area (RECT), m from the edges."""
    x = area.left + m if corner[1] == "l" else area.right - w - m
    y = area.top + m if corner[0] == "t" else area.bottom - h - m
    return x, y


def fullscreen_app(own=()):
    """Monitor rect if the foreground window is fullscreen (game, video, presentation); else None."""
    return fullscreen_info(own)[0]


def _spotify_exe():
    """Installed Spotify exe (from the protocol registration or the default location)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"spotify\shell\open\command") as k:
            cmd = winreg.QueryValueEx(k, "")[0]
        exe = cmd.split('"')[1] if cmd.startswith('"') else cmd.split(" ")[0]
        if exe.lower().endswith(".exe") and os.path.exists(exe):
            return exe
    except (OSError, IndexError):
        pass
    exe = os.path.join(os.environ.get("APPDATA", ""), "Spotify", "Spotify.exe")
    return exe if os.path.exists(exe) else None


def _spotify_window():
    """Spotify's visible main window (None if there is none)."""
    found = []
    try:
        import psutil
        pids = {p.pid for p in psutil.process_iter(["name"])
                if (p.info["name"] or "").lower() == "spotify.exe"}
    except Exception:  # noqa: BLE001
        return None
    if not pids:
        return None
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _l):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(h) and not user32.GetWindow(h, 4):
            t = ctypes.create_unicode_buffer(8)
            user32.GetWindowTextW(h, t, 8)
            if t.value:
                found.append(h)
                return False
        return True

    user32.EnumWindows(CB(cb), 0)
    return found[0] if found else None


def open_spotify(uri=None, web_url=None):
    """Bring Spotify to front (open that page if uri is given: artist, album, search, track).
    Restores its window if open; if closed / in the tray, launches Spotify.exe directly.
    (os.startfile('spotify:…') doesn't start Spotify when closed and doesn't raise the window.)"""
    h = _spotify_window()
    if h:
        if user32.IsIconic(h):
            user32.ShowWindow(h, 9)  # SW_RESTORE
        # tap Alt briefly to get past the focus lock (Windows accepts this as the recommended way)
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(h)
        if not uri:
            return
    exe = _spotify_exe()
    try:
        if exe:
            import subprocess
            # forwards to the running window if any, otherwise starts it
            args = [exe] + ([f"--protocol-uri={uri}"] if uri else [])
            subprocess.Popen(args, close_fds=True)
        else:  # Microsoft Store version
            os.startfile(uri or "shell:AppsFolder\\SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify")
    except OSError:
        webbrowser.open(web_url or "https://open.spotify.com")


def set_window_styles(hwnd):
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    ex = (ex | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex)


def pin_to_desktop(hwnd):
    # Windows owned by Progman (the desktop) aren't hidden by Win+D and stay at the bottom
    progman = user32.FindWindowW("Progman", None)
    user32.SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, progman or 0)
    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    user32.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def move_window(hwnd, x, y):
    user32.SetWindowPos(hwnd, 0, int(x), int(y), 0, 0,
                        SWP_NOSIZE | SWP_NOACTIVATE | 0x0004)  # SWP_NOZORDER


def keep_bottom(hwnd):
    user32.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def pin_on_top(hwnd):
    user32.SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, 0)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


class AppVolume:
    """Spotify's volume in the Windows volume mixer (0..1)."""

    def __init__(self, exe="spotify.exe"):
        self.exe = exe
        self._cache = (0.0, [])
        self._all = (0.0, [])   # (time, [(process name, peak meter)])

    def _sessions(self):
        t, ss = self._cache
        if ss and time.monotonic() - t < 3:
            return ss
        try:
            from pycaw.pycaw import AudioUtilities
            ss = [s.SimpleAudioVolume for s in AudioUtilities.GetAllSessions()
                  if s.Process and s.Process.name().lower() == self.exe]
        except Exception as e:  # noqa: BLE001
            log("could not read audio sessions:", repr(e))
            ss = []
        self._cache = (time.monotonic(), ss)
        return ss

    def _meters(self):
        t, ms = self._all
        if time.monotonic() - t < 8:  # session list is expensive (every process name is read)
            return ms
        ms = []
        try:
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
            for s in AudioUtilities.GetAllSessions():
                if s.Process:
                    try:
                        ms.append((s.Process.name().lower(),
                                   s._ctl.QueryInterface(IAudioMeterInformation)))
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as e:  # noqa: BLE001
            log("could not read peak meters:", repr(e))
        self._all = (time.monotonic(), ms)
        return ms

    def peak(self, names):
        """Current highest output level of the given processes (0..1)."""
        best = 0.0
        for name, m in self._meters():
            if name in names:
                try:
                    best = max(best, float(m.GetPeakValue()))
                except Exception:  # noqa: BLE001
                    self._all = (0.0, [])
        return best

    def get(self):
        for _ in range(2):
            ss = self._sessions()
            if not ss:
                return None
            try:
                return float(ss[0].GetMasterVolume())
            except Exception:  # noqa: BLE001
                self._cache = (0.0, [])
        return None

    def set(self, v):
        v = max(0.0, min(1.0, v))
        for s in self._sessions():
            try:
                s.SetMasterVolume(v, None)
            except Exception:  # noqa: BLE001
                self._cache = (0.0, [])
        return v


MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
HOTKEYS = (  # (id, modifier, key, action, key name, string key)
    (1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x50, "play", "Ctrl+Alt+P", "hk_play"),
    (2, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x27, "next", "Ctrl+Alt+→", "hk_next"),
    (3, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x25, "prev", "Ctrl+Alt+←", "hk_prev"),
    (4, MOD_CONTROL | MOD_ALT, 0x26, "vol+", "Ctrl+Alt+↑", "hk_volup"),
    (5, MOD_CONTROL | MOD_ALT, 0x28, "vol-", "Ctrl+Alt+↓", "hk_voldown"),
    (6, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x53, "search", "Ctrl+Alt+S", "hk_search"),
    (7, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x4C, "like", "Ctrl+Alt+L", "hk_like"),
)
IC_HEART, IC_HEART_O = "", ""


class Hotkeys:
    """Global hotkeys: RegisterHotKey + message loop on a separate thread."""

    def __init__(self, out_queue):
        self.q = out_queue
        self.tid = None
        self.failed = []
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            self.thread.join(1.0)  # wait for the previous stop request to finish
            if self.thread.is_alive():
                return
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        if self.tid:
            ctypes.windll.user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)
        self.tid = None

    def _run(self):
        u = ctypes.windll.user32
        self.tid = ctypes.windll.kernel32.GetCurrentThreadId()
        actions, self.failed = {}, []
        for hid, mods, vk, action, keys, _k in HOTKEYS:
            if u.RegisterHotKey(None, hid, mods, vk):
                actions[hid] = action
            else:
                self.failed.append(keys)
        if self.failed:
            log("could not register hotkey (used by another app):", ", ".join(self.failed))
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in actions:
                self.q.append(actions[msg.wParam])
        for hid in actions:
            u.UnregisterHotKey(None, hid)


def system_prefers_light():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 1
    except Exception:
        return False


# ------------------------------------------------------------------ colors
DEFAULT_SEED = (0.39, 0.70)  # Spotify green
# fixed color choices: (name, (hue, saturation))
ACCENTS = [("green", (0.39, 0.70)), ("blue", (0.60, 0.65)), ("purple", (0.75, 0.55)),
           ("pink", (0.92, 0.60)), ("red", (0.0, 0.65)), ("orange", (0.07, 0.80)),
           ("teal", (0.48, 0.60)), ("mono", (0.0, 0.0))]


def seed_from_image(img):
    small = img.convert("RGB").resize((48, 48))
    q = small.quantize(colors=10, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()
    best, best_score = None, -1.0
    for count, idx in q.getcolors() or []:
        r, g, b = (pal[idx * 3 + i] / 255 for i in range(3))
        h, l, s = colorsys.rgb_to_hls(r, g, b)
        if l < 0.06 or l > 0.96:
            continue
        score = count * (s + 0.05) * (1.15 - abs(l - 0.5))
        if score > best_score:
            best, best_score = (h, s), score
    return best or (0.0, 0.0)


def build_scheme(seed, light):
    h, s = seed
    sp = min(0.85, max(0.40, s)) if s >= 0.15 else s * 0.6
    sn = sp * 0.28  # low saturation for neutral surfaces

    def T(tone, sat):
        return tuple(int(round(c * 255)) for c in colorsys.hls_to_rgb(h, tone / 100, sat))

    if light:
        return {
            "primary": T(40, sp), "on_primary": (255, 255, 255),
            "primary_container": T(88, sp), "on_primary_container": T(15, sp),
            "secondary_container": T(88, sp * 0.35), "on_secondary_container": T(16, sp * 0.35),
            "surface": T(95, sn), "surface_high": T(91, sn), "surface_highest": T(87, sn),
            "on_surface": T(10, sn), "on_surface_variant": T(32, sn), "outline": T(55, sn),
            "border": T(85, sn),
        }
    return {
        "primary": T(80, sp), "on_primary": T(18, sp),
        "primary_container": T(30, sp * 0.8), "on_primary_container": T(90, sp),
        "secondary_container": T(26, sp * 0.35), "on_secondary_container": T(90, sp * 0.35),
        "surface": T(11, sn), "surface_high": T(16, sn), "surface_highest": T(23, sn),
        "on_surface": T(92, sn), "on_surface_variant": T(76, sn), "outline": T(55, sn),
        "border": T(24, sn),
    }


def hexc(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def oklch(L, C, h):
    """CSS oklch() → sRGB (0..255)."""
    a, b = C * math.cos(math.radians(h)), C * math.sin(math.radians(h))
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    lin = (4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
           -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
           -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_)

    def enc(c):
        c = max(0.0, min(1.0, c))
        return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return tuple(round(enc(c) * 255) for c in lin)


# shadcn/ui v4 "zinc" theme — github.com/shadcn-ui/ui, apps/v4/registry/themes.ts
SHADCN_ZINC = {
    "light": {"background": (1, 0, 0), "foreground": (0.141, 0.005, 285.823),
              "card": (1, 0, 0), "primary": (0.21, 0.006, 285.885),
              "primary-foreground": (0.985, 0, 0), "secondary": (0.967, 0.001, 286.375),
              "secondary-foreground": (0.21, 0.006, 285.885),
              "muted-foreground": (0.552, 0.016, 285.938), "accent": (0.967, 0.001, 286.375),
              "accent-foreground": (0.21, 0.006, 285.885), "border": (0.92, 0.004, 286.32),
              "ring": (0.705, 0.015, 286.067), "strong": (0.92, 0.004, 286.32)},
    "dark": {"background": (0.141, 0.005, 285.823), "foreground": (0.985, 0, 0),
             "card": (0.21, 0.006, 285.885), "primary": (0.92, 0.004, 286.32),
             "primary-foreground": (0.21, 0.006, 285.885), "secondary": (0.274, 0.006, 286.033),
             "secondary-foreground": (0.985, 0, 0),
             "muted-foreground": (0.705, 0.015, 286.067), "accent": (0.274, 0.006, 286.033),
             "accent-foreground": (0.985, 0, 0), "ring": (0.552, 0.016, 285.938),
             "strong": (0.37, 0.013, 285.805)},   # chart-4: track / empty bar tone
}
SHADCN_RADIUS = 10  # --radius: 0.625rem
# shadcn/ui v4 colour themes (same themes.ts): name: (light primary, light primary-foreground,
# dark primary, dark primary-foreground), OKLCH. "From cover" picks the one nearest to the cover.
SHADCN_THEMES = {
    "amber": ((0.555, 0.163, 48.998), (0.987, 0.022, 95.277), (0.473, 0.137, 46.201), (0.987, 0.022, 95.277)),
    "blue": ((0.488, 0.243, 264.376), (0.97, 0.014, 254.604), (0.424, 0.199, 265.638), (0.97, 0.014, 254.604)),
    "cyan": ((0.52, 0.105, 223.128), (0.984, 0.019, 200.873), (0.45, 0.085, 224.283), (0.984, 0.019, 200.873)),
    "emerald": ((0.508, 0.118, 165.612), (0.979, 0.021, 166.113), (0.432, 0.095, 166.913), (0.979, 0.021, 166.113)),
    "fuchsia": ((0.518, 0.253, 323.949), (0.977, 0.017, 320.058), (0.452, 0.211, 324.591), (0.977, 0.017, 320.058)),
    "green": ((0.527, 0.154, 150.069), (0.982, 0.018, 155.826), (0.448, 0.119, 151.328), (0.982, 0.018, 155.826)),
    "indigo": ((0.457, 0.24, 277.023), (0.962, 0.018, 272.314), (0.398, 0.195, 277.366), (0.962, 0.018, 272.314)),
    "lime": ((0.841, 0.238, 128.85), (0.405, 0.101, 131.063), (0.768, 0.233, 130.85), (0.405, 0.101, 131.063)),
    "orange": ((0.553, 0.195, 38.402), (0.98, 0.016, 73.684), (0.47, 0.157, 37.304), (0.98, 0.016, 73.684)),
    "pink": ((0.525, 0.223, 3.958), (0.971, 0.014, 343.198), (0.459, 0.187, 3.815), (0.971, 0.014, 343.198)),
    "purple": ((0.496, 0.265, 301.924), (0.977, 0.014, 308.299), (0.438, 0.218, 303.724), (0.977, 0.014, 308.299)),
    "red": ((0.505, 0.213, 27.518), (0.971, 0.013, 17.38), (0.444, 0.177, 26.899), (0.971, 0.013, 17.38)),
    "rose": ((0.514, 0.222, 16.935), (0.969, 0.015, 12.422), (0.455, 0.188, 13.697), (0.969, 0.015, 12.422)),
    "sky": ((0.5, 0.134, 242.749), (0.977, 0.013, 236.62), (0.443, 0.11, 240.79), (0.977, 0.013, 236.62)),
    "teal": ((0.511, 0.096, 186.391), (0.984, 0.014, 180.72), (0.437, 0.078, 188.216), (0.984, 0.014, 180.72)),
    "violet": ((0.491, 0.27, 292.581), (0.969, 0.016, 293.756), (0.432, 0.232, 292.759), (0.969, 0.016, 293.756)),
    "yellow": ((0.852, 0.199, 91.936), (0.421, 0.095, 57.708), (0.795, 0.184, 86.047), (0.421, 0.095, 57.708)),
}


def pick_shadcn(seed):
    """shadcn colour theme for a cover colour (hue, saturation); None (plain zinc) for grey covers."""
    h, sat = seed
    if sat < 0.15:
        return None

    def dist(n):
        hh = colorsys.rgb_to_hls(*(c / 255 for c in oklch(*SHADCN_THEMES[n][0])))[0]
        return min(abs(hh - h), 1 - abs(hh - h))
    return min(SHADCN_THEMES, key=dist)


def build_shadcn(light, accent=None, theme=None):
    """shadcn/ui 'zinc' theme (neutral). theme: a SHADCN_THEMES colour theme; else if accent
    (hue, saturation) is given it becomes the primary color, otherwise black/white."""
    t = {k: oklch(*v) for k, v in SHADCN_ZINC["light" if light else "dark"].items()}
    if not light:  # dark theme border: white 10% (on top of the card background)
        t["border"] = mix(t["card"], (255, 255, 255), 0.10)
    p = {"surface": t["card"], "surface_high": t["secondary"], "surface_highest": t["strong"],
         "on_surface": t["foreground"], "on_surface_variant": t["muted-foreground"],
         "outline": t["ring"], "primary": t["primary"], "on_primary": t["primary-foreground"],
         "border": t["border"], "primary_container": t["accent"],
         "on_primary_container": t["accent-foreground"], "secondary_container": t["secondary"],
         "on_secondary_container": t["secondary-foreground"]}
    if accent is not None and accent[1] > 0.05:  # colored theme (e.g. shadcn 'blue', 'rose')
        h, s = accent
        p["primary"] = tuple(round(c * 255) for c in colorsys.hls_to_rgb(
            h, 0.62 if not light else 0.48, min(0.85, max(0.55, s))))
        p["on_primary"] = (255, 255, 255) if light else hexc("#09090b")
    if theme in SHADCN_THEMES:
        pl, fl, pd, fd = SHADCN_THEMES[theme]
        p["primary"], p["on_primary"] = oklch(*(pl if light else pd)), oklch(*(fl if light else fd))
    return p


# Neumorphic (soft UI): the themes of RYOCS (kisisel_panel ui.html TEMALAR, without High contrast):
# name: (dark, [background, light shadow, dark shadow, text, muted text, line, accent], glass bg).
# The theme whose accent hue is nearest to the album cover is used; grey covers get the base theme
# (dark: Graphite).
NEU_THEMES = {
    "": (False, ["#e9eaec", "#ffffff", "#c5c7cc", "#4a4d55", "#65686f", "#dcdde1", "#8fb996"], "#e9ecf1"),
    "kum": (False, ["#ece6dc", "#fffaf2", "#cfc6b6", "#4d463c", "#6d6355", "#ddd5c7", "#c98b55"], "#ebe4d8"),
    "buz": (False, ["#e4ebf2", "#fbfdff", "#c3ccd8", "#3f4a57", "#5d6978", "#d3dce6", "#5f95c9"], "#e3eaf3"),
    "adacayi": (False, ["#e3e9e2", "#fafdf9", "#c3cdc2", "#3f4a40", "#5d695d", "#d3dbd2", "#5e9468"], "#e2e9e1"),
    "lavanta": (False, ["#e8e5ef", "#fdfcff", "#c9c4d6", "#49445a", "#686279", "#dad6e4", "#9a85c9"], "#e7e3ef"),
    "gul": (False, ["#efe4e4", "#fffafa", "#d2c3c3", "#574547", "#736062", "#e1d3d3", "#c27b88"], "#eee2e3"),
    "nane": (False, ["#e2eeea", "#fafffd", "#c1d2cc", "#38494a", "#576a68", "#d0e0da", "#3f9d8a"], "#e1ede9"),
    "seftali": (False, ["#f1e6df", "#fffaf6", "#d6c5ba", "#54453c", "#725f53", "#e4d5cb", "#e08e6d"], "#f0e4dc"),
    "dark": (True, ["#2a2d32", "#363a41", "#1c1e22", "#d9dbe0", "#959aa3", "#3a3e45", "#8fb996"], None),
    "gece": (True, ["#252b38", "#2f3646", "#1a1f29", "#d8deea", "#9aa4b8", "#343c4d", "#7fb0e6"], None),
    "orman": (True, ["#232a26", "#2c3530", "#19201c", "#d6e0d9", "#96a59c", "#313b35", "#8fb996"], None),
    "kahve": (True, ["#2d2723", "#38302b", "#211c19", "#e6dcd3", "#ab9c8f", "#3d342e", "#d08c5e"], None),
    "grafit": (True, ["#1c1d20", "#25272b", "#121315", "#e4e5e8", "#9a9da5", "#2a2c31", "#d6d8dd"], None),
    "okyanus": (True, ["#1f2b31", "#29373e", "#151e22", "#d5e3e8", "#93a9b1", "#2c3a41", "#5cc0c7"], None),
    "erik": (True, ["#2b2330", "#362c3c", "#1e1822", "#e6dbe9", "#ab9cb1", "#3a3040", "#c58fd6"], None),
}


def pick_neu(seed, light):
    """RYOCS theme for a cover colour (hue, saturation)."""
    h, sat = seed
    if sat < 0.15:
        return "" if light else "grafit"

    def dist(n):
        ye = NEU_THEMES[n][1][6]
        hh = colorsys.rgb_to_hls(*(int(ye[i:i + 2], 16) / 255 for i in (1, 3, 5)))[0]
        return min(abs(hh - h), 1 - abs(hh - h))
    # "dark" has the same accent as "orman", so it would hide Forest; grey dark covers get Graphite
    return min((n for n, t in NEU_THEMES.items() if t[0] != light and n not in ("grafit", "dark")),
               key=dist)


def build_neu(light, seed=DEFAULT_SEED):
    _d, (bg, hi, lo, tx, mu, ln, ye), cz = NEU_THEMES[pick_neu(seed, light)]
    p = {k: hexc(v) for k, v in dict(
        surface=bg, surface_high=bg, surface_highest=ln, secondary_container=bg,
        on_secondary_container=tx, primary_container=ye, primary=ye, on_surface=tx,
        on_surface_variant=mu, outline=mu, border=ln, hi=hi, lo=lo, cz=cz or "#e9ecf1").items()}
    # text on the accent: white if it reaches 4.5:1, else the accent darkened (RYOCS --yeYazi)
    L = _lum(p["primary"])
    p["on_primary"] = p["on_primary_container"] = (255, 255, 255) if 1.05 / (L + 0.05) >= 4.5 \
        else mix(p["primary"], (0, 0, 0), 0.72 if L > 0.35 else 0.85)
    return p


def _glass_crop(small, mon, x, y, w, h):
    """Piece of the quarter-size blurred wallpaper behind screen rect (x, y, w, h), full size."""
    rx, ry = (x - mon[0]) / 4, (y - mon[1]) / 4
    return small.crop((round(rx), round(ry), round(rx + w / 4) + 1, round(ry + h / 4) + 1)).resize(
        (w + 4, h + 4), Image.Resampling.BILINEAR).crop((0, 0, w, h))


def mix(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _lum(c):
    """WCAG relative luminance of an RGB colour."""
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in (v / 255 for v in c)]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def readable(fg, bg, toward, need=4.5):
    """fg moved towards `toward` (the text colour) until small text has `need`:1 contrast on bg."""
    lb = _lum(bg)
    for i in range(11):
        c = mix(fg, toward, i / 10)
        lc = _lum(c)
        if (max(lc, lb) + 0.05) / (min(lc, lb) + 0.05) >= need:
            return c
    return tuple(toward)


def mix_scheme(a, b, t):
    return {k: mix(a[k], b[k], t) for k in a if k in b}   # neu has extra hi / lo keys


# ------------------------------------------------------------------ drawing
_mask_cache = {}


def _rr_mask(w, h, fx0, fy0, fx1, fy1, r, ellipse=False):
    key = (w, h, round(fx0, 2), round(fy0, 2), round(fx1, 2), round(fy1, 2), round(r, 2), ellipse)
    m = _mask_cache.get(key)
    if m is None:
        SS = 4
        big = Image.new("L", (w * SS, h * SS), 0)
        d = ImageDraw.Draw(big)
        box = (fx0 * SS, fy0 * SS, fx1 * SS - 1, fy1 * SS - 1)
        if ellipse:
            d.ellipse(box, fill=255)
        else:
            d.rounded_rectangle(box, radius=r * SS, fill=255)
        m = big.resize((w, h), Image.Resampling.BOX)
        if len(_mask_cache) > 400:
            _mask_cache.clear()
        _mask_cache[key] = m
    return m


# Design language: "material" (Material You: big radii, tonal fills),
# "shadcn" (shadcn/ui: neutral colors, thin borders, small corners) or "neu". In shadcn rr()
# caps the corners and turns button-sized circles into rounded squares.
STYLE = {"shad": False, "neu": False, "r": 0.0, "btn": 0.0,
         # neu: current theme shadow colours, RYOCS glass look (light + glass bg), translucent bg
         "hi": (255, 255, 255), "lo": (197, 199, 204), "glass": False, "clear": False,
         "surf": (233, 234, 236), "see": False}


def set_style(name, S):
    STYLE["shad"] = name == "shadcn"
    STYLE["neu"] = name == "neu"
    STYLE["r"] = 10.0 * S     # largest corner radius: shadcn --radius 0.625rem
    STYLE["btn"] = 30.0 * S   # circles bigger than this are buttons: make them square


def rr(img, x0, y0, x1, y1, r, color, alpha=255, ellipse=False, plain=False):
    """Anti-aliased rounded rectangle (physical pixels)."""
    if x1 - x0 < 0.5 or y1 - y0 < 0.5:
        return
    if STYLE["shad"]:
        if ellipse and (x1 - x0) >= STYLE["btn"] and abs((x1 - x0) - (y1 - y0)) < 1:
            ellipse, r = False, STYLE["r"] * 0.8   # buttons: rounded-md (radius - 2px)
        elif not ellipse:
            r = min(r, STYLE["r"])
    if (STYLE["neu"] and not plain and ellipse and alpha == 255
            and (x1 - x0) >= STYLE["btn"] * 1.2 and abs((x1 - x0) - (y1 - y0)) < 1):
        neu_btn(img, x0, y0, x1, y1, 0, color, ellipse=True)   # round buttons: raised
        return
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    ix0, iy0 = max(0, int(math.floor(x0))), max(0, int(math.floor(y0)))
    ix1 = min(img.width, int(math.ceil(x1)))
    iy1 = min(img.height, int(math.ceil(y1)))
    w, h = ix1 - ix0, iy1 - iy0
    if w <= 0 or h <= 0:
        return
    m = _rr_mask(w, h, x0 - ix0, y0 - iy0, x1 - ix0, y1 - iy0, r, ellipse)
    if alpha < 255:
        m = m.point(lambda v: v * alpha // 255)
    layer = Image.new("RGBA", (w, h), tuple(color) + (255,))
    layer.putalpha(m)
    img.alpha_composite(layer, (ix0, iy0))


_neu_cache = {}


def _neu_shadows(inset):
    """(dx, dy, blur sigma, colour, alpha) like RYOCS --up-s / --in, or their glass versions."""
    S = STYLE["btn"] / 30.0
    if STYLE["glass"]:
        return [(0, 2 * S, 3 * S, (50, 62, 90), 33)] if inset else \
            [(0, 4 * S, 5 * S, (50, 62, 90), 66)]
    d, b = (3 * S, 3.5 * S) if inset else (4 * S, 4.5 * S)   # CSS blur radius / 2 = sigma
    return [(d, d, b, STYLE["lo"], 255), (-d, -d, b, STYLE["hi"], 255)]


def neu_raise(img, x0, y0, x1, y1, r, ellipse=False):
    """Neumorphic soft shadow: light at top left, dark at bottom right. The shadow only lands on
    pixels already filled (opaque) in img; it never spills outside the card / into transparency."""
    from PIL import ImageChops, ImageFilter
    w, h = max(1, round(x1 - x0)), max(1, round(y1 - y0))
    for dx, dy, blur, col, a in _neu_shadows(False):
        pad = int(max(abs(dx), abs(dy)) + blur * 3) + 1
        key = (w, h, round(r), ellipse, round(blur, 1), pad)
        m = _neu_cache.get(key)
        if m is None:
            m = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
            m.paste(_rr_mask(w, h, 0, 0, w, h, max(0.0, min(r, w / 2, h / 2)), ellipse), (pad, pad))
            m = m.filter(ImageFilter.GaussianBlur(blur))
            if len(_neu_cache) > 60:
                _neu_cache.clear()
            _neu_cache[key] = m
        px, py = round(x0 - pad + dx), round(y0 - pad + dy)
        bx0, by0 = max(0, px), max(0, py)
        bx1, by1 = min(img.width, px + m.width), min(img.height, py + m.height)
        if bx1 <= bx0 or by1 <= by0:
            continue
        sm = m.crop((bx0 - px, by0 - py, bx1 - px, by1 - py)).point(lambda v, a=a: v * a // 255)
        sm = ImageChops.multiply(sm, img.getchannel("A").crop((bx0, by0, bx1, by1)))
        layer = Image.new("RGBA", sm.size, tuple(col) + (255,))
        layer.putalpha(sm)
        img.alpha_composite(layer, (bx0, by0))


def _box(x0, y0, x1, y1, r, ellipse):
    """Integer box and anti-aliased shape mask of a rounded rect / circle."""
    ix0, iy0 = int(math.floor(x0)), int(math.floor(y0))
    w, h = int(math.ceil(x1)) - ix0, int(math.ceil(y1)) - iy0
    rad = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    m = _rr_mask(w, h, x0 - ix0, y0 - iy0, x1 - ix0, y1 - iy0, rad, ellipse) \
        if w > 0 and h > 0 else None
    return ix0, iy0, w, h, rad, m


def neu_glass(img, x0, y0, x1, y1, r, ellipse=False):
    """RYOCS glass sheen (--cam): white top-to-bottom gloss and a 1.5 px white inner edge."""
    from PIL import ImageChops
    ix0, iy0, w, h, rad, m = _box(x0, y0, x1, y1, r, ellipse)
    if m is None:
        return
    col = Image.new("L", (1, h))
    col.putdata([round(255 * (0.42 - 0.30 * t / 0.6 if t < 0.6 else 0.12 + 0.10 * (t - 0.6) / 0.4))
                 for t in (i / max(1, h - 1) for i in range(h))])
    sheen = ImageChops.multiply(col.resize((w, h)), m)
    b = max(1.0, 1.5 * STYLE["btn"] / 30.0)
    inner = _rr_mask(w, h, x0 - ix0 + b, y0 - iy0 + b, x1 - ix0 - b, y1 - iy0 - b,
                     max(0.0, rad - b), ellipse)
    sheen = ImageChops.lighter(sheen, ImageChops.subtract(m, inner).point(lambda v: v * 190 // 255))
    layer = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    layer.putalpha(sheen)
    img.alpha_composite(layer, (ix0, iy0))


def neu_btn(img, x0, y0, x1, y1, r, color, ellipse=False, pressed=False):
    """Soft UI button: card-colored body, raised by two soft shadows; no border.
    pressed: sunk in like RYOCS :active (--in). On a glass / translucent card the body shows the
    card through (RYOCS glass: transparent body + sheen) instead of an opaque disc."""
    # accent-filled buttons (e.g. shuffle on) stay solid; card-coloured ones go see-through
    see = (STYLE["glass"] or STYLE["clear"]) and max(abs(a - b) for a, b in zip(color, STYLE["surf"])) < 40
    ix0, iy0, w, h, _rad, m = _box(x0, y0, x1, y1, r, ellipse)
    under = img.crop((ix0, iy0, ix0 + w, iy0 + h)) if see and m is not None else None
    if not pressed:
        neu_raise(img, x0, y0, x1, y1, r, ellipse)
    if under is None:
        rr(img, x0, y0, x1, y1, r, color, ellipse=ellipse, plain=True)
    else:
        img.paste(under, (ix0, iy0), m)           # card back under the body, without shadow
        rr(img, x0, y0, x1, y1, r, color, alpha=60, ellipse=ellipse, plain=True)
        if STYLE["glass"]:
            neu_glass(img, x0, y0, x1, y1, r, ellipse)
    if pressed:
        neu_inset(img, x0, y0, x1, y1, r, ellipse)


def neu_inset(img, x0, y0, x1, y1, r, ellipse=False):
    from PIL import ImageChops, ImageFilter
    ix0, iy0, w, h, _rad, m = _box(x0, y0, x1, y1, r, ellipse)
    if m is None:
        return
    for dx, dy, blur, col, a in _neu_shadows(True):   # lo on the top left inner edge, hi bottom right
        pad = int(max(abs(dx), abs(dy)) + blur * 3) + 1
        key = ("in", w, h, round(r), ellipse, round(blur, 1), pad, round(x0 % 1, 2), round(y0 % 1, 2),
               round(x1 % 1, 2), round(y1 % 1, 2))
        out = _neu_cache.get(key)
        if out is None:
            out = Image.new("L", (w + 2 * pad, h + 2 * pad), 255)
            out.paste(ImageChops.invert(m), (pad, pad))
            out = out.filter(ImageFilter.GaussianBlur(blur))
            if len(_neu_cache) > 60:
                _neu_cache.clear()
            _neu_cache[key] = out
        cx, cy = round(pad - dx), round(pad - dy)
        sm = ImageChops.multiply(out.crop((cx, cy, cx + w, cy + h)), m)
        if a < 255:
            sm = sm.point(lambda v, a=a: v * a // 255)
        layer = Image.new("RGBA", (w, h), tuple(col) + (255,))
        layer.putalpha(sm)
        img.alpha_composite(layer, (ix0, iy0))


def rounded_image(src, size, radius):
    if STYLE["shad"]:
        radius = min(radius, STYLE["r"] * 0.8)
    img = src.convert("RGB")
    w, h = img.size
    m = min(w, h)
    img = img.crop(((w - m) // 2, (h - m) // 2, (w + m) // 2, (h + m) // 2))
    img = img.resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")
    img.putalpha(_rr_mask(size, size, 0, 0, size, size, radius))
    return img


# ---- multilingual text: fallback fonts for characters missing in the main font
# (regular file, bold file, tag)
FALLBACK_FONTS = [
    ("YuGothM.ttc", "YuGothB.ttc", "jp"),      # Japanese
    ("msgothic.ttc", "msgothic.ttc", "jp"),
    ("malgun.ttf", "malgunbd.ttf", "kr"),      # Korean
    ("msyh.ttc", "msyhbd.ttc", "sc"),          # Chinese (simplified)
    ("msjh.ttc", "msjhbd.ttc", "tc"),          # Chinese (traditional)
    ("simsun.ttc", "simsun.ttc", "sc"),
    ("LeelawUI.ttf", "LeelawUI.ttf", "th"),    # Thai etc.
    ("Nirmala.ttc", "Nirmala.ttc", "in"),      # Indic scripts
    ("seguisym.ttf", "seguisym.ttf", "sym"),   # symbols
    ("seguiemj.ttf", "seguiemj.ttf", "emoji"),  # emoji
]
_coverage = {}


def font_coverage(filename):
    """Unicode code points supported by the font."""
    cov = _coverage.get(filename)
    if cov is None:
        try:
            f = TTFont(os.path.join(FONT_DIR, filename), fontNumber=0, lazy=True)
            cov = frozenset(f.getBestCmap() or ())
            f.close()
        except Exception as e:  # noqa: BLE001
            log("could not read font:", filename, repr(e))
            cov = frozenset()
        _coverage[filename] = cov
    return cov


def _fallback_order(text):
    has_kana = any(0x3040 <= ord(c) <= 0x30FF or 0x31F0 <= ord(c) <= 0x31FF for c in text)
    has_hangul = any(0xAC00 <= ord(c) <= 0xD7AF or 0x1100 <= ord(c) <= 0x11FF
                     or 0x3130 <= ord(c) <= 0x318F for c in text)
    first = "jp" if has_kana else "kr" if has_hangul else "sc"
    idx = list(range(len(FALLBACK_FONTS)))
    idx.sort(key=lambda i: 0 if FALLBACK_FONTS[i][2] == first else 1)  # stable sort
    return idx


# bundled fonts (OFL-1.1): Geist (github.com/vercel/geist-font) for shadcn, Poppins
# (github.com/google/fonts ofl/poppins) for neu, at RYOCS's weights: text 300, emphasis 500
BUNDLED_FONTS = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))),
                             "fonts")
_GEIST = {"segoeui.ttf": "Geist-Regular.ttf", "seguisb.ttf": "Geist-SemiBold.ttf"}
_POPPINS = {"segoeui.ttf": "Poppins-Light.ttf", "seguisb.ttf": "Poppins-Medium.ttf"}


def ui_font(name):
    """Font file for the selected design language (shadcn/ui: Geist, neu: Poppins, Material: Segoe UI)."""
    m = _GEIST if STYLE["shad"] else _POPPINS if STYLE["neu"] else {}
    if name in m:
        p = os.path.join(BUNDLED_FONTS, m[name])
        if os.path.exists(p):
            return p
    return name


class TextFont:
    """Font that fills in missing characters from fallback fonts."""

    def __init__(self, filename, px, scale, bold=False):
        self.size = max(6, round(px * scale))
        self.bold = bold
        filename = ui_font(filename)  # Geist / Poppins instead of Segoe in shadcn / neu
        if not os.path.exists(os.path.join(FONT_DIR, filename)):
            filename = "segoeui.ttf"
        self.filename = filename
        self.primary = ImageFont.truetype(os.path.join(FONT_DIR, filename), self.size)
        self.asc, self.desc = self.primary.getmetrics()
        self._fb = {}
        self._runs = {}
        self._len = {}      # text -> width
        self._layers = {}   # (text, fill, sub-pixel x, y) -> rendered text layer

    def _fallback(self, i):
        f = self._fb.get(i)
        if f is None:
            reg, bold, _tag = FALLBACK_FONTS[i]
            name = bold if self.bold else reg
            try:
                f = ImageFont.truetype(os.path.join(FONT_DIR, name), self.size)
            except OSError:
                f = self.primary
            self._fb[i] = f
        return f

    def runs(self, text):
        r = self._runs.get(text)
        if r is not None:
            return r
        pcov = font_coverage(self.filename)
        order = None
        out = []  # [font index (-1 = main), text]
        for ch in text:
            cp = ord(ch)
            if cp in (0xFE0E, 0xFE0F, 0x200B, 0x200D):
                continue  # invisible joiners
            if out and unicodedata.combining(ch):
                out[-1][1] += ch
                continue
            which = -1
            if not (cp < 0x250 or cp in pcov):
                if order is None:
                    order = _fallback_order(text)
                cand = order
                if cp >= 0x1F000:  # emoji from the color font first
                    cand = sorted(order, key=lambda i: FALLBACK_FONTS[i][2] != "emoji")
                for i in cand:
                    reg, bold, _tag = FALLBACK_FONTS[i]
                    if cp in font_coverage(bold if self.bold else reg):
                        which = i
                        break
            if out and out[-1][0] == which:
                out[-1][1] += ch
            else:
                out.append([which, ch])
        r = [(self.primary if i < 0 else self._fallback(i), seg,
              i >= 0 and FALLBACK_FONTS[i][2] == "emoji") for i, seg in out]
        if len(self._runs) > 600:
            self._runs.clear()
        self._runs[text] = r
        return r

    def getlength(self, text):
        w = self._len.get(text)
        if w is None:
            if len(self._len) > 2000:
                self._len.clear()
            w = self._len[text] = sum(f.getlength(seg) for f, seg, _e in self.runs(text))
        return w

    def paste(self, img, xy, text, fill, anchor="la"):
        """Same as draw() on an RGBA image, from a cached layer: the text is rendered once per
        (text, colour, anchor) instead of every frame. The layer is filled with the text colour at
        alpha 0, so compositing it gives the same edges as drawing on the image."""
        fill = tuple(fill) if isinstance(fill, (tuple, list)) else fill
        ln = self.getlength(text)
        px = xy[0] - {"m": ln / 2, "r": ln}.get(anchor[0], 0)   # top left of the text box
        py = xy[1] - {"a": 0, "m": (self.asc + self.desc) / 2}.get(anchor[1], self.asc)
        ix, iy = math.floor(px), math.floor(py)
        fx, fy = round(px - ix, 1), round(py - iy, 1)  # keep the sub-pixel position, like draw()
        key = (text, fill, fx, fy)
        layer = self._layers.get(key)
        pad = self.size // 2 + 2   # room for accents / overhang outside the metrics
        if layer is None:
            rgb = fill[:3] if isinstance(fill, tuple) else (0, 0, 0)
            layer = Image.new("RGBA", (math.ceil(ln) + 2 * pad + 1,
                                       self.asc + self.desc + 2 * pad + 1), tuple(rgb) + (0,))
            self.draw(ImageDraw.Draw(layer), (pad + fx, pad + fy), text, fill, "la")
            if len(self._layers) > 150:
                self._layers.clear()
            self._layers[key] = layer
        img.alpha_composite(layer, (ix - pad, iy - pad))

    def draw(self, d, xy, text, fill, anchor="la"):
        x, y = xy
        if anchor[0] == "m":
            x -= self.getlength(text) / 2
        elif anchor[0] == "r":
            x -= self.getlength(text)
        if anchor[1] == "a":
            base = y + self.asc
        elif anchor[1] == "m":
            base = y + (self.asc - self.desc) / 2
        else:
            base = y
        for f, seg, emoji in self.runs(text):
            d.text((x, base), seg, font=f, fill=fill, anchor="ls", embedded_color=emoji)
            x += f.getlength(seg)


class Drawer:
    """ImageDraw wrapper: draws TextFont text with fallback fonts."""

    def __init__(self, img):
        self.img = img
        self.d = ImageDraw.Draw(img)

    def text(self, xy, text, font=None, fill=None, anchor="la"):
        if isinstance(font, TextFont) and self.img.mode == "RGBA" and fill is not None \
                and 0 <= xy[0] < self.img.width and 0 <= xy[1] < self.img.height:
            font.paste(self.img, xy, text, fill, anchor)
        elif isinstance(font, TextFont):
            font.draw(self.d, xy, text, fill, anchor)
        else:
            self.d.text(xy, text, font=font, fill=fill, anchor=anchor)


def _is_cjk_break(ch):
    cp = ord(ch)
    return (0x2E80 <= cp <= 0xA4CF or 0xF900 <= cp <= 0xFAFF or 0xFF00 <= cp <= 0xFFEF
            or cp >= 0x20000)


def fit_text(text, font, maxw):
    if font.getlength(text) <= maxw:
        return text
    while text and font.getlength(text + "…") > maxw:
        text = text[:-1]
    return text.rstrip() + "…"


def wrap_text(text, font, maxw, maxlines):
    # Tokens: space-separated words; in Japanese/Chinese any character may break
    tokens = []  # (text, preceded by a space)
    for wi, word in enumerate(text.split()):
        buf, first = "", True
        for ch in word:
            if _is_cjk_break(ch):
                if buf:
                    tokens.append((buf, first and wi > 0))
                    buf, first = "", False
                tokens.append((ch, first and wi > 0))
                first = False
            else:
                buf += ch
        if buf:
            tokens.append((buf, first and wi > 0))
    if not tokens:
        return [""]
    lines, cur = [], ""
    for i, (tok, sp) in enumerate(tokens):
        t = cur + (" " if sp and cur else "") + tok
        if not cur or font.getlength(t) <= maxw:
            cur = t
        else:
            lines.append(cur)
            cur = tok
            if len(lines) == maxlines - 1:
                cur = tok + "".join((" " if s else "") + k for k, s in tokens[i + 1:])
                break
    lines.append(cur)
    return [fit_text(l, font, maxw) for l in lines]


def fmt_time(sec):
    sec = max(0, int(sec))
    if sec >= 3600:
        return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
    return f"{sec // 60}:{sec % 60:02d}"


def to_seconds(v):
    if v is None:
        return 0.0
    if hasattr(v, "total_seconds"):
        return v.total_seconds()
    if hasattr(v, "duration"):
        return v.duration / 10_000_000
    return float(v) / 10_000_000


# ------------------------------------------------------------------ media (GSMTC)
class MediaWorker:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.lock = threading.Lock()
        self.state = {"ok": False}
        self.manager = None
        self.session = None
        self._last_track = None
        self._wake = None
        self.fast_until = 0.0   # poll state every 0.1 s until this time
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self._wake = asyncio.Event()
        self.loop.run_until_complete(self._poll_forever())

    def _pick_session(self):
        for s in self.manager.get_sessions():
            if "spotify" in (s.source_app_user_model_id or "").lower():
                return s
        return None

    async def _read_thumb(self, thumb):
        if thumb is None:
            return None
        stream = await thumb.open_read_async()
        size = int(stream.size)
        if size == 0:
            return None
        reader = DataReader(stream.get_input_stream_at(0))
        await reader.load_async(size)
        buf = bytearray(size)
        reader.read_bytes(buf)
        return bytes(buf)

    async def _poll_once(self):
        if self.manager is None:
            self.manager = await MediaManager.request_async()
        self.session = s = self._pick_session()
        if s is None:
            self._last_track = None
            with self.lock:
                self.state = {"ok": False}
            return
        props = await s.try_get_media_properties_async()
        info = s.get_playback_info()
        tl = s.get_timeline_properties()
        try:
            repeat = int(info.auto_repeat_mode) if info.auto_repeat_mode is not None else 0
        except Exception:
            repeat = 0
        new = {
            "ok": True,
            "title": props.title or "",
            "artist": props.artist or "",
            "album": props.album_title or "",
            "playing": info.playback_status == PlaybackStatus.PLAYING,
            "shuffle": bool(info.is_shuffle_active),
            "repeat": repeat,
            "pos": to_seconds(tl.position),
            "end": to_seconds(tl.end_time),
            "updated": tl.last_updated_time,
        }
        track = (new["title"], new["artist"], new["album"])
        if track != self._last_track:
            try:
                new["art"] = await self._read_thumb(props.thumbnail)
            except Exception:
                new["art"] = None
            new["art_changed"] = True
            self._last_track = track
        with self.lock:
            if not new.get("art_changed"):
                new["art"] = self.state.get("art")
                new["art_changed"] = self.state.get("art_changed", False)
            self.state = new

    async def _poll_forever(self):
        while True:
            try:
                await self._poll_once()
            except Exception as e:  # noqa: BLE001
                log("media error:", repr(e))
                with self.lock:
                    self.state = {"ok": False}
                self.manager = None
            # right after a command (skip etc.) poll often so the change shows at once
            fast = time.monotonic() < self.fast_until
            try:
                await asyncio.wait_for(self._wake.wait(), 0.1 if fast else 1.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def snapshot(self):
        with self.lock:
            st = dict(self.state)
            self.state.pop("art_changed", None)
        return st

    def command(self, name, arg=None):
        # extend by the number of skips: each skip takes ~0.3 s
        n = arg if name == "skip_n" and isinstance(arg, int) else 1
        self.fast_until = time.monotonic() + 2.5 + 0.3 * n

        async def go():
            s = self.session
            if s is None:
                return
            try:
                if name == "toggle":
                    await s.try_toggle_play_pause_async()
                elif name == "pause":
                    await s.try_pause_async()
                elif name == "play":
                    await s.try_play_async()
                elif name == "next":
                    await s.try_skip_next_async()
                elif name == "prev":
                    await s.try_skip_previous_async()
                elif name == "seek":
                    await s.try_change_playback_position_async(int(arg * 10_000_000))
                elif name == "shuffle":
                    await s.try_change_shuffle_active_async(bool(arg))
                elif name == "repeat":
                    await s.try_change_auto_repeat_mode_async(RepeatMode(arg))
                elif name == "skip_n":
                    for _ in range(arg):
                        await s.try_skip_next_async()
                        await asyncio.sleep(0.25)
            except Exception as e:  # noqa: BLE001
                log("command error:", name, repr(e))
            await asyncio.sleep(0.15)
            self._wake.set()

        asyncio.run_coroutine_threadsafe(go(), self.loop)


# ------------------------------------------------------------------ network: retry on transient errors
_TRANSIENT = (urllib.error.URLError, http.client.RemoteDisconnected, http.client.IncompleteRead,
              ConnectionError, TimeoutError, ssl.SSLError)


def is_transient(e):
    """Is this a brief connection drop (worth retrying)?"""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (500, 502, 503, 504)  # temporary server-side problem
    return isinstance(e, _TRANSIENT)


def http_read(req, timeout=15, tries=2, idempotent=True):
    """Send the request and return the body; on a dropped connection / timeout retry after a short
    wait. Requests that must not be sent twice (idempotent=False) are only retried if the server
    never received them (connection failed). HTTPError is raised as is."""
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            retry = is_transient(e)
            if retry and not idempotent:
                # only safe if the connection never opened (e.g. DNS error, connection refused)
                import socket
                reason = getattr(e, "reason", None)
                retry = not isinstance(e, urllib.error.HTTPError) and isinstance(
                    reason, (socket.gaierror, ConnectionRefusedError))
            if not retry or i == tries - 1:
                raise
            time.sleep(0.8 * (i + 1))


# ------------------------------------------------------------------ lyrics (LRCLIB)
class Lyrics:
    """Synced lyrics from lrclib.net. lines(): [(seconds or None, text)]."""

    def __init__(self):
        self.lock = threading.Lock()
        self.key = None
        self.state = None       # loading / ok / none / instr / err
        self.synced = False
        self._lines = []
        self.version = 0
        self.cache = {}

    def lines(self):
        with self.lock:
            return self._lines

    def request(self, title, artist, album, dur):
        key = (title, artist)
        if not title:
            return
        if key == self.key:
            # on error (e.g. offline) retry by itself after a while
            if self.state != "err" or time.monotonic() < getattr(self, "retry_at", 0):
                return
        self.key = key
        if key in self.cache:
            self._apply(key, *self.cache[key])
            return
        with self.lock:
            self.state, self._lines = "loading", []
            self.version += 1
        threading.Thread(target=self._fetch, args=(key, album, dur), daemon=True).start()

    def _apply(self, key, state, synced, lines):
        with self.lock:
            if key != self.key:
                return
            self.state, self.synced, self._lines = state, synced, lines
            self.version += 1

    @staticmethod
    def _get(path, params):
        url = "https://lrclib.net/api" + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        return json.loads(http_read(req, timeout=12).decode())

    @staticmethod
    def parse(text):
        out = []
        for raw in (text or "").splitlines():
            stamps, rest = [], raw
            while rest.startswith("[") and "]" in rest:
                tag, rest = rest[1:].split("]", 1)
                try:
                    mm, ss = tag.split(":", 1)
                    stamps.append(int(mm) * 60 + float(ss))
                except ValueError:
                    break
            for t in stamps:
                out.append((t, rest.strip()))
        out.sort(key=lambda x: x[0])
        return out

    def _fetch(self, key, album, dur):
        title, artist = key
        artist1 = artist.split(", ")[0]
        res = None
        try:
            try:
                p = {"track_name": title, "artist_name": artist1}
                if album:
                    p["album_name"] = album
                if dur:
                    p["duration"] = int(round(dur))
                res = self._get("/get", p)
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                found = self._get("/search", {"track_name": title, "artist_name": artist1})
                found = [f for f in found if f.get("syncedLyrics")] or found
                res = found[0] if found else None
        except Exception as e:  # noqa: BLE001
            if not is_transient(e):
                log("could not fetch lyrics:", repr(e))
            self.retry_at = time.monotonic() + 20  # don't cache: retry in 20 s
            self._apply(key, "err", False, [])
            return
        if not res:
            r = ("none", False, [])
        elif res.get("instrumental"):
            r = ("instr", False, [])
        elif res.get("syncedLyrics"):
            r = ("ok", True, self.parse(res["syncedLyrics"]))
        elif res.get("plainLyrics"):
            r = ("ok", False, [(None, ln.strip()) for ln in res["plainLyrics"].splitlines()])
        else:
            r = ("none", False, [])
        if len(self.cache) > 60:
            self.cache.clear()
        self.cache[key] = r
        self._apply(key, *r)


# ------------------------------------------------------------------ Spotify Web API (queue)
class SpotifyWeb:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.token_lock = threading.Lock()
        self.queue = None
        self.error = None
        self.version = 0
        self.access = None
        self.expires = 0.0
        self.track_key = None
        self.playing = False
        self.thumbs = {}
        self.current = None  # current track id, artist / album links
        self.liked = {}      # track id -> liked?
        self.liked_t = {}    # track id -> last check time
        self.auth_status = None
        self.auth_error = False
        self.net_down = False   # is a connection outage ongoing (avoids repeated log lines)
        self._again = 0.0       # second refresh time after a track change
        self.wake = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def connected(self):
        return bool(self.cfg.get("client_id") and self.cfg.get("refresh_token"))

    def notify(self, track_key, playing):
        self.playing = playing
        if track_key != self.track_key:
            self.track_key = track_key
            self.wake.set()

    def refresh_now(self):
        self.wake.set()

    def snapshot(self):
        with self.lock:
            return self.connected, self.queue, self.error, self.version

    def _set(self, queue=None, error=None):
        with self.lock:
            self.queue, self.error = queue, error
            self.version += 1

    # -- HTTP
    def _post_token(self, data):
        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=urllib.parse.urlencode(data).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        return json.loads(http_read(req, timeout=15).decode())

    def _ensure_token(self):
        # one refresh at a time: Spotify rotates the refresh token, so a second parallel refresh
        # with the old one gets 400 and would delete the new token
        with self.token_lock:
            self._refresh_token()

    def _refresh_token(self):
        if self.access and time.time() < self.expires - 30:
            return
        sent = self.cfg["refresh_token"]
        try:
            r = self._post_token({"grant_type": "refresh_token", "refresh_token": sent,
                                  "client_id": self.cfg["client_id"]})
        except urllib.error.HTTPError as e:
            # delete only the token that was refused, not one a reconnect saved meanwhile
            if e.code in (400, 401) and self.cfg.get("refresh_token") == sent:
                self.cfg.pop("refresh_token", None)
                self.cfg.save()
                self._set(None, ("err_expired", {}))
            raise
        self.access = r["access_token"]
        self.expires = time.time() + r.get("expires_in", 3600)
        changed = False
        if r.get("refresh_token"):
            self.cfg["refresh_token"] = r["refresh_token"]
            changed = True
        if r.get("scope") is not None and r["scope"] != self.cfg.get("scopes"):
            # Spotify reports granted scopes: tells whether Like is permitted
            self.cfg["scopes"] = r["scope"]
            changed = True
            log("Spotify scopes:", r["scope"])
        if changed:
            self.cfg.save()

    def _api(self, path, method="GET", body=None):
        self._ensure_token()
        headers = {"Authorization": "Bearer " + self.access}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif method in ("PUT", "POST"):
            data = b""
        req = urllib.request.Request("https://api.spotify.com/v1" + path, method=method,
                                     headers=headers, data=data)
        # don't send POST (add to queue) twice: retry only if the connection never opened
        raw = http_read(req, timeout=15, idempotent=method != "POST")
        try:
            return json.loads(raw.decode()) if raw else {}
        except ValueError:
            return {}

    # -- search, playback, queue, devices
    @property
    def has_control(self):
        return "user-modify-playback-state" in (self.cfg.get("scopes") or "")

    def search(self, q):
        r = self._api("/search?" + urllib.parse.urlencode({"q": q, "type": "track",
                                                            "limit": 8}))
        out = []
        for it in (r.get("tracks") or {}).get("items") or []:
            imgs = (it.get("album") or {}).get("images") or []
            out.append({"id": it.get("id"), "uri": it.get("uri"), "name": it.get("name", ""),
                        "artist": ", ".join(a.get("name", "") for a in it.get("artists") or []),
                        "dur": (it.get("duration_ms") or 0) / 1000,
                        "img": min(imgs, key=lambda i: i.get("width") or 9999)["url"]
                        if imgs else None})
        return out

    def play_uri(self, uri):
        self._api("/me/player/play", "PUT", {"uris": [uri]})

    def queue_uri(self, uri):
        self._api("/me/player/queue?uri=" + urllib.parse.quote(uri), "POST")
        self.wake.set()

    def devices(self):
        return [{"id": d.get("id"), "name": d.get("name", "?"), "type": d.get("type", ""),
                 "active": bool(d.get("is_active"))}
                for d in (self._api("/me/player/devices").get("devices") or []) if d.get("id")]

    def transfer(self, device_id):
        self._api("/me/player", "PUT", {"device_ids": [device_id], "play": True})

    # -- Like (library)
    @property
    def has_library(self):
        return "user-library-modify" in (self.cfg.get("scopes") or "")

    def _library(self, tid, method):
        """Library endpoint (old /me/tracks returns 403, the new URI-based one is used)."""
        uri =urllib.parse.quote("spotify:track:" + tid)
        path = "/me/library/contains?uris=" if method == "CHECK" else "/me/library?uris="
        return self._api(path + uri, "GET" if method == "CHECK" else method)

    def _check_liked(self, tid):
        self.liked_t[tid] = time.time()
        try:
            r = self._library(tid, "CHECK")
            new = bool(r[0]) if isinstance(r, list) and r else False
            if self.liked.get(tid) != new:
                self.liked[tid] = new
                with self.lock:
                    self.version += 1
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode(errors="replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            log("could not read like state:", e.code, body)
            if e.code == 403:  # no permission: reconnect needed
                self.cfg["scopes"] = ""
        except Exception as e:  # noqa: BLE001
            if not is_transient(e):  # connection drops are reported once by the main loop
                log("could not read like state:", repr(e))

    def liked_state(self, title):
        """Is the current track (if the title matches) liked: True / False / None."""
        cur = self.current
        if not cur or not cur.get("id") or cur.get("name") != title:
            return None
        return self.liked.get(cur["id"])

    def toggle_like(self, title):
        cur = self.current
        if not cur or not cur.get("id") or cur.get("name") != title:
            return False
        tid = cur["id"]
        new = not self.liked.get(tid, False)
        self.liked[tid] = new  # optimistic update
        with self.lock:
            self.version += 1

        def go():
            try:
                self._library(tid, "PUT" if new else "DELETE")
            except Exception as e:  # noqa: BLE001
                log("could not change like:", repr(e))
                self.liked[tid] = not new
                with self.lock:
                    self.version += 1

        threading.Thread(target=go, daemon=True).start()
        return True

    def _thumb(self, url):
        if not url or url in self.thumbs:
            return
        if len(self.thumbs) > 60:  # don't grow unbounded; needed covers are downloaded again
            self.thumbs.clear()
        try:
            self.thumbs[url] = Image.open(io.BytesIO(http_read(url, timeout=10))).convert("RGB")
        except Exception as e:  # noqa: BLE001
            if not is_transient(e):  # if the connection dropped, try again later
                self.thumbs[url] = None

    def _fetch(self):
        try:
            data = self._api("/me/player/queue")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self.access = None
                return
            if e.code == 403:
                self._set([], ("err_denied", {}))
            elif e.code == 429:
                time.sleep(int(e.headers.get("Retry-After", "10")))
            else:
                self._set(self.queue, ("err_http", {"code": e.code}))
            return
        cp = data.get("currently_playing") or {}
        tid = cp.get("id") if cp.get("type", "track") == "track" else None
        self.current = {
            "name": cp.get("name"),
            "id": tid,
            "artist_uri": ((cp.get("artists") or [{}])[0]).get("uri")
            or (cp.get("show") or {}).get("uri"),
            "album_uri": (cp.get("album") or {}).get("uri"),
        }
        # refresh like state (may have changed in the Spotify app)
        if tid and self.has_library and time.time() - self.liked_t.get(tid, 0) > 30:
            self._check_liked(tid)
        items = []
        for it in (data.get("queue") or [])[:Q_ROWS]:
            if it.get("type") == "episode":
                sub = (it.get("show") or {}).get("name", "")
                imgs = it.get("images") or (it.get("show") or {}).get("images") or []
            else:
                sub = ", ".join(a.get("name", "") for a in it.get("artists") or [])
                imgs = (it.get("album") or {}).get("images") or []
            # small cover (64 px) for the list; prefer the 300 px one for the big cover
            # shown instantly when picked
            imgs = sorted(imgs, key=lambda i: i.get("width") or 9999)
            url = next((i["url"] for i in imgs if (i.get("width") or 0) >= 200), None) \
                or (imgs[-1]["url"] if imgs else None)
            items.append({"id": it.get("id"), "name": it.get("name", ""), "sub": sub,
                          "url": url, "dur": (it.get("duration_ms") or 0) / 1000})
        old = [i["id"] for i in self.queue] if self.queue else None
        if old != [i["id"] for i in items] or self.error:
            self._set(items, None)  # show the list without waiting for covers
        missing = [i["url"] for i in items if i["url"] and i["url"] not in self.thumbs]
        if missing:
            threading.Thread(target=self._load_thumbs, args=(missing,), daemon=True).start()

    def _load_thumbs(self, urls):
        """Download covers in parallel; redraw the list as each arrives."""
        from concurrent.futures import ThreadPoolExecutor

        def one(u):
            self._thumb(u)
            with self.lock:
                self.version += 1

        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(one, urls))

    def pop_queue(self, n):
        """When a track is picked from up next, drop the skipped tracks from the list at once."""
        with self.lock:
            if self.queue:
                self.queue = self.queue[n:]
                self.version += 1

    def _run(self):
        last = 0.0
        while True:
            woke = self.wake.wait(1.0)
            self.wake.clear()
            if not self.connected:
                continue
            interval = 8 if self.playing else 30
            again = self._again and time.time() >= self._again
            if woke or again or time.time() - last > interval:
                if woke:
                    time.sleep(0.45)  # give Spotify time to update the queue
                    self._again = time.time() + 2.5  # look once more after the skips finish
                else:
                    self._again = 0.0
                last = time.time()
                try:
                    self._fetch()
                    if self.net_down:
                        self.net_down = False
                        log("connection is back")
                except Exception as e:  # noqa: BLE001
                    if not is_transient(e):
                        log("could not fetch queue:", repr(e))
                    elif not self.net_down:  # one line for the whole outage
                        self.net_down = True
                        log("connection problem, will retry automatically:", repr(e))

    # -- OAuth PKCE
    def start_auth(self, client_id):
        threading.Thread(target=self._auth, args=(client_id.strip(),), daemon=True).start()

    def _auth(self, client_id):
        self.auth_status, self.auth_error = T("auth_wait"), False
        verifier = base64.urlsafe_b64encode(os.urandom(48)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(12)
        result = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if urllib.parse.urlparse(self.path).path == "/callback":
                    result["code"] = (q.get("code") or [None])[0]
                    result["state"] = (q.get("state") or [None])[0]
                    result["error"] = (q.get("error") or [None])[0]
                ok = bool(result.get("code"))
                msg = T("auth_page_ok") if ok else T("auth_page_fail")
                body = (f"<html><meta charset='utf-8'><body style='font-family:Segoe UI;"
                        f"background:#111;color:#eee;display:grid;place-items:center;"
                        f"height:100vh;margin:0'><h2>{msg}</h2></body></html>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        try:
            server = HTTPServer(("127.0.0.1", 8888), Handler)
        except OSError:
            self.auth_status, self.auth_error = T("auth_port"), True
            return
        server.timeout = 1
        params = {"client_id": client_id, "response_type": "code",
                  "redirect_uri": REDIRECT_URI, "code_challenge_method": "S256",
                  "code_challenge": challenge, "state": state, "scope": SCOPES}
        webbrowser.open("https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(params))
        deadline = time.time() + 300
        # only the redirect carrying our state ends the wait: a stray /callback from a web page can't cancel it
        while result.get("state") != state and time.time() < deadline:
            server.handle_request()
        server.server_close()
        if not result.get("code") or result.get("state") != state:
            self.auth_status, self.auth_error = T("auth_denied"), True
            return
        try:
            r = self._post_token({"grant_type": "authorization_code", "code": result["code"],
                                  "redirect_uri": REDIRECT_URI, "client_id": client_id,
                                  "code_verifier": verifier})
        except Exception as e:  # noqa: BLE001
            self.auth_status, self.auth_error = T("auth_token", e=e), True
            return
        self.cfg["client_id"] = client_id
        self.cfg["refresh_token"] = r["refresh_token"]
        self.cfg["scopes"] = r.get("scope", SCOPES)
        self.cfg.save()
        self.liked.clear()
        self.liked_t.clear()
        self.access = r["access_token"]
        self.expires = time.time() + r.get("expires_in", 3600)
        self._set(None, None)
        self.auth_status = "ok"
        self.wake.set()

    def disconnect(self):
        self.cfg.pop("refresh_token", None)
        self.cfg.pop("scopes", None)
        self.cfg.save()
        self.access = None
        self._set(None, None)


# ------------------------------------------------------------------ app
class App:
    def __init__(self):
        self.dpi = set_dpi_awareness()
        self.cfg = Config()
        self.cfg.setdefault("scale", 0.85)
        self.cfg.setdefault("queue_rows", 3)
        self.cfg.setdefault("mode", "desktop")
        self.cfg.setdefault("theme", "system")
        self.cfg.setdefault("opacity", 100)
        self.cfg.setdefault("wave", True)
        self.cfg.setdefault("show_queue", True)
        self.cfg.setdefault("queue_collapsed", False)
        self.cfg.setdefault("locked", False)
        self.cfg.setdefault("hover_opaque", True)
        self.cfg.setdefault("idle_opacity", 60)
        self.cfg.setdefault("mini", False)
        self.cfg.setdefault("hotkeys", True)
        self.cfg.setdefault("notify", True)
        self.cfg.setdefault("toast_pos", "br")    # corner: br / tr / bl / tl
        self.cfg.setdefault("toast_secs", 3.5)
        self.cfg.setdefault("list_tab", "queue")
        self.cfg.setdefault("accent", "auto")
        self.cfg.setdefault("bg", "solid")
        self.cfg.setdefault("fps", 60)
        self.cfg.setdefault("ui", "material")   # design language: material / shadcn / neu
        self.cfg.setdefault("memory", True)     # daily "on this day last year" reminder
        self.cfg.setdefault("startup", True)    # launch at Windows startup (on by default)
        if FROZEN and self.cfg["startup"]:
            # exe only: refresh the entry on every launch (writes the new path if the exe moved).
            # Dev / test copies must not touch the user's startup entry.
            self._set_startup(True)
        self._start_t = time.monotonic()
        self.update_tag = None    # newer release tag, shown in the menu
        self.update = None        # its installer (pick_update)
        self._updating = False
        if FROZEN:  # dev / test copies stay offline
            self._recheck_update()
        self.cfg.setdefault("react", True)
        self.cfg.setdefault("duck", True)
        self.cfg.setdefault("osd", True)
        self.cfg.setdefault("duck_apps", ["discord.exe"])
        self.cfg.setdefault("alarm", {"on": False, "time": "08:00"})
        self.level = 0.0          # Spotify's current output level (0..1, relative to the last few seconds)
        self._lvl_lo = self._lvl_hi = None
        self._duck = None         # while ducked: {"orig": volume, "last": last speech time}
        self._duck_fade = None
        self._alarm_day = None
        self._alarm_fade = None
        # volume to put back if the app was killed while it had Spotify turned down
        self._vol_pending = self.cfg.get("vol_restore")
        self.cfg.setdefault("stats_period", "7")
        self.cfg.setdefault("stats_list", "artists")
        self.stats = self._load_stats()
        self.imported = load_imported_history()   # history imported from Spotify
        self.import_ver = 0
        self._index_imported()
        self._stat_t = None
        self._stat_saved = time.monotonic()
        self._glass = {}         # blurred wallpaper cache
        self.glass_ver = 0
        global LANG
        LANG = self.cfg.setdefault("lang", default_lang())
        self.history = self._load_history()
        self.hist_off = 0   # History list scroll offset
        self._hist_thumbs = {}
        self._toast_key = None       # last track a toast was shown for
        self._seen_first = False     # no toast for the first track at launch
        self.inside = False
        self._alpha_job = None
        self._leave_job = None
        self.mini = bool(self.cfg["mini"])
        self.art_mini = None
        # audio
        self.volume = AppVolume()
        self.vol_val = None
        self.vol_until = 0.0
        # hotkeys
        self.hk_queue = []
        self.hotkeys = Hotkeys(self.hk_queue)
        if self.cfg["hotkeys"]:
            self.hotkeys.start()
        # sleep timer
        self.sleep_end = None
        self.sleep_fade = None
        self.sleep_choice = 0

        # the window opens at the largest size; content is drawn top-left at the chosen scale,
        # the remaining transparent area passes clicks through to the desktop
        self.WMAX = round(W * self.dpi * SCALE_MAX)
        self.HMAX = round(TOTAL_H * self.dpi * SCALE_MAX)
        icon_file = "SegoeIcons.ttf" if os.path.exists(
            os.path.join(FONT_DIR, "SegoeIcons.ttf")) else "segmdl2.ttf"
        self._icon_path = os.path.join(FONT_DIR, icon_file)
        self.art_src = None
        self.qthumbs = {}
        self.wave_tiles = {}
        self.content_h = MAIN_H
        self._apply_scale()
        S = self.S

        self.media = MediaWorker()
        self.web = SpotifyWeb(self.cfg)
        self.lyrics = Lyrics()

        # state
        self.light = self._is_light()
        self.seed = DEFAULT_SEED
        self.pal = self._scheme()
        self.pal_from = self.pal_to = self.pal
        self.pal_t = 1.0
        self.pal_version = 0
        self.art_rgba = None
        self.art_version = 0
        self.hover = None
        self.pressed = None
        self.hits = []
        self.base = None
        self.base_key = None
        self.base_strip = None
        self.strip_key = None
        self.marq = None        # scrolling title in mini mode
        self.marq_key = None
        self.amp = 0.0
        self.phase = 0.0
        self.play_r = 18.0
        self.drag_seek = None
        self.win_drag = None
        self.seek_hold = None
        self.play_hold = None
        self.last_t = time.monotonic()
        self.last_theme_check = 0.0
        self.st = {"ok": False}
        self.pending = None   # track picked from up next that Spotify hasn't confirmed yet
        self._morph = None    # mini ↔ normal transition animation
        self._anchor = {"bottom": False, "deferred": None}
        self.last = None
        self._last_saved = 0.0
        self._load_last()

        # window
        self.root = tk.Tk()
        self.root.title("Coverglow")
        try:  # default=: every dialog gets the app icon instead of the tk feather
            self.root.iconbitmap(default=os.path.join(os.path.dirname(BUNDLED_FONTS), "icon.ico"))
        except tk.TclError:
            pass
        self.root.report_callback_exception = lambda *a: log(
            "".join(traceback.format_exception(*a)))
        self.root.overrideredirect(True)
        self.root.configure(bg="black")
        self.root.geometry(f"{self.WMAX}x{self.HMAX}+-30000+-30000")
        self.root.update()
        self.hwnd = user32.GetAncestor(self.root.winfo_id(), 2)  # GA_ROOT
        set_window_styles(self.hwnd)
        self.surface = LayeredSurface(self.hwnd, self.WMAX, self.HMAX)
        self.surface.alpha = self._target_alpha()
        self._render_all()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = self.cfg.get("x", sw - self.WP - round(32 * S))
        y = self.cfg.get("y", round(48 * S))
        # bring it back if off screen
        x = max(0, min(int(x), sw - round(120 * S)))
        y = max(0, min(int(y), sh - round(120 * S)))
        move_window(self.hwnd, x, y)
        self._apply_mode()

        r = self.root
        r.bind("<Motion>", self._on_motion)
        r.bind("<ButtonPress-1>", self._on_press)
        r.bind("<B1-Motion>", self._on_motion)
        r.bind("<ButtonRelease-1>", self._on_release)
        r.bind("<Button-3>", self._on_menu)
        r.bind("<Leave>", self._on_leave)
        r.bind("<MouseWheel>", self._on_wheel)
        r.bind("<Double-Button-1>", self._on_double)
        self.toast = Toast(self)
        self.root.after(30, self._tick)
        self.root.after(1500, self._keep_z)

    # ---------------- recently played (local history)
    HISTORY_PATH = os.path.join(APP_DIR, "history.json")
    HISTORY_MAX = 50

    def _load_history(self):
        try:
            with open(self.HISTORY_PATH, "r", encoding="utf-8") as f:
                h = json.load(f)
            return [x for x in h if isinstance(x, dict) and x.get("title")][:self.HISTORY_MAX]
        except Exception:
            return []

    def _add_history(self, st):
        if self.history and self.history[0].get("title") == st["title"] \
                and self.history[0].get("artist") == st["artist"]:
            return
        thumb = None
        if self.art_src is not None:
            try:
                buf = io.BytesIO()
                self.art_src.resize((96, 96), Image.Resampling.LANCZOS).save(
                    buf, "JPEG", quality=85)
                thumb = base64.b64encode(buf.getvalue()).decode()
            except Exception:  # noqa: BLE001
                thumb = None
        self.history.insert(0, {"title": st["title"], "artist": st["artist"],
                                "album": st.get("album", ""), "t": time.time(),
                                "art": thumb})
        del self.history[self.HISTORY_MAX:]
        try:
            save_json(self.HISTORY_PATH, self.history, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            log("could not save history:", repr(e))

    def _recent_all(self):
        """All recently played except the track on screen (newest first)."""
        v = self._view()
        cur = (v.get("title"), v.get("artist"))
        return [h for h in self.history if (h["title"], h["artist"]) != cur]

    def _recent_items(self, rows):
        return self._recent_all()[:rows]

    def _recent_rows(self, rows):
        """Rows visible in the History tab (scrolled with the mouse wheel).
        Each row: title, sub, thumb, right, q (text to search in Spotify)."""
        items = self._recent_all()
        self.hist_off = max(0, min(self.hist_off, len(items) - rows))
        return [{"title": h["title"], "sub": h["artist"], "thumb": self._hist_thumb(h),
                 "right": time_ago(h.get("t", 0)), "q": f'{h["title"]} {h["artist"]}'}
                for h in items[self.hist_off:self.hist_off + rows]]

    def _hist_thumb(self, h):
        key = (h["title"], h["artist"], h.get("t"), self.S)
        th = self._hist_thumbs.get(key)
        if th is None and h.get("art"):
            try:
                src = Image.open(io.BytesIO(base64.b64decode(h["art"])))
                th = rounded_image(src, round(40 * self.S), 10 * self.S)
            except Exception:  # noqa: BLE001
                th = None
            if len(self._hist_thumbs) > 80:
                self._hist_thumbs.clear()
            self._hist_thumbs[key] = th
        return th

    # ---------------- language
    def _set_lang(self, code):
        global LANG
        LANG = code
        self._set("lang", code)
        self._render_all()

    # ---------------- size
    def _apply_scale(self):
        sc = max(SCALE_MIN, min(SCALE_MAX, float(self.cfg["scale"])))
        self.S = S = self.dpi * sc
        set_style(self.cfg.get("ui", "material"), S)
        self._hist_thumbs = {}
        self.WP, self.HP = round(W * S), round(TOTAL_H * S)
        if self.mini:  # strip holding the thin progress line
            self.sy0 = round((MINI_BAR_Y - 4) * S)
            self.sh = round((MINI_BAR_Y + 4) * S) - self.sy0
        else:
            self.sy0 = round(STRIP_Y0 * S)
            self.sh = round(STRIP_Y1 * S) - self.sy0

        def font(name, px):
            return TextFont(name, px, S, bold=(name == "seguisb.ttf"))

        self.F = {
            "status": font("seguisb.ttf", 11), "title": font("seguisb.ttf", 19),
            "artist": font("segoeui.ttf", 14), "album": font("segoeui.ttf", 12),
            "time": font("segoeui.ttf", 12), "qhead": font("seguisb.ttf", 15),
            "qtitle": font("segoeui.ttf", 14), "qsub": font("segoeui.ttf", 12),
            "btn": font("seguisb.ttf", 13), "body": font("segoeui.ttf", 13),
            "mtitle": font("seguisb.ttf", 15), "tiny": font("segoeui.ttf", 10),
            "lyric": font("seguisb.ttf", 15), "big": font("seguisb.ttf", 34),
        }
        self._icon_fonts = {}
        self.qthumbs.clear()
        self.wave_tiles.clear()
        self._make_art()
        self.base_key = None

    def _make_art(self):
        S = self.S
        if self.art_src is None:
            self.art_rgba = self.art_mini = None
        else:
            self.art_rgba = rounded_image(self.art_src, round(120 * S), 20 * S)
            self.art_mini = rounded_image(self.art_src, round(MINI_ART * S), 16 * S)

    def _set_scale(self, sc):
        sc = round(max(SCALE_MIN, min(SCALE_MAX, sc)), 2)
        if abs(sc - float(self.cfg["scale"])) < 0.001:
            return
        self.cfg["scale"] = sc
        self.cfg.save()
        self._apply_scale()
        self._render_all()

    # ---------------- helpers
    def icon_font(self, px):
        f = self._icon_fonts.get(px)
        if f is None:
            f = self._icon_fonts[px] = ImageFont.truetype(self._icon_path, max(6, round(px * self.S)))
        return f

    def _is_light(self):
        t = self.cfg["theme"]
        return system_prefers_light() if t == "system" else t == "light"

    def _apply_mode(self):
        if self.cfg["mode"] == "top":
            pin_on_top(self.hwnd)
        else:
            pin_to_desktop(self.hwnd)

    def _keep_z(self):
        if self.cfg["mode"] == "desktop":
            keep_bottom(self.hwnd)
        self.root.after(1500, self._keep_z)

    # ---------------- fade in on hover
    def _target_alpha(self):
        pct = self.cfg["opacity"]  # opacity on hover (and when the feature is off)
        if self.cfg["hover_opaque"] and not self.inside:
            pct = min(pct, self.cfg["idle_opacity"])  # fade out when the mouse is away
        return round(255 * pct / 100)

    def _set_inside(self, inside):
        if self._leave_job is not None:
            self.root.after_cancel(self._leave_job)
            self._leave_job = None
        if inside:
            if not self.inside:
                self.inside = True
                self._alpha_step()
        elif self.inside:
            # short delay so it doesn't flicker when crossing the gap between cards
            self._leave_job = self.root.after(250, self._left)

    def toast_area(self):
        """Work area of the monitor the user is on (foreground window), else the widget's."""
        fg = user32.GetForegroundWindow()
        own = fg in (self.hwnd, self.toast.hwnd) or window_class(fg) in _SHELL_CLASSES
        return monitor_info(self.hwnd if not fg or own else fg).rcWork

    def _left(self):
        self._leave_job = None
        self.inside = False
        self._alpha_step()

    def _alpha_step(self):
        if self._alpha_job is not None:
            self.root.after_cancel(self._alpha_job)
            self._alpha_job = None
        target, cur = self._target_alpha(), self.surface.alpha
        if cur == target:
            return
        ms = self.frame_ms()
        nxt = cur + (target - cur) * ease(0.35 if target > cur else 0.18, ms)  # in fast, out slowly
        nxt = target if abs(target - nxt) < 2 else round(nxt)
        if nxt == cur:
            nxt = target
        self.surface.alpha = nxt
        self.surface._commit(None)
        if nxt != target:
            self._alpha_due = next_due(getattr(self, "_alpha_due", 0.0), time.monotonic(), ms)
            self._alpha_job = self.root.after(
                max(1, round((self._alpha_due - time.monotonic()) * 1000)), self._alpha_step)

    def _open_entity(self, kind):
        """Open the artist / album page in Spotify."""
        st = self._view()
        if not st.get("title"):
            return
        uri = None
        cur = self.web.current
        if cur and cur.get("name") == st.get("title"):
            uri = cur.get(kind + "_uri")
        query = st["artist"] if kind == "artist" else f'{st["album"]} {st["artist"]}'
        if not uri:  # no account linked: search inside Spotify
            uri = "spotify:search:" + urllib.parse.quote(query.strip())
        open_spotify(uri, "https://open.spotify.com/search/" + urllib.parse.quote(query))

    @staticmethod
    def _spotify_search(q):
        if not q:
            return
        open_spotify("spotify:search:" + urllib.parse.quote(q),
                     "https://open.spotify.com/search/" + urllib.parse.quote(q))

    def _win_pos(self):
        r = wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        return r.left, r.top

    def _hit(self, x, y):
        for hid, x0, y0, x1, y1 in self.hits:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return hid
        return None

    # ---------------- time / position
    def _position(self):
        st = self.st
        if not st.get("ok"):
            if self.last:  # Spotify closed: where the last played track stopped
                return float(self.last.get("pos") or 0), float(self.last.get("end") or 0)
            return 0.0, 0.0
        pos, end = st["pos"], st["end"]
        playing = self._playing()
        upd = st.get("updated")
        if st["playing"] and isinstance(upd, datetime):
            if upd.tzinfo is None:
                upd = upd.replace(tzinfo=timezone.utc)
            pos += max(0.0, (datetime.now(timezone.utc) - upd).total_seconds())
        if self.seek_hold:
            target, t0 = self.seek_hold
            if time.monotonic() - t0 < 2.5:
                pos = target + ((time.monotonic() - t0) if playing else 0)
            else:
                self.seek_hold = None
        if end > 0:
            pos = min(pos, end)
        return pos, end

    def _playing(self):
        if self.play_hold and time.monotonic() - self.play_hold[1] < 1.5:
            return self.play_hold[0]
        self.play_hold = None
        return bool(self.st.get("playing"))

    # ---------------- main loop
    # ---------------- volume
    def _change_volume(self, delta):
        v = self.volume.get()
        if v is None:
            return
        self.vol_val = self.volume.set(round((v + delta) * 100) / 100)
        self.vol_until = time.monotonic() + 1.5

    # ---------------- sleep timer
    def _set_sleep(self, minutes):
        self._restore_sleep_volume()
        self.sleep_end = time.monotonic() + minutes * 60 if minutes else None
        self.sleep_choice = minutes
        self.base_key = None

    def _restore_sleep_volume(self):
        if self.sleep_fade is not None:
            self.volume.set(self.sleep_fade)
            self._vol_mark(None)
        self.sleep_fade = None

    def _finish_sleep(self):
        fade = self.sleep_fade
        self.sleep_end = self.sleep_fade = None
        self.sleep_choice = 0
        if self.st.get("ok") and self.st.get("playing"):
            self.media.command("pause")
        if fade is not None:  # after pausing, go back to the old volume
            self.root.after(1500, lambda: (self.volume.set(fade), self._vol_mark(None)))
        self.base_key = None

    def _sleep_minutes(self):
        if self.sleep_end is None:
            return None
        return max(1, math.ceil((self.sleep_end - time.monotonic()) / 60))

    # ---------------- mini mode
    def _toggle_mini(self):
        def change():
            self.mini = not self.mini
            self.cfg["mini"] = self.mini
            self.cfg.save()
            self.hover = None
            self._apply_scale()
        self._transition(change)

    def _transition(self, change, dur=None):
        """Animate a view change (mini, tab, list open/close, period): the card size goes from
        old to new from a fixed edge while the old and new views cross-fade.
        change(): function that changes the state. In-between frames come from two ready images."""
        if self._morph:
            self._finish_morph()
        old, old_h = getattr(self, "_frame", None), getattr(self, "_shown_h", None)
        was_mini = self.mini
        self._morph = "pending" if old is not None and old_h else None  # don't push the new frame
        try:
            change()
            self.base_key = None
            self._render_all()
        except Exception:
            self._morph = None  # if it gets stuck at "pending" no frame is pushed
            raise
        if self._morph != "pending":
            return
        if self._frame is old:  # nothing changed
            self._morph = None
            return
        new_h = self._shown_h
        a = self._anchor
        H = max(old_h, new_h)
        placed = []
        for img, h in ((old, old_h), (self._frame, new_h)):
            c = Image.new("RGBA", (self.WMAX, H), (0, 0, 0, 0))
            c.paste(img.crop((0, 0, self.WMAX, h)), (0, H - h if a["bottom"] else 0))
            placed.append(c)
        rad = lambda mini: (MINI_H / 2 if mini else 28) * self.S  # noqa: E731
        cap = STYLE["r"] if STYLE["shad"] else 1e9
        self._morph = {"t0": time.monotonic(), "old": placed[0], "new": placed[1],
                       "oh": old_h, "nh": new_h, "H": H, "bottom": a["bottom"],
                       "r0": min(cap, rad(was_mini)), "r1": min(cap, rad(self.mini)),
                       "dur": dur or (self.MORPH_S if was_mini != self.mini else 0.2)}
        self._morph_step()

    MORPH_S = 0.26  # mini ↔ normal transition time (in-card changes 0.2 s)
    # actions that animate on click (tab, list open/close, stats period / list)
    ANIMATED = ("tab_", "q", "sp_", "sl_", "sup", "sb")  # "q": list open/close + track from up next

    def _morph_step(self):
        """Transition frame: the card grows/shrinks from old to new (from a fixed edge) while the
        two views cross-fade. When done, push the final frame the normal way."""
        m = self._morph
        t = min(1.0, (time.monotonic() - m["t0"]) / m["dur"])
        e = 1 - (1 - t) ** 3
        H, w = m["H"], self.WP
        h = m["oh"] + (m["nh"] - m["oh"]) * e
        top = H - h if m["bottom"] else 0.0
        frame = Image.blend(m["old"], m["new"], e)
        # clip mask: plain rect + 4 cached corners (redrawing every frame costs ~5 ms)
        r = round(m["r0"] + (m["r1"] - m["r0"]) * e)
        it, ib = int(round(top)), int(round(top + h))
        r = max(1, min(r, (ib - it) // 2, w // 2))
        cache = self.__dict__.setdefault("_corners", {})
        c = cache.get(r)
        if c is None:
            c = cache[r] = _rr_mask(2 * r, 2 * r, 0, 0, 2 * r, 2 * r, r)
        mask = Image.new("L", frame.size, 0)
        mask.paste(255, (0, it, w, ib))
        for cx, cy in ((0, 0), (r, 0), (0, r), (r, r)):
            mask.paste(c.crop((cx, cy, cx + r, cy + r)),
                       (cx if cx == 0 else w - r, it if cy == 0 else ib - r))
        from PIL import ImageChops
        frame.putalpha(ImageChops.multiply(frame.getchannel("A"), mask))
        # when growing, the window moves to its new place on the first frame (together with drawing)
        self.surface.full(frame, H + 2, self._take_move() if m["nh"] > m["oh"] else None)
        if t >= 1.0:
            self._finish_morph()

    def _take_move(self):
        mv, self._anchor["deferred"] = self._anchor.get("deferred"), None
        if mv:
            self.cfg["x"], self.cfg["y"] = mv
            self.cfg.save()
        return mv

    def _finish_morph(self):
        m, self._morph = self._morph, None
        if m:  # final frame ready: push without redrawing; when shrinking the window moves now too
            self.surface.full(self._frame, round(self.content_h * self.S) + 4, self._take_move())
            self.strip_key = None
        elif self._anchor.get("deferred"):
            move_window(self.hwnd, *self._take_move())

    def _on_double(self, e):
        x, y = self._xy(e)
        if self._hit(x, y):  # fast double click on a button: treat as a normal click
            return self._on_press(e)
        self._toggle_mini()

    # ---------------- last played track (shown while Spotify is closed)
    def _load_last(self):
        try:
            with open(LAST_PATH, "r", encoding="utf-8") as f:
                last = json.load(f)
            if not last.get("title"):
                return
        except Exception:
            return
        self.last = last
        art = None
        try:
            with open(LAST_ART_PATH, "rb") as f:
                art = f.read()
        except OSError:
            pass
        self._on_new_art(art)
        self.pal = self.pal_to  # no color transition at launch
        self.pal_t = 1.0

    def _save_last(self, st, art_changed):
        pos, end = self._position()
        self.last = {"title": st["title"], "artist": st["artist"], "album": st["album"],
                     "pos": pos, "end": end, "shuffle": st.get("shuffle"),
                     "repeat": st.get("repeat")}
        self._last_saved = time.monotonic()
        try:
            save_json(LAST_PATH, self.last, ensure_ascii=False)
            if art_changed:
                if st.get("art"):
                    with open(LAST_ART_PATH, "wb") as f:
                        f.write(st["art"])
                elif os.path.exists(LAST_ART_PATH):
                    os.remove(LAST_ART_PATH)
        except Exception as e:  # noqa: BLE001
            log("could not save last track:", repr(e))

    def _view(self):
        """Track to show: the playing one or (if Spotify is closed) the last played.
        When a track is picked from up next it's shown until Spotify confirms it."""
        if self.st.get("ok"):
            p = self.pending
            if p:
                return dict(self.st, title=p["title"], artist=p["artist"], album="",
                            playing=True)
            return self.st
        if self.last:
            return dict(self.last, ok=False, offline=True, playing=False)
        return self.st

    def _tick(self):
        t0 = time.monotonic()
        interval = 200
        try:
            interval = self._tick_body()
        finally:  # keep the loop running even if a frame fails (error goes to report_callback_exception)
            now = time.monotonic()
            self._due = next_due(getattr(self, "_due", t0), now, interval)
            self.root.after(max(1, round((self._due - now) * 1000)), self._tick)

    def frame_ms(self):
        """Animation frame interval from the 'Animation smoothness' setting (60 or 30 fps)."""
        return FRAME_MS if self.cfg["fps"] >= 60 else 2 * FRAME_MS

    def _tick_body(self):
        now = time.monotonic()
        dt = min(0.2, now - self.last_t)
        self.last_t = now

        st = self.media.snapshot()
        p = self.pending
        if p:
            if st.get("ok") and st.get("title") == p["title"]:
                self.pending = None  # Spotify switched to the picked track
            elif now > p["until"] or not st.get("ok"):
                self.pending = None  # no match / timeout: back to the real state
                if st.get("ok"):
                    st = dict(st, art_changed=True)  # bring the real cover back
            else:
                # while skipping don't show the tracks in between (no cover / history / toast)
                st = dict(st)
                st.pop("art_changed", None)
        if st.get("art_changed"):
            def change(st=st):
                self.st = st
                self._on_new_art(st.get("art"))
            self._transition(change, dur=0.35)  # track change: transition from the old view to the new
        self.st = st
        playing = self._playing()
        self._stat_tick(now, st, playing)
        if st.get("ok") and self.cfg["list_tab"] == "lyrics" and self.cfg["show_queue"]:
            self.lyrics.request(st.get("title"), st.get("artist"), st.get("album"), st.get("end"))
        if st.get("ok") and not self.pending:  # refresh for the tracks in between while skipping
            self.web.notify((st["title"], st["artist"]), playing)
            if st.get("title") and (st.get("art_changed") or self.last is None
                                    or self.last.get("title") != st["title"]
                                    or now - self._last_saved > 10):
                self._save_last(st, bool(st.get("art_changed")))
            if st.get("art_changed") and st.get("title"):
                if not (self.history and self.history[0].get("title") == st["title"]
                        and self.history[0].get("artist") == st["artist"]):
                    day = self._stat_day()
                    day["plays"] += 1
                    artist1 = (st.get("artist") or "").split(", ")[0].strip() or T("unknown")
                    tkey = f'{st["title"]}\t{artist1}'
                    cnt = day.setdefault("counts", {})
                    cnt[tkey] = cnt.get(tkey, 0) + 1
                self._add_history(st)
                key = (st["title"], st["artist"])
                if not self._seen_first:  # track at launch: no toast
                    self._seen_first = True
                    self._toast_key = key
                elif key != self._toast_key:
                    self._toast_key = key
                    fs, why = fullscreen_info((self.hwnd, self.toast.hwnd))
                    show = self.cfg["notify"] and (fs is not None or (
                        self.cfg["mode"] == "desktop"
                        and own_window_at_cursor() in (None, self.toast.hwnd)))
                    if fs is not None or self.cfg.get("debug_toast"):
                        # short diagnostic line to find out why it's hidden in games
                        log(f"bildirim: tam_ekran={fs is not None} gosterildi={bool(show)} {why}")
                    if show:
                        self.toast.show(st, fs)
        # after the track change handling: an error in these (e.g. pycaw while the audio device
        # changes) would otherwise lose the toast and the history entry of the new track
        self._audio_tick(now, st, playing)
        self._alarm_tick(st)
        self._memory_tick(now, st, playing)

        # hotkeys
        while self.hk_queue:
            act = self.hk_queue.pop(0)
            if act == "vol+":
                self._change_volume(0.05)
            elif act == "vol-":
                self._change_volume(-0.05)
            elif act == "search":
                SearchDialog(self)
                continue
            elif act == "like":
                self._like_hotkey()
                continue
            else:
                self._action(act)
            self._hotkey_osd(act)

        # sleep timer (volume fades out in the last 20 seconds)
        if self.sleep_end is not None:
            rem = self.sleep_end - now
            if rem <= 0:
                self._finish_sleep()
            elif rem <= 20 and playing:
                if self.sleep_fade is None:
                    self.sleep_fade = self.volume.get()
                    self._vol_mark(self.sleep_fade)
                if self.sleep_fade is not None:
                    self.volume.set(self.sleep_fade * rem / 20)

        # safety net: if the <Leave> event is missed (e.g. another window came over the widget
        # under a still cursor), notice it from the window under the cursor
        if self.inside and self._leave_job is None and self.drag_seek is None \
                and not self.win_drag and own_window_at_cursor() is None:
            self._set_inside(False)

        if now - self.last_theme_check > 5:
            self.last_theme_check = now
            light = self._is_light()
            if light != self.light:
                self.light = light
                self._start_palette(self._scheme())
            if self.cfg["bg"] == "glass":
                wp = self._wallpaper_file()
                if wp != self._glass.get("file"):
                    self._glass.clear()
                    self.glass_ver += 1

        animating = False
        # palette transition
        if self.pal_t < 1.0:
            self.pal_t = min(1.0, self.pal_t + dt / 0.5)
            e = 1 - (1 - self.pal_t) ** 3
            self.pal = mix_scheme(self.pal_from, self.pal_to, e)
            self.pal_version += 1
            animating = True
        # play button shape morph (paused: squircle, playing: pill)
        target_r = 28.0 if playing else 16.0
        if abs(self.play_r - target_r) > 0.2:
            self.play_r += (target_r - self.play_r) * min(1.0, dt * 12)
            animating = True
        else:
            self.play_r = target_r
        # wave
        target_amp = AMP if (playing and self.cfg["wave"] and not self.mini) else 0.0
        react = bool(target_amp and self.cfg["react"])
        if react:  # react to music: wave height follows the volume (level is already smoothed)
            target_amp = AMP * (0.25 + 1.75 * self.level)
        if abs(self.amp - target_amp) > 0.05:
            self.amp += (target_amp - self.amp) * min(1.0, dt * (30 if react else 6))
            animating = True
        else:
            self.amp = target_amp
        if self.amp > 0.05:
            # with react-to-music on, the wave flows a bit faster as the volume rises
            self.phase += dt * LAM / 1.4 * ((0.7 + 0.8 * self.level) if react else 1.0)
            animating = True

        self._render_all()
        if isinstance(self._morph, dict):  # mini ↔ normal transition
            self._morph_step()
            animating = True
        if self.marq is not None:
            animating = True
        if animating and self.cfg["mode"] != "top" and not self.inside                 and self.drag_seek is None and not isinstance(self._morph, dict):
            x, y = self._win_pos()
            if covered_by_foreground((x, y, x + self.WP, y + round(self.content_h * self.S)),
                                     (self.hwnd, self.toast.hwnd)):
                animating = False  # nobody can see the wave: tick slowly instead of 60 fps
        if animating or self.drag_seek is not None:  # 60 or 30 fps, from the setting
            interval = self.frame_ms()
        elif playing or self.inside or self._alarm_fade or self._duck or self._duck_fade \
                or self.sleep_end is not None:
            interval = 200   # keep time / progress current
        else:
            interval = 450   # paused and idle: checking less often is enough
        return interval

    def _start_palette(self, target):
        self.pal_from = self.pal
        self.pal_to = target
        self.pal_t = 0.0

    def _on_new_art(self, data):
        self.art_version += 1
        img = None
        if data:
            try:
                img = Image.open(io.BytesIO(data))
                img.load()
            except Exception:
                img = None
        self._set_art(img)

    def _set_art(self, img):
        """Apply the cover (PIL image or None): small copies + color palette."""
        self.art_version += 1
        if img is not None:
            self.art_src = img.convert("RGB")
            self.seed = seed_from_image(img)
        else:
            self.art_src = None
            self.seed = DEFAULT_SEED
        self._make_art()
        # colors switch to the new value at once; _transition's cross-fade does the smooth change
        # (the old half-second color animation needed a full redraw every frame)
        self.pal = self.pal_from = self.pal_to = self._scheme()
        self.pal_t = 1.0
        self.pal_version += 1

    def _scheme(self):
        if self.cfg.get("ui") == "shadcn":
            # shadcn: 'from cover' = the shadcn colour theme nearest to the cover, else the fixed color
            if self.cfg.get("accent") == "auto":
                return build_shadcn(self.light, theme=pick_shadcn(self.seed))
            return build_shadcn(self.light, dict(ACCENTS).get(self.cfg.get("accent")))
        if self.cfg.get("ui") == "neu":  # RYOCS theme nearest to the cover colour
            p = build_neu(self.light, self.seed)
            STYLE["hi"], STYLE["lo"] = p["hi"], p["lo"]
            return p
        seed = dict(ACCENTS).get(self.cfg.get("accent"), self.seed)
        return build_scheme(seed, self.light)

    def _set_ui(self, v):
        self._set("ui", v)
        set_style(v, self.S)
        self._apply_scale()
        self.pal = self.pal_from = self.pal_to = self._scheme()
        self.pal_version += 1
        self.base_key = None
        self._render_all()

    # ---------------- background: solid / glass / translucent
    @staticmethod
    def _wallpaper_file():
        """(path, modified time) or None."""
        buf = ctypes.create_unicode_buffer(520)
        user32.SystemParametersInfoW(0x73, 520, buf, 0)  # SPI_GETDESKWALLPAPER
        for p in (buf.value, os.path.join(os.environ.get("APPDATA", ""),
                                          r"Microsoft\Windows\Themes\TranscodedWallpaper")):
            try:
                if p and os.path.isfile(p):
                    return p, os.path.getmtime(p)
            except OSError:
                pass
        return None

    def _glass_bg(self):
        """Blurred wallpaper piece behind the window (WMAX x HMAX, RGB)."""
        g = self._glass
        wx, wy = self._win_pos()
        mi = monitor_info(self.hwnd).rcMonitor
        mkey = (mi.left, mi.top, mi.right, mi.bottom)
        if g.get("mon") != mkey or "small" not in g:
            g.clear()
            g["file"] = wp = self._wallpaper_file()
            g["mon"] = mkey
            g["small"] = None
            if wp:
                try:
                    from PIL import ImageFilter
                    src = Image.open(wp[0]).convert("RGB")
                    mw, mh = mkey[2] - mkey[0], mkey[3] - mkey[1]
                    style = "10"
                    try:
                        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                            r"Control Panel\Desktop") as k:
                            style = str(winreg.QueryValueEx(k, "WallpaperStyle")[0])
                    except OSError:
                        pass
                    if style == "2":  # stretch
                        full = src.resize((mw // 4, mh // 4), Image.Resampling.BILINEAR)
                    else:  # fill (default): crop to cover
                        sc = max(mw / src.width, mh / src.height) / 4
                        tmp = src.resize((max(1, round(src.width * sc)),
                                          max(1, round(src.height * sc))),
                                         Image.Resampling.BILINEAR)
                        ox, oy = (tmp.width - mw // 4) // 2, (tmp.height - mh // 4) // 2
                        full = tmp.crop((ox, oy, ox + mw // 4, oy + mh // 4))
                    g["small"] = full.filter(ImageFilter.GaussianBlur(9))
                except Exception as e:  # noqa: BLE001
                    log("could not read wallpaper:", repr(e))
        small = g["small"]
        if small is None:
            return None
        key = (wx, wy, self.WMAX, self.HMAX)
        if g.get("crop_key") != key:
            g["crop"] = _glass_crop(small, mkey, wx, wy, self.WMAX, self.HMAX)
            g["crop_key"] = key
        return g["crop"]

    def _glass_at(self, x, y, w, h):
        """Blurred wallpaper behind a screen rect (physical px), e.g. the menu; None without one.
        shortcut: uses the widget's monitor; a menu on another monitor gets the wrong piece."""
        if self._glass_bg() is None:
            return None
        return _glass_crop(self._glass["small"], self._glass["mon"], x, y, w, h)

    def _card(self, img, x0, y0, x1, y1, r, glass=None):
        """Card background (physical pixels) for the selected background style."""
        P, mode = self.pal, self.cfg["bg"]
        STYLE["see"] = mode != "solid"   # shadcn: buttons on a glass / translucent card
        if STYLE["neu"]:
            self._neu_card(img, x0, y0, x1, y1, r, mode, glass)
            return
        if mode == "clear":
            rr(img, x0, y0, x1, y1, r, P["surface"], alpha=150)
            self._card_border(img, x0, y0, x1, y1, r)
            return
        if mode == "glass":
            bg = self._glass_bg() if glass is None else glass
            if bg is not None:
                ix0, iy0 = int(math.floor(x0)), int(math.floor(y0))
                ix1, iy1 = int(math.ceil(x1)), int(math.ceil(y1))
                w, h = ix1 - ix0, iy1 - iy0
                part = bg.crop((ix0, iy0, ix1, iy1))
                part = Image.blend(part, Image.new("RGB", part.size, P["surface"]),
                                   0.5 if self.light else 0.58)
                layer = part.convert("RGBA")
                layer.putalpha(_rr_mask(w, h, x0 - ix0, y0 - iy0, x1 - ix0, y1 - iy0,
                                        max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))))
                img.alpha_composite(layer, (ix0, iy0))
                self._card_border(img, x0, y0, x1, y1, r)
                return
        if STYLE["shad"]:  # shadcn card: 1 px border + flat fill
            rr(img, x0, y0, x1, y1, r, P["border"])
            b = max(1.0, round(self.S))
            rr(img, x0 + b, y0 + b, x1 - b, y1 - b, r - b, P["surface"])
            return
        rr(img, x0, y0, x1, y1, r, P["surface"])

    def _neu_card(self, img, x0, y0, x1, y1, r, mode, glass=None):
        """Neumorphic card: flat RYOCS background. Glass: the wallpaper in the theme's glass tone
        plus the RYOCS glass sheen (light only, as in RYOCS); translucent: see-through."""
        ix0, iy0 = int(math.floor(x0)), int(math.floor(y0))
        w, h = int(math.ceil(x1)) - ix0, int(math.ceil(y1)) - iy0
        P = self.pal
        STYLE["glass"], STYLE["clear"], STYLE["surf"] = False, mode == "clear", tuple(P["surface"])
        if w <= 0 or h <= 0:
            return
        layer = Image.new("RGBA", (w, h), tuple(P["surface"]) + (255,))
        if mode == "glass":
            bg = self._glass_bg() if glass is None else glass
            if bg is not None:
                part = bg.crop((ix0, iy0, ix0 + w, iy0 + h)).convert("RGBA")
                if self.light:   # RYOCS glass: --bg is the glass tone with 30% white
                    tone = mix(P.get("cz", P["surface"]), (255, 255, 255), 0.3)
                    layer = Image.blend(part, Image.new("RGBA", (w, h), tone + (255,)), 0.45)
                    STYLE["glass"] = True
                else:
                    layer = Image.blend(part, layer, 0.58)
                    STYLE["clear"] = True   # dark glass: see-through buttons, no sheen (as in RYOCS)
        m = _rr_mask(w, h, x0 - ix0, y0 - iy0, x1 - ix0, y1 - iy0,
                     max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2)))
        if mode == "clear":
            m = m.point(lambda v: v * 150 // 255)
        layer.putalpha(m)
        img.alpha_composite(layer, (ix0, iy0))
        if STYLE["glass"]:
            neu_glass(img, x0, y0, x1, y1, r)

    def _card_border(self, img, x0, y0, x1, y1, r):
        """In shadcn, glass / translucent cards get a thin border too (outline only)."""
        if not STYLE["shad"]:
            return
        b = max(1.0, round(self.S))
        ix0, iy0 = int(math.floor(x0)), int(math.floor(y0))
        w, h = int(math.ceil(x1)) - ix0, int(math.ceil(y1)) - iy0
        r = min(r, STYLE["r"])
        outer = _rr_mask(w, h, x0 - ix0, y0 - iy0, x1 - ix0, y1 - iy0, r)
        inner = _rr_mask(w, h, x0 - ix0 + b, y0 - iy0 + b, x1 - ix0 - b, y1 - iy0 - b,
                         max(0.0, r - b))
        from PIL import ImageChops
        layer = Image.new("RGBA", (w, h), self.pal["border"] + (255,))
        layer.putalpha(ImageChops.subtract(outer, inner))
        img.alpha_composite(layer, (ix0, iy0))

    # ---------------- listening stats
    def _load_stats(self):
        try:
            with open(STATS_PATH, "r", encoding="utf-8") as f:
                s = json.load(f)
            if not isinstance(s.get("days"), dict):
                raise ValueError("no days")
            return s
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            set_aside(STATS_PATH, e)
        return {"days": {}}

    def _save_stats(self):
        try:
            save_json(STATS_PATH, self.stats, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            log("could not save stats:", repr(e))
        self._stat_saved = time.monotonic()

    def _stat_day(self):
        return self.stats["days"].setdefault(date.today().isoformat(),
                                             {"sec": 0.0, "plays": 0, "artists": {}})

    def _stat_tick(self, now, st, playing):
        prev, self._stat_t = self._stat_t, now
        if prev is None or not (playing and st.get("ok")):
            return
        delta = now - prev
        if not 0 < delta < 5:  # skip big gaps after sleep / freeze
            return
        d = self._stat_day()
        d["sec"] += delta
        hr = str(time.localtime().tm_hour)
        hs = d.setdefault("hours", {})
        hs[hr] = hs.get(hr, 0.0) + delta
        artist =(st.get("artist") or "").split(", ")[0].strip() or T("unknown")
        d["artists"][artist] = d["artists"].get(artist, 0.0) + delta
        if st.get("title"):
            tkey = f'{st["title"]}\t{artist}'
            tr = d.setdefault("tracks", {})
            tr[tkey] = tr.get(tkey, 0.0) + delta
        if now - self._stat_saved > 120:  # big file on the UI thread: not too often
            self._save_stats()

    def _index_imported(self):
        """One-off facts from the imported history: first listen day per artist."""
        first = {}
        for k in sorted(self.imported):
            for a in self.imported[k].get("artists", {}):
                if a not in first:
                    first[a] = k
        self.first_seen = first
        self._streak_cache = None

    def _since(self, artist):
        k = self.first_seen.get(artist)
        if not k:
            for day in sorted(self.stats["days"]):
                if artist in self.stats["days"][day].get("artists", {}):
                    k = day
                    break
        if not k:
            return ""
        return T("since", d=f"{T('months').split()[int(k[5:7]) - 1]} {k[:4]}")

    def _streaks(self):
        """(current streak, longest streak): consecutive days with at least 1 minute of music."""
        today = date.today()
        ck = (today, self.import_ver, len(self.stats["days"]),
              (self._stat_get(today.isoformat()) or {}).get("sec", 0) >= 60)
        if self._streak_cache and self._streak_cache[0] == ck:
            return self._streak_cache[1]
        days = sorted(k for k in set(self.imported) | set(self.stats["days"])
                      if (self._stat_get(k) or {}).get("sec", 0) >= 60)
        best = run = 0
        prev = None
        for k in days:
            d = date.fromisoformat(k)
            run = run + 1 if prev is not None and (d - prev).days == 1 else 1
            best = max(best, run)
            prev = d
        cur = 0
        if prev is not None and (today - prev).days <= 1:  # continues if listened today or yesterday
            cur = run
        res = (cur, best)
        self._streak_cache = (ck, res)
        return res

    def _last_year_today(self):
        """Most played track on this day last year: (track, artist, seconds) or None."""
        t = date.today()
        try:
            ly = t.replace(year=t.year - 1)
        except ValueError:  # 29 February
            ly = t.replace(year=t.year - 1, day=28)
        d = self._stat_get(ly.isoformat())
        if not d or not d.get("tracks"):
            return None
        name, sec = max(d["tracks"].items(), key=lambda kv: kv[1])
        title, _s, artist = name.partition("\t")
        return title, artist, sec

    # ---------------- search in listening history
    @staticmethod
    def _norm(text):
        """Ignore case and accent differences in search (İ/ı/ş/é…)."""
        t = (text or "").replace("İ", "i").replace("I", "ı").lower().replace("ı", "i")
        return "".join(c for c in unicodedata.normalize("NFKD", t)
                       if not unicodedata.combining(c))

    def _history_index(self):
        """Totals per track and artist (imported + live). Refreshed every few minutes."""
        ck = (self.import_ver, len(self.stats["days"]), int(time.time() // 300))
        if getattr(self, "_hidx", (None,))[0] == ck:
            return self._hidx[1]
        tracks, artists = {}, {}

        def bump(store, key, sec, plays, day):
            e = store.get(key)
            if e is None:
                e = store[key] = {"sec": 0.0, "plays": 0, "days": 0, "first": day, "last": day,
                                  "years": {}}
            e["sec"] += sec
            e["plays"] += plays
            e["days"] += 1
            e["first"] = min(e["first"], day)
            e["last"] = max(e["last"], day)
            y = day[:4]
            e["years"][y] = e["years"].get(y, 0.0) + sec

        for day in set(self.imported) | set(self.stats["days"]):
            d = self._stat_get(day) or {}
            counts = d.get("counts", {})
            per_artist = {}
            for tkey, sec in d.get("tracks", {}).items():
                n = counts.get(tkey, 0)
                bump(tracks, tkey, sec, n, day)
                a = tkey.partition("\t")[2]
                pa = per_artist.setdefault(a, [0.0, 0])
                pa[0] += sec
                pa[1] += n
            for a, (sec, n) in per_artist.items():
                bump(artists, a, sec, n, day)
        for k, e in tracks.items():
            e["norm"] = self._norm(k.replace("\t", " "))
        for k, e in artists.items():
            e["norm"] = self._norm(k)
        self._hidx = (ck, (tracks, artists))
        return tracks, artists

    def history_search(self, query, limit=150):
        """[(kind, key, record)] — matching artists first, then tracks (by time)."""
        tracks, artists = self._history_index()
        words = self._norm(query).split()
        if not words:
            return []
        def ok(e):
            return all(w in e["norm"] for w in words)
        arts = sorted(((k, e) for k, e in artists.items() if ok(e)), key=lambda kv: -kv[1]["sec"])
        trs = sorted(((k, e) for k, e in tracks.items() if ok(e)), key=lambda kv: -kv[1]["sec"])
        return ([("artist", k, e) for k, e in arts[:8]] +
                [("track", k, e) for k, e in trs[:limit]])

    def _year_data(self, year):
        y = str(year)
        total = plays = 0.0
        artists, tracks = {}, {}
        months, wdays, hours = [0.0] * 12, [0.0] * 7, [0.0] * 24
        good = []
        for k in set(self.imported) | set(self.stats["days"]):
            if not k.startswith(y):
                continue
            d = self._stat_get(k) or {}
            sec = d.get("sec", 0.0)
            total += sec
            plays += d.get("plays", 0)
            months[int(k[5:7]) - 1] += sec
            wdays[date.fromisoformat(k).weekday()] += sec
            for h, v in (d.get("hours") or {}).items():
                hours[int(h)] += v
            for a, v in d.get("artists", {}).items():
                artists[a] = artists.get(a, 0.0) + v
            for t, v in d.get("tracks", {}).items():
                tracks[t] = tracks.get(t, 0.0) + v
            if sec >= 60:
                good.append(date.fromisoformat(k))
        best = run = 0
        prev = None
        for dd in sorted(good):
            run = run + 1 if prev is not None and (dd - prev).days == 1 else 1
            best = max(best, run)
            prev = dd
        return {"total": total, "plays": int(plays), "months": months, "wdays": wdays,
                "hours": hours, "streak": best,
                "artists": sorted(artists.items(), key=lambda kv: -kv[1])[:5],
                "tracks": sorted(tracks.items(), key=lambda kv: -kv[1])[:5]}

    def _render_recap(self, year):
        """Wrapped-style yearly summary image (shareable, 2x resolution)."""
        yd = self._year_data(year)
        P = self.pal
        S = 2.0
        X = lambda v: v * S  # noqa: E731

        def font(name, px):
            return TextFont(name, px, S, bold=(name == "seguisb.ttf"))

        f = {"cap": font("seguisb.ttf", 12), "big": font("seguisb.ttf", 46),
             "mid": font("seguisb.ttf", 17), "row": font("segoeui.ttf", 15),
             "rowb": font("seguisb.ttf", 15), "sub": font("segoeui.ttf", 12),
             "num": font("seguisb.ttf", 15)}
        Wd, Hd = 420, 760
        img = Image.new("RGBA", (round(X(Wd)), round(X(Hd))), P["surface"] + (255,))
        # gradient from the primary color to the surface at the top
        top = Image.new("RGBA", img.size, P["primary_container"] + (255,))
        mask = Image.linear_gradient("L").resize(img.size).point(lambda v: 255 - v)
        img.paste(top, (0, 0), mask.point(lambda v: int(v * 0.85)))
        d = Drawer(img)
        on, var, pri = P["on_surface"] + (255,), P["on_surface_variant"] + (255,), \
            readable(P["primary"], P["surface"], P["on_surface"]) + (255,)
        x0 = 28
        d.text((X(x0), X(34)), T("wr_title", y=year).upper(), font=f["cap"], fill=pri,
               anchor="lm")
        hours_txt = fmt_dur(yd["total"])
        d.text((X(x0), X(84)), hours_txt, font=f["big"], fill=on, anchor="lm")
        d.text((X(x0), X(122)), f'{T("wr_total")}  ·  '
               f'{yd["plays"]:,}'.replace(",", ".") + f' {T("wr_plays")}', font=f["sub"],
               fill=var, anchor="lm")

        def section(y, title, rows, fmt_name):
            d.text((X(x0), X(y)), title, font=f["cap"], fill=pri, anchor="lm")
            best = rows[0][1] if rows else 1.0
            for i, (name, sec) in enumerate(rows):
                ry = y + 30 + i * 32
                d.text((X(x0), X(ry)), str(i + 1), font=f["num"], fill=pri, anchor="lm")
                main, sub = fmt_name(name)
                dur = fmt_dur(sec)
                avail = X(Wd - x0 - 24 - 30) - f["sub"].getlength(dur) - X(10)
                t1 = fit_text(main, f["rowb"], avail)
                d.text((X(x0 + 24), X(ry - 1)), t1, font=f["rowb"], fill=on, anchor="lm")
                rest = avail - f["rowb"].getlength(t1)
                if sub and rest > X(40):
                    d.text((X(x0 + 24) + f["rowb"].getlength(t1), X(ry)),
                           fit_text(" · " + sub, f["sub"], rest), font=f["sub"], fill=var,
                           anchor="lm")
                d.text((X(Wd - x0), X(ry)), dur, font=f["sub"], fill=var, anchor="rm")
                rr(img, X(x0 + 24), X(ry + 12), X(Wd - x0), X(ry + 14), X(1), P["on_surface"],
                   alpha=18)
                rr(img, X(x0 + 24), X(ry + 12), X(x0 + 24 + (Wd - 2 * x0 - 24) * sec / best),
                   X(ry + 14), X(1), P["primary"], alpha=210)
            return y + 30 + 5 * 32

        y = section(170, T("wr_artists"), yd["artists"], lambda n: (n, ""))
        y = section(y + 12, T("wr_tracks"), yd["tracks"],
                    lambda n: (n.partition("\t")[0], n.partition("\t")[2]))
        months = T("months_full").split()
        wnames = T("weekdays").split()
        facts = [
            (T("wr_month"), months[max(range(12), key=lambda i: yd["months"][i])]
             if yd["total"] else "–"),
            (T("wr_day"), wnames[max(range(7), key=lambda i: yd["wdays"][i])]
             if yd["total"] else "–"),
            (T("wr_hour"), f'{max(range(24), key=lambda i: yd["hours"][i]):02d}:00'
             if any(yd["hours"]) else "–"),
            (T("wr_streak"), T("st_days", n=yd["streak"])),
        ]
        tw, th = (Wd - 2 * x0 - 10) / 2, 62
        for i, (lbl, val) in enumerate(facts):
            tx = x0 + (i % 2) * (tw + 10)
            ty = y + 14 + (i // 2) * (th + 10)
            rr(img, X(tx), X(ty), X(tx + tw), X(ty + th), X(16), P["surface_high"])
            d.text((X(tx + 14), X(ty + 22)), fit_text(val, f["mid"], X(tw - 24)), font=f["mid"],
                   fill=on, anchor="lm")
            d.text((X(tx + 14), X(ty + 44)), lbl, font=f["sub"], fill=var, anchor="lm")
        h = y + 14 + 2 * (th + 10) + 18
        return img.crop((0, 0, img.width, round(X(h)))).convert("RGB")

    def _share_link(self):
        st = self._view()
        cur = self.web.current
        if cur and cur.get("id") and cur.get("name") == st.get("title"):
            url = "https://open.spotify.com/track/" + cur["id"]
        else:
            url = "https://open.spotify.com/search/" + urllib.parse.quote(
                f'{st.get("title", "")} {st.get("artist", "")}'.strip())
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self.toast.show_message("", T("sh_link_ok"))  # link icon

    def _share_card(self):
        """1080x1350 share card with cover + title: copies to the clipboard and saves to Pictures."""
        from PIL import ImageFilter
        st = self._view()
        if not st.get("title"):
            return
        P, S = self.pal, 1080 / 400
        X = lambda v: v * S  # noqa: E731
        Wd, Hd = 400, 500
        img = Image.new("RGB", (1080, 1350), P["surface"])
        if self.art_src is not None:  # blurred cover background
            bgi = self.art_src.resize((1350, 1350), Image.Resampling.BILINEAR).crop(
                (135, 0, 1215, 1350)).filter(ImageFilter.GaussianBlur(60))
            img = Image.blend(bgi, Image.new("RGB", img.size, P["surface"]), 0.45)
        img = img.convert("RGBA")
        d = Drawer(img)
        asz = 280
        ax, ay = (Wd - asz) / 2, 50
        if self.art_src is not None:
            art = rounded_image(self.art_src, round(X(asz)), X(26))
            shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
            rr(shadow, X(ax), X(ay + 10), X(ax + asz), X(ay + asz + 10), X(26), (0, 0, 0),
               alpha=110)
            img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(30)))
            img.alpha_composite(art, (round(X(ax)), round(X(ay))))
        else:
            rr(img, X(ax), X(ay), X(ax + asz), X(ay + asz), X(26), P["surface_highest"])

        def font(name, px):
            return TextFont(name, px, S, bold=(name == "seguisb.ttf"))

        ft, fa, fc = font("seguisb.ttf", 24), font("segoeui.ttf", 16), font("seguisb.ttf", 11)
        y = ay + asz + 40
        for line in wrap_text(st["title"], ft, X(Wd - 60), 2):
            d.text((X(Wd / 2), X(y)), line, font=ft, fill=P["on_surface"] + (255,), anchor="mm")
            y += 30
        d.text((X(Wd / 2), X(y + 4)), fit_text(st.get("artist") or "", fa, X(Wd - 60)), font=fa,
               fill=P["on_surface_variant"] + (255,), anchor="mm")
        d.text((X(Wd / 2), X(Hd - 26)), "♫  " + T("playing"), font=fc,
               fill=readable(P["primary"], P["surface"], P["on_surface"]) + (255,), anchor="mm")
        out = img.convert("RGB")
        copy_image_to_clipboard(out)
        try:
            pics = os.path.join(os.path.expanduser("~"), "Pictures", "Coverglow")
            os.makedirs(pics, exist_ok=True)
            safe = "".join(c for c in st["title"] if c not in '<>:"/\\|?*')[:60].strip() or "song"
            out.save(os.path.join(pics, f"{safe}.png"))
        except Exception as e:  # noqa: BLE001
            log("could not save card:", repr(e))
        self.toast.show_message("", T("sh_card_ok"))  # image icon

    def _fill_devices(self, menu):
        """Fetch devices when the Transfer to device submenu opens."""
        menu.delete(0, "end")
        if not self.web.connected:
            menu.add_command(label=T("s_need"), command=lambda: ConnectDialog(self))
            return
        try:
            devs = self.web.devices()
        except Exception as e:  # noqa: BLE001
            log("devices:", repr(e))
            menu.add_command(label=T("dev_err"), state="disabled")
            return
        if not devs:
            menu.add_command(label=T("dev_none"), state="disabled")
        for dv in devs:
            menu.add_command(label=("● " if dv["active"] else "   ") + f'{dv["name"]}  ({dv["type"]})',
                             command=lambda i=dv["id"]: self._transfer(i))

    def _transfer(self, device_id):
        if not self.web.has_control:
            ConnectDialog(self, reauth=True)
            return

        def go():
            try:
                self.web.transfer(device_id)
            except Exception as e:  # noqa: BLE001
                log("could not transfer to device:", repr(e))

        threading.Thread(target=go, daemon=True).start()

    STAT_PERIODS = ("7", "30", "365", "all")

    def _stat_get(self, key):
        """One day's data: if live tracking and imported history overlap the bigger one wins
        (so the same listen isn't counted twice)."""
        a, b = self.stats["days"].get(key), self.imported.get(key)
        if a is None or (b is not None and b["sec"] > a.get("sec", 0)):
            return b
        return a

    def _stats_summary(self):
        period = self.cfg["stats_period"]
        today = date.today()
        ck = (period, today, int(time.time() // 60), self.import_ver)
        if getattr(self, "_sum_cache", (None,))[0] == ck:
            return self._sum_cache[1]
        end = today
        if period == "7":
            start = today - timedelta(days=6)
        elif period == "30":
            start = today - timedelta(days=29)
        elif period == "365":
            m = today.month - 11
            start = date(today.year + (m - 1) // 12, (m - 1) % 12 + 1, 1)
        elif period.startswith("y"):  # a single year (months)
            yy = int(period[1:])
            start, end = date(yy, 1, 1), min(today, date(yy, 12, 31))
        elif period.startswith("m"):  # a single month (days)
            yy, mo = int(period[1:5]), int(period[6:8])
            start = date(yy, mo, 1)
            nxt = date(yy + mo // 12, mo % 12 + 1, 1)
            end = min(today, nxt - timedelta(days=1))
        else:
            keys = list(self.imported) + list(self.stats["days"])
            start = date.fromisoformat(min(keys)) if keys else today
        s0, s1 = start.isoformat(), end.isoformat()

        # chart buckets; targets: period opened on click (month / year)
        names = T("days").split()
        months = T("months").split()
        targets = None
        if period in ("7", "30") or period.startswith("m"):
            if period.startswith("m"):
                n = (end - start).days + 1
                bdays = [start + timedelta(days=i) for i in range(n)]
                labels = [str(d.day) if d.day in (1, 8, 15, 22, 29) else "" for d in bdays]
            else:
                n = 7 if period == "7" else 30
                bdays = [today - timedelta(days=n - 1 - i) for i in range(n)]
                if n == 7:
                    labels = [names[d.weekday()] for d in bdays]
                else:
                    labels = [str(d.day) if (n - 1 - i) % 7 == 0 else ""
                              for i, d in enumerate(bdays)]
            bkeys = [d.isoformat() for d in bdays]
            bucket = lambda k: k  # noqa: E731
        elif period == "365" or period.startswith("y"):
            bkeys, labels = [], []
            y, mo = start.year, start.month
            for _ in range(12 if period == "365" else end.month):
                bkeys.append(f"{y:04d}-{mo:02d}")
                labels.append(months[mo - 1])
                y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
            targets = ["m" + k for k in bkeys]
            bucket = lambda k: k[:7]  # noqa: E731
        else:
            bkeys = [str(y) for y in range(start.year, today.year + 1)]
            labels = [k if len(bkeys) <= 7 else "'" + k[2:] for k in bkeys]
            targets = ["y" + k for k in bkeys]
            bucket = lambda k: k[:4]  # noqa: E731
        cur = {today.isoformat(), today.isoformat()[:7], today.isoformat()[:4]}

        def add(acc, d, k):
            sec = d.get("sec", 0.0)
            acc[0] += sec
            acc[1] += d.get("plays", 0)
            b = bucket(k)
            if b in acc[2]:
                acc[2][b] += sec
            for a, v in d.get("artists", {}).items():
                acc[3][a] = acc[3].get(a, 0.0) + v
            for t, v in d.get("tracks", {}).items():
                acc[4][t] = acc[4].get(t, 0.0) + v
            hours = d.get("hours")
            if hours:
                row = date.fromisoformat(k).weekday() * 24
                g = acc[5]
                for h, v in hours.items():
                    g[row + int(h)] += v

        # imported (unchanging) part is cached; live days are added every time
        live = [k for k in self.stats["days"] if s0 <= k <= s1]
        bk = (period, today, self.import_ver, tuple(sorted(live)))
        if getattr(self, "_imp_cache", (None,))[0] != bk:
            acc = [0.0, 0, dict.fromkeys(bkeys, 0.0), {}, {}, [0.0] * 168]
            skip = set(live)
            for k, d in self.imported.items():
                if s0 <= k <= s1 and k not in skip:
                    add(acc, d, k)
            self._imp_cache = (bk, acc)
        base = self._imp_cache[1]
        acc = [base[0], base[1], dict(base[2]), dict(base[3]), dict(base[4]), list(base[5])]
        for k in live:
            add(acc, self._stat_get(k), k)
        total, plays, bvals, artists, tracks, grid = acc
        td = self._stat_get(s1) or {}
        if period == "7":
            tiles = [(fmt_dur(td.get("sec", 0.0)), T("st_today")),
                     (fmt_dur(total), T("st_week")), (str(td.get("plays", 0)), T("st_songs"))]
        else:
            ndays = max(1, (end - start).days + 1)
            tiles = [(fmt_dur(total), T("st_total")),
                     (f"{int(plays):,}".replace(",", "."), T("st_plays")),
                     (fmt_dur(total / ndays), T("st_avg"))]
            if period == "all":  # all time: listening streak instead of daily average
                cur_s, best_s = self._streaks()
                tiles[2] = (T("st_streak_v", n=cur_s), T("st_streak", n=best_s))
        top_a = sorted(artists.items(), key=lambda kv: -kv[1])[:3]
        top_t = [(t.split("\t")[0], t.split("\t")[1] if "\t" in t else "", v)
                 for t, v in sorted(tracks.items(), key=lambda kv: -kv[1])[:3]]
        # title of the opened period and one level up
        title = up = None
        if period.startswith("y"):
            title, up = period[1:], "all"
        elif period.startswith("m"):
            title, up = f"{months[int(period[6:8]) - 1]} {period[1:5]}", "y" + period[1:5]
        res = {"tiles": tiles, "top_a": top_a, "top_t": top_t, "title": title, "up": up,
               "grid": grid,
               "bars": [(labels[i], bvals[k], targets[i] if targets else None, k in cur)
                        for i, k in enumerate(bkeys)]}
        self._sum_cache = (ck, res)
        return res

    def _import_data(self):
        from tkinter import filedialog, messagebox
        paths = filedialog.askopenfilenames(
            parent=self.root, title=T("imp_title"),
            filetypes=[("Spotify", "*.zip *.json"), ("*", "*.*")])
        if not paths:
            return
        result = {}

        def work():
            try:
                result["r"] = import_spotify_history(list(paths))
            except Exception as e:  # noqa: BLE001
                result["e"] = e

        th = threading.Thread(target=work, daemon=True)
        th.start()

        def wait():
            if th.is_alive():
                self.root.after(200, wait)
                return
            if "e" in result:
                log("import:", repr(result["e"]))
                messagebox.showerror(T("imp_title"), T("imp_fail", e=result["e"]),
                                     parent=self.root)
                return
            r = result.get("r")
            if not r:
                messagebox.showwarning(T("imp_title"), T("imp_none") + "\n\n" + T("imp_hint"),
                                       parent=self.root)
                return
            self.imported = load_imported_history()
            self.import_ver += 1
            self._index_imported()
            self.cfg["list_tab"], self.cfg["stats_period"] = "stats", "all"
            self.cfg["queue_collapsed"] = False
            self.cfg.save()
            self.base_key = None
            messagebox.showinfo(T("imp_title"), T("imp_ok", n=f"{r['n']:,}".replace(",", "."),
                                                  h=f"{r['hours']:,}".replace(",", "."),
                                                  a=r["first"], b=r["last"]), parent=self.root)

        wait()

    # ---------------- drawing
    def _render_all(self):
        st = self._view()
        connected, queue, qerr, qver = self.web.snapshot()
        playing = self._playing()
        key = (st.get("ok"), st.get("title"), st.get("artist"), st.get("album"), playing,
               st.get("shuffle"), st.get("repeat"), self.hover, self.pressed,
               self.art_version, self.pal_version, round(self.play_r, 1), connected, qver,
               self.cfg["show_queue"], self.cfg["queue_collapsed"], self.light,
               self.S, self.cfg["queue_rows"], self.mini, self._vol_shown(),
               self._sleep_minutes(), LANG, self.cfg["list_tab"], len(self.history),
               self.history[0].get("t") if self.history else None,
               int(time.time() // 60) if self.cfg["list_tab"] != "queue" else 0,
               self.cfg["accent"], self.cfg["bg"], self.glass_ver,
               self.cfg["stats_period"], self.cfg["stats_list"], self.import_ver,
               self.hist_off if self.cfg["list_tab"] == "recent" else 0,
               (self.lyrics.version, self._lyric_index())
               if self.cfg["list_tab"] == "lyrics" and not self.mini else None,
               self._win_pos() if self.cfg["bg"] == "glass" else None)
        full = False
        if key != self.base_key:
            self.base_key = key
            if self.mini:
                self.base, self.hits = self._render_mini()
            else:
                self.marq = None
                self.base, self.hits = self._render_base(connected, queue, qerr)
            self.marq_key = None
            self._keep_on_screen()
            self.base_strip = self.base.crop((0, self.sy0, self.WP, self.sy0 + self.sh))
            full = True
        if self._morph and not full:  # while a transition runs _morph_step pushes the frames
            return
        pos, end = self._position()
        if self.drag_seek is not None:
            frac = self.drag_seek
            shown = frac * end
        else:
            frac = pos / end if end > 0 else 0.0
            shown = pos
        frac = max(0.0, min(1.0, frac))
        skey = (round(frac * (BAR_X1 - BAR_X0) * self.S), int(shown), round(self.amp, 2),
                round(self.phase % LAM, 1), self.hover == "seek", self.drag_seek is not None)
        if full or skey != self.strip_key:
            self.strip_key = skey
            strip = self._render_strip(frac, shown, end)
            if full:
                frame = self.base.copy()
                frame.paste(strip, (0, self.sy0))
                if self.marq:
                    off = self._marq_offset()
                    self.marq_key = off
                    frame.paste(self._render_marquee(off), (self.marq["x"], self.marq["y0"]))
                self._frame = frame
                if not self._morph:  # while the transition animation runs it pushes the frames
                    self.surface.full(frame, round(self.content_h * self.S) + 4)
            elif not self._morph:
                self.surface.region(strip, 0, self.sy0)
        if self.marq and not full and not self._morph:
            off = self._marq_offset()
            if off != self.marq_key:
                self.marq_key = off
                self.surface.region(self._render_marquee(off), self.marq["x"], self.marq["y0"])

    def _keep_on_screen(self):
        """When the height changes (mini ↔ normal, tab, row count): if the widget is in the bottom
        half of the screen it keeps its bottom edge and grows upward; it never goes under the
        taskbar."""
        new_h = round(self.content_h * self.S)
        old_h = getattr(self, "_shown_h", None)
        self._shown_h = new_h
        if old_h is None or old_h == new_h or self.win_drag:
            return
        x, y = self._win_pos()
        if x <= -20000:  # not placed yet
            return
        wa = monitor_info(self.hwnd).rcWork
        ny = y
        bottom = y + old_h / 2 > (wa.top + wa.bottom) / 2  # bottom half: anchor the bottom
        if bottom:
            ny = y + old_h - new_h
        ny = max(wa.top, min(ny, wa.bottom - new_h))
        self._anchor = {"bottom": bottom, "deferred": None}
        if ny != y:
            if self._morph == "pending":
                # don't move the window separately during a transition: it moves with the drawing on the
                # first frame when growing, the last when shrinking (else the old image flashes in the wrong place)
                self._anchor["deferred"] = (x, ny)
                return
            move_window(self.hwnd, x, ny)
            self.cfg["x"], self.cfg["y"] = x, ny
            self.cfg.save()

    def _memory_tick(self, now, st, playing):
        """Once a day, the first time music starts that day: an 'on this day last year' memory.
        Not shown while in a game (fullscreen); it waits for the next good moment."""
        if not (playing and self.cfg["memory"]) or self.pending:
            return
        today = date.today().isoformat()
        if self.cfg.get("memory_day") == today or now - self._start_t < 8:
            return
        if now - getattr(self, "_mem_check", 0) < 30:  # don't check fullscreen too often
            return
        self._mem_check = now
        ly = self._last_year_today()
        if not ly:
            self.cfg["memory_day"] = today
            return
        if self.inside or fullscreen_app((self.hwnd, self.toast.hwnd)) is not None:
            return
        self.cfg["memory_day"] = today
        self.cfg.save()
        title, artist, _sec = ly
        self.toast.show_message(IC_CAL, T("lastyear").upper(), None, hold=6000,
                                sub=f"{title} · {artist}",
                                on_click=lambda: self._spotify_search(f"{title} {artist}"))

    def _like_hotkey(self):
        """Ctrl+Alt+L: like / unlike the current track and show a short heart indicator
        (oyundayken de)."""
        st, web = self.st, self.web
        if not st.get("ok"):
            return
        if not (web.connected and web.has_library):
            ConnectDialog(self, reauth=web.connected)
            return
        fs = fullscreen_app((self.hwnd, self.toast.hwnd))
        title = st.get("title") or ""
        if not web.toggle_like(title):  # Spotify hasn't sent the track id yet
            web.refresh_now()
            self.toast.show_message(IC_HEART_O, T("like_wait"), fs, hold=1400)
            return
        self.base_key = None
        liked = web.liked_state(title)
        if not self.inside:
            self.toast.show_message(IC_HEART if liked else IC_HEART_O,
                                    (T("osd_liked") if liked else T("osd_unliked"))
                                    + (f" · {title}" if title else ""), fs, hold=1300)

    def _hotkey_osd(self, act):
        """Briefly show the hotkey action on screen (if the widget isn't visible: fullscreen
        app or another window in front)."""
        if not self.cfg["osd"] or self.inside:
            return
        fs = fullscreen_app((self.hwnd, self.toast.hwnd))
        if fs is None and self.cfg["mode"] == "desktop":
            fg = user32.GetForegroundWindow()
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(fg, cls, 64)
            if not fg or cls.value in _SHELL_CLASSES:  # desktop in front: widget is already visible
                return
        elif fs is None:
            return  # always on top: widget is already visible
        if act in ("vol+", "vol-"):
            v = self.vol_val if self.vol_val is not None else self.volume.get()
            if v is not None:
                self.toast.show_message(IC_MUTE if v <= 0 else IC_VOL, T("osd_vol"), fs,
                                        value=v, hold=1000)
        elif act == "play":
            playing = not self.st.get("playing")  # command not processed yet: it will be the opposite
            title = self.st.get("title") or ""
            self.toast.show_message(IC_PAUSE if playing else IC_PLAY,
                                    (T("osd_play") if playing else T("osd_pause"))
                                    + (f" · {title}" if title else ""), fs)
        elif act in ("next", "prev") and not self.cfg["notify"]:
            # if toasts are on the new track is shown anyway
            self.toast.show_message(IC_NEXT if act == "next" else IC_PREV,
                                    T("hk_next" if act == "next" else "hk_prev"), fs, hold=900)

    # ---------------- audio: react to music, duck for Discord
    def _vol_mark(self, v):
        """Remember the user's volume while Coverglow has turned Spotify down (None: back to normal),
        so a forced close (installer, shutdown, crash) doesn't leave it low."""
        if self.cfg.get("vol_restore") != v:
            if v is None:
                self.cfg.pop("vol_restore", None)
            else:
                self.cfg["vol_restore"] = v
            self.cfg.save()

    def _audio_tick(self, now, st, playing):
        if self._vol_pending is not None and st.get("ok") and self.volume.get() is not None:
            log("restoring Spotify volume left low by the last run")
            self.volume.set(self._vol_pending)
            self._vol_pending = None
            self._vol_mark(None)
        # wave reacting to music: Spotify's output level (0..1 with auto gain)
        if self.cfg["react"] and self.cfg["wave"] and playing and not self.mini:
            p = self.volume.peak(("spotify.exe",))
            # Mastered music keeps peaks high all the time; scaling by the max keeps the wave
            # flat. Instead scale by the min–max range of the last few seconds:
            # beats swing between 0↔1.
            if self._lvl_lo is None:
                self._lvl_lo, self._lvl_hi = p * 0.6, p
            lo, hi = self._lvl_lo, self._lvl_hi
            # frame-rate independent: ratio relative to one frame at 60 fps
            last_t = getattr(self, "_lvl_t", None)
            k = max(0.2, min(3.0, (now - last_t) * 60)) if last_t else 1.0
            self._lvl_t = now
            hi = max(p, hi + (lo - hi) * 0.015 * k)   # ceiling slowly drops
            lo = min(p, lo + (hi - lo) * 0.015 * k)   # floor slowly rises
            self._lvl_lo, self._lvl_hi = lo, hi
            target = 0.0 if p < 0.01 else min(1.0, max(0.0, (p - lo) / max(hi - lo, 0.04)))
            f = (0.7 if target > self.level else 0.3) * k  # rise fast, fall slowly
            self.level += (target - self.level) * min(1.0, f)
        else:
            self.level *= 0.8
            self._lvl_lo = None

        # duck Spotify when someone talks on Discord, restore when they stop
        # (if Spotify isn't playing there's nothing to duck: don't scan audio sessions at all)
        if not playing and not self._duck and not self._duck_fade:
            return
        if not self.cfg["duck"] or self.sleep_fade is not None or self._alarm_fade:
            if self._duck:
                self._duck_restore()
            return
        if self._duck_fade:  # restore (smooth)
            f = self._duck_fade
            t = min(1.0, (now - f["t0"]) / 0.8)
            self.volume.set(f["from"] + (f["to"] - f["from"]) * t)
            if t >= 1.0:
                self._duck_fade = None
                self._vol_mark(None)
        talking = self.volume.peak(tuple(self.cfg["duck_apps"])) > 0.015
        if talking:
            if self._duck is None and st.get("ok"):
                v = self.volume.get()
                # talk resumed mid-restore: the real volume is the fade's target, not the half-way one
                v = self._duck_fade["to"] if self._duck_fade else v
                if v is not None and v > 0.02:
                    self._duck_fade = None
                    self._duck = {"orig": v, "low": v * 0.3, "last": now}
                    self.volume.set(v * 0.3)
                    self._vol_mark(v)
            elif self._duck:
                self._duck["last"] = now
        elif self._duck and now - self._duck["last"] > 1.2:
            self._duck_restore()

    def _duck_restore(self):
        d, self._duck = self._duck, None
        cur = self.volume.get()
        # only a raise means the user changed it; lower (e.g. Discord's own attenuation) still restores
        if cur is not None and cur < d["low"] + 0.03:
            self._duck_fade = {"from": cur, "to": d["orig"], "t0": time.monotonic()}
        else:
            self._vol_mark(None)

    # ---------------- alarm
    def _alarm_tick(self, st):
        al = self.cfg["alarm"]
        now = time.localtime()
        if al.get("on") and time.strftime("%H:%M", now) == al.get("time") \
                and self._alarm_day != now.tm_yday:
            self._alarm_day = now.tm_yday
            log("alarm:", al.get("time"))
            if not st.get("ok"):
                open_spotify()
            self._alarm_fade = {"t0": None, "target": None, "deadline": time.monotonic() + 90}
            self.toast.show_message(IC_ALARM, T("al_fired"), fullscreen_app(
                (self.hwnd, self.toast.hwnd)))
        f = self._alarm_fade
        if not f:
            return
        mono = time.monotonic()
        if f["t0"] is None:  # wait for Spotify to open, then lower the volume and play
            if mono > f["deadline"]:
                self._alarm_fade = None
                return
            if st.get("ok"):
                v = self.volume.get()
                if v is None:
                    return
                f["target"] = v if v > 0.1 else 0.6
                self.volume.set(0.02)
                self._vol_mark(f["target"])
                if not st.get("playing"):
                    self.media.command("play")
                f["t0"] = mono
            return
        t = min(1.0, (mono - f["t0"]) / 60.0)  # raise the volume over 1 minute
        self.volume.set(0.02 + (f["target"] - 0.02) * t)
        if t >= 1.0:
            self._alarm_fade = None
            self._vol_mark(None)

    def _vol_shown(self):
        if self.vol_val is not None and time.monotonic() < self.vol_until:
            return round(self.vol_val * 100)
        return None

    def _draw_volume(self, img, d, x0, y0, size, radius):
        """Volume indicator on top of the cover."""
        v = self._vol_shown()
        if v is None:
            return
        S = self.S
        X = lambda q: q * S  # noqa: E731
        rr(img, X(x0), X(y0), X(x0 + size), X(y0 + size), X(radius), (0, 0, 0), alpha=150)
        white = (255, 255, 255, 255)
        cx = x0 + size / 2
        big = size >= 90
        glyph = "" if v == 0 else ""  # muted / speaker
        if big:
            d.text((X(cx), X(y0 + size / 2 - 16)), glyph, font=self.icon_font(24), fill=white,
                   anchor="mm")
            d.text((X(cx), X(y0 + size / 2 + 14)), f"%{v}", font=self.F["title"], fill=white,
                   anchor="mm")
        else:
            d.text((X(cx), X(y0 + size / 2 - 3)), f"%{v}", font=self.F["btn"], fill=white,
                   anchor="mm")
        # bar at the bottom
        m = 12 if big else 8
        by = y0 + size - (14 if big else 9)
        rr(img, X(x0 + m), X(by), X(x0 + size - m), X(by + 4), X(2), (255, 255, 255), alpha=70)
        if v > 0:
            rr(img, X(x0 + m), X(by), X(x0 + m + (size - 2 * m) * v / 100), X(by + 4), X(2),
               (255, 255, 255))

    MARQ_PAUSE = 2.0   # pause at the start (s)
    MARQ_SPEED = 32    # px/s (unscaled)
    MARQ_GAP = 48      # gap between repeats

    def _make_marquee(self, title, x, cy, w, tlen, color):
        S, F = self.S, self.F
        h = round(26 * S)
        gap = round(self.MARQ_GAP * S)
        span = round(tlen) + gap
        layer = Image.new("RGBA", (span * 2 + 2, h), (0, 0, 0, 0))
        d = Drawer(layer)
        for k in (0, 1):  # two copies for a seamless loop
            d.text((k * span, h / 2), title, font=F["mtitle"], fill=color + (255,), anchor="lm")
        w = round(w)
        fade = max(1, round(12 * S))
        ramp = [round(255 * i / fade) for i in range(fade)]
        right = Image.new("L", (w, 1), 255)
        right.putdata([255] * (w - fade) + ramp[::-1])
        both = right.copy()
        both.putdata(ramp + [255] * (w - 2 * fade) + ramp[::-1])
        prev = self.marq
        self.marq = {"title": title, "x": round(x), "y0": round(cy - h / 2), "w": w, "h": h,
                     "span": span, "layer": layer,
                     "masks": (right.resize((w, h)), both.resize((w, h))),
                     "t0": prev["t0"] if prev and prev["title"] == title else time.monotonic()}

    def _marq_offset(self):
        m = self.marq
        speed = self.MARQ_SPEED * self.S
        cycle = self.MARQ_PAUSE + m["span"] / speed
        t = (time.monotonic() - m["t0"]) % cycle
        return 0 if t < self.MARQ_PAUSE else round((t - self.MARQ_PAUSE) * speed) % m["span"]

    def _render_marquee(self, off):
        m = self.marq
        x, y0, w, h = m["x"], m["y0"], m["w"], m["h"]
        region = self.base.crop((x, y0, x + w, y0 + h))
        text = m["layer"].crop((off, 0, off + w, h))
        a = text.getchannel("A")
        text.putalpha(Image.composite(a, Image.new("L", a.size, 0),
                                      m["masks"][1 if off else 0]))
        region.alpha_composite(text)
        return region

    def _render_mini(self):
        """Mini mode: pill view with cover, title and controls."""
        S, P, F = self.S, self.pal, self.F
        X = lambda v: v * S  # noqa: E731
        img = Image.new("RGBA", (self.WMAX, self.HMAX), (0, 0, 0, 0))
        d = Drawer(img)
        hits = []
        self.content_h = MINI_H
        st = self._view()
        ok, offline = st.get("ok"), bool(st.get("offline"))
        playing = self._playing()
        hov = self.hover

        self._card(img, 0, 0, X(W), X(MINI_H), X(MINI_H / 2))
        # cover
        ax = ay = 12
        if self.art_mini is not None:
            img.alpha_composite(self.art_mini, (round(X(ax)), round(X(ay))))
        else:
            rr(img, X(ax), X(ay), X(ax + MINI_ART), X(ay + MINI_ART), X(16),
               P["surface_highest"])
            d.text((X(ax + MINI_ART / 2), X(ay + MINI_ART / 2)), IC_MUSIC,
                   font=self.icon_font(22), fill=P["on_surface_variant"] + (255,), anchor="mm")
        if hov == "art" and self._vol_shown() is None:
            rr(img, X(ax), X(ay), X(ax + MINI_ART), X(ay + MINI_ART), X(16), (0, 0, 0),
               alpha=110)
            d.text((X(ax + MINI_ART / 2), X(ay + MINI_ART / 2)),
                   IC_INFO if self.st.get("ok") else IC_OPEN,
                   font=self.icon_font(18), fill=(255, 255, 255, 255), anchor="mm")
        self._draw_volume(img, d, ax, ay, MINI_ART, 16)
        hits.append(("art", ax, ay, ax + MINI_ART, ay + MINI_ART))

        # controls (right)
        cy = MINI_H / 2 - 2
        nx, px_, vx = W - 34, W - 84, W - 134
        for hid, bx, glyph in (("prev", vx, IC_PREV), ("next", nx, IC_NEXT)):
            if hov == hid:
                rr(img, X(bx - 20), X(cy - 20), X(bx + 20), X(cy + 20), 0, P["on_surface"],
                   alpha=26, ellipse=True)
            d.text((X(bx), X(cy)), glyph, font=self.icon_font(17),
                   fill=P["on_surface_variant"] + (255,), anchor="mm")
            hits.append((hid, bx - 20, cy - 20, bx + 20, cy + 20))
        pw, ph = 52, 44
        col = P["primary"]
        if hov == "play":
            col = mix(col, P["on_primary"], 0.10)
        if STYLE["neu"]:
            neu_btn(img, X(px_ - pw / 2), X(cy - ph / 2), X(px_ + pw / 2), X(cy + ph / 2),
                      X(min(22.0, self.play_r * 0.8)),
                    mix(P["secondary_container"], P["on_surface"], 0.06 if hov == "play" else 0))
        else:
            rr(img, X(px_ - pw / 2), X(cy - ph / 2), X(px_ + pw / 2), X(cy + ph / 2),
               X(min(22.0, self.play_r * 0.8)), col)
        d.text((X(px_), X(cy)), IC_PAUSE if playing else IC_PLAY, font=self.icon_font(19),
               fill=(P["on_surface"] if STYLE["neu"] else P["on_primary"]) + (255,), anchor="mm")
        hits.append(("play", px_ - pw / 2, cy - ph / 2, px_ + pw / 2, cy + ph / 2))

        # text
        tx = ax + MINI_ART + 12
        tw = X(vx - 24 - tx)
        if ok or offline:
            title, sub = st["title"] or T("unknown"), st["artist"]
        else:
            title, sub = T("waiting_title"), T("mini_waiting_sub")
        tlen = F["mtitle"].getlength(title)
        if tlen > tw:  # doesn't fit: drawn on a separate layer scrolling sideways
            self._make_marquee(title, X(tx), X(cy - 9), tw, tlen, P["on_surface"])
        else:
            self.marq = None
            d.text((X(tx), X(cy - 9)), title, font=F["mtitle"],
                   fill=P["on_surface"] + (255,), anchor="lm")
        if offline:
            sub, sub_col = T("mini_offline"), P["primary"]
            hits.append(("open", tx, cy + 2, tx + tw / S, cy + 20))
        else:
            sub_col = P["on_surface_variant"]
            mins = self._sleep_minutes()
            if mins:
                sub = T("sleep_sub", m=mins) + (sub or "")
        d.text((X(tx), X(cy + 11)), fit_text(sub or "", F["album"], tw), font=F["album"],
               fill=sub_col + (255,), anchor="lm")
        return img, hits

    def _render_base(self, connected, queue, qerr):
        S, P, F = self.S, self.pal, self.F
        img = Image.new("RGBA", (self.WMAX, self.HMAX), (0, 0, 0, 0))
        self.content_h = MAIN_H
        d = Drawer(img)
        hits = []
        X = lambda v: v * S  # noqa: E731
        st = self._view()
        ok = st.get("ok")
        offline = bool(st.get("offline"))  # Spotify closed, showing the last played
        playing = self._playing()
        hov = self.hover

        def state_layer(x0, y0, x1, y1, r, on, ellipse=False, strength=0.10):
            rr(img, X(x0), X(y0), X(x1), X(y1), X(r), on, alpha=round(255 * strength),
               ellipse=ellipse)

        def icon(cx, cy, glyph, px, color):
            d.text((X(cx), X(cy)), glyph, font=self.icon_font(px), fill=tuple(color) + (255,),
                   anchor="mm")

        # main card
        self._card(img, 0, 0, X(W), X(MAIN_H), X(28))

        # cover
        ax, ay, asz = 16, 16, 120
        if STYLE["neu"]:
            neu_raise(img, X(ax), X(ay), X(ax + asz), X(ay + asz), X(20))
        if self.art_rgba is not None:
            img.alpha_composite(self.art_rgba, (round(X(ax)), round(X(ay))))
        else:
            rr(img, X(ax), X(ay), X(ax + asz), X(ay + asz), X(20), P["surface_highest"])
            icon(ax + asz / 2, ay + asz / 2, IC_MUSIC, 40, P["on_surface_variant"])
        if hov == "art" and self._vol_shown() is None:
            rr(img, X(ax), X(ay), X(ax + asz), X(ay + asz), X(20), (0, 0, 0), alpha=110)
            icon(ax + asz / 2, ay + asz / 2, IC_INFO if ok else IC_OPEN, 26, (255, 255, 255))
        self._draw_volume(img, d, ax, ay, asz, 20)
        hits.append(("art", ax, ay, ax + asz, ay + asz))

        # like button (top right)
        tx = 152
        tw = X(W - 18 - tx)
        if ok:
            hx, hy = W - 30, 26
            liked = self.web.liked_state(st.get("title")) if self.web.has_library else None
            if hov == "like":
                rr(img, X(hx - 18), X(hy - 18), X(hx + 18), X(hy + 18), 0, P["on_surface"],
                   alpha=26, ellipse=True)
            ready = self.web.connected and self.web.has_library
            icon(hx, hy, "" if liked else "", 18,
                 P["primary"] if liked else P["on_surface_variant"] if ready else P["outline"])
            hits.append(("like", hx - 18, hy - 18, hx + 18, hy + 18))
            tw = X(W - 18 - tx - 30)

        # texts
        if ok:
            status = T("playing") if playing else T("paused")
            mins = self._sleep_minutes()
            if mins:
                status += T("sleep_status", m=mins)
            title, artist, album = st["title"] or T("unknown"), st["artist"], st["album"]
        elif offline:
            status = T("offline_chip")
            title, artist, album = st["title"], st["artist"], st["album"]
        else:
            status, title = "SPOTIFY", T("waiting_title")
            artist, album = T("waiting_sub"), ""
        if offline:
            # warning chip: opens Spotify on click
            sw_ = F["status"].getlength(status) / S
            cx0, cx1 = tx - 2, tx + 14 + sw_ + 10
            chip = P["secondary_container"]
            if hov == "open":
                chip = mix(chip, P["on_secondary_container"], 0.12)
            rr(img, X(cx0), X(11), X(cx1), X(29), X(9), chip)
            rr(img, X(tx + 4), X(17), X(tx + 10), X(23), 0, (232, 120, 100), ellipse=True)
            d.text((X(tx + 15), X(20)), status, font=F["status"],
                   fill=P["on_secondary_container"] + (255,), anchor="lm")
            hits.append(("open", cx0, 9, cx1, 31))
        else:
            d.text((X(tx), X(20)), status, font=F["status"],
                   fill=readable(P["primary"], P["surface"], P["on_surface"]) + (255,), anchor="lm")
        y = 34
        for line in wrap_text(title, F["title"], tw, 2):
            d.text((X(tx), X(y)), line, font=F["title"], fill=P["on_surface"] + (255,))
            y += 25
        y += 2
        # artist and album are clickable: open their page in Spotify
        for hid, text, fkey, base_col, lh, show in (
                ("artist", artist, "artist", P["on_surface_variant"], 20, True),
                ("album", album, "album", P["outline"], 17, y < 122 - (21 if artist else 0))):
            if not text or not show:
                continue
            txt = fit_text(text, F[fkey], tw)
            col = P["primary"] if ((ok or offline) and hov == hid) else base_col
            d.text((X(tx), X(y)), txt, font=F[fkey], fill=col + (255,))
            wpx = F[fkey].getlength(txt)
            if ok or offline:
                if hov == hid:  # underline
                    uy = X(y) + F[fkey].asc + max(1.0, 1.5 * S)
                    rr(img, X(tx), uy, X(tx) + wpx, uy + max(1.0, S), 0, col)
                hits.append((hid, tx, y, tx + wpx / S, y + lh))
            y += lh + 1

        # progress bar area (_render_strip draws it)
        hits.append(("seek", BAR_X0 - 8, 146, BAR_X1 + 8, 174))

        # controls
        cy = CTRL_CY
        # shuffle
        sx = CX - 148
        if st.get("shuffle"):
            rr(img, X(sx - 20), X(cy - 20), X(sx + 20), X(cy + 20), 0, P["primary_container"],
               ellipse=True)
            c = P["on_primary_container"]
        else:
            c = P["on_surface_variant"]
        if hov == "shuffle":
            state_layer(sx - 20, cy - 20, sx + 20, cy + 20, 0, P["on_surface"], ellipse=True)
        icon(sx, cy, IC_SHUFFLE, 18, c)
        hits.append(("shuffle", sx - 22, cy - 22, sx + 22, cy + 22))
        # repeat
        rx = CX + 148
        rep = st.get("repeat") or 0
        if rep:
            rr(img, X(rx - 20), X(cy - 20), X(rx + 20), X(cy + 20), 0, P["primary_container"],
               ellipse=True)
            c = P["on_primary_container"]
        else:
            c = P["on_surface_variant"]
        if hov == "repeat":
            state_layer(rx - 20, cy - 20, rx + 20, cy + 20, 0, P["on_surface"], ellipse=True)
        icon(rx, cy, IC_REPEAT_ONE if rep == 1 else IC_REPEAT, 18, c)
        hits.append(("repeat", rx - 22, cy - 22, rx + 22, cy + 22))
        # previous / next (tonal)
        for hid, bx, glyph in (("prev", CX - 84, IC_PREV), ("next", CX + 84, IC_NEXT)):
            col = P["secondary_container"]
            if hov == hid:
                col = mix(col, P["on_secondary_container"], 0.10)
            if STYLE["neu"]:
                neu_btn(img, X(bx - 26), X(cy - 26), X(bx + 26), X(cy + 26), 0, col, ellipse=True,
                        pressed=self.pressed == hid)
            else:
                if self.pressed == hid:
                    col = mix(col, P["on_secondary_container"], 0.16)
                if STYLE["shad"] and STYLE["see"]:  # glassy shadcn: border + bg-secondary/60
                    rr(img, X(bx - 26), X(cy - 26), X(bx + 26), X(cy + 26), 0, P["border"],
                       alpha=200, ellipse=True)
                    b = max(1.0, round(self.S))
                    rr(img, X(bx - 26) + b, X(cy - 26) + b, X(bx + 26) - b, X(cy + 26) - b, 0, col,
                       alpha=150, ellipse=True)
                else:
                    rr(img, X(bx - 26), X(cy - 26), X(bx + 26), X(cy + 26), 0, col, ellipse=True)
            icon(bx, cy, glyph, 19, P["on_secondary_container"])
            hits.append((hid, bx - 26, cy - 26, bx + 26, cy + 26))
        # play / pause (primary, shape-morphing)
        pw, ph = 80, 58
        col = P["primary"]
        if hov == "play":
            col = mix(col, P["on_primary"], 0.10)
        if self.pressed == "play":
            col = mix(col, P["on_primary"], 0.18)
        if STYLE["neu"]:
            neu_btn(img, X(CX - pw / 2), X(cy - ph / 2), X(CX + pw / 2), X(cy + ph / 2),
                    X(self.play_r), mix(P["secondary_container"], P["on_surface"],
                                        0.06 if hov == "play" else 0),
                    pressed=self.pressed == "play")
        else:
            rr(img, X(CX - pw / 2), X(cy - ph / 2), X(CX + pw / 2), X(cy + ph / 2),
               X(self.play_r), col)
        icon(CX, cy, IC_PAUSE if playing else IC_PLAY, 24,
             P["on_surface"] if STYLE["neu"] else P["on_primary"])
        hits.append(("play", CX - pw / 2, cy - ph / 2, CX + pw / 2, cy + ph / 2))

        # up next card
        if self.cfg["show_queue"]:
            self._render_queue(img, d, hits, connected, queue, qerr)
        return img, hits

    def _render_queue(self, img, d, hits, connected, queue, qerr):
        S, P, F = self.S, self.pal, self.F
        X = lambda v: v * S  # noqa: E731
        y0 = MAIN_H + GAP
        collapsed = self.cfg["queue_collapsed"]
        hov = self.hover
        rows = max(1, min(Q_ROWS, int(self.cfg["queue_rows"])))
        tab = self.cfg["list_tab"]

        # rows to show: (name, subtitle, thumbnail, right text)
        items, message, need_connect = None, None, False
        if tab in ("stats", "lyrics"):
            pass
        elif tab == "recent":
            items = [(r["title"], r["sub"], r["thumb"], r["right"]) for r in self._recent_rows(rows)]
            if not items:
                message = T("recent_empty")
        elif not connected:
            need_connect = True
        elif queue:
            items = [(it["name"], it["sub"], self._queue_thumb(it.get("url")),
                      fmt_time(it["dur"]) if it.get("dur") else "") for it in queue[:rows]]
        else:
            message = (T(qerr[0], **qerr[1]) if qerr else
                       T("loading") if queue is None else T("queue_empty"))

        if collapsed:
            body = 0
        elif tab == "stats":
            body = STATS_H
        elif tab == "lyrics":
            body = max(rows, 3) * Q_ROW + Q_PAD
        elif need_connect:
            body = 86
        elif items:
            body = len(items) * Q_ROW + Q_PAD
        else:
            body = 64
        self._card(img, 0, X(y0), X(W), X(y0 + Q_HEAD + body), X(28))
        self.content_h = y0 + Q_HEAD + body

        # header: tabs + collapse
        hy = y0 + Q_HEAD / 2
        x = 12
        for key, lbl in (("queue", T("tab_queue")), ("recent", T("tab_recent")),
                         ("lyrics", T("tab_lyrics")), ("stats", T("tab_stats"))):
            w = F["btn"].getlength(lbl) / S + 20
            hid = "tab_" + key
            if tab == key:
                rr(img, X(x), X(hy - 16), X(x + w), X(hy + 16), X(16), P["secondary_container"])
                col = P["on_secondary_container"]
            else:
                if hov == hid:
                    rr(img, X(x), X(hy - 16), X(x + w), X(hy + 16), X(16), P["on_surface"],
                       alpha=22)
                col = P["on_surface_variant"]
            d.text((X(x + w / 2), X(hy)), lbl, font=F["btn"], fill=col + (255,), anchor="mm")
            hits.append((hid, x, hy - 16, x + w, hy + 16))
            x += w + 4
        cx = W - 28
        if hov == "qcollapse":
            rr(img, X(cx - 16), X(hy - 16), X(cx + 16), X(hy + 16), 0, P["on_surface"],
               alpha=22, ellipse=True)
        d.text((X(cx), X(hy)), IC_DOWN if collapsed else IC_UP, font=self.icon_font(13),
               fill=P["on_surface_variant"] + (255,), anchor="mm")
        hits.append(("qcollapse", cx - 18, hy - 18, cx + 18, hy + 18))

        top = y0 + Q_HEAD
        if collapsed:
            return
        if tab == "stats":
            self._render_stats(img, d, top, hits)
            return
        if tab == "lyrics":
            self._render_lyrics(img, d, top, body, hits)
            return
        if need_connect:
            d.text((X(22), X(top + 4)), T("connect_line1"), font=F["body"],
                   fill=P["on_surface_variant"] + (255,))
            d.text((X(22), X(top + 24)), T("connect_line2"), font=F["body"],
                   fill=P["on_surface_variant"] + (255,))
            bw = F["btn"].getlength(T("connect_btn")) / S + 40
            bx0, by0, bx1, by1 = W - 22 - bw, top + 6, W - 22, top + 46
            col = P["primary"]
            if hov == "connect":
                col = mix(col, P["on_primary"], 0.10)
            rr(img, X(bx0), X(by0), X(bx1), X(by1), X(20), col)
            d.text((X((bx0 + bx1) / 2), X((by0 + by1) / 2)), T("connect_btn"), font=F["btn"],
                   fill=P["on_primary"] + (255,), anchor="mm")
            hits.append(("connect", bx0, by0, bx1, by1))
            return
        if not items:
            d.text((X(22), X(top + 22)), fit_text(message or "", F["body"], X(W - 44)),
                   font=F["body"], fill=P["on_surface_variant"] + (255,), anchor="lm")
            return
        prefix = "h" if tab == "recent" else "q"
        for i, (name, sub, th, right) in enumerate(items):
            ry = top + i * Q_ROW
            hid = f"{prefix}{i}"
            if hov == hid:
                rr(img, X(8), X(ry + 2), X(W - 8), X(ry + Q_ROW - 2), X(16), P["on_surface"],
                   alpha=22)
            # thumbnail
            tx0, ty0 = 20, ry + (Q_ROW - 40) / 2
            if th == "cal":  # on this day last year
                rr(img, X(tx0), X(ty0), X(tx0 + 40), X(ty0 + 40), X(10), P["primary_container"])
                d.text((X(tx0 + 20), X(ty0 + 20)), IC_CAL, font=self.icon_font(16),
                       fill=P["on_primary_container"] + (255,), anchor="mm")
            elif th is not None:
                img.alpha_composite(th, (round(X(tx0)), round(X(ty0))))
            else:
                rr(img, X(tx0), X(ty0), X(tx0 + 40), X(ty0 + 40), X(10), P["surface_highest"])
                d.text((X(tx0 + 20), X(ty0 + 20)), IC_MUSIC, font=self.icon_font(16),
                       fill=P["on_surface_variant"] + (255,), anchor="mm")
            maxw = X(W - 72 - 24) - (F["qsub"].getlength(right) + X(12) if right else 0)
            cy = ry + Q_ROW / 2
            d.text((X(72), X(cy - 9)), fit_text(name, F["qtitle"], maxw),
                   font=F["qtitle"], fill=P["on_surface"] + (255,), anchor="lm")
            d.text((X(72), X(cy + 10)), fit_text(sub or "", F["qsub"], maxw), font=F["qsub"],
                   fill=P["on_surface_variant"] + (255,), anchor="lm")
            if right:
                d.text((X(W - 24), X(cy)), right, font=F["qsub"],
                       fill=P["on_surface_variant"] + (255,), anchor="rm")
            hits.append((hid, 8, ry, W - 8, ry + Q_ROW))
        if tab == "recent":  # scroll indicator (if there are more tracks)
            total = len(self._recent_all())
            if total > rows:
                ty0, ty1 = top + 4, top + len(items) * Q_ROW - 4
                th_ = max(14.0, (ty1 - ty0) * rows / total)
                tyy = ty0 + (ty1 - ty0 - th_) * self.hist_off / max(1, total - rows)
                rr(img, X(W - 7), X(ty0), X(W - 4), X(ty1), X(1.5), P["on_surface"], alpha=18)
                rr(img, X(W - 7), X(tyy), X(W - 4), X(tyy + th_), X(1.5), P["on_surface_variant"],
                   alpha=150)

    def _render_stats(self, img, d, top, hits):
        S, P, F = self.S, self.pal, self.F
        X = lambda v: v * S  # noqa: E731
        sm = self._stats_summary()
        solid = self.cfg["bg"] == "solid"

        def tile(x0, y0, x1, y1, r):
            if solid:
                rr(img, X(x0), X(y0), X(x1), X(y1), X(r), P["surface_high"])
            else:
                rr(img, X(x0), X(y0), X(x1), X(y1), X(r), P["on_surface"], alpha=22)

        hov = self.hover
        period, lst = self.cfg["stats_period"], self.cfg["stats_list"]

        # period picker
        x, cy = 16, top + 12
        for p in self.STAT_PERIODS:
            lbl = T("sp_" + p)
            w = F["qsub"].getlength(lbl) / S + 20
            hid = "sp_" + p
            if p == period:
                rr(img, X(x), X(cy - 13), X(x + w), X(cy + 13), X(13), P["secondary_container"])
                col = P["on_secondary_container"]
            else:
                if hov == hid:
                    rr(img, X(x), X(cy - 13), X(x + w), X(cy + 13), X(13), P["on_surface"],
                       alpha=22)
                col = P["on_surface_variant"]
            d.text((X(x + w / 2), X(cy)), lbl, font=F["qsub"], fill=col + (255,), anchor="mm")
            hits.append((hid, x, cy - 14, x + w, cy + 14))
            x += w + 4
        if sm["title"]:  # opened year / month: click to go up one level
            lbl = "← " + sm["title"]
            w = F["qsub"].getlength(lbl) / S + 22
            x = max(x + 4, W - 16 - w)
            col = P["primary"] if hov != "sup" else mix(P["primary"], P["on_primary"], 0.12)
            rr(img, X(x), X(cy - 13), X(x + w), X(cy + 13), X(13), col)
            d.text((X(x + w / 2), X(cy)), lbl, font=F["qsub"], fill=P["on_primary"] + (255,),
                   anchor="mm")
            hits.append(("sup", x, cy - 14, x + w, cy + 14))
        else:  # else: search history button
            bx = W - 30
            if hov == "shist":
                rr(img, X(bx - 14), X(cy - 14), X(bx + 14), X(cy + 14), 0, P["on_surface"],
                   alpha=22, ellipse=True)
            d.text((X(bx), X(cy)), "", font=self.icon_font(13),
                   fill=P["on_surface_variant"] + (255,), anchor="mm")
            hits.append(("shist", bx - 15, cy - 15, bx + 15, cy + 15))

        # summary boxes (with a year open the third box opens the yearly summary card)
        gap, x0, ty = 8, 16, top + 34
        tw = (W - 32 - 2 * gap) / 3
        ly = self._last_year_today() if period == "7" else None
        for i, (val, lbl) in enumerate(sm["tiles"]):
            tx = x0 + i * (tw + gap)
            if i == 2 and ly:  # 7 days: most played on this day last year (clickable)
                col = P["primary_container"]
                if hov == "sly":
                    col = mix(col, P["on_primary_container"], 0.12)
                rr(img, X(tx), X(ty), X(tx + tw), X(ty + 52), X(16), col)
                d.text((X(tx + 12), X(ty + 19)), fit_text(ly[0], F["qhead"], X(tw - 20)),
                       font=F["qhead"], fill=P["on_primary_container"] + (255,), anchor="lm")
                d.text((X(tx + 12), X(ty + 37)), fit_text(T("ly_short"), F["qsub"], X(tw - 20)),
                       font=F["qsub"], fill=P["on_primary_container"] + (255,), anchor="lm")
                hits.append(("sly", tx, ty, tx + tw, ty + 52))
                continue
            if i == 2 and period.startswith("y"):
                col = P["primary_container"]
                if hov == "swrap":
                    col = mix(col, P["on_primary_container"], 0.12)
                rr(img, X(tx), X(ty), X(tx + tw), X(ty + 52), X(16), col)
                d.text((X(tx + 12), X(ty + 19)), T("wrap_btn"), font=F["qhead"],
                       fill=P["on_primary_container"] + (255,), anchor="lm")
                d.text((X(tx + 12), X(ty + 37)), fit_text(T("wr_title", y=period[1:]),
                                                          F["qsub"], X(tw - 20)),
                       font=F["qsub"], fill=P["on_primary_container"] + (255,), anchor="lm")
                hits.append(("swrap", tx, ty, tx + tw, ty + 52))
                continue
            tile(tx, ty, tx + tw, ty + 52, 16)
            d.text((X(tx + 12), X(ty + 19)), fit_text(val, F["qhead"], X(tw - 20)),
                   font=F["qhead"], fill=(readable(P["primary"], P["surface"], P["on_surface"])
                                          if i == 0 else P["on_surface"]) + (255,),
                   anchor="lm")
            d.text((X(tx + 12), X(ty + 37)), fit_text(lbl, F["qsub"], X(tw - 20)),
                   font=F["qsub"], fill=P["on_surface_variant"] + (255,), anchor="lm")

        # chart (day / month / year)
        bars = sm["bars"]
        n = max(1, len(bars))
        by0, by1 = top + 100, top + 146
        cw = (W - 32) / n
        bw = min(22.0, cw * 0.62)
        br = min(8.0, bw / 2)
        mx = max([b[1] for b in bars] + [1.0])
        for i, (lbl, sec, target, hl) in enumerate(bars):
            cx = 16 + cw * (i + 0.5)
            hid = f"sb{i}"
            over = target is not None and hov == hid
            if over:
                rr(img, X(cx - cw / 2 + 1), X(by0 - 6), X(cx + cw / 2 - 1), X(by1 + 22), X(10),
                   P["on_surface"], alpha=20)
            rr(img, X(cx - bw / 2), X(by0), X(cx + bw / 2), X(by1), X(br), P["on_surface"],
               alpha=16)
            if sec > 0:
                h = max(bw, (by1 - by0) * sec / mx)
                rr(img, X(cx - bw / 2), X(by1 - h), X(cx + bw / 2), X(by1), X(br), P["primary"],
                   alpha=255 if (hl or over) else 130)
            if lbl:
                d.text((X(cx), X(by1 + 13)), lbl, font=F["qsub"],
                       fill=(readable(P["primary"], P["surface"], P["on_surface"]) if (hl or over)
                             else P["on_surface_variant"]) + (255,),
                       anchor="mm")
            if target is not None:
                hits.append((hid, cx - cw / 2, by0 - 6, cx + cw / 2, by1 + 22))

        # top lists: artists / tracks / hours
        hy = top + 182
        d.text((X(20), X(hy)), T({"artists": "st_top_a", "tracks": "st_top_t"}.get(
            lst, "st_hours")), font=F["status"],
            fill=readable(P["primary"], P["surface"], P["on_surface"]) + (255,), anchor="lm")
        x = W - 16
        for key in ("hours", "tracks", "artists"):
            lbl = T("sl_" + key)
            w = F["qsub"].getlength(lbl) / S + 16
            x -= w
            hid = "sl_" + key
            if key == lst:
                rr(img, X(x), X(hy - 11), X(x + w), X(hy + 11), X(11), P["secondary_container"])
                col = P["on_secondary_container"]
            else:
                if hov == hid:
                    rr(img, X(x), X(hy - 11), X(x + w), X(hy + 11), X(11), P["on_surface"],
                       alpha=22)
                col = P["on_surface_variant"]
            d.text((X(x + w / 2), X(hy)), lbl, font=F["qsub"], fill=col + (255,), anchor="mm")
            hits.append((hid, x, hy - 12, x + w, hy + 12))
            x -= 4
        if lst == "hours":
            self._render_heatmap(img, d, hy + 14, sm["grid"])
            return
        rows = sm["top_a"] if lst == "artists" else [(t, v) for t, _a, v in sm["top_t"]]
        if not rows:
            d.text((X(20), X(hy + 30)), T("st_empty"), font=F["body"],
                   fill=P["on_surface_variant"] + (255,), anchor="lm")
            return
        best = rows[0][1] or 1.0
        for i, (name, sec) in enumerate(rows):
            ry = hy + 24 + i * 26
            hid = f"sr{i}"
            if hov == hid:
                rr(img, X(10), X(ry - 11), X(W - 10), X(ry + 19), X(10), P["on_surface"],
                   alpha=18)
            dur = fmt_dur(sec)
            avail = X(W - 40) - F["qsub"].getlength(dur) - X(12)
            t1 = fit_text(name, F["qtitle"], avail)
            d.text((X(20), X(ry)), t1, font=F["qtitle"], fill=P["on_surface"] + (255,),
                   anchor="lm")
            # artist for tracks, first listen date for artists
            extra = sm["top_t"][i][1] if lst == "tracks" else self._since(name)
            rest = avail - F["qtitle"].getlength(t1)
            if extra and rest > X(40):
                d.text((X(20) + avail - rest, X(ry + 1)),
                       fit_text(" · " + extra, F["qsub"], rest), font=F["qsub"],
                       fill=P["on_surface_variant"] + (255,), anchor="lm")
            d.text((X(W - 20), X(ry)), dur, font=F["qsub"],
                   fill=P["on_surface_variant"] + (255,), anchor="rm")
            rr(img, X(20), X(ry + 13), X(W - 20), X(ry + 16), X(1.5), P["on_surface"],
               alpha=16)
            rr(img, X(20), X(ry + 13), X(20 + (W - 40) * sec / best), X(ry + 16), X(1.5),
               P["primary"], alpha=200)
            hits.append((hid, 10, ry - 11, W - 10, ry + 19))

    def _lyric_index(self):
        """Index of the line being sung (-1 if not synced)."""
        if not self.lyrics.synced:
            return -1
        pos = self._position()[0] + 0.25
        idx = -1
        for i, (t, _txt) in enumerate(self.lyrics.lines()):
            if t is not None and t <= pos:
                idx = i
            else:
                break
        return idx

    def _render_lyrics(self, img, d, top, body, hits):
        S, P, F = self.S, self.pal, self.F
        X = lambda v: v * S  # noqa: E731
        st = self._view()
        if not st.get("ok") and not st.get("offline"):
            msg = T("waiting_title")
        else:
            msg = {"loading": T("ly_loading"), "none": T("ly_none"), "instr": T("ly_instr"),
                   "err": T("ly_err")}.get(self.lyrics.state)
        lines = self.lyrics.lines()
        if msg or not lines:
            d.text((X(W / 2), X(top + body / 2 - 6)), msg or T("ly_none"), font=F["body"],
                   fill=P["on_surface_variant"] + (255,), anchor="mm")
            return
        lh = 27
        n = max(3, int((body - 8) // lh))
        cur = self._lyric_index()
        if self.lyrics.synced:
            first = max(0, min(cur - 1, len(lines) - n)) if cur >= 0 else 0
        else:
            first = 0
        maxw = X(W - 44)
        for row in range(n):
            i = first + row
            if i >= len(lines):
                break
            t, text = lines[i]
            y = top + 4 + row * lh + lh / 2
            hid = f"ly{i}"
            if t is not None and self.hover == hid:
                rr(img, X(10), X(y - lh / 2 + 1), X(W - 10), X(y + lh / 2 - 1), X(10),
                   P["on_surface"], alpha=18)
            if i == cur:
                font, col = F["lyric"], P["primary"]
            elif i < cur:
                font, col = F["qtitle"], P["outline"]
            else:
                font, col = F["qtitle"], (P["on_surface"] if not self.lyrics.synced
                                          else P["on_surface_variant"])
            d.text((X(22), X(y)), fit_text(text or "♪", font, maxw), font=font,
                   fill=col + (255,), anchor="lm")
            if t is not None:
                hits.append((hid, 10, y - lh / 2, W - 10, y + lh / 2))
        if not self.lyrics.synced:
            d.text((X(W - 22), X(top + body - 10)), T("ly_plain"), font=F["tiny"],
                   fill=P["outline"] + (255,), anchor="rm")

    def _render_heatmap(self, img, d, y0, grid):
        """Weekday × hour heat map (7 rows, 24 columns)."""
        S, P, F = self.S, self.pal, self.F
        X = lambda v: v * S  # noqa: E731
        names = T("days").split()
        mx = max(grid) or 1.0
        gx0, gx1 = 42, W - 18
        cw, ch = (gx1 - gx0) / 24, 8.6
        for r in range(7):
            ry = y0 + r * ch
            if r in (0, 2, 4, 6):
                d.text((X(20), X(ry + ch / 2)), names[r], font=F["tiny"],
                       fill=P["on_surface_variant"] + (255,), anchor="lm")
            for c in range(24):
                v = grid[r * 24 + c]
                cx = gx0 + c * cw
                if v <= 0:
                    rr(img, X(cx + 0.8), X(ry + 0.8), X(cx + cw - 0.8), X(ry + ch - 0.8), X(2),
                       P["on_surface"], alpha=12)
                else:
                    a = 40 + round(215 * (v / mx) ** 0.65)
                    rr(img, X(cx + 0.8), X(ry + 0.8), X(cx + cw - 0.8), X(ry + ch - 0.8), X(2),
                       P["primary"], alpha=a)
        ly = y0 + 7 * ch + 8
        for hr in (0, 6, 12, 18, 23):
            d.text((X(gx0 + (hr + 0.5) * cw), X(ly)), f"{hr:02d}", font=F["tiny"],
                   fill=P["on_surface_variant"] + (255,), anchor="mm")

    def _queue_thumb(self, url):
        if not url:
            return None
        if url in self.qthumbs:
            return self.qthumbs[url]
        src = self.web.thumbs.get(url)
        if src is None:
            return None
        th = rounded_image(src, round(40 * self.S), 10 * self.S)
        if len(self.qthumbs) > 60:
            self.qthumbs.clear()
        self.qthumbs[url] = th
        return th

    def _wave_tile(self, amp, color):
        key = (round(amp, 2), color)
        t = self.wave_tiles.get(key)
        if t is not None:
            return t
        S, SS = self.S, 4
        w = int(math.ceil((BAR_X1 - BAR_X0 + LAM + 4) * S))
        h = int(math.ceil(16 * S))
        big = Image.new("L", (w * SS, h * SS), 0)
        mid = h * SS / 2
        pts = []
        for xb in range(0, w * SS + 1, 3):
            xl = xb / (SS * S)
            pts.append((xb, mid + amp * S * SS * math.sin(2 * math.pi * xl / LAM)))
        ImageDraw.Draw(big).line(pts, fill=255, width=max(1, round(4 * S * SS)), joint="curve")
        mask = big.resize((w, h), Image.Resampling.BOX)
        t = Image.new("RGBA", (w, h), tuple(color) + (255,))
        t.putalpha(mask)
        if len(self.wave_tiles) > 40:
            self.wave_tiles.clear()
        self.wave_tiles[key] = t
        return t

    def _render_strip(self, frac, shown, end):
        S, P = self.S, self.pal
        X = lambda v: v * S  # noqa: E731
        strip = self.base_strip.copy()
        if self.mini:  # mini mode: thin flat line under the card
            cy = X(MINI_BAR_Y) - self.sy0
            x0, x1 = X(44), X(W - 44)
            rr(strip, x0, cy - X(1.5), x1, cy + X(1.5), X(1.5), P["surface_highest"])
            if frac > 0:
                rr(strip, x0, cy - X(1.5), x0 + (x1 - x0) * frac, cy + X(1.5), X(1.5),
                   P["primary"])
            return strip
        d = Drawer(strip)
        cy = X(BAR_CY) - self.sy0
        bx0, bx1 = X(BAR_X0), X(BAR_X1)
        px = bx0 + (bx1 - bx0) * frac
        active = self.hover == "seek" or self.drag_seek is not None
        tw, th = (6, 26) if active else (4, 20)
        gap = X(5 + tw / 2)
        primary = P["primary"]

        # played part: wave
        played_end = px - gap
        if played_end > bx0 + 1:
            amp_q = round(self.amp * 4) / 4
            tile = self._wave_tile(amp_q, primary)
            off_l = (LAM - (self.phase % LAM)) % LAM
            off = int(off_l * S)
            length = int(played_end - bx0)
            crop = tile.crop((off, 0, off + length, tile.height))
            top = int(round(cy - tile.height / 2))
            strip.alpha_composite(crop, (int(bx0), top))
            # round caps
            for xl, xp in ((off / S, bx0), ((off + length) / S, bx0 + length)):
                yy = top + tile.height / 2 + amp_q * S * math.sin(2 * math.pi * xl / LAM)
                rr(strip, xp - X(2), yy - X(2), xp + X(2), yy + X(2), 0, primary, ellipse=True)
        # remaining part
        rem = px + gap
        if rem < bx1 - 1:
            rr(strip, rem, cy - X(2), bx1, cy + X(2), X(2), P["surface_highest"])
            if rem < bx1 - X(10) and not STYLE["shad"]:
                rr(strip, bx1 - X(4), cy - X(2), bx1, cy + X(2), 0, primary, ellipse=True)
        # thumb (Material: vertical pill; shadcn: bordered circle)
        if STYLE["shad"]:
            rt = 9 if active else 7.5
            rr(strip, px - X(rt), cy - X(rt), px + X(rt), cy + X(rt), 0, primary, ellipse=True)
            ri = rt - 2
            rr(strip, px - X(ri), cy - X(ri), px + X(ri), cy + X(ri), 0, P["surface"],
               ellipse=True)
        else:
            rr(strip, px - X(tw / 2), cy - X(th / 2), px + X(tw / 2), cy + X(th / 2),
               X(tw / 2), primary)
        # times
        ty = X(180) - self.sy0
        d.text((bx0, ty), fmt_time(shown), font=self.F["time"],
               fill=P["on_surface_variant"] + (255,), anchor="lm")
        d.text((bx1, ty), fmt_time(end), font=self.F["time"],
               fill=P["on_surface_variant"] + (255,), anchor="rm")
        return strip

    # ---------------- events
    def _xy(self, e):
        return e.x / self.S, e.y / self.S

    def _seek_frac(self, x):
        return max(0.0, min(1.0, (x - BAR_X0) / (BAR_X1 - BAR_X0)))

    def _on_motion(self, e):
        self._set_inside(True)
        x, y = self._xy(e)
        if self.drag_seek is not None:
            self.drag_seek = self._seek_frac(x)
            return
        if self.win_drag:
            move_window(self.hwnd, e.x_root - self.win_drag[0], e.y_root - self.win_drag[1])
            return
        h = self._hit(x, y)
        if h != self.hover:
            self.hover = h
            self.root.config(cursor="hand2" if h else "")
            self._render_all()

    def _on_leave(self, _e):
        self._set_inside(False)
        if self.hover and self.drag_seek is None and not self.win_drag:
            self.hover = None
            self.root.config(cursor="")
            self._render_all()

    def _on_press(self, e):
        x, y = self._xy(e)
        h = self._hit(x, y)
        if h == "seek" and self.st.get("ok") and self.st.get("end", 0) > 0:
            self.drag_seek = self._seek_frac(x)
        elif h:
            self.pressed = h
        elif not self.cfg["locked"]:
            wx, wy = self._win_pos()
            self.win_drag = (e.x_root - wx, e.y_root - wy)
        self._render_all()

    def _on_release(self, e):
        x, y = self._xy(e)
        if self.drag_seek is not None:
            end = self.st.get("end", 0)
            target = self.drag_seek * end
            self.drag_seek = None
            self.seek_hold = (target, time.monotonic())
            self.media.command("seek", target)
        elif self.pressed:
            h = self._hit(x, y)
            p, self.pressed = self.pressed, None
            if h == p:
                if p.startswith(self.ANIMATED) and not self.mini:
                    self._transition(lambda: self._action(p))
                else:
                    self._action(p)
        elif self.win_drag:
            self.win_drag = None
            self.cfg["x"], self.cfg["y"] = self._win_pos()
            self.cfg.save()
        self._render_all()

    def _on_wheel(self, e):
        if e.state & 0x4:  # Ctrl + wheel: resize
            self._set_scale(float(self.cfg["scale"]) + (SCALE_STEP if e.delta > 0
                                                         else -SCALE_STEP))
            return
        x, y = self._xy(e)
        h = self._hit(x, y) or ""
        if self.cfg["list_tab"] == "recent" and not self.mini and (
                (h[:1] == "h" and h[1:].isdigit()) or y > MAIN_H + GAP + Q_HEAD):
            # scroll the History list
            total = len(self._recent_all())
            rows = max(1, min(Q_ROWS, int(self.cfg["queue_rows"])))
            off = max(0, min(total - rows, self.hist_off + (-1 if e.delta > 0 else 1)))
            if off != self.hist_off:
                self.hist_off = off
                self.base_key = None
                self.hover = None
                self._render_all()
            return
        if h == "art":  # wheel over the cover: Spotify volume
            self._change_volume(0.05 if e.delta > 0 else -0.05)
            self._render_all()
            return
        if h == "seek" and self.st.get("ok"):
            pos, end = self._position()
            target = max(0.0, min(end, pos + (5 if e.delta > 0 else -5)))
            self.seek_hold = (target, time.monotonic())
            self.media.command("seek", target)

    def _action(self, a):
        st = self.st
        if a == "open" or (a == "play" and not st.get("ok")):
            a = "art"  # Spotify closed: open Spotify
        if a == "play":
            self.play_hold = (not self._playing(), time.monotonic())
            self.media.command("toggle")
        elif a in ("prev", "next"):
            self.media.command(a)
        elif a == "shuffle":
            self.media.command("shuffle", not st.get("shuffle"))
        elif a == "repeat":
            nxt = {0: 2, 2: 1, 1: 0}.get(st.get("repeat") or 0, 0)  # off → context → track
            self.media.command("repeat", nxt)
        elif a == "art":
            if st.get("ok") and st.get("title"):
                SongDialog(self)   # current track details
            else:
                open_spotify()     # Spotify closed: open it
        elif a in ("artist", "album"):
            self._open_entity(a)
        elif a == "like":
            if not (self.web.connected and self.web.has_library):
                ConnectDialog(self, reauth=self.web.connected)
            else:
                self.web.toggle_like(st.get("title"))
        elif a == "qcollapse":
            self.cfg["queue_collapsed"] = not self.cfg["queue_collapsed"]
            self.cfg.save()
        elif a in ("tab_queue", "tab_recent", "tab_stats", "tab_lyrics"):
            self.cfg["list_tab"] = a[4:]
            self.cfg["queue_collapsed"] = False
            self.cfg.save()
        elif a == "connect":
            ConnectDialog(self)
        elif a.startswith("sp_"):
            self._set("stats_period", a[3:])
        elif a == "shist":
            HistoryDialog(self)
        elif a == "sly":  # on this day last year: open that track in Spotify
            ly = self._last_year_today()
            if ly:
                self._spotify_search(f"{ly[0]} {ly[1]}")
        elif a == "swrap":
            RecapWindow(self, int(self.cfg["stats_period"][1:]))
        elif a == "sup":  # from an opened month to the year, from the year to all time
            up = self._stats_summary()["up"]
            if up:
                self._set("stats_period", up)
        elif a.startswith("sb"):  # year / month bar in the chart: open that period
            bars = self._stats_summary()["bars"]
            i = int(a[2:])
            if i < len(bars) and bars[i][2]:
                self._set("stats_period", bars[i][2])
        elif a.startswith("sl_"):
            self._set("stats_list", a[3:])
        elif a.startswith("sr"):  # top list: search in Spotify
            sm, i = self._stats_summary(), int(a[2:])
            if self.cfg["stats_list"] == "artists":
                q = sm["top_a"][i][0] if i < len(sm["top_a"]) else ""
            else:
                q = " ".join(sm["top_t"][i][:2]) if i < len(sm["top_t"]) else ""
            if q:
                self._spotify_search(q)
        elif a.startswith("h"):  # recently played: search in Spotify
            items = self._recent_rows(int(self.cfg["queue_rows"]))
            i = int(a[1:])
            if i < len(items):
                self._spotify_search(items[i]["q"].strip())
        elif a.startswith("ly"):  # lyrics line: seek there
            i = int(a[2:])
            lines = self.lyrics.lines()
            if lines and i < len(lines) and lines[i][0] is not None:
                self.seek_hold = (lines[i][0], time.monotonic())
                self.media.command("seek", lines[i][0])
        elif a.startswith("q"):
            n = int(a[1:]) + 1
            _c, queue, _e, _v = self.web.snapshot()
            if queue and n <= len(queue):  # show the picked track at once, before Spotify confirms
                it = queue[n - 1]
                self.pending = {"title": it["name"], "artist": it["sub"], "dur": it.get("dur") or 0,
                                "until": time.monotonic() + 3.0 + 0.4 * n}
                th = self.web.thumbs.get(it.get("url"))
                if th is not None:
                    self._set_art(th)
                self.seek_hold = (0.0, time.monotonic())
                self.web.pop_queue(n)  # drop the skipped tracks from the list right away
                self.base_key = None
            self.media.command("skip_n", n)
            # refresh the queue after the skips (don't flicker by showing the old list in between)
            self.root.after(int(300 * n), self.web.refresh_now)

    # ---------------- menu (our own drawn Material menu: MenuPanel)
    def _on_menu(self, e):
        MenuPanel.toggle(self, e.x_root, e.y_root)

    def _set(self, k, v):
        self.cfg[k] = v
        self.cfg.save()
        self.base_key = None

    def _set_mode(self, v):
        self._set("mode", v)
        self._apply_mode()

    def _set_theme(self, v):
        self._set("theme", v)
        self.light = self._is_light()
        self._start_palette(self._scheme())

    def _set_accent(self, v):
        self._set("accent", v)
        self._start_palette(self._scheme())

    def _set_opacity(self, v):
        self._set("opacity", v)
        self.surface.alpha = self._target_alpha()
        self._render_all()

    def _set_fade(self, k, v):
        self._set(k, v)
        self._alpha_step()

    def _toggle_alarm(self, on):
        self.cfg["alarm"] = dict(self.cfg["alarm"], on=bool(on))
        self.cfg.save()
        self._alarm_day = None

    def _set_hotkeys(self, on):
        self._set("hotkeys", on)
        if on:
            self.hotkeys.start()
        else:
            self.hotkeys.stop()

    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

    @classmethod
    def _startup_on(cls):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cls.RUN_KEY) as k:
                winreg.QueryValueEx(k, "Coverglow")
            return True
        except OSError:
            return False

    def _recheck_update(self):
        """Look for a new release now and every 6 hours (a widget runs for days). Background
        timer, no Tk: this runs in __init__ before the window (self.root) exists."""
        threading.Thread(target=self._check_update, daemon=True).start()
        t = threading.Timer(6 * 3600, self._recheck_update)
        t.daemon = True
        t.start()

    def _check_update(self):
        try:
            self.update = latest_release()
            self.update_tag = self.update["tag"] if self.update else None
        except Exception as e:  # noqa: BLE001 - offline or rate limited: just skip
            log("update check:", repr(e))

    def _install_update(self):
        """Download the new installer and run it silently over this install; it closes this
        copy, installs into the same folder and starts the new version. No browser."""
        if self._updating or not self.update:
            return
        if not FROZEN:
            self.toast.show_message(MI["open"], T("upd_src"), hold=3000)
            return
        self._updating = True
        self.toast.show_message(MI["import"], T("upd_dl"), hold=60000)
        upd, res = self.update, {}

        def work():   # no Tk calls here: the Tk thread picks up res
            try:
                res["path"] = download_update(upd, os.path.join(
                    os.environ.get("TEMP") or os.path.dirname(sys.executable), "Coverglow-update"))
            except Exception as e:  # noqa: BLE001
                log("update download:", repr(e))
                res["err"] = e

        def wait():
            if "path" in res:
                self._run_installer(res["path"])
            elif "err" in res:
                self._update_failed()
            else:
                self.root.after(200, wait)
        threading.Thread(target=work, daemon=True).start()
        wait()

    def _update_failed(self):
        self._updating = False
        self.toast.show_message(MI["close"], T("upd_fail"), hold=3000)

    def _run_installer(self, path):
        import subprocess
        self.toast.show_message(MI["import"], T("upd_inst"), hold=60000)
        self._save_stats()   # the installer stops this copy with taskkill /f: nothing is saved after
        self.cfg.save()
        try:
            subprocess.Popen(installer_args(path, os.path.dirname(sys.executable),
                                            os.environ.get("LOCALAPPDATA")),
                             creationflags=0x00000008)  # DETACHED_PROCESS: outlives this copy
        except OSError as e:
            log("update start:", repr(e))
            self._update_failed()
            return
        # no self-close: if the install never starts (UAC "No", error) the widget keeps running;
        # after 2 min the menu item works again
        self.root.after(120000, lambda: setattr(self, "_updating", False))

    def _set_startup(self, on):
        """Launch at Windows startup: HKCU\\...\\Run entry (shown in Task Manager > Startup)."""
        self.cfg["startup"] = bool(on)
        self.cfg.save()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                if on:
                    if FROZEN:
                        cmd = f'"{sys.executable}"'
                    else:
                        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                        cmd = f'"{pyw if os.path.exists(pyw) else sys.executable}" ' \
                              f'"{os.path.abspath(__file__)}"'
                    winreg.SetValueEx(k, "Coverglow", 0, winreg.REG_SZ, cmd)
                for name in (() if on else ("Coverglow",)) + ("SpotifyWidget",):  # + pre-rename entry
                    try:
                        winreg.DeleteValue(k, name)
                    except FileNotFoundError:
                        pass
        except OSError as e:
            log("startup setting:", repr(e))

    def run(self):
        self.root.mainloop()
        self._save_stats()
        v = self.cfg.get("vol_restore")
        if v is not None and self.volume.get() is not None:  # ducked / fading: put it back
            self.volume.set(v)
            self._vol_mark(None)


# ------------------------------------------------------------------ toast
class Toast:
    """Short 'now playing' bubble that appears bottom right when the track changes."""
    TW, TH = 340, 88
    HOLD_MS = 3500

    def __init__(self, app):
        self.app = app
        self.WM = round(self.TW * app.dpi * SCALE_MAX)
        self.HM = round(self.TH * app.dpi * SCALE_MAX)
        t = self.top = tk.Toplevel(app.root)
        t.overrideredirect(True)
        t.configure(bg="black")
        t.geometry(f"{self.WM}x{self.HM}+-30000+-30000")
        t.update()
        self.hwnd = user32.GetAncestor(t.winfo_id(), 2)
        set_window_styles(self.hwnd)
        # ownerless window: don't stay tied to the main window pinned to the desktop
        user32.SetWindowLongPtrW(self.hwnd, GWLP_HWNDPARENT, 0)
        self.surface = LayeredSurface(self.hwnd, self.WM, self.HM)
        self.surface.alpha = 0
        self.fs = False
        self._hold_until = 0.0
        self.hold = self.HOLD_MS
        self.surface.full(Image.new("RGBA", (self.WM, self.HM), (0, 0, 0, 0)))
        t.bind("<Button-1>", self._clicked)
        self._track = False       # showing the track toast (has the next button)
        self.on_click = None
        self._job = None
        self._target = 0
        self._pos = (0, 0)
        self._offset = 0.0

    def _render(self, st):
        app = self.app
        S, P, F = app.S, app.pal_to, app.F
        X = lambda v: v * S  # noqa: E731
        img = Image.new("RGBA", (self.WM, self.HM), (0, 0, 0, 0))
        d = Drawer(img)
        rr(img, 0, 0, X(self.TW), X(self.TH), X(24), P["surface_highest"])
        rr(img, X(1), X(1), X(self.TW - 1), X(self.TH - 1), X(23), P["surface_high"])
        if app.art_src is not None:
            img.alpha_composite(rounded_image(app.art_src, round(64 * S), 16 * S),
                                (round(X(12)), round(X(12))))
        else:
            rr(img, X(12), X(12), X(76), X(76), X(16), P["surface_highest"])
            d.text((X(44), X(44)), IC_MUSIC, font=app.icon_font(26),
                   fill=P["on_surface_variant"] + (255,), anchor="mm")
        tw = X(self.TW - 88 - 48)   # room for the next button
        d.text((X(self.TW - 30), X(self.TH / 2)), IC_NEXT, font=app.icon_font(17),
               fill=P["on_surface_variant"] + (255,), anchor="mm")
        d.text((X(88), X(24)), T("playing"), font=F["status"],
               fill=readable(P["primary"], P["surface"], P["on_surface"]) + (255,), anchor="lm")
        d.text((X(88), X(45)), fit_text(st.get("title") or T("unknown"), F["mtitle"], tw),
               font=F["mtitle"], fill=P["on_surface"] + (255,), anchor="lm")
        d.text((X(88), X(65)), fit_text(st.get("artist") or "", F["album"], tw),
               font=F["album"], fill=P["on_surface_variant"] + (255,), anchor="lm")
        return img

    def show(self, st, fs=None):
        """fs: monitor of the fullscreen app (if any the toast appears on it, top right,
        and passes clicks through to the game)."""
        try:
            secs = min(60.0, max(1.0, float(self.app.cfg.get("toast_secs", 3.5))))
        except (TypeError, ValueError):
            secs = self.HOLD_MS / 1000
        self._present(self._render(st), self.TW, self.TH, fs, round(1000 * secs))
        self._track = True

    def _clicked(self, e=None):
        """Track toast: the next button skips (toast stays briefly), elsewhere opens Spotify.
        Messages: their own on_click, if any."""
        fn, self.on_click = self.on_click, None
        if self._track and e is not None and e.x / self.app.S >= self.TW - 56:
            self.app.media.command("next")
            self._hold_until = time.monotonic() + 0.6
            return
        if self._track and fn is None:
            fn = open_spotify
        self.hide()
        if fn:
            fn()


    def show_message(self, icon, text, fs=None, value=None, hold=1400, on_click=None, sub=None):
        """Short on-screen indicator (hotkeys, alarm, copied): icon + text
        (+ a volume bar if value is given)."""
        app = self.app
        S, P, F = app.S, app.pal_to, app.F
        X = lambda v: v * S  # noqa: E731
        w, h = 300, 60
        img = Image.new("RGBA", (self.WM, self.HM), (0, 0, 0, 0))
        d = Drawer(img)
        rr(img, 0, 0, X(w), X(h), X(30), P["surface_highest"])
        rr(img, X(1), X(1), X(w - 1), X(h - 1), X(29), P["surface_high"])
        rr(img, X(10), X(10), X(50), X(50), 0, P["primary"], ellipse=True)
        d.text((X(30), X(30)), icon, font=app.icon_font(17), fill=P["on_primary"] + (255,),
               anchor="mm")
        if sub is not None:  # two lines: small heading + main text
            d.text((X(64), X(21)), fit_text(text, F["status"], X(w - 80)), font=F["status"],
                   fill=readable(P["primary"], P["surface_high"], P["on_surface"]) + (255,),
                   anchor="lm")
            d.text((X(64), X(40)), fit_text(sub, F["mtitle"], X(w - 80)), font=F["mtitle"],
                   fill=P["on_surface"] + (255,), anchor="lm")
        elif value is None:
            d.text((X(64), X(30)), fit_text(text, F["mtitle"], X(w - 80)), font=F["mtitle"],
                   fill=P["on_surface"] + (255,), anchor="lm")
        else:
            d.text((X(64), X(22)), text, font=F["btn"], fill=P["on_surface"] + (255,),
                   anchor="lm")
            d.text((X(w - 20), X(22)), f"%{round(value * 100)}", font=F["btn"],
                   fill=readable(P["primary"], P["surface_high"], P["on_surface"]) + (255,),
                   anchor="rm")
            rr(img, X(64), X(36), X(w - 20), X(41), X(2.5), P["on_surface"], alpha=30)
            if value > 0:
                rr(img, X(64), X(36), X(64 + (w - 84) * value), X(41), X(2.5), P["primary"])
        self._present(img, w, h, fs, hold)
        self.on_click = on_click

    def _present(self, img, w, h, fs, hold):
        S = self.app.S
        self.hold = hold
        self._track = False
        self._z_logged = False
        self.on_click = None
        self.fs = fs is not None
        ex = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        ex = (ex | WS_EX_TRANSPARENT) if self.fs else (ex & ~WS_EX_TRANSPARENT)
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, ex)
        self.surface.full(img)
        m = round(16 * S)
        corner = self.app.cfg.get("toast_pos")
        corner = corner if corner in ("br", "tr", "bl", "tl") else "br"
        if fs is not None:  # games: top right, away from the usual bottom HUD
            corner = "tr"
            self._pos = (fs.right - round(w * S) - m, fs.top + m + round(8 * S))
        else:  # the monitor the user is working on
            self._pos = toast_xy(self.app.toast_area(), round(w * S), round(h * S), corner, m)
        self._offset = (14 if corner[0] == "b" else -14) * S  # slides in from the near edge
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, self._pos[0],
                            self._pos[1] + int(self._offset), 0, 0,
                            SWP_NOSIZE | SWP_NOACTIVATE)
        self._animate(255)

    def hide(self):
        self._animate(0)

    def _animate(self, target):
        self._target = target
        if self._job is not None:
            self.top.after_cancel(self._job)
            self._job = None
        self._step()

    def _step(self):
        self._job = None
        cur, target = self.surface.alpha, self._target
        ms = self.app.frame_ms()
        nxt = cur + (target - cur) * ease(0.3 if target > cur else 0.2, ms)
        nxt = target if abs(target - nxt) < 3 else round(nxt)
        self.surface.alpha = nxt
        if target > 0:
            self._offset *= 1 - ease(0.3, ms)
            # re-raise to the top every step (games can bring themselves to front)
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, self._pos[0],
                                self._pos[1] + int(self._offset), 0, 0,
                                SWP_NOSIZE | SWP_NOACTIVATE)
        self.surface._commit(None)
        if nxt != target:
            self._due = next_due(getattr(self, "_due", 0.0), time.monotonic(), ms)
            self._job = self.top.after(max(1, round((self._due - time.monotonic()) * 1000)),
                                       self._step)
        elif target > 0:  # wait (staying on top), then disappear
            self._hold_until = time.monotonic() + self.hold / 1000
            self._job = self.top.after(250, self._hold)
        else:  # hidden: move off screen
            move_window(self.hwnd, -30000, -30000)

    def _hold(self):
        self._job = None
        if not self.fs and own_window_at_cursor() == self.hwnd:  # hovered: stay, and a moment after
            self._hold_until = max(self._hold_until, time.monotonic() + 1.2)
        if time.monotonic() >= self._hold_until:
            self.hide()
            return
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | 0x0040)  # SHOWWINDOW
        if self.fs and not getattr(self, "_z_logged", False):
            self._z_logged = True
            self._log_z()
        # in fullscreen a game may often bring itself to front: re-raise more often
        self._job = self.top.after(100 if self.fs else 250, self._hold)

    def _log_z(self):
        """Diagnostics: is the toast above the foreground fullscreen window?"""
        fg = user32.GetForegroundWindow()
        h, n = user32.GetTopWindow(None), 0
        while h and n < 3000:
            if h == self.hwnd:
                log("toast z-order: above the game")
                return
            if h == fg:
                log("toast z-order: stayed BELOW the game")
                return
            h, n = user32.GetWindow(h, 2), n + 1


# ------------------------------------------------------------------ connect dialog
# ------------------------------------------------------------------ right-click menu
MI_BACK, MI_CHEV, MI_CHECK = "", "", ""
MI = {"search": "", "mini": "", "lock": "", "share": "",
      "open": "", "look": "", "widget": "",
      "notif": "", "sleep": "", "alarm": "", "device": "",
      "account": "", "lang": "", "close": "", "import": "",
      "link": "", "image": "", "auto": ""}


class MenuPanel:
    """Paged right-click menu with the same Material You look as the widget (layered window).
    Submenus open as pages in the same panel instead of dropdowns."""
    _inst = None
    PW = 300          # panel width (logical px)
    M = 14            # shadow margin
    HEIGHTS = {"quick": 66, "sep": 9, "nav": 34, "toggle": 36, "action": 34, "radio": 34,
               "seg": 64, "swatch": 64, "info": 22, "header": 26, "title": 46}

    @classmethod
    def toggle(cls, app, x, y):
        if cls._inst is not None:  # if open: reopen at the new position
            cls._inst.close()
        cls._inst = MenuPanel(app, x, y)

    @classmethod
    def close_open(cls):
        if cls._inst is not None:
            cls._inst.close()

    def __init__(self, app, x, y):
        self.app = app
        self.S = S = app.dpi
        self._font_spec = {k: (n, px, n == "seguisb.ttf") for k, (n, px) in {
            "label": ("segoeui.ttf", 13), "value": ("segoeui.ttf", 12),
            "head": ("seguisb.ttf", 11), "title": ("seguisb.ttf", 15),
            "seg": ("segoeui.ttf", 12), "segb": ("seguisb.ttf", 12),
            "tiny": ("segoeui.ttf", 10)}.items()}
        self.F = {k: TextFont(n, px, S, bold=b) for k, (n, px, b) in self._font_spec.items()}
        self._ifonts = {}
        self.pages = ["main"]
        self.scroll = 0.0
        self.hover = None
        self.hits = []
        self.devices = None      # None: loading, list, or "err"
        self.anchor = (x, y)
        wa = self._work_area(x, y)
        self.max_h = (wa.bottom - wa.top) / S - 2 * self.M - 8
        self.WM = round((self.PW + 2 * self.M) * S)
        self.HM = round((self.max_h + 2 * self.M) * S) + 2
        t = self.top = tk.Toplevel(app.root)
        t.overrideredirect(True)
        t.configure(bg="black")
        t.geometry(f"{self.WM}x{self.HM}+-30000+-30000")
        t.update_idletasks()
        self.hwnd = user32.GetAncestor(t.winfo_id(), 2)
        ex = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE,
                                 (ex | WS_EX_LAYERED | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
        user32.SetWindowLongPtrW(self.hwnd, GWLP_HWNDPARENT, 0)
        self.surface = LayeredSurface(self.hwnd, self.WM, self.HM)
        self.surface.alpha = 0
        t.bind("<Motion>", self._on_motion)
        t.bind("<Leave>", lambda _e: self._set_hover(None))
        t.bind("<ButtonRelease-1>", self._on_click)
        t.bind("<MouseWheel>", self._on_wheel)
        t.bind("<Escape>", lambda _e: self._back_or_close())
        t.bind("<BackSpace>", lambda _e: self._back_or_close())
        self._closing = False
        self.render(place=True)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)
        t.focus_force()
        user32.SetForegroundWindow(self.hwnd)
        self._fade(0)
        t.after(250, lambda: t.bind("<FocusOut>", self._on_focus_out))
        self._was_down = True
        self._watch()

    def _watch(self):
        """Work even without focus: close on a click outside the panel."""
        if self._closing:
            return
        down = bool(user32.GetAsyncKeyState(0x01) & 0x8000 or user32.GetAsyncKeyState(0x02) & 0x8000)
        if down and not self._was_down:
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            S, M = self.S, self.M
            x0, y0 = self.pos[0] + round(M * S), self.pos[1] + round(M * S)
            if not (x0 <= pt.x <= x0 + round(self.PW * S) and y0 <= pt.y <= y0 + round(self.vh * S)):
                self.close()
                return
        self._was_down = down
        self.top.after(40, self._watch)

    # -- helpers
    @staticmethod
    def _work_area(x, y):
        pt = wintypes.POINT(x, y)
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(user32.MonitorFromPoint(pt, 2), ctypes.byref(mi))
        return mi.rcWork

    def icon(self, px):
        f = self._ifonts.get(px)
        if f is None:
            f = self._ifonts[px] = ImageFont.truetype(self.app._icon_path, max(6, round(px * self.S)))
        return f

    def _fade(self, step):
        if self._closing:
            return
        self.surface.alpha = min(255, round(255 * (step + 1) / 5))
        self.surface._commit(None)
        if step < 4:
            self.top.after(14, lambda: self._fade(step + 1))

    def close(self):
        if self._closing:
            return
        self._closing = True
        MenuPanel._inst = None
        try:
            self.top.destroy()
        except tk.TclError:
            pass

    def _on_focus_out(self, _e):
        # close if focus really moved to another window
        self.top.after(60, lambda: (None if self._closing or
                                    user32.GetForegroundWindow() == self.hwnd else self.close()))

    def _back_or_close(self):
        if len(self.pages) > 1:
            self._go_back()
        else:
            self.close()

    def _open_page(self, page):
        def change():
            self.pages.append(page)
            if page == "devices":
                self._load_devices()
        self._page_fade(change)

    def _go_back(self):
        if len(self.pages) > 1:
            self._page_fade(self.pages.pop)

    def _page_fade(self, change, down=(170, 95), up=(165, 220, 255)):
        """Page change: quick fade out → draw the new page (faded, together with the move) → fade in.
        Only the window opacity changes; no redraw per frame."""
        def step(seq, i, then):
            if self._closing:
                return
            if i < len(seq):
                self.surface.alpha = seq[i]
                self.surface._commit(None)
                self.top.after(14, lambda: step(seq, i + 1, then))
            elif then:
                then()

        def swap():
            change()
            self.scroll = 0.0
            self.hover = None
            self.render()
            step(up, 0, None)

        step(down, 0, swap)

    def _then_close(self, fn):
        """Do the action and close the menu (actions that open a window / one-off actions)."""
        def go():
            self.close()
            fn()
        return go

    def _restyle(self):
        """Redraw the menu with the new font / corners when the design language changes."""
        S = self.S
        self.F = {k: TextFont(n, px, S, bold=b) for k, (n, px, b) in self._font_spec.items()}
        self._bg_key = None
        self._sw = {}

    def _load_devices(self):
        self.devices = None
        web = self.app.web
        if not web.connected:
            self.devices = "need"
            return

        def work():
            try:
                self.devices = web.devices()
            except Exception as e:  # noqa: BLE001
                log("devices:", repr(e))
                self.devices = "err"

        th = threading.Thread(target=work, daemon=True)
        th.start()

        def wait():
            if self._closing:
                return
            if th.is_alive():
                self.top.after(150, wait)
            elif self.pages[-1] == "devices":
                self.render()

        wait()

    # -- pages (rebuilt from the current state on every draw)
    def items(self):
        app, c = self.app, self.app.cfg
        page = self.pages[-1]
        if page == "main":
            th = {"system": T("th_system"), "dark": T("th_dark"), "light": T("th_light")}
            mins = app._sleep_minutes()
            al = c["alarm"]
            return [
                {"t": "quick", "buttons": [
                    ("q_search", MI["search"], T("q_search"), False,
                     self._then_close(lambda: SearchDialog(app))),
                    ("q_mini", MI["mini"], T("q_mini"), app.mini, app._toggle_mini),
                    ("q_lock", MI["lock"], T("q_lock"), c["locked"],
                     lambda: app._set("locked", not c["locked"])),
                    ("q_share", MI["share"], T("q_share"), False,
                     lambda: self._open_page("share")),
                    ("q_open", MI["open"], T("q_open"), False,
                     self._then_close(open_spotify))]},
                {"t": "header", "label": T("mh_custom")},
                {"t": "nav", "id": "p_look", "icon": MI["look"], "label": T("mn_appearance"),
                 "value": th.get(c["theme"], "") + (f' · %{c["opacity"]}' if c["opacity"] < 100
                                                    else ""), "page": "look"},
                {"t": "nav", "id": "p_widget", "icon": MI["widget"], "label": T("mn_widget"),
                 "value": "", "page": "widget"},
                {"t": "header", "label": T("mh_play")},
                {"t": "nav", "id": "p_sleep", "icon": MI["sleep"], "label": T("m_sleep"),
                 "value": T("sleep_left", m=mins) if mins else T("off"), "page": "sleep"},
                {"t": "nav", "id": "p_alarm", "icon": MI["alarm"], "label": T("m_alarm"),
                 "value": al.get("time", "08:00") if al.get("on") else T("off"), "page": "alarm"},
                {"t": "nav", "id": "p_dev", "icon": MI["device"], "label": T("m_devices"),
                 "value": "", "page": "devices"},
                {"t": "action", "id": "a_hist", "icon": "\ue81c", "label": T("hs_menu"),
                 "do": self._then_close(lambda: HistoryDialog(app))},
                {"t": "header", "label": T("mh_app")},
                {"t": "nav", "id": "p_notif", "icon": MI["notif"], "label": T("mn_notif"),
                 "value": "", "page": "notif"},
                {"t": "nav", "id": "p_acc", "icon": MI["account"], "label": T("mn_account"),
                 "value": T("acc_on") if app.web.connected else T("acc_off"), "page": "account"},
                {"t": "nav", "id": "p_lang", "icon": MI["lang"], "label": T("mn_language"),
                 "value": "Türkçe" if LANG == "tr" else "English", "page": "lang"},
                {"t": "sep"},
            ] + ([{"t": "action", "id": "a_upd", "icon": MI["import"],
                   "label": T("upd_new", v=app.update_tag),
                   "do": self._then_close(app._install_update)}]
                 if app.update_tag else []) + [
                {"t": "action", "id": "a_close", "icon": MI["close"], "label": T("m_close"),
                 "value": f"v{APP_VERSION}", "do": self._then_close(app.root.destroy)}]
        title = {"look": T("mn_appearance"),
                 "widget": T("mn_widget"), "notif": T("mn_notif"), "sleep": T("m_sleep"),
                 "alarm": T("m_alarm"), "devices": T("m_devices"), "account": T("mn_account"),
                 "lang": T("mn_language"), "share": T("mn_share")}[page]
        out = [{"t": "title", "label": title}]
        if page == "look":
            out += [
                {"t": "seg", "id": "s_ui", "label": T("m_ui"), "value": c["ui"],
                 "options": [("material", "Material You"), ("shadcn", "shadcn/ui"),
                             ("neu", "Neumorphic")],
                 "set": lambda v: (app._set_ui(v), self._restyle())},
                {"t": "seg", "id": "s_theme", "label": T("m_theme"), "value": c["theme"],
                 "options": [("system", T("th_system")), ("dark", T("th_dark")),
                             ("light", T("th_light"))], "set": app._set_theme},
                {"t": "swatch", "id": "s_acc", "label": T("m_accent"), "value": c["accent"],
                 "options": [("auto", None)] + list(ACCENTS), "set": app._set_accent},
            ][:-1 if c["ui"] == "neu" else None]   # neumorphic: fixed palette, no color choice
            out += [
                {"t": "seg", "id": "s_bg", "label": T("m_bg"), "value": c["bg"],
                 "options": [("solid", T("bg_solid")), ("glass", T("bg_glass").split(" (")[0]),
                             ("clear", T("bg_clear"))], "set": lambda v: app._set("bg", v)},
                {"t": "seg", "id": "s_size", "label": T("m_size"),
                 "value": min(SIZES, key=lambda kv: abs(kv[1] - float(c["scale"])))[1],
                 "options": [(v, T(k)) for k, v in SIZES], "set": app._set_scale},
                {"t": "info", "label": T("size_hint")},
                {"t": "header", "label": T("mn_transparency")},
                {"t": "seg", "id": "s_op", "label": T("m_opacity"), "value": c["opacity"],
                 "options": [(v, f"%{v}") for v in (100, 90, 80, 70, 60)],
                 "set": app._set_opacity},
                {"t": "toggle", "id": "t_fade", "label": T("m_fade"), "on": c["hover_opaque"],
                 "set": lambda v: app._set_fade("hover_opaque", v)},
                {"t": "seg", "id": "s_idle", "label": T("m_idle_s"), "value": c["idle_opacity"],
                 "options": [(v, f"%{v}") for v in (80, 70, 60, 50, 40, 30, 20)],
                 "set": lambda v: app._set_fade("idle_opacity", v),
                 "disabled": not c["hover_opaque"]},
            ]
        elif page == "widget":
            out += [
                {"t": "seg", "id": "s_layer", "label": T("m_layer"), "value": c["mode"],
                 "options": [("desktop", T("seg_desktop")), ("top", T("seg_top"))],
                 "set": app._set_mode},
                {"t": "toggle", "id": "t_mini", "label": T("m_mini"), "on": app.mini,
                 "set": lambda v: app._toggle_mini()},
                {"t": "toggle", "id": "t_lock", "label": T("m_lock"), "on": c["locked"],
                 "set": lambda v: app._set("locked", v)},
                {"t": "toggle", "id": "t_wave", "label": T("m_wave"), "on": c["wave"],
                 "set": lambda v: app._set("wave", v)},
                {"t": "toggle", "id": "t_react", "label": T("m_react"), "on": c["react"],
                 "set": lambda v: app._set("react", v), "disabled": not c["wave"]},
                {"t": "seg", "id": "s_fps", "label": T("m_fps"), "value": c["fps"],
                 "options": [(30, T("fps_30")), (60, T("fps_60"))],
                 "set": lambda v: app._set("fps", v)},
                {"t": "toggle", "id": "t_list", "label": T("m_showlist").split(" (")[0],
                 "on": c["show_queue"], "set": lambda v: app._set("show_queue", v)},
                {"t": "seg", "id": "s_rows", "label": T("m_rows_s"), "value": int(c["queue_rows"]),
                 "options": [(v, str(v)) for v in (2, 3, 4, 5)],
                 "set": lambda v: app._set("queue_rows", v)},
                {"t": "toggle", "id": "t_start", "label": T("m_startup"),
                 "on": app._startup_on(), "set": app._set_startup},
            ]
        elif page == "notif":
            out += [
                {"t": "toggle", "id": "t_notify", "label": T("m_notify"), "on": c["notify"],
                 "set": lambda v: app._set("notify", v)},
                {"t": "seg", "id": "s_tpos", "label": T("m_tpos"), "value": c["toast_pos"],
                 "options": [("br", "↘"), ("tr", "↗"), ("bl", "↙"), ("tl", "↖")],
                 "set": lambda v: app._set("toast_pos", v), "disabled": not c["notify"]},
                {"t": "seg", "id": "s_tsec", "label": T("m_tsecs"), "value": c["toast_secs"],
                 "options": [(v, f"{v:g} {T('sec')}".replace(".", "," if LANG == "tr" else "."))
                             for v in (2, 3.5, 6, 10)],
                 "set": lambda v: app._set("toast_secs", v), "disabled": not c["notify"]},
                {"t": "action", "id": "a_tprev", "icon": MI["notif"], "label": T("m_tprev"),
                 "disabled": not c["notify"],
                 "do": lambda: app.toast.show(app.st if app.st.get("title") else
                                              {"title": "Coverglow", "artist": APP_VERSION})},
                {"t": "toggle", "id": "t_osd", "label": T("m_osd"), "on": c["osd"],
                 "set": lambda v: app._set("osd", v)},
                {"t": "toggle", "id": "t_mem", "label": T("m_memory"), "on": c["memory"],
                 "set": lambda v: app._set("memory", v)},
                {"t": "toggle", "id": "t_duck", "label": T("m_duck"), "on": c["duck"],
                 "set": lambda v: app._set("duck", v)},
                {"t": "sep"},
                {"t": "toggle", "id": "t_hk", "label": T("hk_enable"), "on": c["hotkeys"],
                 "set": app._set_hotkeys},
                {"t": "header", "label": T("hk_list")},
            ] + [{"t": "info", "label": T(key), "value": keys} for *_x, keys, key in HOTKEYS]
        elif page == "sleep":
            cur = app.sleep_choice if app.sleep_end is not None else 0
            out += [{"t": "radio", "id": f"r_sl{v}", "label": T("sleep_off") if v == 0
                     else T("sleep_n", n=v), "on": cur == v,
                     "do": (lambda v=v: app._set_sleep(v))} for v in (0, 15, 30, 45, 60, 90)]
            mins = app._sleep_minutes()
            if mins:
                out.append({"t": "info", "label": T("sleep_left", m=mins)})
        elif page == "alarm":
            al = c["alarm"]
            out += [
                {"t": "toggle", "id": "t_alarm", "label": T("al_on"), "on": bool(al.get("on")),
                 "set": app._toggle_alarm},
                {"t": "action", "id": "a_alarm", "icon": MI["alarm"],
                 "label": T("al_set", t=al.get("time", "08:00")),
                 "do": self._then_close(lambda: AlarmDialog(app))},
            ]
        elif page == "devices":
            d = self.devices
            if d is None:
                out.append({"t": "info", "label": T("loading")})
            elif d == "need":
                out.append({"t": "action", "id": "a_conn", "icon": MI["account"],
                            "label": T("m_connect"),
                            "do": self._then_close(lambda: ConnectDialog(app))})
            elif d == "err":
                out.append({"t": "info", "label": T("dev_err")})
            elif not d:
                out.append({"t": "info", "label": T("dev_none")})
            else:
                out += [{"t": "radio", "id": f"r_dev{i}", "label": dv["name"],
                         "value": dv["type"], "on": dv["active"],
                         "do": self._then_close(lambda i=dv["id"]: app._transfer(i))}
                        for i, dv in enumerate(d)]
        elif page == "account":
            web = app.web
            if web.connected:
                out += [{"t": "info", "label": "✓ " + T("acc_on")},
                        {"t": "action", "id": "a_re", "icon": MI["account"],
                         "label": T("m_reconnect"),
                         "do": self._then_close(lambda: ConnectDialog(app, reauth=True))},
                        {"t": "action", "id": "a_disc", "icon": MI["close"],
                         "label": T("m_disconnect"), "do": web.disconnect}]
            else:
                out.append({"t": "action", "id": "a_conn", "icon": MI["account"],
                            "label": T("m_connect"),
                            "do": self._then_close(lambda: ConnectDialog(app))})
            out += [{"t": "sep"},
                    {"t": "action", "id": "a_imp", "icon": MI["import"], "label": T("m_import"),
                     "do": self._then_close(app._import_data)}]
        elif page == "lang":
            out += [{"t": "radio", "id": "r_tr", "label": "Türkçe", "on": LANG == "tr",
                     "do": lambda: app._set_lang("tr")},
                    {"t": "radio", "id": "r_en", "label": "English", "on": LANG == "en",
                     "do": lambda: app._set_lang("en")}]
        elif page == "share":
            out += [{"t": "action", "id": "a_link", "icon": MI["link"], "label": T("sh_link"),
                     "do": self._then_close(app._share_link)},
                    {"t": "action", "id": "a_card", "icon": MI["image"], "label": T("sh_card"),
                     "do": self._then_close(app._share_card)}]
        return out

    # -- drawing
    def _slide_pos(self, iid, tgt):
        """Selected segment position (index units) and whether it is still moving: slides from
        the old choice with a small overshoot, like RYOCS cubic-bezier(.3, 1.35, .5, 1) .42 s."""
        sl = self.__dict__.setdefault("_slides", {})
        now = time.monotonic()
        st = sl.get(iid)
        if st is None:
            sl[iid] = st = (tgt, tgt, now - 1)
        elif st[1] != tgt:
            sl[iid] = st = (self._slide_pos(iid, st[1])[0], tgt, now)
        f, to, t0 = st
        k = (now - t0) / 0.42
        if k >= 1:
            return to, False
        e = 1 + 2.2 * (k - 1) ** 3 + 1.2 * (k - 1) ** 2   # ease-out-back
        return f + (to - f) * e, True

    def _slide_frame(self):
        self._slide_job = None
        self.render()

    def render(self, place=False):
        if self._closing:
            return
        app, S, F = self.app, self.S, self.F
        P = app.pal_to
        X = lambda v: v * S  # noqa: E731
        items = self.items()
        PW, M = self.PW, self.M
        total = 8 + sum(self.HEIGHTS[it["t"]] for it in items) + 8
        vh = min(total, self.max_h)
        self.scroll = max(0.0, min(self.scroll, total - vh))
        content = Image.new("RGBA", (round(X(PW)), round(X(total))), (0, 0, 0, 0))
        d = Drawer(content)
        hits = []
        hov = self.hover
        animate = False
        glass = app.cfg["bg"] == "glass"
        on, var, pri = P["on_surface"] + (255,), P["on_surface_variant"] + (255,), \
            readable(P["primary"], P["surface_high"], P["on_surface"]) + (255,)
        y = 8
        for it in items:
            h = self.HEIGHTS[it["t"]]
            t = it["t"]
            iid = it.get("id")
            dis = it.get("disabled")
            if t in ("nav", "toggle", "action", "radio") and hov == iid and not dis:
                rr(content, X(6), X(y + 2), X(PW - 6), X(y + h - 2), X(12), P["on_surface"],
                   alpha=22)
            if t == "quick":
                bw = (PW - 16) / len(it["buttons"])
                for i, (bid, glyph, lbl, active, _fn) in enumerate(it["buttons"]):
                    cx = 8 + bw * (i + 0.5)
                    col = P["primary_container"] if active else P["surface_highest"]
                    if hov == bid:
                        col = mix(col, P["on_surface"], 0.10)
                    rr(content, X(cx - 20), X(y + 6), X(cx + 20), X(y + 42), X(14), col)
                    d.text((X(cx), X(y + 24)), glyph, font=self.icon(16),
                           fill=(P["on_primary_container"] if active else P["on_surface"])
                           + (255,), anchor="mm")
                    d.text((X(cx), X(y + 54)), lbl, font=F["tiny"], fill=var, anchor="mm")
                    hits.append((bid, cx - bw / 2, y, cx + bw / 2, y + h, _fn))
            elif t == "sep":
                rr(content, X(16), X(y + 4), X(PW - 16), X(y + 5), 0, P["on_surface"], alpha=24)
            elif t == "title":
                bh = "back"
                if hov == bh:
                    rr(content, X(8), X(y + 7), X(44), X(y + 43), 0, P["on_surface"], alpha=22,
                       ellipse=True)
                d.text((X(26), X(y + 25)), MI_BACK, font=self.icon(15), fill=on, anchor="mm")
                d.text((X(52), X(y + 25)), it["label"], font=F["title"], fill=on, anchor="lm")
                hits.append((bh, 0, y, PW, y + h, self._go_back))
            elif t == "header":
                d.text((X(18), X(y + h / 2 + 3)), it["label"], font=F["head"], fill=pri, anchor="lm")
            elif t == "info":
                d.text((X(18), X(y + h / 2)), fit_text(it["label"], F["value"], X(PW - 120)
                                                       if it.get("value") else X(PW - 36)),
                       font=F["value"], fill=var, anchor="lm")
                if it.get("value"):
                    d.text((X(PW - 18), X(y + h / 2)), it["value"], font=F["value"], fill=on,
                           anchor="rm")
            elif t in ("nav", "action", "toggle", "radio"):
                fg = on if not dis else P["outline"] + (255,)
                lx = 18
                if it.get("icon"):
                    d.text((X(30), X(y + h / 2)), it["icon"], font=self.icon(16),
                           fill=var if not dis else fg, anchor="mm")
                    lx = 52
                right = 0
                if t == "nav":
                    d.text((X(PW - 22), X(y + h / 2)), MI_CHEV, font=self.icon(11), fill=var,
                           anchor="mm")
                    right = 34
                    if it.get("value"):
                        d.text((X(PW - right), X(y + h / 2)), it["value"], font=F["value"],
                               fill=var, anchor="rm")
                        right += F["value"].getlength(it["value"]) / S + 8
                elif t == "action" and it.get("value"):
                    d.text((X(PW - 18), X(y + h / 2)), it["value"], font=F["tiny"], fill=var,
                           anchor="rm")
                    right = F["tiny"].getlength(it["value"]) / S + 22
                elif t == "toggle":
                    self._switch(content, PW - 54, y + h / 2, it["on"], dis, P, S)
                    right = 64
                elif t == "radio":
                    if it["on"]:
                        d.text((X(PW - 24), X(y + h / 2)), MI_CHECK, font=self.icon(14),
                               fill=pri, anchor="mm")
                    right = 40
                    if it.get("value"):
                        d.text((X(PW - right), X(y + h / 2)), it["value"], font=F["value"],
                               fill=var, anchor="rm")
                        right += F["value"].getlength(it["value"]) / S + 8
                    if it["on"]:
                        fg = pri
                d.text((X(lx), X(y + h / 2)), fit_text(it["label"], F["label"],
                                                       X(PW - lx - right - 6)),
                       font=F["label"], fill=fg, anchor="lm")
                if not dis:
                    if t == "nav":
                        fn = (lambda p=it["page"]: self._open_page(p))
                    elif t == "toggle":
                        fn = (lambda it=it: it["set"](not it["on"]))
                    else:
                        fn = it["do"]
                    hits.append((iid, 6, y, PW - 6, y + h, fn))
            elif t == "seg":
                d.text((X(18), X(y + 14)), it["label"], font=F["value"],
                       fill=var if not dis else P["outline"] + (255,), anchor="lm")
                opts = it["options"]
                x0, x1, sy0, sy1 = 14, PW - 14, y + 26, y + 58
                if STYLE["neu"]:   # RYOCS glass: .seg background white 30%
                    rr(content, X(x0), X(sy0), X(x1), X(sy1), X(16),
                       *(((255, 255, 255), 77) if glass and app.light else (P["surface_high"], 255)))
                    neu_inset(content, X(x0), X(sy0), X(x1), X(sy1), X(16))
                else:
                    rr(content, X(x0), X(sy0), X(x1), X(sy1), X(16), P["outline"], alpha=110)
                    rr(content, X(x0 + 1), X(sy0 + 1), X(x1 - 1), X(sy1 - 1), X(15),
                       P["surface_high"], alpha=150 if glass else 255)
                sw = (x1 - x0) / len(opts)
                # selected background slides from the previous choice (RYOCS .seg::before)
                tgt = next((i for i, (v, _l) in enumerate(opts) if v == it["value"]), None)
                if tgt is not None:
                    pos, moving = self._slide_pos(iid, tgt)
                    animate |= moving
                    sx0 = x0 + pos * sw
                    if STYLE["neu"]:
                        neu_btn(content, X(sx0 + 4), X(sy0 + 4), X(sx0 + sw - 4), X(sy1 - 4), X(12),
                                mix(P["surface"], (255, 255, 255), 0.7) if app.light
                                else P.get("hi", P["surface_highest"]))
                    elif STYLE["shad"]:  # shadcn Tabs: background color + thin border
                        rr(content, X(sx0 + 3), X(sy0 + 3), X(sx0 + sw - 3), X(sy1 - 3),
                           X(13), P["border"])
                        rr(content, X(sx0 + 3) + 1, X(sy0 + 3) + 1, X(sx0 + sw - 3) - 1,
                           X(sy1 - 3) - 1, X(13), P["surface"])
                    else:
                        rr(content, X(sx0 + 3), X(sy0 + 3), X(sx0 + sw - 3), X(sy1 - 3),
                           X(13), P["secondary_container"])
                for i, (val, lbl) in enumerate(opts):
                    sx0 = x0 + i * sw
                    sel = val == it["value"]
                    sid = f'{iid}:{i}'
                    if not sel and hov == sid and not dis:
                        rr(content, X(sx0 + 3), X(sy0 + 3), X(sx0 + sw - 3), X(sy1 - 3), X(13),
                           P["on_surface"], alpha=20)
                    col = (P["on_secondary_container"] if sel else
                           (P["on_surface_variant"] if STYLE["shad"] or STYLE["neu"] else P["on_surface"])) \
                        if not dis else P["outline"]
                    d.text((X(sx0 + sw / 2), X((sy0 + sy1) / 2)),
                           fit_text(lbl, F["segb" if sel else "seg"], X(sw - 8)),
                           font=F["segb" if sel else "seg"], fill=col + (255,), anchor="mm")
                    if not dis:
                        hits.append((sid, sx0, sy0, sx0 + sw, sy1,
                                     (lambda s=it["set"], v=val: s(v))))
            elif t == "swatch":
                d.text((X(18), X(y + 14)), it["label"], font=F["value"], fill=var, anchor="lm")
                opts = it["options"]
                sw = (PW - 28) / len(opts)
                for i, (val, seed) in enumerate(opts):
                    cx, cy = 14 + sw * (i + 0.5), y + 44
                    sid = f'{iid}:{i}'
                    sel = val == it["value"]
                    if sel or hov == sid:
                        rr(content, X(cx - 15), X(cy - 15), X(cx + 15), X(cy + 15), 0,
                           P["primary"] if sel else P["outline"], ellipse=True)
                        rr(content, X(cx - 13), X(cy - 13), X(cx + 13), X(cy + 13), 0,
                           P["surface_high"], ellipse=True)
                    if seed is None:  # from the album cover
                        rr(content, X(cx - 11), X(cy - 11), X(cx + 11), X(cy + 11), 0,
                           P["surface_highest"], ellipse=True)
                        d.text((X(cx), X(cy)), MI["auto"], font=self.icon(12), fill=on,
                               anchor="mm")
                    else:
                        ck = (seed, app.light)
                        cache = self.__dict__.setdefault("_sw", {})
                        if ck not in cache:
                            cache[ck] = build_scheme(seed, app.light)["primary"]
                        col = cache[ck]
                        rr(content, X(cx - 11), X(cy - 11), X(cx + 11), X(cy + 11), 0, col,
                           ellipse=True)
                    hits.append((sid, cx - sw / 2, y + 26, cx + sw / 2, y + 62,
                                 (lambda s=it["set"], v=val: s(v))))
            y += h
        if animate and not getattr(self, "_slide_job", None):
            self._slide_due = next_due(getattr(self, "_slide_due", 0.0), time.monotonic(),
                                       app.frame_ms())
            self._slide_job = self.top.after(
                max(1, round((self._slide_due - time.monotonic()) * 1000)), self._slide_frame)

        # place in the window: shadow + card + content
        pos = self._place(vh, place)
        sk = (vh, P["surface_high"], STYLE["shad"], STYLE["neu"], glass and tuple(pos))
        if getattr(self, "_bg_key", None) != sk:  # shadow + card background (when the height changes)
            gl = app._glass_at(pos[0], pos[1], self.WM, self.HM) if glass else None
            from PIL import ImageFilter
            bg = Image.new("RGBA", (self.WM, self.HM), (0, 0, 0, 0))
            shadow = Image.new("RGBA", (round(X(PW + 2 * M)), round(X(vh + 2 * M))),
                               (0, 0, 0, 0))
            rr(shadow, X(M), X(M + 3), X(M + PW), X(M + vh + 3), X(18), (0, 0, 0), alpha=110)
            bg.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(X(7))))
            if gl is not None:  # glass: the widget's glass card style, wallpaper behind the menu
                app._card(bg, X(M), X(M), X(M + PW), X(M + vh), X(18), glass=gl)
            elif STYLE["shad"]:  # shadcn popover: border + flat fill
                rr(bg, X(M), X(M), X(M + PW), X(M + vh), X(18), P["border"])
                rr(bg, X(M) + 1, X(M) + 1, X(M + PW) - 1, X(M + vh) - 1, X(18), P["surface"])
            else:
                rr(bg, X(M), X(M), X(M + PW), X(M + vh), X(18), P["surface_high"])
            self._bg, self._bg_key = bg, sk
        canvas = self._bg.copy()
        part = content.crop((0, round(X(self.scroll)), content.width,
                             round(X(self.scroll + vh))))
        mask = _rr_mask(part.width, part.height, 0, 0, part.width, part.height, X(18))
        a = part.getchannel("A")
        part.putalpha(Image.composite(a, Image.new("L", a.size, 0), mask))
        canvas.alpha_composite(part, (round(X(M)), round(X(M))))
        if total > vh:  # scrollbar
            th_ = vh * vh / total
            ty = self.scroll / (total - vh) * (vh - th_)
            rr(canvas, X(M + PW - 6), X(M + ty + 4), X(M + PW - 3), X(M + ty + th_ - 4),
               X(1.5), P["on_surface"], alpha=70)
        self.hits = hits
        self.vh = vh
        self.surface.full(canvas, pos=pos)

    @staticmethod
    def _switch(img, x, cy, on, dis, P, S):
        X = lambda v: v * S  # noqa: E731
        w, h = 40, 24
        if STYLE["neu"]:
            rr(img, X(x), X(cy - h / 2), X(x + w), X(cy + h / 2), X(h / 2), P["surface_high"])
            neu_inset(img, X(x), X(cy - h / 2), X(x + w), X(cy + h / 2), X(h / 2))
            r, cx = 8, (x + w - h / 2 if on else x + h / 2)
            rr(img, X(cx - r), X(cy - r), X(cx + r), X(cy + r), 0,
               P["outline"] if dis else P["primary"] if on else P["on_surface_variant"],
               ellipse=True, plain=True)
            return
        if on:
            rr(img, X(x), X(cy - h / 2), X(x + w), X(cy + h / 2), X(h / 2),
               P["primary"] if not dis else P["outline"])
            r = 9
            cx = x + w - h / 2
            rr(img, X(cx - r), X(cy - r), X(cx + r), X(cy + r), 0, P["on_primary"], ellipse=True)
        else:
            rr(img, X(x), X(cy - h / 2), X(x + w), X(cy + h / 2), X(h / 2), P["outline"],
               alpha=200 if not dis else 90)
            rr(img, X(x + 2), X(cy - h / 2 + 2), X(x + w - 2), X(cy + h / 2 - 2), X(h / 2 - 2),
               P["surface_highest"])
            r = 6
            cx = x + h / 2
            rr(img, X(cx - r), X(cy - r), X(cx + r), X(cy + r), 0, P["outline"], ellipse=True)

    def _place(self, vh, first):
        """At the cursor on first open; on page change, positioned so it doesn't leave the screen."""
        S, M = self.S, self.M
        ax, ay = self.anchor
        wa = self._work_area(ax, ay)
        w, h = round((self.PW + 2 * M) * S), round((vh + 2 * M) * S)
        if first:
            x = ax - round(M * S) + 2
            y = ay - round(M * S) + 2
            if x + w > wa.right:
                x = ax - w + round(M * S) - 2
            self.up = y + h > wa.bottom  # open upward if it doesn't fit below
            if self.up:
                y = ay - h + round(M * S) - 2
            self.pos = [x, y]
            self.bottom = y + h
        x, y = self.pos
        if self.up:  # opened upward: bottom edge fixed so short pages stay next to the cursor
            y = self.bottom - h
        x = max(wa.left - round(M * S), min(x, wa.right - w + round(M * S)))
        y = max(wa.top - round(M * S), min(y, wa.bottom - h + round(M * S)))
        self.pos = [x, y]
        return x, y   # the move happens in the same call as the drawing (surface.full pos=)

    # -- events
    def _hit(self, e):
        x = e.x / self.S - self.M
        y = e.y / self.S - self.M
        if not (0 <= x <= self.PW and 0 <= y <= self.vh):
            return None
        y += self.scroll
        for h in self.hits:
            if h[1] <= x <= h[3] and h[2] <= y <= h[4]:
                return h
        return None

    def _set_hover(self, hid):
        if hid != self.hover:
            self.hover = hid
            self.top.config(cursor="hand2" if hid else "")
            self.render()

    def _on_motion(self, e):
        h = self._hit(e)
        self._set_hover(h[0] if h else None)

    def _on_click(self, e):
        h = self._hit(e)
        x, y = e.x / self.S - self.M, e.y / self.S - self.M
        if h is None:
            if not (0 <= x <= self.PW and 0 <= y <= self.vh):
                self.close()  # click on the shadow / outside
            return
        h[5]()
        if not self._closing:
            self.app.base_key = None
            self.render()

    def _on_wheel(self, e):
        self.scroll -= e.delta / 120 * 40
        self.render()


def copy_image_to_clipboard(img):
    """Put a PIL image on the Windows clipboard (CF_DIB)."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "BMP")
    data = buf.getvalue()[14:]  # DIB without BITMAPFILEHEADER
    k32 = ctypes.windll.kernel32
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    h = k32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
    p = k32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    k32.GlobalUnlock(h)
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        return bool(user32.SetClipboardData(8, h))  # CF_DIB
    finally:
        user32.CloseClipboard()


def _hx(c):
    return "#%02x%02x%02x" % tuple(c[:3])


_PRIVATE_FONTS = set()


def load_private_fonts():
    """Register the bundled fonts (Geist, Poppins) with Windows for this process only,
    so tk windows can use them too."""
    try:
        for f in os.listdir(BUNDLED_FONTS):
            if f.lower().endswith(".ttf") and f not in _PRIVATE_FONTS:
                if ctypes.windll.gdi32.AddFontResourceExW(os.path.join(BUNDLED_FONTS, f),
                                                          0x10, 0):  # FR_PRIVATE
                    _PRIVATE_FONTS.add(f)
    except OSError:
        pass


def style_titlebar(win, bg, fg, dark):
    """Windows 11: paint the title bar and border in the window color, round the corners."""
    try:
        win.update_idletasks()
        hwnd = user32.GetAncestor(win.winfo_id(), 2)
        dwm = ctypes.windll.dwmapi

        def setv(attr, val):
            v = ctypes.c_int(val)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

        def cref(c):
            return c[0] | (c[1] << 8) | (c[2] << 16)

        setv(20, 1 if dark else 0)      # DWMWA_USE_IMMERSIVE_DARK_MODE
        setv(33, 2)                      # DWMWA_WINDOW_CORNER_PREFERENCE: round
        setv(35, cref(bg))               # DWMWA_CAPTION_COLOR
        setv(36, cref(fg))               # DWMWA_TEXT_COLOR
        setv(34, cref(bg))               # DWMWA_BORDER_COLOR
    except Exception:  # noqa: BLE001  (some of these attributes don't exist on Windows 10)
        pass


class PillButton(tk.Label):
    """Material You / shadcn button: PIL-drawn rounded background + text, with hover / pressed
    states. kind: filled (primary), tonal, text, outline."""

    def __init__(self, dlg, parent, text, cmd, kind="tonal", small=False):
        super().__init__(parent, bg=dlg.bg, bd=0, cursor="hand2", highlightthickness=0,
                         takefocus=1)
        self.dlg, self.cmd = dlg, cmd
        P, dpi = dlg.P, dlg.app.dpi
        shad = STYLE["shad"]
        if shad and kind == "tonal":
            kind = "outline"   # shadcn 'outline' button
        fill, fg, border = {
            "filled": (P["primary"], P["on_primary"], None),
            "tonal": (P["secondary_container"], P["on_secondary_container"], None),
            "outline": (dlg.Pbg, P["on_surface"], P.get("border", P["outline"])),
            "text": (None, P["primary"], None),
        }[kind]
        neu = STYLE["neu"] and kind in ("filled", "tonal")
        if neu:  # raised button; room around it for two shadows
            fill = P["primary_container"] if kind == "filled" else P["secondary_container"]
            fg = P["on_primary_container"] if kind == "filled" else P["on_secondary_container"]
        f = TextFont("seguisb.ttf", 12 if small else 13, dpi, bold=True)
        h = round((32 if small else 40) * dpi)
        sh = round(5 * dpi) if neu else 0
        pad = (14 if small else 22) * dpi
        w = round(f.getlength(text) + 2 * pad)
        r = h / 2 if not shad else 8 * dpi
        self._imgs = []
        from PIL import ImageTk
        for k in (0.0, 0.08, 0.14):   # normal, hover, pressed
            img = Image.new("RGBA", (w + 2 * sh, h + 2 * sh), (0, 0, 0, 0))
            base = fill if fill is not None else dlg.Pbg
            col = mix(base, fg, k) if (fill is not None or k) else None
            if neu:
                img.paste(tuple(dlg.Pbg) + (255,), (0, 0) + img.size)
                neu_btn(img, sh, sh, sh + w, sh + h, r, mix(base, fg, min(k, 0.08)), pressed=k > 0.1)
            elif border is not None:
                rr(img, 0, 0, w, h, r, border)
                rr(img, 1, 1, w - 1, h - 1, r - 1, col or base)
            elif col is not None:
                rr(img, 0, 0, w, h, r, col)
            Drawer(img).text((sh + w / 2, sh + h / 2), text, font=f, fill=tuple(fg) + (255,),
                             anchor="mm")
            self._imgs.append(ImageTk.PhotoImage(img))
        self.config(image=self._imgs[0])
        self.bind("<Enter>", lambda _e: self.config(image=self._imgs[1]))
        self.bind("<Leave>", lambda _e: self.config(image=self._imgs[0]))
        self.bind("<ButtonPress-1>", lambda _e: self.config(image=self._imgs[2]))
        self.bind("<ButtonRelease-1>", self._release)
        # keyboard: reached with Tab (shown highlighted), Space / Enter presses
        self.bind("<FocusIn>", lambda _e: self.config(image=self._imgs[1]))
        self.bind("<FocusOut>", lambda _e: self.config(image=self._imgs[0]))
        for key in ("<space>", "<Return>"):
            self.bind(key, lambda _e: self.cmd and self.cmd())

    def _release(self, e):
        inside = 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height()
        self.config(image=self._imgs[1 if inside else 0])
        if inside and self.cmd:
            self.cmd()


class _Dialog:
    """Base tk window matching the widget's design language (Material You / shadcn), single instance."""
    _open = {}

    def _setup(self, app, title):
        old = _Dialog._open.get(type(self).__name__)
        if old is not None:
            try:
                old.lift()
                old.focus_force()
                return False
            except tk.TclError:
                pass
        self.app = app
        P = self.P = app.pal_to
        shad = STYLE["shad"]
        # M3 dialog: surface-container-high; shadcn: card background
        self.Pbg = P["surface"] if shad else P["surface_high"]
        self.bg = _hx(self.Pbg)
        self.fg = _hx(P["on_surface"])
        self.dim = _hx(P["on_surface_variant"])
        self.acc = _hx(P["primary"])
        self.on_acc = _hx(P["on_primary"])
        self.field = _hx(P["surface_highest"] if not shad else P["surface_high"])
        self.line = _hx(P.get("border", P["outline"]) if shad else P["surface_highest"])
        load_private_fonts()
        if shad and "Geist-Regular.ttf" in _PRIVATE_FONTS:
            self.ff, self.ffb = "Geist", "Geist SemiBold"
        elif STYLE["neu"] and "Poppins-Light.ttf" in _PRIVATE_FONTS:
            self.ff, self.ffb = "Poppins Light", "Poppins Medium"
        else:
            self.ff, self.ffb = "Segoe UI", "Segoe UI Semibold"
        t = self.top = tk.Toplevel(app.root)
        _Dialog._open[type(self).__name__] = t
        t.title(title)
        t.configure(bg=self.bg, padx=24, pady=20)
        t.resizable(False, False)
        t.attributes("-topmost", True)
        t.protocol("WM_DELETE_WINDOW", self.close)
        t.bind("<Escape>", lambda _e: self.close())
        style_titlebar(t, self.Pbg, P["on_surface"], not app.light)
        return True

    def font(self, size, bold=False):
        return (self.ffb if bold else self.ff, size)

    def btn(self, parent, text, cmd, primary=False, small=False, kind=None):
        return PillButton(self, parent, text, cmd,
                          kind=kind or ("filled" if primary else "tonal"), small=small)

    def entry_box(self, parent, width, font=None, mono=False):
        """Rounded input (Material: pill, shadcn: 8 px + border).
        Returns (container, Entry); the border turns primary on focus."""
        from PIL import ImageTk
        dpi = self.app.dpi
        w, h = round(width * dpi), round(44 * dpi)
        c = tk.Canvas(parent, width=w, height=h, bg=self.bg, highlightthickness=0, bd=0)
        P, shad = self.P, STYLE["shad"]
        r = h / 2 if not shad else 8 * dpi
        imgs = {}
        for focus in (False, True):
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            edge = P["primary"] if focus else (P.get("border", P["outline"]) if shad
                                                else mix(self.Pbg, (0, 0, 0), 0.35)
                                                if STYLE["neu"] else P["outline"])
            bw = 2 if focus else 1
            rr(img, 0, 0, w, h, r, edge)
            fillc = P["surface_highest"] if not shad else self.Pbg
            rr(img, bw, bw, w - bw, h - bw, r - bw, fillc)
            imgs[focus] = ImageTk.PhotoImage(img)
        c._imgs = imgs
        item = c.create_image(0, 0, image=imgs[False], anchor="nw")
        fillc = _hx(P["surface_highest"] if not shad else self.Pbg)
        e = tk.Entry(c, bg=fillc, fg=self.fg, insertbackground=self.fg, relief="flat", bd=0,
                     highlightthickness=0, font=font or (("Consolas", 11) if mono
                                                          else self.font(12)))
        pad = round(18 * dpi) if not shad else round(12 * dpi)
        c.create_window(pad, h / 2, window=e, anchor="w", width=w - 2 * pad)
        e.bind("<FocusIn>", lambda _e: c.itemconfig(item, image=imgs[True]), add="+")
        e.bind("<FocusOut>", lambda _e: c.itemconfig(item, image=imgs[False]), add="+")
        return c, e

    def switch(self, parent, text, value):
        """Material / shadcn switch + label. Read the value with .get()."""
        from PIL import ImageTk
        dpi = self.app.dpi
        row = tk.Frame(parent, bg=self.bg, cursor="hand2")
        S = dpi
        imgs = {}
        for on in (False, True):
            img = Image.new("RGBA", (round(46 * S), round(28 * S)), (0, 0, 0, 0))
            MenuPanel._switch(img, 3, 14, on, False, self.P, S)
            imgs[on] = ImageTk.PhotoImage(img)
        state = {"on": bool(value)}
        sw = tk.Label(row, image=imgs[state["on"]], bg=self.bg, bd=0)
        sw._imgs = imgs
        lbl = tk.Label(row, text=text, bg=self.bg, fg=self.fg, font=self.font(10))
        sw.pack(side="left")
        lbl.pack(side="left", padx=(8, 0))

        def toggle(_e=None):
            state["on"] = not state["on"]
            sw.config(image=imgs[state["on"]])

        for w_ in (row, sw, lbl):
            w_.bind("<Button-1>", toggle)
        row.get = lambda: state["on"]
        return row

    def title_label(self, parent, text, size=16):
        return tk.Label(parent, text=text, bg=self.bg, fg=self.fg, font=self.font(size, True),
                        justify="left", anchor="w")

    def close(self):
        _Dialog._open.pop(type(self).__name__, None)
        try:
            self.top.destroy()
        except tk.TclError:
            pass


class ConnectDialog(_Dialog):
    """Spotify account connection (PKCE) window."""

    def __init__(self, app, reauth=False):
        if not self._setup(app, T("dlg_title")):
            return
        t = self.top
        self.title_label(t, T("dlg_head")).pack(anchor="w")
        if reauth:
            tk.Label(t, text=T("dlg_reauth"), bg=self.bg, fg=self.acc, justify="left",
                     wraplength=round(470 * app.dpi),
                     font=self.font(10, True)).pack(anchor="w", pady=(6, 0))
        tk.Label(t, text=T("dlg_steps", uri=REDIRECT_URI), bg=self.bg, fg=self.dim,
                 justify="left", font=self.font(10)).pack(anchor="w", pady=(8, 14))
        row = tk.Frame(t, bg=self.bg)
        row.pack(anchor="w", pady=(0, 16))
        self.btn(row, T("dlg_dev"),
                 lambda: webbrowser.open("https://developer.spotify.com/dashboard")
                 ).pack(side="left")
        self.btn(row, T("dlg_copy"), self._copy).pack(side="left", padx=8)
        tk.Label(t, text="Client ID", bg=self.bg, fg=self.dim,
                 font=self.font(9)).pack(anchor="w", pady=(0, 4))
        holder, self.entry_w = self.entry_box(t, 470, mono=True)
        holder.pack(anchor="w", pady=(0, 16))
        if app.cfg.get("client_id"):
            self.entry_w.insert(0, app.cfg["client_id"])
        self.entry_w.bind("<Return>", lambda _e: self._connect())
        bottom = tk.Frame(t, bg=self.bg)
        bottom.pack(fill="x")
        self.status = tk.Label(bottom, text="", bg=self.bg, fg=self.dim, font=self.font(9),
                               wraplength=round(280 * app.dpi), justify="left")
        self.status.pack(side="left")
        self.btn(bottom, T("dlg_connect"), self._connect, primary=True).pack(side="right")
        self.btn(bottom, T("dlg_close"), self.close, kind="text").pack(side="right", padx=4)
        t.focus_force()
        self.entry_w.focus_set()

    def _copy(self):
        self.top.clipboard_clear()
        self.top.clipboard_append(REDIRECT_URI)
        self.status.config(text=T("dlg_copied"))

    def _connect(self):
        cid = self.entry_w.get().strip()
        if len(cid) < 16:
            self.status.config(text=T("dlg_invalid"))
            return
        self.app.web.auth_status = None
        self.app.web.start_auth(cid)
        self._poll()

    def _poll(self):
        try:
            s = self.app.web.auth_status
            if s == "ok":
                self.status.config(text=T("dlg_ok"))
                self.top.after(900, self.close)
                return
            self.status.config(text=s or "")
            if self.app.web.auth_error:
                return
            self.top.after(300, self._poll)
        except tk.TclError:  # window closed
            pass


class SearchDialog(_Dialog):
    def __init__(self, app):
        if not self._setup(app, T("s_title")):
            return
        t, web = self.top, app.web
        tk.Label(t, text=T("s_title"), bg=self.bg, fg=self.fg,
                 font=self.font(15, True)).pack(anchor="w")
        tk.Label(t, text=T("s_hint"), bg=self.bg, fg=self.dim,
                 font=self.font(9)).pack(anchor="w", pady=(2, 8))
        holder, self.entry = self.entry_box(t, 480)
        holder.pack(anchor="w")
        self.entry.bind("<Return>", lambda _e: self._search())
        self.results = tk.Frame(t, bg=self.bg)
        self.results.pack(fill="x", pady=(10, 0))
        self.status = tk.Label(t, text="", bg=self.bg, fg=self.dim, font=self.font(9),
                               wraplength=420, justify="left")
        self.status.pack(anchor="w", pady=(8, 0))
        self._imgs = []
        if not web.connected:
            self.status.config(text=T("s_need"))
            self.btn(t, T("connect_btn"), lambda: (self.close(), ConnectDialog(app)),
                     primary=True).pack(anchor="e", pady=(8, 0))
        t.focus_force()
        self.entry.focus_set()

    def _search(self):
        q = self.entry.get().strip()
        if not q or not self.app.web.connected:
            return
        self.status.config(text=T("loading"))
        res = {}

        def work():
            try:
                items = self.app.web.search(q)
                for it in items:
                    self.app.web._thumb(it["img"])
                res["items"] = items
            except Exception as e:  # noqa: BLE001
                res["err"] = e

        th = threading.Thread(target=work, daemon=True)
        th.start()

        def wait():
            if th.is_alive():
                self.top.after(120, wait)
                return
            if "err" in res:
                log("search:", repr(res["err"]))
                self.status.config(text=T("err_http", code=getattr(res["err"], "code", "?")))
                return
            self._show(res["items"])

        wait()

    def _show(self, items):
        from PIL import ImageTk
        for w in self.results.winfo_children():
            w.destroy()
        self._imgs.clear()
        self.status.config(text="" if items else T("s_none"))
        for it in items:
            row = tk.Frame(self.results, bg=self.bg)
            row.pack(fill="x", pady=3)
            src = self.app.web.thumbs.get(it["img"])
            if src is not None:
                ph = ImageTk.PhotoImage(rounded_image(src, 40, 8))
                self._imgs.append(ph)
                tk.Label(row, image=ph, bg=self.bg).pack(side="left", padx=(0, 10))
            txt = tk.Frame(row, bg=self.bg)
            txt.pack(side="left", fill="x", expand=True)
            tk.Label(txt, text=it["name"][:48], bg=self.bg, fg=self.fg, anchor="w",
                     font=self.font(10, True)).pack(anchor="w")
            tk.Label(txt, text=f'{it["artist"][:40]}  ·  {fmt_time(it["dur"])}', bg=self.bg,
                     fg=self.dim, anchor="w", font=self.font(9)).pack(anchor="w")
            self.btn(row, "＋ " + T("s_queue"), lambda u=it["uri"]: self._do("queue", u),
                     small=True).pack(side="right", padx=(6, 0))
            self.btn(row, "▶ " + T("s_play"), lambda u=it["uri"]: self._do("play", u),
                     primary=True, small=True).pack(side="right")

    def _do(self, what, uri):
        web = self.app.web
        if not web.has_control:
            if what == "play":  # no permission: open in the Spotify app
                open_spotify(uri)
            self.status.config(text=T("need_scope"))
            if not getattr(self, "_reauth_btn", None):
                self._reauth_btn = self.btn(
                    self.top, T("connect_btn"),
                    lambda: (self.close(), ConnectDialog(self.app, reauth=True)), primary=True)
                self._reauth_btn.pack(anchor="e", pady=(8, 0))
            return
        res = {}

        def work():
            try:
                (web.play_uri if what == "play" else web.queue_uri)(uri)
            except urllib.error.HTTPError as e:
                res["code"] = e.code
            except Exception as e:  # noqa: BLE001
                res["code"] = repr(e)

        th = threading.Thread(target=work, daemon=True)
        th.start()

        def wait():
            if th.is_alive():
                self.top.after(120, wait)
                return
            code = res.get("code")
            if code is None:
                self.status.config(text=T("s_queued") if what == "queue" else "▶")
            elif code in (403, 404):  # no Premium or no active device
                open_spotify(uri)
                self.status.config(text=T("s_premium"))
            else:
                self.status.config(text=T("err_http", code=code))

        wait()


class AlarmDialog(_Dialog):
    def __init__(self, app):
        if not self._setup(app, T("al_title")):
            return
        t = self.top
        al = app.cfg["alarm"]
        hh, mm = (al.get("time") or "08:00").split(":")
        tk.Label(t, text=T("al_title"), bg=self.bg, fg=self.fg,
                 font=self.font(15, True)).pack(anchor="w")
        tk.Label(t, text=T("al_text"), bg=self.bg, fg=self.dim, justify="left",
                 font=self.font(9)).pack(anchor="w", pady=(4, 12))
        row = tk.Frame(t, bg=self.bg)
        row.pack(anchor="w")
        # time picker: big digits + up/down buttons (the wheel works too)
        self.h = self._picker(row, int(hh), 24)
        self.h.pack(side="left")
        tk.Label(row, text=":", bg=self.bg, fg=self.fg,
                 font=self.font(28, True)).pack(side="left", padx=8)
        self.m = self._picker(row, int(mm), 60)
        self.m.pack(side="left")
        self.on = self.switch(t, T("al_on"), True)
        self.on.pack(anchor="w", pady=(14, 0))
        bottom = tk.Frame(t, bg=self.bg)
        bottom.pack(fill="x", pady=(16, 0))
        self.btn(bottom, T("al_save"), self._save, primary=True).pack(side="right")
        self.btn(bottom, T("dlg_close"), self.close, kind="text").pack(side="right", padx=4)
        t.focus_force()

    def _picker(self, parent, value, mod):
        """Material time picker box: big digit on a tonal background, arrow buttons above/below."""
        box = tk.Frame(parent, bg=self.bg)
        state = {"v": value % mod}
        up = self.btn(box, "▲", lambda: step(1), small=True, kind="text")
        lbl = tk.Label(box, text=f"{state['v']:02d}", bg=_hx(self.P["secondary_container"]),
                       fg=_hx(self.P["on_secondary_container"]), font=self.font(28, True),
                       width=3, pady=4)
        down = self.btn(box, "▼", lambda: step(-1), small=True, kind="text")

        def step(d):
            state["v"] = (state["v"] + d) % mod
            lbl.config(text=f"{state['v']:02d}")

        for w_ in (box, up, lbl, down):  # wheel works anywhere on the box
            w_.bind("<MouseWheel>", lambda e: step(1 if e.delta > 0 else -1))
        up.pack()
        lbl.pack(pady=2)
        down.pack()
        box.get = lambda: state["v"]
        return box

    def _save(self):
        h, m = int(self.h.get()) % 24, int(self.m.get()) % 60
        self.app.cfg["alarm"] = {"on": bool(self.on.get()), "time": f"{h:02d}:{m:02d}"}
        self.app.cfg.save()
        self.app._alarm_day = None
        self.close()


class RecapWindow(_Dialog):
    def __init__(self, app, year):
        if not self._setup(app, T("wr_title", y=year)):
            return
        from PIL import ImageTk
        self.img = app._render_recap(year)
        disp = self.img
        if disp.height > 720:  # fit on the screen
            sc = 720 / disp.height
            disp = disp.resize((round(disp.width * sc), 720), Image.Resampling.LANCZOS)
        self.ph = ImageTk.PhotoImage(disp)
        tk.Label(self.top, image=self.ph, bg=self.bg).pack()
        bottom = tk.Frame(self.top, bg=self.bg)
        bottom.pack(fill="x", pady=(12, 0))
        self.status = tk.Label(bottom, text="", bg=self.bg, fg=self.dim, font=self.font(9))
        self.status.pack(side="left")
        self.btn(bottom, T("wr_copy"), self._copy, primary=True).pack(side="right")
        self.btn(bottom, T("wr_save"), lambda: self._save(year)).pack(side="right", padx=8)
        self.top.focus_force()

    def _copy(self):
        if copy_image_to_clipboard(self.img):
            self.status.config(text=T("wr_copied"))

    def _save(self, year):
        from tkinter import filedialog
        p = filedialog.asksaveasfilename(parent=self.top, defaultextension=".png",
                                         initialfile=f"spotify-{year}.png",
                                         filetypes=[("PNG", "*.png")])
        if p:
            self.img.save(p)
            self.status.config(text="✓ " + os.path.basename(p))


class SongDialog(_Dialog):
    """Current track details: cover, artists, album, date, length, position + your listening."""

    def __init__(self, app):
        st = app._view()
        if not st.get("title"):
            return
        if not self._setup(app, st["title"]):
            return
        self.st = st
        t, dpi = self.top, app.dpi
        self.web = app.web
        cur = self.web.current
        self.tid = cur.get("id") if cur and cur.get("name") == st["title"] else None
        body = tk.Frame(t, bg=self.bg)
        body.pack(fill="both")
        # cover
        sz = round(210 * dpi)
        self._art_lbl = tk.Label(body, bg=self.bg)
        self._art_lbl.pack(side="left", anchor="n", padx=(0, 18))
        self._set_art(app.art_src, sz)
        # details
        info = tk.Frame(body, bg=self.bg)
        info.pack(side="left", fill="both", anchor="n")
        self.info = info
        tk.Label(info, text=st["title"], bg=self.bg, fg=self.fg, justify="left", anchor="w",
                 wraplength=round(330 * dpi), font=self.font(16, True)).pack(anchor="w")
        self.artists = tk.Frame(info, bg=self.bg)
        self.artists.pack(anchor="w", pady=(2, 0))
        self._link(self.artists, st.get("artist") or T("unknown"),
                   lambda: app._open_entity("artist"), size=11).pack(side="left")
        self.album_lbl = self._link(info, st.get("album") or "", lambda: app._open_entity("album"),
                                    size=10, dim=True)
        self.album_lbl.pack(anchor="w", pady=(2, 10))
        self.meta = tk.Frame(info, bg=self.bg)
        self.meta.pack(anchor="w", fill="x")
        self._rows = 0
        pos, end = app._position()
        if end:
            self._meta(T("sd_dur"), fmt_time(end))
        liked = self.web.liked_state(st["title"])
        if liked is not None:
            self._meta(T("sd_liked"), "♥ " + T("osd_liked") if liked else "—")
        # your listening (from history)
        tk.Frame(t, bg=self.line, height=1).pack(fill="x", pady=(16, 10))
        tk.Label(t, text=T("sd_yours"), bg=self.bg, fg=self.acc,
                 font=self.font(9, True)).pack(anchor="w")
        self.mine = tk.Label(t, text=T("loading"), bg=self.bg, fg=self.fg, justify="left",
                             anchor="w", font=self.font(10))
        self.mine.pack(anchor="w", pady=(4, 0))
        row = tk.Frame(t, bg=self.bg)
        row.pack(fill="x", pady=(14, 0))
        self.status = tk.Label(row, text="", bg=self.bg, fg=self.dim, font=self.font(9))
        self.status.pack(side="left")
        self.btn(row, T("sd_open"), self._open, primary=True).pack(side="right")
        self.btn(row, T("sh_link"), self._copy).pack(side="right", padx=8)
        t.focus_force()
        t.after(30, self._fill_mine)
        if self.tid and self.web.connected:
            self._load_track()

    # -- helpers
    def _set_art(self, img, sz):
        from PIL import ImageTk
        if img is None:
            ph = Image.new("RGB", (sz, sz), self.app.pal["surface_highest"])
            img = ph
        self._ph = ImageTk.PhotoImage(rounded_image(img, sz, 14 * self.app.dpi))
        self._art_lbl.config(image=self._ph)

    def _link(self, parent, text, cmd, size=10, dim=False):
        lbl = tk.Label(parent, text=text, bg=self.bg, fg=self.dim if dim else self.fg,
                       cursor="hand2", font=self.font(size), justify="left",
                       wraplength=round(330 * self.app.dpi))
        lbl.bind("<Button-1>", lambda _e: cmd())
        lbl.bind("<Enter>", lambda _e: lbl.config(fg=self.acc))
        lbl.bind("<Leave>", lambda _e: lbl.config(fg=self.dim if dim else self.fg))
        return lbl

    def _meta(self, key, val):
        tk.Label(self.meta, text=key, bg=self.bg, fg=self.dim, font=self.font(9),
                 anchor="w").grid(row=self._rows, column=0, sticky="w", pady=1)
        tk.Label(self.meta, text=val, bg=self.bg, fg=self.fg, font=self.font(10),
                 anchor="w").grid(row=self._rows, column=1, sticky="w", padx=(14, 0), pady=1)
        self._rows += 1

    def _fill_mine(self):
        app, st = self.app, self.st
        artist1 = (st.get("artist") or "").split(", ")[0].strip()
        tracks, _a = app._history_index()
        e = tracks.get(f'{st["title"]}\t{artist1}')
        if not e:  # the artist name may be spelled differently: search by title
            for k, v in tracks.items():
                if k.partition("\t")[0] == st["title"]:
                    e = v
                    break
        if not e:
            self.mine.config(text=T("sd_first"))
            return
        best = max(e["years"].items(), key=lambda kv: kv[1])[0] if e["years"] else "–"
        self.mine.config(text=T("hs_detail", name="", dur=fmt_dur(e["sec"]),
                                plays=f'{e["plays"]:,}'.replace(",", "."), days=e["days"],
                                first=e["first"], last=e["last"], year=best).strip())

    def _load_track(self):
        res = {}

        def work():
            try:
                tr = self.web._api("/tracks/" + self.tid)
                res["t"] = tr
                imgs = sorted((tr.get("album") or {}).get("images") or [],
                              key=lambda i: -(i.get("width") or 0))
                if imgs:  # largest cover (640 px)
                    res["img"] = Image.open(io.BytesIO(http_read(imgs[0]["url"], 10))).convert("RGB")
            except Exception as ex:  # noqa: BLE001
                res["err"] = ex

        th = threading.Thread(target=work, daemon=True)
        th.start()

        def wait():
            if th.is_alive():
                self.top.after(100, wait)
                return
            try:
                self._apply(res)
            except tk.TclError:  # window closed
                pass

        wait()

    def _apply(self, res):
        tr = res.get("t")
        if not tr:
            if res.get("err"):
                log("could not fetch track info:", repr(res["err"]))
            return
        if res.get("img") is not None:
            self._set_art(res["img"], round(210 * self.app.dpi))
        # artists (each clickable)
        for w_ in self.artists.winfo_children():
            w_.destroy()
        arts = tr.get("artists") or []
        for i, a in enumerate(arts):
            uri = a.get("uri")
            self._link(self.artists, a.get("name", ""),
                       lambda u=uri: open_spotify(u), size=11).pack(side="left")
            if i < len(arts) - 1:
                tk.Label(self.artists, text=", ", bg=self.bg, fg=self.dim,
                         font=self.font(11)).pack(side="left")
        al = tr.get("album") or {}
        self.album_lbl.config(text=al.get("name") or "")
        self.album_lbl.bind("<Button-1>", lambda _e, u=al.get("uri"): open_spotify(u))
        for w_ in self.meta.winfo_children():
            w_.destroy()
        self._rows = 0
        kind = {"album": T("sd_album"), "single": T("sd_single"),
                "compilation": T("sd_comp")}.get(al.get("album_type"), "")
        if kind:
            self._meta(T("sd_type"), kind)
        rd = al.get("release_date") or ""
        if rd:
            if al.get("release_date_precision") == "day" and len(rd) == 10:
                months = T("months_full").split()
                rd = f"{int(rd[8:10])} {months[int(rd[5:7]) - 1]} {rd[:4]}"
            self._meta(T("sd_release"), rd)
        if tr.get("duration_ms"):
            self._meta(T("sd_dur"), fmt_time(tr["duration_ms"] / 1000))
        if tr.get("track_number"):
            pos = f'{tr["track_number"]} / {al.get("total_tracks", "?")}'
            if (tr.get("disc_number") or 1) > 1:
                pos += f'  ·  {T("sd_disc")} {tr["disc_number"]}'
            self._meta(T("sd_track"), pos)
        if tr.get("explicit"):
            self._meta(T("sd_explicit"), "🅴")
        liked = self.web.liked_state(self.st["title"])
        if liked is not None:
            self._meta(T("sd_liked"), "♥ " + T("osd_liked") if liked else "—")
        isrc = (tr.get("external_ids") or {}).get("isrc")
        if isrc:
            self._meta("ISRC", isrc)

    def _url(self):
        if self.tid:
            return "https://open.spotify.com/track/" + self.tid
        q = f'{self.st.get("title", "")} {self.st.get("artist", "")}'.strip()
        return "https://open.spotify.com/search/" + urllib.parse.quote(q)

    def _open(self):
        if self.tid:
            open_spotify("spotify:track:" + self.tid, self._url())
        else:
            self.app._spotify_search(f'{self.st.get("title", "")} {self.st.get("artist", "")}')

    def _copy(self):
        self.top.clipboard_clear()
        self.top.clipboard_append(self._url())
        self.status.config(text=T("sh_link_ok"))


class HistoryDialog(_Dialog):
    """Search tracks / artists in the listening history (imported + live)."""

    def __init__(self, app, query=""):
        if not self._setup(app, T("hs_title")):
            return
        from tkinter import ttk
        t = self.top
        tk.Label(t, text=T("hs_title"), bg=self.bg, fg=self.fg,
                 font=self.font(15, True)).pack(anchor="w")
        tk.Label(t, text=T("hs_hint"), bg=self.bg, fg=self.dim,
                 font=self.font(9)).pack(anchor="w", pady=(2, 8))
        holder, self.entry = self.entry_box(t, 740)
        holder.pack(anchor="w")
        self.entry.bind("<KeyRelease>", lambda _e: self._schedule())
        self.entry.bind("<Return>", lambda _e: self._run())

        st = ttk.Style(t)
        st.theme_use("clam")
        st.configure("H.Treeview", background=self.bg, fieldbackground=self.bg,
                     foreground=self.fg, rowheight=26, borderwidth=0, font=self.font(10),
                     bordercolor=self.bg, lightcolor=self.bg, darkcolor=self.bg)
        st.layout("H.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])  # borderless
        st.configure("H.Treeview.Heading", background=self.field, foreground=self.dim,
                     relief="flat", font=self.font(9, True))
        st.map("H.Treeview", background=[("selected", _hx(self.P["secondary_container"]))],
               foreground=[("selected", _hx(self.P["on_secondary_container"]))])
        st.configure("Vertical.TScrollbar", background=self.field, troughcolor=self.bg,
                     bordercolor=self.bg, arrowcolor=self.dim, relief="flat")
        st.map("H.Treeview.Heading", background=[("active", self.field)])
        cols = ("time", "plays", "days", "first", "last")
        frame = tk.Frame(t, bg=self.bg)
        frame.pack(fill="both", expand=True, pady=(10, 0))
        self.tree = ttk.Treeview(frame, columns=cols, style="H.Treeview", height=14,
                                 selectmode="browse")
        self.tree.heading("#0", text=f'{T("hs_song")} / {T("hs_artist")}', anchor="w")
        self.tree.column("#0", width=330, anchor="w")
        for c, key, w in zip(cols, ("hs_time", "hs_plays", "hs_days", "hs_first", "hs_last"),
                             (90, 64, 50, 112, 112)):
            self.tree.heading(c, text=T(key), anchor="e" if c in ("time", "plays", "days")
                              else "center")
            self.tree.column(c, width=w, anchor="e" if c in ("time", "plays", "days")
                             else "center", stretch=False)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("artist", foreground=self.acc,
                                font=self.font(10, True))
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._detail())
        self.tree.bind("<Double-1>", lambda _e: self._open())

        bottom = tk.Frame(t, bg=self.bg)
        bottom.pack(fill="x", pady=(10, 0))
        self.detail = tk.Label(bottom, text="", bg=self.bg, fg=self.fg, justify="left",
                               anchor="w", font=self.font(10))
        self.detail.pack(side="left", fill="x", expand=True)
        self.chart = tk.Canvas(bottom, width=200, height=58, bg=self.bg,
                               highlightthickness=0)
        self.chart.pack(side="right")
        self.status = tk.Label(t, text="", bg=self.bg, fg=self.dim, font=self.font(9))
        self.status.pack(anchor="w", pady=(6, 0))
        row = tk.Frame(t, bg=self.bg)
        row.pack(fill="x", pady=(6, 0))
        self.btn(row, T("hs_open"), self._open, primary=True).pack(side="right")
        self.btn(row, T("dlg_close"), self.close).pack(side="right", padx=8)
        self._job = None
        self._rows = {}
        if not app.imported and not app.stats["days"]:
            self.status.config(text=T("hs_empty"))
        if query:
            self.entry.insert(0, query)
            self._run()
        t.focus_force()
        self.entry.focus_set()

    def _schedule(self):
        if self._job is not None:
            self.top.after_cancel(self._job)
        self._job = self.top.after(220, self._run)

    def _run(self):
        self._job = None
        q = self.entry.get().strip()
        self.tree.delete(*self.tree.get_children())
        self._rows.clear()
        if not q:
            self.status.config(text="")
            return
        res = self.app.history_search(q)
        self.status.config(text=T("hs_count", n=len(res)) if res else T("hs_none"))
        for kind, key, e in res:
            if kind == "artist":
                label = "♪  " + key
            else:
                title, _s, artist = key.partition("\t")
                label = f"{title}  ·  {artist}"
            iid = self.tree.insert("", "end", text=label, tags=(kind,), values=(
                fmt_dur(e["sec"]), f'{e["plays"]:,}'.replace(",", "."), e["days"],
                e["first"], e["last"]))
            self._rows[iid] = (kind, key, e)
        kids = self.tree.get_children()
        if kids:
            self.tree.selection_set(kids[0])

    def _detail(self):
        sel = self.tree.selection()
        if not sel or sel[0] not in self._rows:
            return
        kind, key, e = self._rows[sel[0]]
        name = key if kind == "artist" else key.replace("\t", " · ")
        best = max(e["years"].items(), key=lambda kv: kv[1])[0] if e["years"] else "–"
        self.detail.config(text=T("hs_detail", name=name, dur=fmt_dur(e["sec"]),
                                  plays=f'{e["plays"]:,}'.replace(",", "."), days=e["days"],
                                  first=e["first"], last=e["last"], year=best))
        # small bar chart by year
        c = self.chart
        c.delete("all")
        ys = sorted(e["years"])
        if not ys:
            return
        allys = [str(y) for y in range(int(ys[0]), int(ys[-1]) + 1)]
        mx = max(e["years"].values()) or 1
        bw = min(26, 196 / len(allys))
        for i, y in enumerate(allys):
            v = e["years"].get(y, 0)
            h = 40 * v / mx
            x0 = 2 + i * bw
            c.create_rectangle(x0 + 2, 44 - h, x0 + bw - 2, 44, fill=self.acc, width=0)
            c.create_text(x0 + bw / 2, 52, text="'" + y[2:], fill=self.dim,
                          font=self.font(7))

    def _open(self):
        sel = self.tree.selection()
        if not sel or sel[0] not in self._rows:
            return
        kind, key, _e = self._rows[sel[0]]
        self.app._spotify_search(key if kind == "artist" else key.replace("\t", " "))

    def close(self):
        self.app._hidx = (None,)  # release the search index from memory (rebuilt if needed)
        super().close()


if __name__ == "__main__":
    # single instance: don't start a second one if the widget is already running
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\Coverglow")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        # say so instead of exiting silently (a second copy, e.g. a fresh download, looks broken)
        ctypes.windll.user32.MessageBoxW(
            None, "Coverglow zaten çalışıyor." if default_lang() == "tr"
            else "Coverglow is already running.", "Coverglow", 0x40)  # MB_ICONINFORMATION
        sys.exit(0)
    # the Windows timer ticks in 15.6 ms steps by default: 1 ms so animations don't stutter
    ctypes.windll.winmm.timeBeginPeriod(1)
    try:
        App().run()
    except Exception:
        log(traceback.format_exc())
        raise
