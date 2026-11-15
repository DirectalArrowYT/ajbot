"""Check that this machine can run the bot, and say what is missing.

    python doctor.py

Covers the four things that have to line up: Python packages, the virtual
controller (ViGEmBus on Windows, /dev/uinput on Linux), the game's ASI setup,
and the trained policy.
"""
import ctypes
import importlib
import os
import sys
from pathlib import Path

LINUX = sys.platform.startswith("linux")

GAME_HINTS = [
    r"D:\SteamLibrary\steamapps\common\MY HERO ACADEMIA All's Justice",
    r"C:\Program Files (x86)\Steam\steamapps\common\MY HERO ACADEMIA All's Justice",
    r"C:\SteamLibrary\steamapps\common\MY HERO ACADEMIA All's Justice",
]
if LINUX:
    GAME_HINTS = [p for p in [__import__("ajbot.local", fromlist=["get"]).get("game.dir")] if p] + [str(Path(d).expanduser() / "steamapps/common/MY HERO ACADEMIA All's Justice")
                  for d in ("~/.local/share/Steam", "~/.steam/steam")]

ok = True


def check(label, good, detail=""):
    global ok
    ok &= good
    print(f"  [{'OK ' if good else 'MISS'}] {label}" + (f"  - {detail}" if detail else ""))


def find_game():
    for h in GAME_HINTS:
        if Path(h).exists():
            return Path(h)
    return None


print("python")
check(f"python {sys.version.split()[0]}", sys.version_info >= (3, 9))

print("\npackages")
for mod, why in [("numpy", "arrays"), ("sklearn", "the models"),
                 ("evdev", "the virtual controller") if LINUX else
                 ("vgamepad", "the virtual controller")]:
    try:
        m = importlib.import_module(mod)
        check(f"{mod} ({why})", True, getattr(m, "__version__", ""))
    except ImportError:
        check(f"{mod} ({why})", False, "pip install -r requirements.txt")

print("\nvirtual controller driver")
if LINUX:
    check("/dev/uinput writable", os.access("/dev/uinput", os.W_OK),
          "needs the uaccess ACL (Steam's udev rules) or the input group")
else:
  try:
      ctypes.windll.LoadLibrary("ViGEmClient.dll")
      check("ViGEmBus / ViGEmClient", True)
  except OSError:
      # vgamepad bundles the client dll, so importing it is the real test.
      try:
          import vgamepad  # noqa: F401
          pad = vgamepad.VX360Gamepad()
          pad.reset()
          check("ViGEmBus driver", True, "virtual pad created")
      except Exception as e:
          check("ViGEmBus driver", False,
                f"install ViGEmBus, then retry ({type(e).__name__})")

print("\ngame + hook")
game = find_game()
check("game folder found", game is not None,
      str(game) if game else "edit GAME_HINTS in this file")
if game:
    win64 = game / "HeroGame" / "Binaries" / "Win64"
    check("AJGAME.exe", (win64 / "AJGAME.exe").exists())
    # Under Proton nothing loads dinput8, so there the loader goes in as
    # dsound.dll, which AJGAME.exe imports at startup.
    loader = "dsound.dll" if LINUX else "dinput8.dll"
    check(f"ASI loader ({loader})", (win64 / loader).exists(),
          f"Ultimate ASI Loader, renamed to {loader}")
    # UE4SS (dwmapi.dll proxy) runs the CursorExport mod, which feeds the
    # character-select cursor to ajhook; without it --pick falls back to
    # blind press counting. Keep HookLoadMap = 0 in UE4SS-settings.ini.
    check("UE4SS + CursorExport mod",
          (win64 / "dwmapi.dll").exists()
          and (win64 / "ue4ss/Mods/CursorExport/Scripts/main.lua").exists(),
          "dwmapi.dll + ue4ss/ (see the game-files MANIFEST.txt on your training server)")
    check("ajhook.asi", (win64 / "ajhook.asi").exists(),
          "copy ajhook/target/release/ajhook.dll here, renamed to ajhook.asi")
    log = win64 / "ajhook.log"
    if log.exists():
        tail = [l.strip() for l in log.read_text(errors="replace").splitlines() if l.strip()]
        check("hook has run", any("hooked" in l for l in tail[-6:]),
              tail[-1] if tail else "")
    if LINUX:
        print("  (Steam launch options need: WINEDLLOVERRIDES=\"dsound,dwmapi=n,b\" %command%)")
        try:
            from ajbot.hook import Hook
            with Hook(timeout=0.5) as h:
                check("hook reachable on TCP", True, f"xinput slots {sorted(h.xinput_slots())}")
        except Exception as e:
            print(f"  [ -- ] hook not reachable (fine unless the game is running): {e}")

print("\npolicy")
pol = Path("models/policy.pkl")
check("models/policy.pkl", pol.exists(),
      f"{pol.stat().st_size // 1024 // 1024} MB" if pol.exists()
      else "python train.py --save models")

print("\nrecordings (only needed to retrain)")
runs = len(list(Path("runs").glob("*.jsonl"))) if Path("runs").exists() else 0
print(f"  runs/: {runs} match(es)")

print("\n" + ("all good" if ok else "something above needs fixing"))
sys.exit(0 if ok else 1)
