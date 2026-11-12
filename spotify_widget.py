"""
Spotify Masaüstü Widget'ı — Material You (Windows 10/11)

- Rainmeter gibi masaüstüne yapışır: pencerelerin altında durur, Win+D'de kaybolmaz,
  görev çubuğunda / Alt+Tab'da görünmez.
- Albüm kapağından dinamik Material You renk paleti (koyu / açık / sistem).
- Dalgalı ilerleme çubuğu: tıkla / sürükle / tekerlek ile ileri-geri sar.
- Karışık çal, tekrar (kapalı / liste / şarkı), önceki / oynat / sonraki.
- Sıradaki listesi (isteğe bağlı Spotify hesabı bağlantısı ile), tıklayınca o şarkıya atlar.
- Kapağa tıkla -> Spotify'ı aç. Sağ tık -> ayarlar menüsü.

Çalma bilgisi ve kontroller Windows medya API'sinden (GSMTC) gelir; API anahtarı gerekmez.
Sıradaki listesi için Spotify Web API (PKCE, gizli anahtar gerekmez) kullanılır.
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

FROZEN = getattr(sys, "frozen", False)  # PyInstaller .exe içinde mi?
APP_DIR = (os.path.dirname(sys.executable) if FROZEN
           else os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LAST_PATH = os.path.join(APP_DIR, "last_track.json")
LAST_ART_PATH = os.path.join(APP_DIR, "last_art.img")
STATS_PATH = os.path.join(APP_DIR, "stats.json")
LOG_PATH = os.path.join(APP_DIR, "widget.log")
STARTUP_VBS = os.path.join(
    os.environ.get("APPDATA", ""),
    r"Microsoft\Windows\Start Menu\Programs\Startup\SpotifyWidget.vbs",
)
FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

# pythonw altında stderr yok: hataları dosyaya yaz
if sys.stderr is None or sys.stdout is None:
    _f = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    sys.stderr = sys.stdout = _f


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ yerleşim
W = 380            # mantıksal piksel (96 DPI)
MAIN_H = 268
GAP = 8
Q_HEAD = 52
Q_ROW = 56
Q_ROWS = 5
Q_PAD = 10
TOTAL_H = MAIN_H + GAP + Q_HEAD + Q_ROWS * Q_ROW + Q_PAD
STATS_H = 286  # istatistik sekmesinin gövde yüksekliği (en fazla Q_ROWS * Q_ROW + Q_PAD)

# boyut ön ayarları (DPI ölçeğinin üstüne çarpan); Ctrl + tekerlek ile ara değerler
SIZES = (("size_s", 0.75), ("size_m", 0.85), ("size_n", 1.0), ("size_l", 1.15))
SCALE_MIN, SCALE_MAX, SCALE_STEP = 0.6, 1.2, 0.05

MINI_H = 80               # mini mod yüksekliği
MINI_BAR_Y = 73           # mini moddaki ince ilerleme çizgisi
MINI_ART = 56
BAR_X0, BAR_X1, BAR_CY = 24, W - 24, 160
STRIP_Y0, STRIP_Y1 = 144, 194
LAM, AMP = 36.0, 3.0          # dalga boyu / genliği
CTRL_CY = 228
CX = W / 2

# Segoe Fluent Icons
IC_PLAY, IC_PAUSE = "\uF5B0", "\uE769"
IC_PREV, IC_NEXT = "\uE892", "\uE893"
IC_SHUFFLE, IC_REPEAT, IC_REPEAT_ONE = "\uE8B1", "\uE8EE", "\uE8ED"
IC_MUSIC, IC_OPEN = "\uE8D6", "\uE8A7"
IC_DOWN, IC_UP = "\uE70D", "\uE70E"
IC_CAL, IC_VOL, IC_MUTE, IC_ALARM = "\uE787", "\uE767", "\uE74F", "\uEA8F"

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = ("user-read-playback-state user-read-currently-playing "
          "user-library-read user-library-modify user-modify-playback-state")
UA = "SpotifyDesktopWidget/1.0 (+https://lrclib.net)"


# ------------------------------------------------------------------ dil
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
        "lastyear": "Geçen yıl bugün", "wrap_btn": "★ Özet",
        "wr_title": "{y} Özeti", "wr_total": "toplam dinleme", "wr_plays": "çalma",
        "wr_artists": "EN ÇOK DİNLENEN SANATÇILAR", "wr_tracks": "EN ÇOK DİNLENEN ŞARKILAR",
        "wr_month": "En yoğun ay", "wr_day": "En sevdiğin gün", "wr_hour": "En sevdiğin saat",
        "wr_streak": "En uzun seri", "wr_copy": "Panoya kopyala", "wr_save": "Kaydet…",
        "wr_copied": "Görsel panoya kopyalandı",
        "weekdays": "Pazartesi Salı Çarşamba Perşembe Cuma Cumartesi Pazar",
        "m_react": "Müziğe tepki veren dalga", "m_duck": "Discord'da konuşulunca Spotify'ı kıs",
        "m_osd": "Kısayolda ekran göstergesi",
        "osd_play": "Çalıyor", "osd_pause": "Duraklatıldı", "osd_vol": "Ses",
        "m_search": "Şarkı ara…  (Ctrl+Alt+S)", "hk_search": "Şarkı ara",
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
        "hk_list": "KISAYOLLAR",
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
        "m_open": "Spotify'ı aç", "m_startup": "Windows açılışında başlat", "m_close": "Kapat",
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
        "lastyear": "On this day last year", "wrap_btn": "★ Recap",
        "wr_title": "{y} Recap", "wr_total": "total listening", "wr_plays": "plays",
        "wr_artists": "TOP ARTISTS", "wr_tracks": "TOP SONGS",
        "wr_month": "Busiest month", "wr_day": "Favorite day", "wr_hour": "Favorite hour",
        "wr_streak": "Longest streak", "wr_copy": "Copy to clipboard", "wr_save": "Save…",
        "wr_copied": "Image copied to clipboard",
        "weekdays": "Monday Tuesday Wednesday Thursday Friday Saturday Sunday",
        "m_react": "Wave reacts to music", "m_duck": "Lower Spotify when someone talks on Discord",
        "m_osd": "On-screen display for shortcuts",
        "osd_play": "Playing", "osd_pause": "Paused", "osd_vol": "Volume",
        "m_search": "Search songs…  (Ctrl+Alt+S)", "hk_search": "Search songs",
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
        "hk_list": "SHORTCUTS",
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
        "m_open": "Open Spotify", "m_startup": "Start with Windows", "m_close": "Close",
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
    try:  # Windows arayüz dili Türkçe ise tr, değilse en
        return "tr" if (ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF) == 0x1F \
            else "en"
    except Exception:  # noqa: BLE001
        return "tr"


def fmt_dur(sec):
    m = int(sec // 60)
    if m >= 6000:  # 100 saatten fazlası: sadece saat
        return T("dur_h", h=f"{m // 60:,}".replace(",", "."))
    return T("dur_min", m=m) if m < 60 else T("dur_hm", h=m // 60, m=m % 60)


STATS_IMPORT_PATH = os.path.join(APP_DIR, "stats_import.json")


def import_spotify_history(paths):
    """Spotify 'Verilerini indir' dışa aktarımını (zip ya da json) günlük özetlere çevirir.
    Hem genişletilmiş geçmiş (ts, ms_played, master_metadata_*) hem de hesap verisi
    (endTime, msPlayed, artistName, trackName) biçimini destekler. Sadece müzik sayılır."""
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
    n, total, first, last = 0, 0.0, None, None
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
            continue  # podcast, sesli kitap, bozuk kayıt
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
        d = days.setdefault(day, {"s": 0.0, "p": 0, "a": {}, "t": {}, "h": {}})
        d["s"] += sec
        hr = str(local.hour)
        d["h"][hr] = d["h"].get(hr, 0.0) + sec
        if ms >= 30000:  # Spotify 30 sn'yi bir dinleme sayar
            d["p"] += 1
        d["a"][ai] = d["a"].get(ai, 0.0) + sec
        d["t"][ti] = d["t"].get(ti, 0.0) + sec
        n += 1
        total += sec
        first = day if first is None or day < first else first
        last = day if last is None or day > last else last
    if not n:
        return None
    for d in days.values():  # dosya küçük kalsın
        d["s"] = round(d["s"], 1)
        d["a"] = {str(k): round(v, 1) for k, v in d["a"].items()}
        d["t"] = {str(k): round(v, 1) for k, v in d["t"].items()}
        d["h"] = {k: round(v, 1) for k, v in d["h"].items()}
    data = {"version": 2, "first": first, "last": last, "artists": artists, "tracks": tracks,
            "days": days}
    with open(STATS_IMPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    return {"n": n, "hours": round(total / 3600), "first": first, "last": last}


def load_imported_history():
    """stats_import.json → {gün: {"sec", "plays", "artists": {ad: sn}, "tracks": {ad\\tsanatçı: sn}}}"""
    try:
        with open(STATS_IMPORT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        artists, tracks = data["artists"], data["tracks"]
        tnames = [f"{t}\t{artists[a]}" for t, a in tracks]
        out = {}
        for day, d in data["days"].items():
            out[day] = {"sec": d["s"], "plays": d["p"], "hours": d.get("h", {}),
                        "artists": {artists[int(k)]: v for k, v in d["a"].items()},
                        "tracks": {tnames[int(k)]: v for k, v in d["t"].items()}}
        return out
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        log("içe aktarılan geçmiş okunamadı:", repr(e))
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


# ------------------------------------------------------------------ ayarlar
class Config(dict):
    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                self.update(json.load(f))
        except Exception:
            pass

    def save(self):
        with self.lock:
            try:
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(dict(self), f, indent=1)
            except Exception as e:
                log("config kaydedilemedi:", e)


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
    """Pencere içeriğini per-pixel alfa ile (UpdateLayeredWindowIndirect) çizer."""

    def __init__(self, hwnd, w, h):
        self.hwnd, self.w, self.h = hwnd, w, h
        self.alpha = 255
        self.screen_dc = user32.GetDC(None)
        self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # yukarıdan aşağı
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        self.bits = ctypes.c_void_p()
        self.hbmp = gdi32.CreateDIBSection(self.mem_dc, ctypes.byref(bmi), 0,
                                           ctypes.byref(self.bits), None, 0)
        gdi32.SelectObject(self.mem_dc, self.hbmp)
        self._warned = False

    @staticmethod
    def to_bgra(img):
        r, g, b, a = img.convert("RGBa").split()  # ön-çarpımlı alfa
        return Image.merge("RGBA", (b, g, r, a)).tobytes()

    def full(self, img):
        data = self.to_bgra(img)
        ctypes.memmove(self.bits, data, len(data))
        self._commit(None)

    def region(self, img, x, y):
        data = self.to_bgra(img)
        rw = img.width * 4
        base = self.bits.value
        for r in range(img.height):
            ctypes.memmove(base + ((y + r) * self.w + x) * 4, data[r * rw:(r + 1) * rw], rw)
        self._commit(wintypes.RECT(x, y, x + img.width, y + img.height))

    def _commit(self, rect):
        size = wintypes.SIZE(self.w, self.h)
        src = wintypes.POINT(0, 0)
        blend = BLENDFUNCTION(0, 0, self.alpha, 1)
        info = ULWINFO()
        info.cbSize = ctypes.sizeof(ULWINFO)
        info.hdcDst = self.screen_dc
        info.pptDst = None
        info.psize = ctypes.pointer(size)
        info.hdcSrc = self.mem_dc
        info.pptSrc = ctypes.pointer(src)
        info.pblend = ctypes.pointer(blend)
        info.dwFlags = ULW_ALPHA
        info.prcDirty = ctypes.pointer(rect) if rect is not None else None
        if not user32.UpdateLayeredWindowIndirect(self.hwnd, ctypes.byref(info)):
            if not self._warned:
                log("UpdateLayeredWindowIndirect başarısız:", ctypes.GetLastError())
                self._warned = True


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user32.GetForegroundWindow.restype = wintypes.HWND
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def monitor_info(hwnd):
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi))  # NEAREST
    return mi


def fullscreen_app(own=()):
    """Ön plandaki pencere bir monitörü tamamen kaplıyorsa (oyun, video, sunum)
    o monitörün dikdörtgenini döndürür; değilse None."""
    try:
        state = ctypes.c_int(0)
        ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
        hwnd = user32.GetForegroundWindow()
        if not hwnd or hwnd in own:
            return None
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value in _SHELL_CLASSES:
            return None
        mi = monitor_info(hwnd)
        m = mi.rcMonitor
        if state.value == 3:  # QUNS_RUNNING_D3D_FULL_SCREEN (özel tam ekran)
            return m
        rc = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rc))
        if rc.left <= m.left and rc.top <= m.top and rc.right >= m.right \
                and rc.bottom >= m.bottom:
            return m
    except Exception:
        pass
    return None


def set_window_styles(hwnd):
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    ex = (ex | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex)


def pin_to_desktop(hwnd):
    # Sahibi Progman (masaüstü) olan pencereler Win+D'de gizlenmez ve en altta kalır
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
    """Spotify'ın Windows ses karıştırıcısındaki ses düzeyi (0..1)."""

    def __init__(self, exe="spotify.exe"):
        self.exe = exe
        self._cache = (0.0, [])
        self._all = (0.0, [])   # (zaman, [(süreç adı, ses seviyesi ölçer)])

    def _sessions(self):
        t, ss = self._cache
        if ss and time.monotonic() - t < 3:
            return ss
        try:
            from pycaw.pycaw import AudioUtilities
            ss = [s.SimpleAudioVolume for s in AudioUtilities.GetAllSessions()
                  if s.Process and s.Process.name().lower() == self.exe]
        except Exception as e:  # noqa: BLE001
            log("ses oturumları okunamadı:", repr(e))
            ss = []
        self._cache = (time.monotonic(), ss)
        return ss

    def _meters(self):
        t, ms = self._all
        if time.monotonic() - t < 3:
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
            log("ses ölçerleri okunamadı:", repr(e))
        self._all = (time.monotonic(), ms)
        return ms

    def peak(self, names):
        """Verilen süreçlerin o anki en yüksek çıkış seviyesi (0..1)."""
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
HOTKEYS = (  # (kimlik, değiştirici, tuş, eylem, tuş adı, metin anahtarı)
    (1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x50, "play", "Ctrl+Alt+P", "hk_play"),
    (2, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x27, "next", "Ctrl+Alt+→", "hk_next"),
    (3, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x25, "prev", "Ctrl+Alt+←", "hk_prev"),
    (4, MOD_CONTROL | MOD_ALT, 0x26, "vol+", "Ctrl+Alt+↑", "hk_volup"),
    (5, MOD_CONTROL | MOD_ALT, 0x28, "vol-", "Ctrl+Alt+↓", "hk_voldown"),
    (6, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x53, "search", "Ctrl+Alt+S", "hk_search"),
)


