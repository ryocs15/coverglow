"""Start the App the way the exe does (FROZEN=True) from a temp copy, without network or touching
your real settings: catches exe-only startup bugs before a release. Run: python tools/smoke_exe.py
The startup entry is written to a throwaway registry key, never to the real Run key."""
import json, os, shutil, sys, tempfile, winreg
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SMOKE_KEY = r"Software\CoverglowSmoke"
TMP = tempfile.mkdtemp()
try:
    shutil.copy(os.path.join(ROOT, "coverglow.py"), TMP)
    shutil.copytree(os.path.join(ROOT, "fonts"), os.path.join(TMP, "fonts"))
    with open(os.path.join(TMP, "config.json"), "w") as f:
        json.dump({"lang": "en", "startup": True, "memory": False}, f)
    sys.path.insert(0, TMP)
    import coverglow as s
    s.FROZEN = True                  # exe code paths (APP_DIR stays the temp copy: set at import)
    s.App.RUN_KEY = SMOKE_KEY        # "Start with Windows" runs, but into the throwaway key
    winreg.CreateKey(winreg.HKEY_CURRENT_USER, SMOKE_KEY).Close()
    calls = []
    s.latest_release = lambda: calls.append("check") or None   # no network
    a = s.App()
    a.cfg.save = lambda: None
    a.root.after(1500, a.root.quit)
    a.root.mainloop()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SMOKE_KEY) as k:
        entry = winreg.QueryValueEx(k, "Coverglow")[0]
    assert calls == ["check"] and entry, (calls, entry)
    print("started ok: update check ran, startup entry written")
    a.root.destroy()
finally:
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, SMOKE_KEY)
    except OSError:
        pass
    shutil.rmtree(TMP, ignore_errors=True)
