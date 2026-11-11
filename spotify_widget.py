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
from datetime import datetime, timezone
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

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = ("user-read-playback-state user-read-currently-playing "
          "user-library-read user-library-modify")


# ------------------------------------------------------------------ dil
STRINGS = {
    "tr": {
        "playing": "ŞİMDİ ÇALIYOR", "paused": "DURAKLATILDI",
        "sleep_status": " · ☾ {m} DK", "sleep_sub": "☾ {m} dk · ",
        "offline_chip": "SPOTIFY KAPALI · AÇMAK İÇİN TIKLA",
        "waiting_title": "Spotify bekleniyor", "waiting_sub": "Spotify'ı aç ve bir şarkı çal",
        "mini_waiting_sub": "Spotify'ı aç", "mini_offline": "Spotify kapalı · aç",
        "unknown": "Bilinmeyen",
        "tab_queue": "Sıradaki", "tab_recent": "Son çalınanlar",
        "connect_line1": "Sıradaki şarkıları görmek için", "connect_line2": "Spotify hesabını bağla.",
        "connect_btn": "Bağla", "loading": "Yükleniyor…", "queue_empty": "Sırada şarkı yok",
        "recent_empty": "Henüz çalınan şarkı yok",
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
        "dlg_reauth": "Beğen butonu için hesabı bir kez yeniden bağla (yeni izin gerekiyor).",
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
        "tab_queue": "Up next", "tab_recent": "Recently played",
        "connect_line1": "Connect your Spotify account", "connect_line2": "to see what's up next.",
        "connect_btn": "Connect", "loading": "Loading…", "queue_empty": "Queue is empty",
        "recent_empty": "Nothing played yet",
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
        "dlg_reauth": "Reconnect once to use the Like button (needs a new permission).",
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
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())

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

    def _api(self, path, method="GET"):
        self._ensure_token()
        req = urllib.request.Request("https://api.spotify.com/v1" + path, method=method,
                                     headers={"Authorization": "Bearer " + self.access})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read()
            return json.loads(body.decode()) if body else {}

    # -- Beğen (kütüphane)
    @property
    def has_library(self):
        return "user-library-modify" in (self.cfg.get("scopes") or "")

    def _library(self, tid, method):
        """Kütüphane uç noktası; eskisi kaldırılmışsa yeni URI tabanlı uç noktayı dener."""
        try:
            path = "/me/tracks/contains?ids=" if method == "CHECK" else "/me/tracks?ids="
            return self._api(path + tid, "GET" if method == "CHECK" else method)
        except urllib.error.HTTPError as e:
            if e.code not in (404, 410):
                raise
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
            with urllib.request.urlopen(url, timeout=10) as r:
                self.thumbs[url] = Image.open(io.BytesIO(r.read())).convert("RGB")
        except Exception:
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
                except Exception as e:  # noqa: BLE001
                    log("sıradaki listesi alınamadı:", repr(e))

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

        # durum
        self.light = self._is_light()
        self.seed = DEFAULT_SEED
        self.pal = build_scheme(self.seed, self.light)
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
        self._build_menu()
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
        self.menu.destroy()
        self._build_menu()
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
            "mtitle": font("seguisb.ttf", 15),
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
        self.v_scale.set(sc)
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
        self.v_sleep.set(minutes)
        self.base_key = None

    def _restore_sleep_volume(self):
        if self.sleep_fade is not None:
            self.volume.set(self.sleep_fade)
        self.sleep_fade = None

    def _finish_sleep(self):
        fade = self.sleep_fade
        self.sleep_end = self.sleep_fade = None
        self.v_sleep.set(0)
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
        self.v_mini.set(self.mini)
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
        if st.get("ok"):
            self.web.notify((st["title"], st["artist"]), playing)
            if st.get("title") and (st.get("art_changed") or self.last is None
                                    or self.last.get("title") != st["title"]
                                    or now - self._last_saved > 10):
                self._save_last(st, bool(st.get("art_changed")))
            if st.get("art_changed") and st.get("title"):
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
            else:
                self._action(act)

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
                self._start_palette(build_scheme(self.seed, light))

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
        if abs(self.amp - target_amp) > 0.05:
            self.amp += (target_amp - self.amp) * min(1.0, dt * 6)
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
        self._start_palette(build_scheme(self.seed, self.light))

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
               int(time.time() // 60) if self.cfg["list_tab"] == "recent" else 0)
        full = False
        if key != self.base_key:
            self.base_key = key
            if self.mini:
                self.base, self.hits = self._render_mini()
            else:
                self.marq = None
                self.base, self.hits = self._render_base(connected, queue, qerr)
            self.marq_key = None
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

        rr(img, 0, 0, X(W), X(MINI_H), X(MINI_H / 2), P["surface"])
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
        rr(img, 0, 0, X(W), X(MAIN_H), X(28), P["surface"])

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
        if tab == "recent":
            items = [(h["title"], h["artist"], self._hist_thumb(h), time_ago(h.get("t", 0)))
                     for h in self._recent_items(rows)]
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
        elif need_connect:
            body = 86
        elif items:
            body = len(items) * Q_ROW + Q_PAD
        else:
            body = 64
        rr(img, 0, X(y0), X(W), X(y0 + Q_HEAD + body), X(28), P["surface"])
        self.content_h = y0 + Q_HEAD + body

        # başlık: sekmeler + daralt
        hy = y0 + Q_HEAD / 2
        x = 12
        for key, lbl in (("queue", T("tab_queue")), ("recent", T("tab_recent"))):
            w = F["btn"].getlength(lbl) / S + 26
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
            if th is not None:
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
        elif a in ("tab_queue", "tab_recent"):
            self.cfg["list_tab"] = a[4:]
            self.cfg["queue_collapsed"] = False
            self.cfg.save()
        elif a == "connect":
            ConnectDialog(self)
        elif a.startswith("h"):  # son çalınan: Spotify'da ara
            items = self._recent_items(int(self.cfg["queue_rows"]))
            i = int(a[1:])
            if i < len(items):
                q = f'{items[i]["title"]} {items[i]["artist"]}'.strip()
                try:
                    os.startfile("spotify:search:" + urllib.parse.quote(q))
                except OSError:
                    webbrowser.open("https://open.spotify.com/search/" + urllib.parse.quote(q))
        elif a.startswith("q"):
            n = int(a[1:]) + 1
            self.media.command("skip_n", n)
            self.web.refresh_now()

    # ---------------- menü
    def _build_menu(self):
        c = self.cfg
        self.v_mode = tk.StringVar(value=c["mode"])
        self.v_theme = tk.StringVar(value=c["theme"])
        self.v_opacity = tk.IntVar(value=c["opacity"])
        self.v_locked = tk.BooleanVar(value=c["locked"])
        self.v_wave = tk.BooleanVar(value=c["wave"])
        self.v_queue = tk.BooleanVar(value=c["show_queue"])
        self.v_startup = tk.BooleanVar(value=os.path.exists(STARTUP_VBS))

        m = self.menu = tk.Menu(self.root, tearoff=0)

        def sub():
            return tk.Menu(m, tearoff=0)

        # görünüm
        pos = sub()
        pos.add_radiobutton(label=T("m_desktop"), value="desktop", variable=self.v_mode,
                            command=self._set_mode)
        pos.add_radiobutton(label=T("m_top"), value="top", variable=self.v_mode,
                            command=self._set_mode)
        m.add_cascade(label=T("m_layer"), menu=pos)
        m.add_checkbutton(label=T("m_lock"), variable=self.v_locked,
                          command=lambda: self._set("locked", self.v_locked.get()))
        th = sub()
        for k, val in (("th_system", "system"), ("th_dark", "dark"), ("th_light", "light")):
            th.add_radiobutton(label=T(k), value=val, variable=self.v_theme,
                               command=self._set_theme)
        m.add_cascade(label=T("m_theme"), menu=th)
        self.v_scale = tk.DoubleVar(value=float(c["scale"]))
        sz = sub()
        for k, v in SIZES:
            sz.add_radiobutton(label=f"{T(k)} (%{round(v * 100)})", value=v,
                               variable=self.v_scale, command=lambda v=v: self._set_scale(v))
        sz.add_separator()
        sz.add_command(label=T("size_hint"), state="disabled")
        m.add_cascade(label=T("m_size"), menu=sz)
        op = sub()
        for v in (100, 90, 80, 70, 60):
            op.add_radiobutton(label=f"%{v}", value=v, variable=self.v_opacity,
                               command=self._set_opacity)
        m.add_cascade(label=T("m_opacity"), menu=op)
        self.v_hover = tk.BooleanVar(value=c["hover_opaque"])
        m.add_checkbutton(label=T("m_fade"), variable=self.v_hover,
                          command=lambda: (self._set("hover_opaque", self.v_hover.get()),
                                           self._alpha_step()))
        self.v_idle = tk.IntVar(value=c["idle_opacity"])
        idle = sub()
        for v in (80, 70, 60, 50, 40, 30, 20):
            idle.add_radiobutton(label=f"%{v}", value=v, variable=self.v_idle,
                                 command=lambda: (self._set("idle_opacity", self.v_idle.get()),
                                                  self._alpha_step()))
        m.add_cascade(label=T("m_idle"), menu=idle)
        self.v_mini = tk.BooleanVar(value=self.mini)
        m.add_checkbutton(label=T("m_mini"), variable=self.v_mini, command=self._toggle_mini)
        m.add_checkbutton(label=T("m_wave"), variable=self.v_wave,
                          command=lambda: self._set("wave", self.v_wave.get()))
        m.add_checkbutton(label=T("m_showlist"), variable=self.v_queue,
                          command=lambda: self._set("show_queue", self.v_queue.get()))
        self.v_rows = tk.IntVar(value=int(c["queue_rows"]))
        rows = sub()
        for v in (2, 3, 4, 5):
            rows.add_radiobutton(label=T("rows_n", n=v), value=v, variable=self.v_rows,
                                 command=lambda: self._set("queue_rows", self.v_rows.get()))
        m.add_cascade(label=T("m_rows"), menu=rows)
        m.add_separator()
        # özellikler
        self.v_notify = tk.BooleanVar(value=c["notify"])
        m.add_checkbutton(label=T("m_notify"), variable=self.v_notify,
                          command=lambda: self._set("notify", self.v_notify.get()))
        if not hasattr(self, "v_sleep"):
            self.v_sleep = tk.IntVar(value=0)
        sl = sub()
        for v in (0, 15, 30, 45, 60, 90):
            sl.add_radiobutton(label=T("sleep_off") if v == 0 else T("sleep_n", n=v), value=v,
                               variable=self.v_sleep, command=lambda v=v: self._set_sleep(v))
        m.add_cascade(label=T("m_sleep"), menu=sl)
        self.v_hotkeys = tk.BooleanVar(value=c["hotkeys"])
        hk = sub()
        hk.add_checkbutton(label=T("hk_enable"), variable=self.v_hotkeys,
                           command=self._set_hotkeys)
        hk.add_separator()
        for *_x, keys, key in HOTKEYS:
            hk.add_command(label=f"{keys}   {T(key)}", state="disabled")
        m.add_cascade(label=T("m_hotkeys"), menu=hk)
        self.v_lang = tk.StringVar(value=LANG)
        lg = sub()
        lg.add_radiobutton(label="Türkçe", value="tr", variable=self.v_lang,
                           command=lambda: self._set_lang("tr"))
        lg.add_radiobutton(label="English", value="en", variable=self.v_lang,
                           command=lambda: self._set_lang("en"))
        m.add_cascade(label="Dil / Language", menu=lg)
        m.add_separator()
        # Spotify
        m.add_command(label=T("m_connect"), command=lambda: ConnectDialog(self))
        m.add_command(label=T("m_disconnect"), command=self.web.disconnect)
        self._disc_index = m.index("end")
        m.add_command(label=T("m_open"), command=lambda: self._action("art"))
        m.add_checkbutton(label=T("m_startup"), variable=self.v_startup,
                          command=self._set_startup)
        m.add_separator()
        m.add_command(label=T("m_close"), command=self.root.destroy)

    def _set_hotkeys(self):
        self._set("hotkeys", self.v_hotkeys.get())
        if self.v_hotkeys.get():
            self.hotkeys.start()
        else:
            self.hotkeys.stop()

    def _on_menu(self, e):
        try:
            self.menu.entryconfig(self._disc_index,
                                  state="normal" if self.web.connected else "disabled")
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _set(self, k, v):
        self.cfg[k] = v
        self.cfg.save()
        self.base_key = None

    def _set_mode(self):
        self._set("mode", self.v_mode.get())
        self._apply_mode()

    def _set_theme(self):
        self._set("theme", self.v_theme.get())
        self.light = self._is_light()
        self._start_palette(build_scheme(self.seed, self.light))

    def _set_opacity(self):
        self._set("opacity", self.v_opacity.get())
        self.surface.alpha = self._target_alpha()
        self.base_key = None
        self._render_all()

    def _set_startup(self):
        try:
            if self.v_startup.get():
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
        self.v_startup.set(os.path.exists(STARTUP_VBS))

    def run(self):
        self.root.mainloop()


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
        S = self.app.S
        self.fs = fs is not None
        ex = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        ex = (ex | WS_EX_TRANSPARENT) if self.fs else (ex & ~WS_EX_TRANSPARENT)
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, ex)
        self.surface.full(self._render(st))
        m = round(16 * S)
        if fs is not None:
            self._pos = (fs.right - round(self.TW * S) - m, fs.top + m + round(8 * S))
        else:
            rc = wintypes.RECT()
            user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rc), 0)  # SPI_GETWORKAREA
            self._pos = (rc.right - round(self.TW * S) - m,
                         rc.bottom - round(self.TH * S) - m)
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
            self._hold_until = time.monotonic() + self.HOLD_MS / 1000
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
