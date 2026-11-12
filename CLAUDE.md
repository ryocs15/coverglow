# Spotify Masaüstü Widget'ı

Windows için Python ile yazılmış, Material You tasarımlı Spotify masaüstü widget'ı.
Kullanıcıyla Türkçe konuş.

## Proje
- Tüm uygulama tek dosyada: `spotify_widget.py` (~5000 satır).
- Python 3.15, Windows 11. Bağımlılıklar: Pillow, winrt (Windows.Media.Control vb.), pycaw/comtypes,
  fontTools, PyInstaller.
- Git deposu: github.com/ryocs15/spotify-widget (private).
  git: `%LOCALAPPDATA%\Programs\MinGit\cmd\git.exe`, gh: `%LOCALAPPDATA%\Programs\GitHubCLI\bin\gh.exe`

## Mimari
- **Çizim:** tkinter sadece olaylar ve diyaloglar için. Widget Pillow ile RGBA görüntü olarak çizilip
  `UpdateLayeredWindowIndirect` ile katmanlı pencereye basılır (`LayeredSurface`: `full()` / `region()`).
- **Koordinatlar** mantıksal px; fiziksel = değer × `self.S` (dpi × ölçek). Kodda `X = lambda v: v * S`
  kalıbı var.
- **Yardımcılar:** `rr()` yuvarlak dikdörtgen, `rounded_image()`, `Drawer` + `TextFont` (Japonca/Korece/emoji
  yedek yazı tipleri), `fit_text()`, `wrap_text()`, `fmt_dur()`, `fmt_time()`.
- **Önbellek:** ana görüntü `base_key` demetiyle önbelleklenir (`_render_all`). Yeni görsel durum eklersen
  `base_key`'e ekle. İlerleme şeridi ve mini moddaki kayan başlık `region()` ile kısmi güncellenir.
- **Döngü:** `App._tick` (animasyonda 40 ms, değilse 200 ms). Çizim ucuz kalmalı; pahalı işleri önbelleğe al.
- **Veri kaynakları:** `MediaWorker` (GSMTC, asyncio iş parçacığı: çalan şarkı, kapak, komutlar),
  `SpotifyWeb` (PKCE OAuth, sıra, beğeni, arama, cihazlar), `AppVolume` (pycaw: Spotify sesi, ses
  ölçerleri), `Lyrics` (lrclib.net), `Hotkeys` (RegisterHotKey).
- **Arayüz parçaları:** `_render_base` / `_render_mini` / `_render_queue` (sekmeler: queue, recent, lyrics,
  stats), `_render_stats`, `Toast` (şarkı bildirimi + `show_message` ile kısa gösterge), `MenuPanel` (sağ tık
  menüsü: `items()` içindeki sözlüklerle tanımlanır; türler: quick, nav, toggle, seg, swatch, radio, action,
  info, header, sep, title), `_Dialog` tabanlı pencereler (`SearchDialog`, `AlarmDialog`, `RecapWindow`),
  `ConnectDialog`.
- **Renkler:** albüm kapağından Material You paleti (`self.pal`). Anahtarlar: primary, on_primary,
  primary_container, on_primary_container, secondary_container, on_secondary_container, surface,
  surface_high, surface_highest, on_surface, on_surface_variant, outline. Sabit renk kullanma.
- **Dil:** `STRINGS["tr"]` ve `STRINGS["en"]`. Her yeni metni iki dile de ekle, `T("anahtar")` ile kullan.
- **Ayarlar:** `config.json` (`App.__init__` içinde `cfg.setdefault` ile varsayılan ver). Exe çalışırken
  dosyalar `dist\` klasöründedir: config.json, stats.json (canlı istatistik), stats_import.json (içe
  aktarılan Spotify geçmişi), history.json, last_track.json, widget.log.

## Kurallar
- `config.json`'daki refresh_token gizli: asla yazdırma, loglama, commit etme. Kişisel veri dosyaları
  `.gitignore`'da kalmalı.
- Kullanıcının müziğini çalan/değiştiren komutları (play, queue, transfer, seek) test için kendiliğinden
  gönderme; gerekiyorsa önce sor.
- Spotify Web API'yi script'ten çağıracaksan önce çalışan exe'yi kapat (`Stop-Process -Name SpotifyWidget`),
  sonra yeniden başlat: token yenilenince iki kopya çakışabilir.
- Spotify notları: eski `/me/tracks` uç noktası 403 veriyor, `/me/library` kullanılıyor. Çalma kontrolü
  Premium ister. Yeni bir izin (scope) eklersen kullanıcının hesabı yeniden bağlaması gerekir; bunu söyle.
- Mevcut özellikleri bozma: mini mod, ekranın altındayken yukarı büyüme, tam ekranda bildirim, cam arka
  plan, Discord'da kısma, istatistik önbellekleri, menünün yukarı açılınca alt kenarı sabit tutması.

## Çalışma şekli
1. İlgili kodu oku, sonra değiştir. Var olan yardımcıları ve kalıpları kullan.
2. Görsel değişiklikleri başsız test et: script'te `App()` oluştur, `a.cfg.save` ve `a.media.command`'ı
   etkisizleştir, durumu ayarla, `a.base_key = None; a._render_all()` çağır, `a.base`'i PNG olarak kaydedip
   incele. Her iki dili ve mini/normal modu kontrol et.
3. Derle: `cd D:\Calude\spotify_widget; Stop-Process -Name SpotifyWidget;
   python -m PyInstaller --noconfirm SpotifyWidget.spec` → `dist\SpotifyWidget.exe`'yi başlat,
   `dist\widget.log`'da yeni hata olmadığını kontrol et.
4. Commit at ve push et (Türkçe, kısa commit mesajı).
5. Sonunda neyi değiştirdiğini, neyi test ettiğini ve neyi gerçek kullanımda deneyemediğini açıkça yaz.