class Hotkeys:
    """Genel klavye kısayolları: ayrı bir iş parçacığında RegisterHotKey + mesaj döngüsü."""

    def __init__(self, out_queue):
        self.q = out_queue
        self.tid = None
        self.failed = []
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            self.thread.join(1.0)  # önceki durdurma isteğinin bitmesini bekle
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
            log("kısayol kaydedilemedi (başka uygulama kullanıyor):", ", ".join(self.failed))
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


# ------------------------------------------------------------------ renkler
DEFAULT_SEED = (0.39, 0.70)  # Spotify yeşili
# sabit renk seçenekleri: (ad, (ton, doygunluk))
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
    sn = sp * 0.28  # nötr yüzeyler için düşük doygunluk

    def T(tone, sat):
        return tuple(int(round(c * 255)) for c in colorsys.hls_to_rgb(h, tone / 100, sat))

    if light:
        return {
            "primary": T(40, sp), "on_primary": (255, 255, 255),
            "primary_container": T(88, sp), "on_primary_container": T(15, sp),
            "secondary_container": T(88, sp * 0.35), "on_secondary_container": T(16, sp * 0.35),
            "surface": T(95, sn), "surface_high": T(91, sn), "surface_highest": T(87, sn),
            "on_surface": T(10, sn), "on_surface_variant": T(32, sn), "outline": T(55, sn),
        }
    return {
        "primary": T(80, sp), "on_primary": T(18, sp),
        "primary_container": T(30, sp * 0.8), "on_primary_container": T(90, sp),
        "secondary_container": T(26, sp * 0.35), "on_secondary_container": T(90, sp * 0.35),
        "surface": T(11, sn), "surface_high": T(16, sn), "surface_highest": T(23, sn),
        "on_surface": T(92, sn), "on_surface_variant": T(76, sn), "outline": T(55, sn),
    }


