"""Start the App the way the exe does (FROZEN=True) from a temp copy, without network or touching
your real settings: catches exe-only startup bugs before a release. Run: python tools/smoke_exe.py"""
import json, os, shutil, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
shutil.copy(os.path.join(ROOT, "coverglow.py"), TMP)
shutil.copytree(os.path.join(ROOT, "fonts"), os.path.join(TMP, "fonts"))
json.dump({"lang": "en", "startup": False, "memory": False}, open(os.path.join(TMP, "config.json"), "w"))
sys.path.insert(0, TMP)
import coverglow as s
s.FROZEN = True                      # exe code paths (APP_DIR stays the temp copy: set at import)
calls = []
s.latest_release = lambda: calls.append("check") or None   # no network
a = s.App()
a.cfg.save = lambda: None
a.root.after(1500, a.root.quit)
a.root.mainloop()
print("started ok, update checks:", calls)
a.root.destroy()
shutil.rmtree(TMP, ignore_errors=True)