def mix(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def mix_scheme(a, b, t):
    return {k: mix(a[k], b[k], t) for k in a}


# ------------------------------------------------------------------ çizim
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


def rr(img, x0, y0, x1, y1, r, color, alpha=255, ellipse=False):
    """Kenarları yumuşatılmış yuvarlak dikdörtgen (fiziksel piksel)."""
    if x1 - x0 < 0.5 or y1 - y0 < 0.5:
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


def rounded_image(src, size, radius):
    img = src.convert("RGB")
    w, h = img.size
    m = min(w, h)
    img = img.crop(((w - m) // 2, (h - m) // 2, (w + m) // 2, (h + m) // 2))
    img = img.resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")
    img.putalpha(_rr_mask(size, size, 0, 0, size, size, radius))
    return img


# ---- çok dilli yazı: ana yazı tipinde olmayan karakterler için yedek yazı tipleri
# (normal dosya, kalın dosya, etiket)
FALLBACK_FONTS = [
    ("YuGothM.ttc", "YuGothB.ttc", "jp"),      # Japonca
    ("msgothic.ttc", "msgothic.ttc", "jp"),
    ("malgun.ttf", "malgunbd.ttf", "kr"),      # Korece
    ("msyh.ttc", "msyhbd.ttc", "sc"),          # Çince (basitleştirilmiş)
    ("msjh.ttc", "msjhbd.ttc", "tc"),          # Çince (geleneksel)
    ("simsun.ttc", "simsun.ttc", "sc"),
    ("LeelawUI.ttf", "LeelawUI.ttf", "th"),    # Tayca vb.
    ("Nirmala.ttc", "Nirmala.ttc", "in"),      # Hint yazıları
    ("seguisym.ttf", "seguisym.ttf", "sym"),   # semboller
    ("seguiemj.ttf", "seguiemj.ttf", "emoji"),  # emoji
]
_coverage = {}


def font_coverage(filename):
    """Yazı tipinin desteklediği Unicode kod noktaları."""
    cov = _coverage.get(filename)
    if cov is None:
        try:
            f = TTFont(os.path.join(FONT_DIR, filename), fontNumber=0, lazy=True)
            cov = frozenset(f.getBestCmap() or ())
            f.close()
        except Exception as e:  # noqa: BLE001
            log("yazı tipi okunamadı:", filename, repr(e))
            cov = frozenset()
        _coverage[filename] = cov
    return cov


def _fallback_order(text):
    has_kana = any(0x3040 <= ord(c) <= 0x30FF or 0x31F0 <= ord(c) <= 0x31FF for c in text)
    has_hangul = any(0xAC00 <= ord(c) <= 0xD7AF or 0x1100 <= ord(c) <= 0x11FF
                     or 0x3130 <= ord(c) <= 0x318F for c in text)
    first = "jp" if has_kana else "kr" if has_hangul else "sc"
    idx = list(range(len(FALLBACK_FONTS)))
    idx.sort(key=lambda i: 0 if FALLBACK_FONTS[i][2] == first else 1)  # kararlı sıralama
    return idx


class TextFont:
    """Eksik karakterleri yedek yazı tiplerinden tamamlayan yazı tipi."""

    def __init__(self, filename, px, scale, bold=False):
        self.size = max(6, round(px * scale))
        self.bold = bold
        if not os.path.exists(os.path.join(FONT_DIR, filename)):
            filename = "segoeui.ttf"
        self.filename = filename
        self.primary = ImageFont.truetype(os.path.join(FONT_DIR, filename), self.size)
        self.asc, self.desc = self.primary.getmetrics()
        self._fb = {}
        self._runs = {}

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
        out = []  # [yazı tipi indeksi (-1 = ana), metin]
        for ch in text:
            cp = ord(ch)
            if cp in (0xFE0E, 0xFE0F, 0x200B, 0x200D):
                continue  # görünmez birleştiriciler
            if out and unicodedata.combining(ch):
                out[-1][1] += ch
                continue
            which = -1
            if not (cp < 0x250 or cp in pcov):
                if order is None:
                    order = _fallback_order(text)
                cand = order
                if cp >= 0x1F000:  # emoji önce renkli yazı tipinden
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
        return sum(f.getlength(seg) for f, seg, _e in self.runs(text))

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
    """ImageDraw sarmalayıcı: TextFont ile yazılan metinleri yedek yazı tipleriyle çizer."""

    def __init__(self, img):
        self.d = ImageDraw.Draw(img)

    def text(self, xy, text, font=None, fill=None, anchor="la"):
        if isinstance(font, TextFont):
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
    # Parçalar: boşlukla ayrılmış kelimeler; Japonca/Çince'de her karakter ayrı bölünebilir
    tokens = []  # (metin, önünde boşluk var mı)
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


# ------------------------------------------------------------------ medya (GSMTC)
class MediaWorker:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.lock = threading.Lock()
        self.state = {"ok": False}
        self.manager = None
        self.session = None
        self._last_track = None
        self._wake = None
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
                log("medya hatası:", repr(e))
                with self.lock:
                    self.state = {"ok": False}
                self.manager = None
            try:
                await asyncio.wait_for(self._wake.wait(), 1.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def snapshot(self):
        with self.lock:
            st = dict(self.state)
            self.state.pop("art_changed", None)
        return st

    def command(self, name, arg=None):
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
                log("komut hatası:", name, repr(e))
            await asyncio.sleep(0.15)
            self._wake.set()

        asyncio.run_coroutine_threadsafe(go(), self.loop)


# ------------------------------------------------------------------ ağ: geçici hatalarda tekrar dene
_TRANSIENT = (urllib.error.URLError, http.client.RemoteDisconnected, http.client.IncompleteRead,
              ConnectionError, TimeoutError, ssl.SSLError)


def is_transient(e):
    """İnternetin anlık gidip gelmesinden kaynaklanan hata mı (tekrar denemeye değer)?"""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (500, 502, 503, 504)  # sunucu tarafında geçici sorun
    return isinstance(e, _TRANSIENT)


def http_read(req, timeout=15, tries=2, idempotent=True):
    """İsteği gönderip gövdeyi döndürür; bağlantı koparsa / zaman aşımında kısa bir beklemeyle
    tekrar dener. İki kez gönderilmesi sorun olan istekler (idempotent=False) sadece sunucu
    isteği hiç almadıysa (bağlantı kurulamadı) tekrarlanır. HTTPError olduğu gibi yükseltilir."""
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            retry = is_transient(e)
            if retry and not idempotent:
                # sadece bağlantı hiç kurulamadıysa güvenli (ör. DNS hatası, bağlantı reddi)
                import socket
                reason = getattr(e, "reason", None)
                retry = not isinstance(e, urllib.error.HTTPError) and isinstance(
                    reason, (socket.gaierror, ConnectionRefusedError))
            if not retry or i == tries - 1:
                raise
            time.sleep(0.8 * (i + 1))


# ------------------------------------------------------------------ şarkı sözleri (LRCLIB)
class Lyrics:
    """lrclib.net'ten senkronize sözler. lines(): [(saniye ya da None, metin)]."""

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
            # hata aldıysa (ör. internet gitti) bir süre sonra kendiliğinden tekrar dene
            if self.state != "err" or time.monotonic() < getattr(self, "retry_at", 0):
                return
            self.key = None
        if key == self.key:
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
                log("sözler alınamadı:", repr(e))
            self.retry_at = time.monotonic() + 20  # önbelleğe alma: 20 sn sonra tekrar dene
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


# ------------------------------------------------------------------ Spotify Web API (sıradaki)
class SpotifyWeb:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.queue = None
        self.error = None
        self.version = 0
        self.access = None
        self.expires = 0.0
        self.track_key = None
        self.playing = False
        self.thumbs = {}
        self.current = None  # çalan şarkının kimliği, sanatçı / albüm bağlantıları
        self.liked = {}      # şarkı kimliği -> beğenildi mi
        self.liked_t = {}    # şarkı kimliği -> son kontrol zamanı
        self.auth_status = None
        self.auth_error = False
        self.net_down = False   # bağlantı kesintisi sürüyor mu (log tekrarını önler)
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
        if self.access and time.time() < self.expires - 30:
            return
        try:
            r = self._post_token({"grant_type": "refresh_token",
                                  "refresh_token": self.cfg["refresh_token"],
                                  "client_id": self.cfg["client_id"]})
        except urllib.error.HTTPError as e:
            if e.code in (400, 401):
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
            # Spotify verilen izinleri bildirir: Beğen için izin var mı buradan anlaşılır
            self.cfg["scopes"] = r["scope"]
            changed = True
            log("Spotify izinleri:", r["scope"])
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
        # POST (sıraya ekleme) iki kez gitmesin: sadece bağlantı hiç kurulamadıysa tekrarla
        raw = http_read(req, timeout=15, idempotent=method != "POST")
        try:
            return json.loads(raw.decode()) if raw else {}
        except ValueError:
            return {}

    # -- arama, çalma, sıra, cihazlar
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

    # -- Beğen (kütüphane)
    @property
    def has_library(self):
        return "user-library-modify" in (self.cfg.get("scopes") or "")

    def _library(self, tid, method):
        """Kütüphane uç noktası; eskisi kaldırılmışsa yeni URI tabanlı uç noktayı dener."""
        if not getattr(self, "_lib_new", False):
            try:
                path = "/me/tracks/contains?ids=" if method == "CHECK" else "/me/tracks?ids="
                return self._api(path + tid, "GET" if method == "CHECK" else method)
            except urllib.error.HTTPError as e:
                # Spotify eski uç noktayı kapattı (403/404/410): yenisine geç ve hatırla
                if e.code not in (403, 404, 410):
                    raise
                self._lib_new = True
        uri = urllib.parse.quote("spotify:track:" + tid)
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
            log("beğeni durumu okunamadı:", e.code, body)
            if e.code == 403:  # izin yok: yeniden bağlanmak gerekir
                self.cfg["scopes"] = ""
        except Exception as e:  # noqa: BLE001
            if not is_transient(e):  # bağlantı kopmalarını ana döngü bir kez bildirir
                log("beğeni durumu okunamadı:", repr(e))

    def liked_state(self, title):
        """Çalan şarkı (başlığa göre eşleşirse) beğenilmiş mi: True / False / None."""
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
        self.liked[tid] = new  # iyimser güncelleme
        with self.lock:
            self.version += 1

        def go():
            try:
                self._library(tid, "PUT" if new else "DELETE")
            except Exception as e:  # noqa: BLE001
                log("beğeni değiştirilemedi:", repr(e))
                self.liked[tid] = not new
                with self.lock:
                    self.version += 1

        threading.Thread(target=go, daemon=True).start()
        return True

    def _thumb(self, url):
        if not url or url in self.thumbs:
            return
        try:
            self.thumbs[url] = Image.open(io.BytesIO(http_read(url, timeout=10))).convert("RGB")
        except Exception as e:  # noqa: BLE001
            if not is_transient(e):  # bağlantı koptuysa sonra tekrar denensin
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
        # beğeni durumunu yenile (Spotify uygulamasında değiştirilmiş olabilir)
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
            url = min(imgs, key=lambda i: i.get("width") or 9999)["url"] if imgs else None
            self._thumb(url)
            items.append({"id": it.get("id"), "name": it.get("name", ""), "sub": sub,
                          "url": url, "dur": (it.get("duration_ms") or 0) / 1000})
        old = [i["id"] for i in self.queue] if self.queue else None
        if old != [i["id"] for i in items] or self.error:
            self._set(items, None)

    def _run(self):
        last = 0.0
        while True:
            woke = self.wake.wait(1.0)
            self.wake.clear()
            if not self.connected:
                continue
            interval = 8 if self.playing else 30
            if woke or time.time() - last > interval:
                if woke:
                    time.sleep(0.8)  # Spotify'ın sırayı güncellemesine izin ver
                last = time.time()
                try:
                    self._fetch()
                    if self.net_down:
                        self.net_down = False
                        log("bağlantı geri geldi")
                except Exception as e:  # noqa: BLE001
                    if not is_transient(e):
                        log("sıradaki listesi alınamadı:", repr(e))
                    elif not self.net_down:  # kesinti boyunca tek satır
                        self.net_down = True
                        log("bağlantı sorunu, otomatik tekrar denenecek:", repr(e))

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
        while "code" not in result and time.time() < deadline:
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


# ------------------------------------------------------------------ uygulama
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
        self.cfg.setdefault("list_tab", "queue")
        self.cfg.setdefault("accent", "auto")
        self.cfg.setdefault("bg", "solid")
        self.cfg.setdefault("react", True)
        self.cfg.setdefault("duck", True)
        self.cfg.setdefault("osd", True)
        self.cfg.setdefault("duck_apps", ["discord.exe"])
        self.cfg.setdefault("alarm", {"on": False, "time": "08:00"})
        self.level = 0.0          # Spotify'ın anlık ses seviyesi (0..1, otomatik kazançlı)
        self._lvl_max = 0.3
        self._duck = None         # kısılmışken: {"orig": ses, "last": son konuşma zamanı}
        self._duck_fade = None
        self._alarm_day = None
        self._alarm_fade = None
        self.cfg.setdefault("stats_period", "7")
        self.cfg.setdefault("stats_list", "artists")
        self.stats = self._load_stats()
        self.imported = load_imported_history()   # Spotify'dan içe aktarılan geçmiş
        self.import_ver = 0
        self._index_imported()
        self._stat_t = None
        self._stat_saved = time.monotonic()
        self._glass = {}         # bulanık duvar kağıdı önbelleği
        self.glass_ver = 0
        global LANG
        LANG = self.cfg.setdefault("lang", default_lang())
        self.history = self._load_history()
        self._hist_thumbs = {}
        self._toast_key = None       # son bildirimi gösterilen şarkı
        self._seen_first = False     # açılıştaki ilk şarkı için bildirim gösterme
        self.inside = False
        self._alpha_job = None
        self._leave_job = None
        self.mini = bool(self.cfg["mini"])
        self.art_mini = None
        # ses
        self.volume = AppVolume()
        self.vol_val = None
        self.vol_until = 0.0
        # klavye kısayolları
        self.hk_queue = []
        self.hotkeys = Hotkeys(self.hk_queue)
        if self.cfg["hotkeys"]:
            self.hotkeys.start()
        # uyku zamanlayıcısı
        self.sleep_end = None
        self.sleep_fade = None
        self.sleep_choice = 0

        # pencere en büyük boyuta göre açılır; içerik seçilen ölçekte sol üste çizilir,
        # kalan şeffaf alan tıklamaları masaüstüne geçirir
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

        # durum
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
        self.marq = None        # mini modda kayan şarkı adı
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
        self.last = None
        self._last_saved = 0.0
        self._load_last()

        # pencere
        self.root = tk.Tk()
        self.root.title("Spotify Widget")
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
        # ekran dışında kalmışsa geri getir
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

    # ---------------- son çalınanlar (yerel geçmiş)
    HISTORY_PATH = os.path.join(APP_DIR, "history.json")
    HISTORY_MAX = 20

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
            with open(self.HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            log("geçmiş kaydedilemedi:", repr(e))

    def _recent_items(self, rows):
        """Şu an gösterilen şarkı hariç son çalınanlar."""
        v = self._view()
        cur = (v.get("title"), v.get("artist"))
        return [h for h in self.history if (h["title"], h["artist"]) != cur][:rows]

    def _recent_rows(self, rows):
        """Geçmiş sekmesinin satırları: varsa en üstte 'geçen yıl bugün', sonra son çalınanlar.
        Her satır: title, sub, thumb, right, q (Spotify'da aranacak metin)."""
        out = []
        ly = self._last_year_today()
        if ly:
            title, artist, sec = ly
            out.append({"title": T("lastyear"), "sub": f"{title} · {artist}", "thumb": "cal",
                        "right": fmt_dur(sec), "q": f"{title} {artist}"})
        for h in self._recent_items(rows - len(out)):
            out.append({"title": h["title"], "sub": h["artist"], "thumb": self._hist_thumb(h),
                        "right": time_ago(h.get("t", 0)), "q": f'{h["title"]} {h["artist"]}'})
        return out

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

    # ---------------- dil
    def _set_lang(self, code):
        global LANG
        LANG = code
        self._set("lang", code)
        self._render_all()

    # ---------------- boyut
    def _apply_scale(self):
        sc = max(SCALE_MIN, min(SCALE_MAX, float(self.cfg["scale"])))
        self.S = S = self.dpi * sc
        self.WP, self.HP = round(W * S), round(TOTAL_H * S)
        if self.mini:  # ince ilerleme çizgisinin bulunduğu şerit
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

    # ---------------- yardımcılar
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

    # ---------------- fare üstündeyken opaklaşma
    def _target_alpha(self):
        pct = self.cfg["opacity"]  # fare üstündeyken (ve özellik kapalıyken) opaklık
        if self.cfg["hover_opaque"] and not self.inside:
            pct = min(pct, self.cfg["idle_opacity"])  # fare uzaktayken soluklaş
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
            # kartlar arasındaki boşluktan geçerken titremesin diye kısa gecikme
            self._leave_job = self.root.after(250, self._left)

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
        speed = 0.35 if target > cur else 0.18  # hızlı belirir, yavaş solar
        nxt = cur + (target - cur) * speed
        nxt = target if abs(target - nxt) < 2 else round(nxt)
        if nxt == cur:
            nxt = target
        self.surface.alpha = nxt
        self.surface._commit(None)
        if nxt != target:
            self._alpha_job = self.root.after(16, self._alpha_step)

    def _open_entity(self, kind):
        """Sanatçı / albüm sayfasını Spotify'da aç."""
        st = self._view()
        if not st.get("title"):
            return
        uri = None
        cur = self.web.current
        if cur and cur.get("name") == st.get("title"):
            uri = cur.get(kind + "_uri")
        query = st["artist"] if kind == "artist" else f'{st["album"]} {st["artist"]}'
        if not uri:  # hesap bağlı değilse: Spotify içinde arama
            uri = "spotify:search:" + urllib.parse.quote(query.strip())
        try:
            os.startfile(uri)
        except OSError:
            webbrowser.open("https://open.spotify.com/search/" + urllib.parse.quote(query))

    @staticmethod
    def _spotify_search(q):
        if not q:
            return
        try:
            os.startfile("spotify:search:" + urllib.parse.quote(q))
        except OSError:
            webbrowser.open("https://open.spotify.com/search/" + urllib.parse.quote(q))

    def _win_pos(self):
        r = wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        return r.left, r.top

    def _hit(self, x, y):
        for hid, x0, y0, x1, y1 in self.hits:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return hid
        return None

    # ---------------- zaman / konum
    def _position(self):
        st = self.st
        if not st.get("ok"):
            if self.last:  # Spotify kapalı: son çalınan şarkının kaldığı yer
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

    # ---------------- ana döngü
    # ---------------- ses
    def _change_volume(self, delta):
        v = self.volume.get()
        if v is None:
            return
        self.vol_val = self.volume.set(round((v + delta) * 100) / 100)
        self.vol_until = time.monotonic() + 1.5

    # ---------------- uyku zamanlayıcısı
    def _set_sleep(self, minutes):
        self._restore_sleep_volume()
        self.sleep_end = time.monotonic() + minutes * 60 if minutes else None
        self.sleep_choice = minutes
        self.base_key = None

    def _restore_sleep_volume(self):
        if self.sleep_fade is not None:
            self.volume.set(self.sleep_fade)
        self.sleep_fade = None

    def _finish_sleep(self):
        fade = self.sleep_fade
        self.sleep_end = self.sleep_fade = None
        self.sleep_choice = 0
        if self.st.get("ok") and self.st.get("playing"):
            self.media.command("pause")
        if fade is not None:  # duraklattıktan sonra eski ses düzeyine dön
            self.root.after(1500, lambda: self.volume.set(fade))
        self.base_key = None

    def _sleep_minutes(self):
        if self.sleep_end is None:
            return None
        return max(1, math.ceil((self.sleep_end - time.monotonic()) / 60))

    # ---------------- mini mod
    def _toggle_mini(self):
        self.mini = not self.mini
        self.cfg["mini"] = self.mini
        self.cfg.save()
        self.hover = None
        self._apply_scale()
        self._render_all()

    def _on_double(self, e):
        x, y = self._xy(e)
        if self._hit(x, y):  # butona hızlı çift tıklama: normal tıklama gibi davran
            return self._on_press(e)
        self._toggle_mini()

    # ---------------- son çalınan şarkı (Spotify kapalıyken gösterilir)
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
        self.pal = self.pal_to  # açılışta renk geçişi olmadan
        self.pal_t = 1.0

    def _save_last(self, st, art_changed):
        pos, end = self._position()
        self.last = {"title": st["title"], "artist": st["artist"], "album": st["album"],
                     "pos": pos, "end": end, "shuffle": st.get("shuffle"),
                     "repeat": st.get("repeat")}
        self._last_saved = time.monotonic()
        try:
            with open(LAST_PATH, "w", encoding="utf-8") as f:
                json.dump(self.last, f, ensure_ascii=False)
            if art_changed:
                if st.get("art"):
                    with open(LAST_ART_PATH, "wb") as f:
                        f.write(st["art"])
                elif os.path.exists(LAST_ART_PATH):
                    os.remove(LAST_ART_PATH)
        except Exception as e:  # noqa: BLE001
            log("son şarkı kaydedilemedi:", repr(e))

    def _view(self):
        """Ekranda gösterilecek şarkı: çalan şarkı ya da (Spotify kapalıysa) son çalınan."""
        if self.st.get("ok"):
            return self.st
        if self.last:
            return dict(self.last, ok=False, offline=True, playing=False)
        return self.st

    def _tick(self):
        now = time.monotonic()
        dt = min(0.2, now - self.last_t)
        self.last_t = now

        st = self.media.snapshot()
        if st.get("art_changed"):
            self._on_new_art(st.get("art"))
        self.st = st
        playing = self._playing()
        self._stat_tick(now, st, playing)
        if st.get("ok") and self.cfg["list_tab"] == "lyrics" and self.cfg["show_queue"]:
            self.lyrics.request(st.get("title"), st.get("artist"), st.get("album"), st.get("end"))
        self._audio_tick(now, st, playing)
        self._alarm_tick(st)
        if st.get("ok"):
            self.web.notify((st["title"], st["artist"]), playing)
            if st.get("title") and (st.get("art_changed") or self.last is None
                                    or self.last.get("title") != st["title"]
                                    or now - self._last_saved > 10):
                self._save_last(st, bool(st.get("art_changed")))
            if st.get("art_changed") and st.get("title"):
                if not (self.history and self.history[0].get("title") == st["title"]
                        and self.history[0].get("artist") == st["artist"]):
                    self._stat_day()["plays"] += 1
                self._add_history(st)
                key = (st["title"], st["artist"])
                if not self._seen_first:  # açılıştaki şarkı: bildirim yok
                    self._seen_first = True
                    self._toast_key = key
                elif key != self._toast_key:
                    self._toast_key = key
                    fs = fullscreen_app((self.hwnd, self.toast.hwnd))
                    if self.cfg["notify"] and (fs is not None or (
                            self.cfg["mode"] == "desktop" and not self.inside)):
                        self.toast.show(st, fs)

        # klavye kısayolları
        while self.hk_queue:
            act = self.hk_queue.pop(0)
            if act == "vol+":
                self._change_volume(0.05)
            elif act == "vol-":
                self._change_volume(-0.05)
            elif act == "search":
                SearchDialog(self)
                continue
            else:
                self._action(act)
            self._hotkey_osd(act)

        # uyku zamanlayıcısı (son 20 saniyede ses yavaşça kısılır)
        if self.sleep_end is not None:
            rem = self.sleep_end - now
            if rem <= 0:
                self._finish_sleep()
            elif rem <= 20 and playing:
                if self.sleep_fade is None:
                    self.sleep_fade = self.volume.get()
                if self.sleep_fade is not None:
                    self.volume.set(self.sleep_fade * rem / 20)

        # güvence: <Leave> olayı kaçarsa imleç konumundan fark et
        if self.inside and self._leave_job is None and self.drag_seek is None \
                and not self.win_drag:
            pt, rc = wintypes.POINT(), wintypes.RECT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.GetWindowRect(self.hwnd, ctypes.byref(rc))
            cw, ch = self.WP, round(self.content_h * self.S)  # görünen kısım
            if not (rc.left <= pt.x < rc.left + cw and rc.top <= pt.y < rc.top + ch):
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
        # palet geçişi
        if self.pal_t < 1.0:
            self.pal_t = min(1.0, self.pal_t + dt / 0.5)
            e = 1 - (1 - self.pal_t) ** 3
            self.pal = mix_scheme(self.pal_from, self.pal_to, e)
            self.pal_version += 1
            animating = True
        # oynat butonu şekil dönüşümü (duraklatıldı: köşeli, çalıyor: hap)
        target_r = 28.0 if playing else 16.0
        if abs(self.play_r - target_r) > 0.2:
            self.play_r += (target_r - self.play_r) * min(1.0, dt * 12)
            animating = True
        else:
            self.play_r = target_r
        # dalga
        target_amp = AMP if (playing and self.cfg["wave"] and not self.mini) else 0.0
        if target_amp and self.cfg["react"]:  # müziğe tepki: dalga yüksekliği sese göre
            target_amp = AMP * (0.35 + 1.45 * self.level)
        if abs(self.amp - target_amp) > 0.05:
            self.amp += (target_amp - self.amp) * min(1.0, dt * (14 if self.cfg["react"] else 6))
            animating = True
        else:
            self.amp = target_amp
        if self.amp > 0.05:
            self.phase += dt * LAM / 1.4
            animating = True

        self._render_all()
        if self.marq is not None:
            animating = True
        self.root.after(40 if (animating or self.drag_seek is not None) else 200, self._tick)

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
        if img is not None:
            self.art_src = img.convert("RGB")
            self.seed = seed_from_image(img)
        else:
            self.art_src = None
        self._make_art()
        if img is None:
            self.seed = DEFAULT_SEED
        self._start_palette(self._scheme())

    def _scheme(self):
        seed = dict(ACCENTS).get(self.cfg.get("accent"), self.seed)
        return build_scheme(seed, self.light)

    # ---------------- arka plan: düz / cam / yarı şeffaf
    @staticmethod
    def _wallpaper_file():
        """(yol, değişme zamanı) ya da None."""
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
        """Pencerenin arkasındaki duvar kağıdı parçası, bulanık (WMAX x HMAX, RGB)."""
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
                    if style == "2":  # uzat
                        full = src.resize((mw // 4, mh // 4), Image.Resampling.BILINEAR)
                    else:  # doldur (varsayılan): kırparak kapla
                        sc = max(mw / src.width, mh / src.height) / 4
                        tmp = src.resize((max(1, round(src.width * sc)),
                                          max(1, round(src.height * sc))),
                                         Image.Resampling.BILINEAR)
                        ox, oy = (tmp.width - mw // 4) // 2, (tmp.height - mh // 4) // 2
                        full = tmp.crop((ox, oy, ox + mw // 4, oy + mh // 4))
                    g["small"] = full.filter(ImageFilter.GaussianBlur(9))
                except Exception as e:  # noqa: BLE001
                    log("duvar kağıdı okunamadı:", repr(e))
        small = g["small"]
        if small is None:
            return None
        key = (wx, wy, self.WMAX, self.HMAX)
        if g.get("crop_key") != key:
            rx, ry = (wx - mkey[0]) / 4, (wy - mkey[1]) / 4
            g["crop"] = small.crop((round(rx), round(ry), round(rx + self.WMAX / 4) + 1,
                                    round(ry + self.HMAX / 4) + 1)).resize(
                (self.WMAX + 4, self.HMAX + 4), Image.Resampling.BILINEAR).crop(
                (0, 0, self.WMAX, self.HMAX))
            g["crop_key"] = key
        return g["crop"]

    def _card(self, img, x0, y0, x1, y1, r):
        """Kart zemini (fiziksel piksel), seçili arka plan stiline göre."""
        P, mode = self.pal, self.cfg["bg"]
        if mode == "clear":
            rr(img, x0, y0, x1, y1, r, P["surface"], alpha=150)
            return
        if mode == "glass":
            bg = self._glass_bg()
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
                return
        rr(img, x0, y0, x1, y1, r, P["surface"])

    # ---------------- dinleme istatistikleri
    def _load_stats(self):
        try:
            with open(STATS_PATH, "r", encoding="utf-8") as f:
                s = json.load(f)
            if isinstance(s.get("days"), dict):
                return s
        except Exception:  # noqa: BLE001
            pass
        return {"days": {}}

    def _save_stats(self):
        days = self.stats["days"]
        cutoff = (date.today() - timedelta(days=400)).isoformat()
        for k in [k for k in days if k < cutoff]:
            del days[k]
        try:
            with open(STATS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.stats, f, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            log("istatistik kaydedilemedi:", repr(e))
        self._stat_saved = time.monotonic()

    def _stat_day(self):
        return self.stats["days"].setdefault(date.today().isoformat(),
                                             {"sec": 0.0, "plays": 0, "artists": {}})

    def _stat_tick(self, now, st, playing):
        prev, self._stat_t = self._stat_t, now
        if prev is None or not (playing and st.get("ok")):
            return
        delta = now - prev
        if not 0 < delta < 5:  # uyku / donma sonrası büyük boşlukları sayma
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
        if now - self._stat_saved > 30:
            self._save_stats()

    def _index_imported(self):
        """İçe aktarılan geçmişten bir kerelik çıkarımlar: sanatçıyı ilk dinleme günü."""
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
        """(şu anki seri, en uzun seri): en az 1 dakika müzik dinlenen ardışık günler."""
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
        if prev is not None and (today - prev).days <= 1:  # bugün ya da dün dinlendiyse sürüyor
            cur = run
        res = (cur, best)
        self._streak_cache = (ck, res)
        return res

    def _last_year_today(self):
        """Geçen yıl bugün en çok dinlenen şarkı: (şarkı, sanatçı, saniye) ya da None."""
        t = date.today()
        try:
            ly = t.replace(year=t.year - 1)
        except ValueError:  # 29 Şubat
            ly = t.replace(year=t.year - 1, day=28)
        d = self._stat_get(ly.isoformat())
        if not d or not d.get("tracks"):
            return None
        name, sec = max(d["tracks"].items(), key=lambda kv: kv[1])
        title, _s, artist = name.partition("\t")
        return title, artist, sec

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
        """Wrapped tarzı yıllık özet görseli (paylaşılabilir, 2x çözünürlük)."""
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
        # üstte birincil renkten yüzeye geçen degrade
        top = Image.new("RGBA", img.size, P["primary_container"] + (255,))
        mask = Image.linear_gradient("L").resize(img.size).point(lambda v: 255 - v)
        img.paste(top, (0, 0), mask.point(lambda v: int(v * 0.85)))
        d = Drawer(img)
        on, var, pri = P["on_surface"] + (255,), P["on_surface_variant"] + (255,), \
            P["primary"] + (255,)
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
        self.toast.show_message("", T("sh_link_ok"))  # bağlantı simgesi

    def _share_card(self):
        """Kapak + şarkı adı ile 1080x1350 paylaşım kartı: panoya kopyalar ve Resimler'e kaydeder."""
        from PIL import ImageFilter
        st = self._view()
        if not st.get("title"):
            return
        P, S = self.pal, 1080 / 400
        X = lambda v: v * S  # noqa: E731
        Wd, Hd = 400, 500
        img = Image.new("RGB", (1080, 1350), P["surface"])
        if self.art_src is not None:  # bulanık kapak arka planı
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
               fill=P["primary"] + (255,), anchor="mm")
        out = img.convert("RGB")
        copy_image_to_clipboard(out)
        try:
            pics = os.path.join(os.path.expanduser("~"), "Pictures", "SpotifyWidget")
            os.makedirs(pics, exist_ok=True)
            safe = "".join(c for c in st["title"] if c not in '<>:"/\\|?*')[:60].strip() or "song"
            out.save(os.path.join(pics, f"{safe}.png"))
        except Exception as e:  # noqa: BLE001
            log("kart kaydedilemedi:", repr(e))
        self.toast.show_message("", T("sh_card_ok"))  # resim simgesi

    def _fill_devices(self, menu):
        """Cihaza aktar alt menüsü açılırken cihazları getir."""
        menu.delete(0, "end")
        if not self.web.connected:
            menu.add_command(label=T("s_need"), command=lambda: ConnectDialog(self))
            return
        try:
            devs = self.web.devices()
        except Exception as e:  # noqa: BLE001
            log("cihazlar:", repr(e))
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
                log("cihaza aktarılamadı:", repr(e))

        threading.Thread(target=go, daemon=True).start()

    STAT_PERIODS = ("7", "30", "365", "all")

    def _stat_get(self, key):
        """Bir günün verisi: canlı takip ve içe aktarılan geçmiş çakışırsa büyüğü alınır
        (aynı dinlemenin iki kez sayılmaması için)."""
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
        elif period.startswith("y"):  # tek bir yıl (aylar)
            yy = int(period[1:])
            start, end = date(yy, 1, 1), min(today, date(yy, 12, 31))
        elif period.startswith("m"):  # tek bir ay (günler)
            yy, mo = int(period[1:5]), int(period[6:8])
            start = date(yy, mo, 1)
            nxt = date(yy + mo // 12, mo % 12 + 1, 1)
            end = min(today, nxt - timedelta(days=1))
        else:
            keys = list(self.imported) + list(self.stats["days"])
            start = date.fromisoformat(min(keys)) if keys else today
        s0, s1 = start.isoformat(), end.isoformat()

        # grafik kovaları; targets: tıklanınca açılacak dönem (ay / yıl)
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

        # içe aktarılan (değişmeyen) kısım önbellekte; canlı günler her seferinde eklenir
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
            if period == "all":  # tüm zamanlar: günlük ortalama yerine dinleme serisi
                cur_s, best_s = self._streaks()
                tiles[2] = (T("st_streak_v", n=cur_s), T("st_streak", n=best_s))
        top_a = sorted(artists.items(), key=lambda kv: -kv[1])[:3]
        top_t = [(t.split("\t")[0], t.split("\t")[1] if "\t" in t else "", v)
                 for t, v in sorted(tracks.items(), key=lambda kv: -kv[1])[:3]]
        # açılan dönemin başlığı ve bir üst seviye
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
                log("içe aktarma:", repr(result["e"]))
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

    # ---------------- çizim
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
                self.surface.full(frame)
            else:
                self.surface.region(strip, 0, self.sy0)
        if self.marq and not full:
            off = self._marq_offset()
            if off != self.marq_key:
                self.marq_key = off
                self.surface.region(self._render_marquee(off), self.marq["x"], self.marq["y0"])

    def _keep_on_screen(self):
        """Yükseklik değişince (mini ↔ normal, sekme, satır sayısı): widget ekranın alt
        yarısındaysa alt kenarını sabit tutup yukarı doğru büyür; her durumda görev
        çubuğunun altına taşmaz."""
        new_h = round(self.content_h * self.S)
        old_h = getattr(self, "_shown_h", None)
        self._shown_h = new_h
        if old_h is None or old_h == new_h or self.win_drag:
            return
        x, y = self._win_pos()
        if x <= -20000:  # henüz yerleştirilmedi
            return
        wa = monitor_info(self.hwnd).rcWork
        ny = y
        if y + old_h / 2 > (wa.top + wa.bottom) / 2:  # alt yarıda: alttan sabitle
            ny = y + old_h - new_h
        ny = max(wa.top, min(ny, wa.bottom - new_h))
        if ny != y:
            move_window(self.hwnd, x, ny)
            self.cfg["x"], self.cfg["y"] = x, ny
            self.cfg.save()

    def _hotkey_osd(self, act):
        """Kısayolla yapılan işlemi ekranda kısaca göster (widget görünmüyorsa: tam ekran
        uygulama ya da önde başka bir pencere varken)."""
        if not self.cfg["osd"] or self.inside:
            return
        fs = fullscreen_app((self.hwnd, self.toast.hwnd))
        if fs is None and self.cfg["mode"] == "desktop":
            fg = user32.GetForegroundWindow()
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(fg, cls, 64)
            if not fg or cls.value in _SHELL_CLASSES:  # masaüstü önde: widget zaten görünüyor
                return
        elif fs is None:
            return  # her zaman üstte: widget zaten görünüyor
        if act in ("vol+", "vol-"):
            v = self.vol_val if self.vol_val is not None else self.volume.get()
            if v is not None:
                self.toast.show_message(IC_MUTE if v <= 0 else IC_VOL, T("osd_vol"), fs,
                                        value=v, hold=1000)
        elif act == "play":
            playing = not self.st.get("playing")  # komut henüz işlenmedi: tersi olacak
            title = self.st.get("title") or ""
            self.toast.show_message(IC_PAUSE if playing else IC_PLAY,
                                    (T("osd_play") if playing else T("osd_pause"))
                                    + (f" · {title}" if title else ""), fs)
        elif act in ("next", "prev") and not self.cfg["notify"]:
            # bildirim açıksa yeni şarkı zaten gösterilecek
            self.toast.show_message(IC_NEXT if act == "next" else IC_PREV,
                                    T("hk_next" if act == "next" else "hk_prev"), fs, hold=900)

    # ---------------- ses: müziğe tepki, Discord'da kısma
    def _audio_tick(self, now, st, playing):
        # müziğe tepki veren dalga: Spotify'ın çıkış seviyesi (otomatik kazançla 0..1)
        if self.cfg["react"] and self.cfg["wave"] and playing and not self.mini:
            p = self.volume.peak(("spotify.exe",))
            self._lvl_max = max(p, self._lvl_max * 0.995, 0.05)
            target = min(1.0, (p / self._lvl_max) ** 0.8)
            k = 0.6 if target > self.level else 0.25  # hızlı yüksel, yavaş düş
            self.level += (target - self.level) * k
        else:
            self.level *= 0.8

        # Discord'da biri konuşunca Spotify'ı kıs, susunca geri aç
        if not self.cfg["duck"] or self.sleep_fade is not None or self._alarm_fade:
            if self._duck:
                self._duck_restore()
            return
        if self._duck_fade:  # geri açılış (yumuşak)
            f = self._duck_fade
            t = min(1.0, (now - f["t0"]) / 0.8)
            self.volume.set(f["from"] + (f["to"] - f["from"]) * t)
            if t >= 1.0:
                self._duck_fade = None
        talking = self.volume.peak(tuple(self.cfg["duck_apps"])) > 0.015
        if talking:
            if self._duck is None and st.get("ok"):
                v = self.volume.get()
                if v is not None and v > 0.02:
                    self._duck_fade = None
                    self._duck = {"orig": v, "low": v * 0.3, "last": now}
                    self.volume.set(v * 0.3)
            elif self._duck:
                self._duck["last"] = now
        elif self._duck and now - self._duck["last"] > 1.2:
            self._duck_restore()

    def _duck_restore(self):
        d, self._duck = self._duck, None
        cur = self.volume.get()
        # kullanıcı bu arada sesi değiştirdiyse ona dokunma
        if cur is not None and abs(cur - d["low"]) < 0.03:
            self._duck_fade = {"from": cur, "to": d["orig"], "t0": time.monotonic()}

    # ---------------- alarm
    def _alarm_tick(self, st):
        al = self.cfg["alarm"]
        now = time.localtime()
        if al.get("on") and time.strftime("%H:%M", now) == al.get("time") \
                and self._alarm_day != now.tm_yday:
            self._alarm_day = now.tm_yday
            log("alarm:", al.get("time"))
            if not st.get("ok"):
                try:
                    os.startfile("spotify:")
                except OSError:
                    pass
            self._alarm_fade = {"t0": None, "target": None, "deadline": time.monotonic() + 90}
            self.toast.show_message(IC_ALARM, T("al_fired"), fullscreen_app(
                (self.hwnd, self.toast.hwnd)))
        f = self._alarm_fade
        if not f:
            return
        mono = time.monotonic()
        if f["t0"] is None:  # Spotify'ın açılmasını bekle, sonra sesi kısıp çal
            if mono > f["deadline"]:
                self._alarm_fade = None
                return
            if st.get("ok"):
                v = self.volume.get()
                if v is None:
                    return
                f["target"] = v if v > 0.1 else 0.6
                self.volume.set(0.02)
                if not st.get("playing"):
                    self.media.command("play")
                f["t0"] = mono
            return
        t = min(1.0, (mono - f["t0"]) / 60.0)  # 1 dakikada sesi aç
        self.volume.set(0.02 + (f["target"] - 0.02) * t)
        if t >= 1.0:
            self._alarm_fade = None

    def _vol_shown(self):
        if self.vol_val is not None and time.monotonic() < self.vol_until:
            return round(self.vol_val * 100)
        return None

    def _draw_volume(self, img, d, x0, y0, size, radius):
        """Kapağın üstüne ses göstergesi."""
        v = self._vol_shown()
        if v is None:
            return
        S = self.S
        X = lambda q: q * S  # noqa: E731
        rr(img, X(x0), X(y0), X(x0 + size), X(y0 + size), X(radius), (0, 0, 0), alpha=150)
        white = (255, 255, 255, 255)
        cx = x0 + size / 2
        big = size >= 90
        glyph = "" if v == 0 else ""  # sessiz / hoparlör
        if big:
            d.text((X(cx), X(y0 + size / 2 - 16)), glyph, font=self.icon_font(24), fill=white,
                   anchor="mm")
            d.text((X(cx), X(y0 + size / 2 + 14)), f"%{v}", font=self.F["title"], fill=white,
                   anchor="mm")
        else:
            d.text((X(cx), X(y0 + size / 2 - 3)), f"%{v}", font=self.F["btn"], fill=white,
                   anchor="mm")
        # alttaki çubuk
        m = 12 if big else 8
        by = y0 + size - (14 if big else 9)
        rr(img, X(x0 + m), X(by), X(x0 + size - m), X(by + 4), X(2), (255, 255, 255), alpha=70)
        if v > 0:
            rr(img, X(x0 + m), X(by), X(x0 + m + (size - 2 * m) * v / 100), X(by + 4), X(2),
               (255, 255, 255))

    MARQ_PAUSE = 2.0   # başta bekleme (sn)
    MARQ_SPEED = 32    # px/sn (ölçeksiz)
    MARQ_GAP = 48      # tekrar arası boşluk

    def _make_marquee(self, title, x, cy, w, tlen, color):
        S, F = self.S, self.F
        h = round(26 * S)
        gap = round(self.MARQ_GAP * S)
        span = round(tlen) + gap
        layer = Image.new("RGBA", (span * 2 + 2, h), (0, 0, 0, 0))
        d = Drawer(layer)
        for k in (0, 1):  # kesintisiz döngü için iki kopya
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
        """Mini mod: kapak, şarkı adı ve kontrollerden oluşan hap görünüm."""
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
        # kapak
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
            d.text((X(ax + MINI_ART / 2), X(ay + MINI_ART / 2)), IC_OPEN,
                   font=self.icon_font(18), fill=(255, 255, 255, 255), anchor="mm")
        self._draw_volume(img, d, ax, ay, MINI_ART, 16)
        hits.append(("art", ax, ay, ax + MINI_ART, ay + MINI_ART))

        # kontroller (sağda)
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
        rr(img, X(px_ - pw / 2), X(cy - ph / 2), X(px_ + pw / 2), X(cy + ph / 2),
           X(min(22.0, self.play_r * 0.8)), col)
        d.text((X(px_), X(cy)), IC_PAUSE if playing else IC_PLAY, font=self.icon_font(19),
               fill=P["on_primary"] + (255,), anchor="mm")
        hits.append(("play", px_ - pw / 2, cy - ph / 2, px_ + pw / 2, cy + ph / 2))

        # metin
        tx = ax + MINI_ART + 12
        tw = X(vx - 24 - tx)
        if ok or offline:
            title, sub = st["title"] or T("unknown"), st["artist"]
        else:
            title, sub = T("waiting_title"), T("mini_waiting_sub")
        tlen = F["mtitle"].getlength(title)
        if tlen > tw:  # sığmıyor: ayrı katmanda yana kayarak çizilir
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
        offline = bool(st.get("offline"))  # Spotify kapalı, son çalınan gösteriliyor
        playing = self._playing()
        hov = self.hover

        def state_layer(x0, y0, x1, y1, r, on, ellipse=False, strength=0.10):
            rr(img, X(x0), X(y0), X(x1), X(y1), X(r), on, alpha=round(255 * strength),
               ellipse=ellipse)

        def icon(cx, cy, glyph, px, color):
            d.text((X(cx), X(cy)), glyph, font=self.icon_font(px), fill=tuple(color) + (255,),
                   anchor="mm")

        # ana kart
        self._card(img, 0, 0, X(W), X(MAIN_H), X(28))

        # kapak
        ax, ay, asz = 16, 16, 120
        if self.art_rgba is not None:
            img.alpha_composite(self.art_rgba, (round(X(ax)), round(X(ay))))
        else:
            rr(img, X(ax), X(ay), X(ax + asz), X(ay + asz), X(20), P["surface_highest"])
            icon(ax + asz / 2, ay + asz / 2, IC_MUSIC, 40, P["on_surface_variant"])
        if hov == "art" and self._vol_shown() is None:
            rr(img, X(ax), X(ay), X(ax + asz), X(ay + asz), X(20), (0, 0, 0), alpha=110)
            icon(ax + asz / 2, ay + asz / 2, IC_OPEN, 26, (255, 255, 255))
        self._draw_volume(img, d, ax, ay, asz, 20)
        hits.append(("art", ax, ay, ax + asz, ay + asz))

        # beğen butonu (sağ üst)
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

        # metinler
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
            # uyarı çipi: tıklayınca Spotify'ı açar
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
            d.text((X(tx), X(20)), status, font=F["status"], fill=P["primary"] + (255,),
                   anchor="lm")
        y = 34
        for line in wrap_text(title, F["title"], tw, 2):
            d.text((X(tx), X(y)), line, font=F["title"], fill=P["on_surface"] + (255,))
            y += 25
        y += 2
        # sanatçı ve albüm tıklanabilir: Spotify'da sayfasını açar
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
                if hov == hid:  # alt çizgi
                    uy = X(y) + F[fkey].asc + max(1.0, 1.5 * S)
                    rr(img, X(tx), uy, X(tx) + wpx, uy + max(1.0, S), 0, col)
                hits.append((hid, tx, y, tx + wpx / S, y + lh))
            y += lh + 1

        # ilerleme çubuğu bölgesi (çizimi _render_strip yapar)
        hits.append(("seek", BAR_X0 - 8, 146, BAR_X1 + 8, 174))

        # kontroller
        cy = CTRL_CY
        # karışık
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
        # tekrar
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
        # önceki / sonraki (tonal)
        for hid, bx, glyph in (("prev", CX - 84, IC_PREV), ("next", CX + 84, IC_NEXT)):
            col = P["secondary_container"]
            if hov == hid:
                col = mix(col, P["on_secondary_container"], 0.10)
            if self.pressed == hid:
                col = mix(col, P["on_secondary_container"], 0.16)
            rr(img, X(bx - 26), X(cy - 26), X(bx + 26), X(cy + 26), 0, col, ellipse=True)
            icon(bx, cy, glyph, 19, P["on_secondary_container"])
            hits.append((hid, bx - 26, cy - 26, bx + 26, cy + 26))
        # oynat / duraklat (birincil, şekil değiştiren)
        pw, ph = 80, 58
        col = P["primary"]
        if hov == "play":
            col = mix(col, P["on_primary"], 0.10)
        if self.pressed == "play":
            col = mix(col, P["on_primary"], 0.18)
        rr(img, X(CX - pw / 2), X(cy - ph / 2), X(CX + pw / 2), X(cy + ph / 2),
           X(self.play_r), col)
        icon(CX, cy, IC_PAUSE if playing else IC_PLAY, 24, P["on_primary"])
        hits.append(("play", CX - pw / 2, cy - ph / 2, CX + pw / 2, cy + ph / 2))

        # sıradaki kartı
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

        # gösterilecek satırlar: (ad, alt yazı, küçük kapak, sağdaki metin)
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

        # başlık: sekmeler + daralt
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
            # küçük kapak
            tx0, ty0 = 20, ry + (Q_ROW - 40) / 2
            if th == "cal":  # geçen yıl bugün
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

        # dönem seçici
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
        if sm["title"]:  # açılmış yıl / ay: tıklayınca bir üst seviyeye döner
            lbl = "← " + sm["title"]
            w = F["qsub"].getlength(lbl) / S + 22
            x = max(x + 4, W - 16 - w)
            col = P["primary"] if hov != "sup" else mix(P["primary"], P["on_primary"], 0.12)
            rr(img, X(x), X(cy - 13), X(x + w), X(cy + 13), X(13), col)
            d.text((X(x + w / 2), X(cy)), lbl, font=F["qsub"], fill=P["on_primary"] + (255,),
                   anchor="mm")
            hits.append(("sup", x, cy - 14, x + w, cy + 14))

        # özet kutuları (bir yıl açıkken üçüncü kutu yıllık özet kartını açar)
        gap, x0, ty = 8, 16, top + 34
        tw = (W - 32 - 2 * gap) / 3
        for i, (val, lbl) in enumerate(sm["tiles"]):
            tx = x0 + i * (tw + gap)
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
                   font=F["qhead"], fill=(P["primary"] if i == 0 else P["on_surface"]) + (255,),
                   anchor="lm")
            d.text((X(tx + 12), X(ty + 37)), fit_text(lbl, F["qsub"], X(tw - 20)),
                   font=F["qsub"], fill=P["on_surface_variant"] + (255,), anchor="lm")

        # grafik (gün / ay / yıl)
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
                       fill=(P["primary"] if (hl or over) else P["on_surface_variant"]) + (255,),
                       anchor="mm")
            if target is not None:
                hits.append((hid, cx - cw / 2, by0 - 6, cx + cw / 2, by1 + 22))

        # en çok dinlenenler: sanatçı / şarkı / saatler
        hy = top + 182
        d.text((X(20), X(hy)), T({"artists": "st_top_a", "tracks": "st_top_t"}.get(
            lst, "st_hours")), font=F["status"], fill=P["primary"] + (255,), anchor="lm")
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
            # şarkılarda sanatçı, sanatçılarda ilk dinleme tarihi
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
        """Şu an söylenen satırın indeksi (senkronize değilse -1)."""
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
        """Haftanın günü × saat ısı haritası (7 satır, 24 sütun)."""
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
        if self.mini:  # mini mod: kartın altında ince düz çizgi
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

        # çalınan kısım: dalga
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
            # yuvarlak uçlar
            for xl, xp in ((off / S, bx0), ((off + length) / S, bx0 + length)):
                yy = top + tile.height / 2 + amp_q * S * math.sin(2 * math.pi * xl / LAM)
                rr(strip, xp - X(2), yy - X(2), xp + X(2), yy + X(2), 0, primary, ellipse=True)
        # kalan kısım
        rem = px + gap
        if rem < bx1 - 1:
            rr(strip, rem, cy - X(2), bx1, cy + X(2), X(2), P["surface_highest"])
            if rem < bx1 - X(10):
                rr(strip, bx1 - X(4), cy - X(2), bx1, cy + X(2), 0, primary, ellipse=True)
        # tutamaç
        rr(strip, px - X(tw / 2), cy - X(th / 2), px + X(tw / 2), cy + X(th / 2), X(tw / 2),
           primary)
        # süreler
        ty = X(180) - self.sy0
        d.text((bx0, ty), fmt_time(shown), font=self.F["time"],
               fill=P["on_surface_variant"] + (255,), anchor="lm")
        d.text((bx1, ty), fmt_time(end), font=self.F["time"],
               fill=P["on_surface_variant"] + (255,), anchor="rm")
        return strip

    # ---------------- olaylar
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
                self._action(p)
        elif self.win_drag:
            self.win_drag = None
            self.cfg["x"], self.cfg["y"] = self._win_pos()
            self.cfg.save()
        self._render_all()

    def _on_wheel(self, e):
        if e.state & 0x4:  # Ctrl + tekerlek: boyutu değiştir
            self._set_scale(float(self.cfg["scale"]) + (SCALE_STEP if e.delta > 0
                                                         else -SCALE_STEP))
            return
        x, y = self._xy(e)
        if self._hit(x, y) == "art":  # kapak üstünde tekerlek: Spotify'ın sesi
            self._change_volume(0.05 if e.delta > 0 else -0.05)
            self._render_all()
            return
        if self._hit(x, y) == "seek" and self.st.get("ok"):
            pos, end = self._position()
            target = max(0.0, min(end, pos + (5 if e.delta > 0 else -5)))
            self.seek_hold = (target, time.monotonic())
            self.media.command("seek", target)

    def _action(self, a):
        st = self.st
        if a == "open" or (a == "play" and not st.get("ok")):
            a = "art"  # Spotify kapalıyken: Spotify'ı aç
        if a == "play":
            self.play_hold = (not self._playing(), time.monotonic())
            self.media.command("toggle")
        elif a in ("prev", "next"):
            self.media.command(a)
        elif a == "shuffle":
            self.media.command("shuffle", not st.get("shuffle"))
        elif a == "repeat":
            nxt = {0: 2, 2: 1, 1: 0}.get(st.get("repeat") or 0, 0)  # kapalı → liste → şarkı
            self.media.command("repeat", nxt)
        elif a == "art":
            try:
                os.startfile("spotify:")
            except OSError:
                webbrowser.open("https://open.spotify.com")
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
        elif a == "swrap":
            RecapWindow(self, int(self.cfg["stats_period"][1:]))
        elif a == "sup":  # açılmış aydan yıla, yıldan tümüne
            up = self._stats_summary()["up"]
            if up:
                self._set("stats_period", up)
        elif a.startswith("sb"):  # grafikte yıl / ay çubuğu: o dönemi aç
            bars = self._stats_summary()["bars"]
            i = int(a[2:])
            if i < len(bars) and bars[i][2]:
                self._set("stats_period", bars[i][2])
        elif a.startswith("sl_"):
            self._set("stats_list", a[3:])
        elif a.startswith("sr"):  # en çok dinlenen: Spotify'da ara
            sm, i = self._stats_summary(), int(a[2:])
            if self.cfg["stats_list"] == "artists":
                q = sm["top_a"][i][0] if i < len(sm["top_a"]) else ""
            else:
                q = " ".join(sm["top_t"][i][:2]) if i < len(sm["top_t"]) else ""
            if q:
                try:
                    os.startfile("spotify:search:" + urllib.parse.quote(q))
                except OSError:
                    webbrowser.open("https://open.spotify.com/search/" + urllib.parse.quote(q))
        elif a.startswith("h"):  # son çalınan: Spotify'da ara
            items = self._recent_rows(int(self.cfg["queue_rows"]))
            i = int(a[1:])
            if i < len(items):
                self._spotify_search(items[i]["q"].strip())
        elif a.startswith("ly"):  # söz satırı: o ana atla
            i = int(a[2:])
            lines = self.lyrics.lines()
            if lines and i < len(lines) and lines[i][0] is not None:
                self.seek_hold = (lines[i][0], time.monotonic())
                self.media.command("seek", lines[i][0])
        elif a.startswith("q"):
            n = int(a[1:]) + 1
            self.media.command("skip_n", n)
            self.web.refresh_now()

    # ---------------- menü (kendi çizdiğimiz Material menü: MenuPanel)
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

    @staticmethod
    def _startup_on():
        return os.path.exists(STARTUP_VBS)

    def _set_startup(self, on):
        try:
            if on:
                if FROZEN:
                    cmd = f'"""{sys.executable}"""'
                else:
                    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                    if not os.path.exists(pyw):
                        pyw = sys.executable
                    cmd = f'"""{pyw}"" ""{os.path.abspath(__file__)}"""'
                with open(STARTUP_VBS, "w", encoding="utf-8") as f:
                    f.write(f'CreateObject("WScript.Shell").Run {cmd}, 0, False\n')
            elif os.path.exists(STARTUP_VBS):
                os.remove(STARTUP_VBS)
        except Exception as e:  # noqa: BLE001
            log("başlangıç ayarı:", e)

    def run(self):
        self.root.mainloop()
        self._save_stats()
        if self._duck:  # kapanırken kısılmış sesi geri getir
            self.volume.set(self._duck["orig"])


# ------------------------------------------------------------------ bildirim baloncuğu
class Toast:
    """Şarkı değişince sağ altta beliren kısa 'şimdi çalıyor' baloncuğu."""
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
        # sahipsiz pencere: masaüstüne iliştirilmiş ana pencereye bağlı kalmasın
        user32.SetWindowLongPtrW(self.hwnd, GWLP_HWNDPARENT, 0)
        self.surface = LayeredSurface(self.hwnd, self.WM, self.HM)
        self.surface.alpha = 0
        self.fs = False
        self._hold_until = 0.0
        self.hold = self.HOLD_MS
        self.surface.full(Image.new("RGBA", (self.WM, self.HM), (0, 0, 0, 0)))
        t.bind("<Button-1>", lambda _e: self.hide())
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
        tw = X(self.TW - 88 - 18)
        d.text((X(88), X(24)), T("playing"), font=F["status"], fill=P["primary"] + (255,),
               anchor="lm")
        d.text((X(88), X(45)), fit_text(st.get("title") or T("unknown"), F["mtitle"], tw),
               font=F["mtitle"], fill=P["on_surface"] + (255,), anchor="lm")
        d.text((X(88), X(65)), fit_text(st.get("artist") or "", F["album"], tw),
               font=F["album"], fill=P["on_surface_variant"] + (255,), anchor="lm")
        return img

    def show(self, st, fs=None):
        """fs: tam ekran uygulamanın monitörü (varsa bildirim onun üstünde, sağ üstte çıkar
        ve tıklamaları oyuna geçirir)."""
        self._present(self._render(st), self.TW, self.TH, fs, self.HOLD_MS)

    def show_message(self, icon, text, fs=None, value=None, hold=1400):
        """Kısa ekran göstergesi (kısayollar, alarm, kopyalandı): simge + metin
        (+ value verilirse ses çubuğu)."""
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
        if value is None:
            d.text((X(64), X(30)), fit_text(text, F["mtitle"], X(w - 80)), font=F["mtitle"],
                   fill=P["on_surface"] + (255,), anchor="lm")
        else:
            d.text((X(64), X(22)), text, font=F["btn"], fill=P["on_surface"] + (255,),
                   anchor="lm")
            d.text((X(w - 20), X(22)), f"%{round(value * 100)}", font=F["btn"],
                   fill=P["primary"] + (255,), anchor="rm")
            rr(img, X(64), X(36), X(w - 20), X(41), X(2.5), P["on_surface"], alpha=30)
            if value > 0:
                rr(img, X(64), X(36), X(64 + (w - 84) * value), X(41), X(2.5), P["primary"])
        self._present(img, w, h, fs, hold)

    def _present(self, img, w, h, fs, hold):
        S = self.app.S
        self.hold = hold
        self.fs = fs is not None
        ex = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        ex = (ex | WS_EX_TRANSPARENT) if self.fs else (ex & ~WS_EX_TRANSPARENT)
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, ex)
        self.surface.full(img)
        m = round(16 * S)
        if fs is not None:
            self._pos = (fs.right - round(w * S) - m, fs.top + m + round(8 * S))
        else:
            rc = wintypes.RECT()
            user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rc), 0)  # SPI_GETWORKAREA
            self._pos = (rc.right - round(w * S) - m, rc.bottom - round(h * S) - m)
        self._offset = 14 * S  # aşağıdan yukarı kayarak gelir
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
        speed = 0.3 if target > cur else 0.2
        nxt = cur + (target - cur) * speed
        nxt = target if abs(target - nxt) < 3 else round(nxt)
        self.surface.alpha = nxt
        if target > 0:
            self._offset *= 0.7
            # her adımda en üste yeniden yerleş (oyunlar kendini öne alabiliyor)
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, self._pos[0],
                                self._pos[1] + int(self._offset), 0, 0,
                                SWP_NOSIZE | SWP_NOACTIVATE)
        self.surface._commit(None)
        if nxt != target:
            self._job = self.top.after(16, self._step)
        elif target > 0:  # bekle (en üstte kalarak), sonra kaybol
            self._hold_until = time.monotonic() + self.hold / 1000
            self._job = self.top.after(250, self._hold)
        else:  # gizlendi: ekran dışına al
            move_window(self.hwnd, -30000, -30000)

    def _hold(self):
        self._job = None
        if time.monotonic() >= self._hold_until:
            self.hide()
            return
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
        self._job = self.top.after(250, self._hold)


# ------------------------------------------------------------------ bağlantı penceresi
class ConnectDialog:
    _open = None

    def __init__(self, app, reauth=False):
        if ConnectDialog._open is not None:
            try:
                ConnectDialog._open.lift()
                return
            except tk.TclError:
                pass
        self.app = app
        light = app.light
        bg = "#F7F5FA" if light else "#1C1B1F"
        fg = "#1C1B1F" if light else "#E6E1E5"
        dim = "#49454F" if light else "#CAC4D0"
        acc = "#%02x%02x%02x" % app.pal["primary"]
        on_acc = "#%02x%02x%02x" % app.pal["on_primary"]
        field = "#E7E0EC" if light else "#2B2930"

        t = self.top = tk.Toplevel(app.root)
        ConnectDialog._open = t
        t.title(T("dlg_title"))
        t.configure(bg=bg, padx=24, pady=20)
        t.resizable(False, False)
        t.attributes("-topmost", True)
        t.protocol("WM_DELETE_WINDOW", self._close)

        tk.Label(t, text=T("dlg_head"), bg=bg, fg=fg,
                 font=("Segoe UI Semibold", 15)).pack(anchor="w")
        if reauth:
            tk.Label(t, text=T("dlg_reauth"), bg=bg, fg=acc, justify="left", wraplength=460,
                     font=("Segoe UI Semibold", 10)).pack(anchor="w", pady=(6, 0))
        tk.Label(t, text=T("dlg_steps", uri=REDIRECT_URI), bg=bg, fg=dim, justify="left",
                 font=("Segoe UI", 10)).pack(anchor="w", pady=(8, 12))

        row = tk.Frame(t, bg=bg)
        row.pack(anchor="w", pady=(0, 14))
        self._btn(row, T("dlg_dev"), field, fg,
                  lambda: webbrowser.open("https://developer.spotify.com/dashboard")
                  ).pack(side="left")
        self._btn(row, T("dlg_copy"), field, fg, self._copy).pack(side="left", padx=8)

        tk.Label(t, text="Client ID", bg=bg, fg=dim, font=("Segoe UI", 9)).pack(anchor="w")
        self.entry = tk.Entry(t, bg=field, fg=fg, insertbackground=fg, relief="flat",
                              font=("Consolas", 11), width=40)
        self.entry.pack(anchor="w", ipady=6, ipadx=6, pady=(2, 14), fill="x")
        if app.cfg.get("client_id"):
            self.entry.insert(0, app.cfg["client_id"])

        bottom = tk.Frame(t, bg=bg)
        bottom.pack(fill="x")
        self.status = tk.Label(bottom, text="", bg=bg, fg=dim, font=("Segoe UI", 9),
                               wraplength=300, justify="left")
        self.status.pack(side="left")
        self._btn(bottom, T("dlg_connect"), acc, on_acc, self._connect,
                  bold=True).pack(side="right")
        self._btn(bottom, T("dlg_close"), bg, fg, self._close).pack(side="right", padx=8)
        t.focus_force()
        self.entry.focus_set()

    @staticmethod
    def _btn(parent, text, bg, fg, cmd, bold=False):
        return tk.Button(parent, text=text, bg=bg, fg=fg, activebackground=bg,
                         activeforeground=fg, relief="flat", bd=0, padx=16, pady=7,
                         cursor="hand2", command=cmd,
                         font=("Segoe UI Semibold" if bold else "Segoe UI", 10))

    def _copy(self):
        self.top.clipboard_clear()
        self.top.clipboard_append(REDIRECT_URI)
        self.status.config(text=T("dlg_copied"))

    def _connect(self):
        cid = self.entry.get().strip()
        if len(cid) < 16:
            self.status.config(text=T("dlg_invalid"))
            return
        self.app.web.auth_status = None
        self.app.web.start_auth(cid)
        self._poll()

    def _poll(self):
        s = self.app.web.auth_status
        if s == "ok":
            self.status.config(text=T("dlg_ok"))
            self.top.after(900, self._close)
            return
        self.status.config(text=s or "")
        if self.app.web.auth_error:
            return
        self.top.after(300, self._poll)

    def _close(self):
        ConnectDialog._open = None
        self.top.destroy()


# ------------------------------------------------------------------ sağ tık menüsü
MI_BACK, MI_CHEV, MI_CHECK = "", "", ""
MI = {"search": "", "mini": "", "lock": "", "share": "",
      "open": "", "look": "", "alpha": "", "widget": "",
      "notif": "", "sleep": "", "alarm": "", "device": "",
      "account": "", "lang": "", "close": "", "import": "",
      "link": "", "image": "", "auto": ""}


class MenuPanel:
    """Widget'la aynı Material You görünümlü, sayfalı sağ tık menüsü (katmanlı pencere).
    Alt menüler açılır liste yerine aynı panelde sayfa olarak açılır."""
    _inst = None
    PW = 300          # panel genişliği (mantıksal px)
    M = 14            # gölge payı
    HEIGHTS = {"quick": 80, "sep": 9, "nav": 40, "toggle": 40, "action": 40, "radio": 38,
               "seg": 66, "swatch": 66, "info": 24, "header": 30, "title": 50}

    @classmethod
    def toggle(cls, app, x, y):
        if cls._inst is not None:  # açıksa: yeni konumda yeniden aç
            cls._inst.close()
        cls._inst = MenuPanel(app, x, y)

    @classmethod
    def close_open(cls):
        if cls._inst is not None:
            cls._inst.close()

    def __init__(self, app, x, y):
        self.app = app
        self.S = S = app.dpi
        self.F = {k: TextFont(n, px, S, bold=(n == "seguisb.ttf")) for k, (n, px) in {
            "label": ("segoeui.ttf", 13), "value": ("segoeui.ttf", 12),
            "head": ("seguisb.ttf", 11), "title": ("seguisb.ttf", 15),
            "seg": ("segoeui.ttf", 12), "segb": ("seguisb.ttf", 12),
            "tiny": ("segoeui.ttf", 10)}.items()}
        self._ifonts = {}
        self.pages = ["main"]
        self.scroll = 0.0
        self.hover = None
        self.hits = []
        self.devices = None      # None: yükleniyor, list, ya da "err"
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
        """Odak alınamazsa da çalışsın: panelin dışına tıklanınca kapan."""
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

    # -- yardımcılar
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
        # odak gerçekten başka bir pencereye geçtiyse kapan
        self.top.after(60, lambda: (None if self._closing or
                                    user32.GetForegroundWindow() == self.hwnd else self.close()))

    def _back_or_close(self):
        if len(self.pages) > 1:
            self._go_back()
        else:
            self.close()

    def _open_page(self, page):
        self.pages.append(page)
        self.scroll = 0.0
        self.hover = None
        if page == "devices":
            self._load_devices()
        self.render()

    def _go_back(self):
        if len(self.pages) > 1:
            self.pages.pop()
            self.scroll = 0.0
            self.hover = None
            self.render()

    def _then_close(self, fn):
        """İşlemi yap ve menüyü kapat (pencere açan / tek seferlik işlemler)."""
        def go():
            self.close()
            fn()
        return go

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
                log("cihazlar:", repr(e))
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

    # -- sayfalar (her çizimde güncel durumdan yeniden kurulur)
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
                     self._then_close(lambda: app._action("art")))]},
                {"t": "sep"},
                {"t": "nav", "id": "p_look", "icon": MI["look"], "label": T("mn_appearance"),
                 "value": th.get(c["theme"], ""), "page": "look"},
                {"t": "nav", "id": "p_alpha", "icon": MI["alpha"], "label": T("mn_transparency"),
                 "value": f'%{c["opacity"]}', "page": "alpha"},
                {"t": "nav", "id": "p_widget", "icon": MI["widget"], "label": T("mn_widget"),
                 "value": "", "page": "widget"},
                {"t": "sep"},
                {"t": "nav", "id": "p_notif", "icon": MI["notif"], "label": T("mn_notif"),
                 "value": "", "page": "notif"},
                {"t": "nav", "id": "p_sleep", "icon": MI["sleep"], "label": T("m_sleep"),
                 "value": T("sleep_left", m=mins) if mins else T("off"), "page": "sleep"},
                {"t": "nav", "id": "p_alarm", "icon": MI["alarm"], "label": T("m_alarm"),
                 "value": al.get("time", "08:00") if al.get("on") else T("off"), "page": "alarm"},
                {"t": "sep"},
                {"t": "nav", "id": "p_dev", "icon": MI["device"], "label": T("m_devices"),
                 "value": "", "page": "devices"},
                {"t": "nav", "id": "p_acc", "icon": MI["account"], "label": T("mn_account"),
                 "value": T("acc_on") if app.web.connected else T("acc_off"), "page": "account"},
                {"t": "nav", "id": "p_lang", "icon": MI["lang"], "label": T("mn_language"),
                 "value": "Türkçe" if LANG == "tr" else "English", "page": "lang"},
                {"t": "sep"},
                {"t": "action", "id": "a_close", "icon": MI["close"], "label": T("m_close"),
                 "do": self._then_close(app.root.destroy)},
            ]
        title = {"look": T("mn_appearance"), "alpha": T("mn_transparency"),
                 "widget": T("mn_widget"), "notif": T("mn_notif"), "sleep": T("m_sleep"),
                 "alarm": T("m_alarm"), "devices": T("m_devices"), "account": T("mn_account"),
                 "lang": T("mn_language"), "share": T("mn_share")}[page]
        out = [{"t": "title", "label": title}]
        if page == "look":
            out += [
                {"t": "seg", "id": "s_theme", "label": T("m_theme"), "value": c["theme"],
                 "options": [("system", T("th_system")), ("dark", T("th_dark")),
                             ("light", T("th_light"))], "set": app._set_theme},
                {"t": "swatch", "id": "s_acc", "label": T("m_accent"), "value": c["accent"],
                 "options": [("auto", None)] + list(ACCENTS), "set": app._set_accent},
                {"t": "seg", "id": "s_bg", "label": T("m_bg"), "value": c["bg"],
                 "options": [("solid", T("bg_solid")), ("glass", T("bg_glass").split(" (")[0]),
                             ("clear", T("bg_clear"))], "set": lambda v: app._set("bg", v)},
                {"t": "seg", "id": "s_size", "label": T("m_size"),
                 "value": min(SIZES, key=lambda kv: abs(kv[1] - float(c["scale"])))[1],
                 "options": [(v, T(k)) for k, v in SIZES], "set": app._set_scale},
                {"t": "info", "label": T("size_hint")},
            ]
        elif page == "alpha":
            out += [
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
                {"t": "toggle", "id": "t_osd", "label": T("m_osd"), "on": c["osd"],
                 "set": lambda v: app._set("osd", v)},
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

    # -- çizim
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
        on, var, pri = P["on_surface"] + (255,), P["on_surface_variant"] + (255,), \
            P["primary"] + (255,)
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
                    rr(content, X(cx - 22), X(y + 8), X(cx + 22), X(y + 52), X(16), col)
                    d.text((X(cx), X(y + 30)), glyph, font=self.icon(17),
                           fill=(P["on_primary_container"] if active else P["on_surface"])
                           + (255,), anchor="mm")
                    d.text((X(cx), X(y + 66)), lbl, font=F["tiny"], fill=var, anchor="mm")
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
                d.text((X(18), X(y + 18)), it["label"], font=F["head"], fill=pri, anchor="lm")
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
                elif t == "toggle":
                    self._switch(content, PW - 54, y + h / 2, it["on"], dis, P)
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
                rr(content, X(x0), X(sy0), X(x1), X(sy1), X(16), P["outline"], alpha=110)
                rr(content, X(x0 + 1), X(sy0 + 1), X(x1 - 1), X(sy1 - 1), X(15), P["surface_high"])
                sw = (x1 - x0) / len(opts)
                for i, (val, lbl) in enumerate(opts):
                    sx0 = x0 + i * sw
                    sel = val == it["value"]
                    sid = f'{iid}:{i}'
                    if sel:
                        rr(content, X(sx0 + 3), X(sy0 + 3), X(sx0 + sw - 3), X(sy1 - 3), X(13),
                           P["secondary_container"])
                    elif hov == sid and not dis:
                        rr(content, X(sx0 + 3), X(sy0 + 3), X(sx0 + sw - 3), X(sy1 - 3), X(13),
                           P["on_surface"], alpha=20)
                    col = (P["on_secondary_container"] if sel else P["on_surface"]) \
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
                    if seed is None:  # albüm kapağından
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

        # pencereye yerleştir: gölge + kart + içerik
        sk = (vh, P["surface_high"])
        if getattr(self, "_bg_key", None) != sk:  # gölge + kart zemini (yükseklik değişince)
            from PIL import ImageFilter
            bg = Image.new("RGBA", (self.WM, self.HM), (0, 0, 0, 0))
            shadow = Image.new("RGBA", (round(X(PW + 2 * M)), round(X(vh + 2 * M))),
                               (0, 0, 0, 0))
            rr(shadow, X(M), X(M + 3), X(M + PW), X(M + vh + 3), X(18), (0, 0, 0), alpha=110)
            bg.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(X(7))))
            rr(bg, X(M), X(M), X(M + PW), X(M + vh), X(18), P["surface_high"])
            self._bg, self._bg_key = bg, sk
        canvas = self._bg.copy()
        part = content.crop((0, round(X(self.scroll)), content.width,
                             round(X(self.scroll + vh))))
        mask = _rr_mask(part.width, part.height, 0, 0, part.width, part.height, X(18))
        a = part.getchannel("A")
        part.putalpha(Image.composite(a, Image.new("L", a.size, 0), mask))
        canvas.alpha_composite(part, (round(X(M)), round(X(M))))
        if total > vh:  # kaydırma çubuğu
            th_ = vh * vh / total
            ty = self.scroll / (total - vh) * (vh - th_)
            rr(canvas, X(M + PW - 6), X(M + ty + 4), X(M + PW - 3), X(M + ty + th_ - 4),
               X(1.5), P["on_surface"], alpha=70)
        self.hits = hits
        self.vh = vh
        self.surface.full(canvas)
        self._place(vh, place)

    def _switch(self, img, x, cy, on, dis, P):
        X = lambda v: v * self.S  # noqa: E731
        w, h = 40, 24
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
        """İlk açılışta imleç konumuna; sayfa değişince ekrandan taşmayacak şekilde."""
        S, M = self.S, self.M
        ax, ay = self.anchor
        wa = self._work_area(ax, ay)
        w, h = round((self.PW + 2 * M) * S), round((vh + 2 * M) * S)
        if first:
            x = ax - round(M * S) + 2
            y = ay - round(M * S) + 2
            if x + w > wa.right:
                x = ax - w + round(M * S) - 2
            self.up = y + h > wa.bottom  # aşağı sığmıyorsa yukarı aç
            if self.up:
                y = ay - h + round(M * S) - 2
            self.pos = [x, y]
            self.bottom = y + h
        x, y = self.pos
        if self.up:  # yukarı açıldıysa alt kenar sabit: kısa sayfalar imlecin yanında kalır
            y = self.bottom - h
        x = max(wa.left - round(M * S), min(x, wa.right - w + round(M * S)))
        y = max(wa.top - round(M * S), min(y, wa.bottom - h + round(M * S)))
        self.pos = [x, y]
        move_window(self.hwnd, x, y)

    # -- olaylar
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
                self.close()  # gölgeye / dışarıya tıklama
            return
        h[5]()
        if not self._closing:
            self.app.base_key = None
            self.render()

    def _on_wheel(self, e):
        self.scroll -= e.delta / 120 * 40
        self.render()


def copy_image_to_clipboard(img):
    """PIL görüntüsünü Windows panosuna (CF_DIB) koyar."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "BMP")
    data = buf.getvalue()[14:]  # BITMAPFILEHEADER'sız DIB
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


class _Dialog:
    """Materyal renklerinde basit tk penceresi tabanı (tek kopya)."""
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
        light = app.light
        self.bg = "#F7F5FA" if light else "#1C1B1F"
        self.fg = "#1C1B1F" if light else "#E6E1E5"
        self.dim = "#49454F" if light else "#CAC4D0"
        self.acc = "#%02x%02x%02x" % app.pal["primary"]
        self.on_acc = "#%02x%02x%02x" % app.pal["on_primary"]
        self.field = "#E7E0EC" if light else "#2B2930"
        t = self.top = tk.Toplevel(app.root)
        _Dialog._open[type(self).__name__] = t
        t.title(title)
        t.configure(bg=self.bg, padx=22, pady=18)
        t.resizable(False, False)
        t.attributes("-topmost", True)
        t.protocol("WM_DELETE_WINDOW", self.close)
        t.bind("<Escape>", lambda _e: self.close())
        return True

    def btn(self, parent, text, cmd, primary=False, small=False):
        bg, fg = (self.acc, self.on_acc) if primary else (self.field, self.fg)
        return tk.Button(parent, text=text, bg=bg, fg=fg, activebackground=bg,
                         activeforeground=fg, relief="flat", bd=0,
                         padx=10 if small else 16, pady=3 if small else 7, cursor="hand2",
                         command=cmd, font=("Segoe UI Semibold" if primary else "Segoe UI",
                                            9 if small else 10))

    def close(self):
        _Dialog._open.pop(type(self).__name__, None)
        try:
            self.top.destroy()
        except tk.TclError:
            pass


class SearchDialog(_Dialog):
    def __init__(self, app):
        if not self._setup(app, T("s_title")):
            return
        t, web = self.top, app.web
        tk.Label(t, text=T("s_title"), bg=self.bg, fg=self.fg,
                 font=("Segoe UI Semibold", 15)).pack(anchor="w")
        tk.Label(t, text=T("s_hint"), bg=self.bg, fg=self.dim,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 8))
        self.entry = tk.Entry(t, bg=self.field, fg=self.fg, insertbackground=self.fg,
                              relief="flat", font=("Segoe UI", 12), width=42)
        self.entry.pack(fill="x", ipady=7, ipadx=8)
        self.entry.bind("<Return>", lambda _e: self._search())
        self.results = tk.Frame(t, bg=self.bg)
        self.results.pack(fill="x", pady=(10, 0))
        self.status = tk.Label(t, text="", bg=self.bg, fg=self.dim, font=("Segoe UI", 9),
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
                log("arama:", repr(res["err"]))
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
                     font=("Segoe UI Semibold", 10)).pack(anchor="w")
            tk.Label(txt, text=f'{it["artist"][:40]}  ·  {fmt_time(it["dur"])}', bg=self.bg,
                     fg=self.dim, anchor="w", font=("Segoe UI", 9)).pack(anchor="w")
            self.btn(row, "＋ " + T("s_queue"), lambda u=it["uri"]: self._do("queue", u),
                     small=True).pack(side="right", padx=(6, 0))
            self.btn(row, "▶ " + T("s_play"), lambda u=it["uri"]: self._do("play", u),
                     primary=True, small=True).pack(side="right")

    def _do(self, what, uri):
        web = self.app.web
        if not web.has_control:
            if what == "play":  # izin yoksa: Spotify uygulamasında aç
                try:
                    os.startfile(uri)
                except OSError:
                    pass
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
            elif code in (403, 404):  # Premium yok ya da etkin cihaz yok
                try:
                    os.startfile(uri)
                except OSError:
                    pass
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
                 font=("Segoe UI Semibold", 15)).pack(anchor="w")
        tk.Label(t, text=T("al_text"), bg=self.bg, fg=self.dim, justify="left",
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 12))
        row = tk.Frame(t, bg=self.bg)
        row.pack(anchor="w")
        self.h = tk.Spinbox(row, from_=0, to=23, width=3, format="%02.0f", wrap=True,
                            font=("Segoe UI Semibold", 22), justify="center", relief="flat",
                            bg=self.field, fg=self.fg, buttonbackground=self.field,
                            insertbackground=self.fg)
        self.m = tk.Spinbox(row, from_=0, to=59, width=3, format="%02.0f", wrap=True,
                            font=("Segoe UI Semibold", 22), justify="center", relief="flat",
                            bg=self.field, fg=self.fg, buttonbackground=self.field,
                            insertbackground=self.fg)
        self.h.delete(0, "end")
        self.h.insert(0, hh)
        self.m.delete(0, "end")
        self.m.insert(0, mm)
        self.h.pack(side="left")
        tk.Label(row, text=":", bg=self.bg, fg=self.fg,
                 font=("Segoe UI Semibold", 22)).pack(side="left", padx=4)
        self.m.pack(side="left")
        self.on = tk.BooleanVar(value=True)
        tk.Checkbutton(t, text=T("al_on"), variable=self.on, bg=self.bg, fg=self.fg,
                       selectcolor=self.field, activebackground=self.bg,
                       activeforeground=self.fg, font=("Segoe UI", 10)).pack(anchor="w",
                                                                           pady=(12, 0))
        bottom = tk.Frame(t, bg=self.bg)
        bottom.pack(fill="x", pady=(14, 0))
        self.btn(bottom, T("al_save"), self._save, primary=True).pack(side="right")
        self.btn(bottom, T("dlg_close"), self.close).pack(side="right", padx=8)
        t.focus_force()

    def _save(self):
        try:
            h = max(0, min(23, int(self.h.get())))
            m = max(0, min(59, int(self.m.get())))
        except ValueError:
            return
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
        if disp.height > 720:  # ekrana sığsın
            sc = 720 / disp.height
            disp = disp.resize((round(disp.width * sc), 720), Image.Resampling.LANCZOS)
        self.ph = ImageTk.PhotoImage(disp)
        tk.Label(self.top, image=self.ph, bg=self.bg).pack()
        bottom = tk.Frame(self.top, bg=self.bg)
        bottom.pack(fill="x", pady=(12, 0))
        self.status = tk.Label(bottom, text="", bg=self.bg, fg=self.dim, font=("Segoe UI", 9))
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


if __name__ == "__main__":
    # tek kopya: widget zaten açıksa ikincisini başlatma
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\SpotifyMaterialWidget")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)
    try:
        App().run()
    except Exception:
        log(traceback.format_exc())
        raise
