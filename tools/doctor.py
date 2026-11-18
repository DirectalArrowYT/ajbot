"""Check that this machine can run the bot, say what is missing, and fix it.

    python tools/doctor.py                  # readable report
    python tools/doctor.py --json           # the same, for the GUI's Setup page
    python tools/doctor.py --fix packages   # run one fix (ids are in the report)
    python tools/doctor.py --fix game --path "D:/SteamLibrary/.../MY HERO ACADEMIA All's Justice"

Checks: Python packages, the VC++ runtime, the virtual controller driver
(ViGEmBus on Windows, /dev/uinput on Linux), the game folder, the game-side
files (ASI loader, UE4SS with the CursorExport mod and HookLoadMap = 0,
ajhook.asi) and the trained policy.

The game-side files come from the latest GitHub release of RELEASE_REPO
(asset ajbot-gamefiles-*.zip, built by tools/make_release.py); the mods in
this repo's ue4ss_mods/ are copied over that.
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root on sys.path and as cwd)
import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

from ajbot import local

LINUX = sys.platform.startswith("linux")
RELEASE_REPO = "DirectalArrowYT/ajbot"
RELEASES_URL = f"https://github.com/{RELEASE_REPO}/releases"
VIGEM_URL = "https://github.com/nefarius/ViGEmBus/releases/latest"
VCREDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
GAME_NAME = "MY HERO ACADEMIA All's Justice"
GAMEFILES_STAMP = "ajbot_gamefiles.json"
# Mods the bot needs enabled in ue4ss/Mods/mods.txt (shipped in ue4ss_mods/).
OUR_MODS = ("CursorExport", "AJProbe")
PACKAGES = [("numpy", "arrays"), ("sklearn", "the models"), ("xgboost", "the models"),
            ("evdev", "the virtual controller") if LINUX else
            ("vgamepad", "the virtual controller")]


# ------------------------------------------------------------------ game dir

def steam_libraries():
    """Every Steam library folder on this machine (Windows: registry +
    libraryfolders.vdf; Linux: the usual Steam roots)."""
    roots = []
    if LINUX:
        roots = [Path(d).expanduser() for d in ("~/.local/share/Steam", "~/.steam/steam")]
    else:
        try:
            import winreg
            for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        for name in ("SteamPath", "InstallPath"):
                            try:
                                roots.append(Path(winreg.QueryValueEx(k, name)[0]))
                            except OSError:
                                pass
                except OSError:
                    pass
        except ImportError:
            pass
        roots.append(Path(r"C:\Program Files (x86)\Steam"))
    libs = []
    for r in roots:
        libs.append(r)
        vdf = r / "steamapps" / "libraryfolders.vdf"
        try:
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(errors="ignore")):
                libs.append(Path(m.group(1).replace("\\\\", "\\")))
        except OSError:
            pass
    if not LINUX:      # common hand-made library folders
        for drive in "CDEFGH":
            libs.append(Path(f"{drive}:/SteamLibrary"))
    seen, out = set(), []
    for lib in libs:
        k = str(lib).lower()
        if k not in seen:
            seen.add(k)
            out.append(lib)
    return out


def find_game():
    saved = local.get("game.dir")
    if saved and Path(saved, "HeroGame").is_dir():
        return Path(saved)
    for lib in steam_libraries():
        g = lib / "steamapps" / "common" / GAME_NAME
        if (g / "HeroGame").is_dir():
            return g
    return None


def win64_of(game):
    return Path(game) / "HeroGame" / "Binaries" / "Win64"


def game_running():
    if LINUX:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq AJGAME.exe"],
                             capture_output=True, text=True, timeout=10).stdout
        return "AJGAME.exe" in out
    except Exception:
        return False


# ------------------------------------------------------------------- checks

def ini_hookloadmap(win64):
    ini = win64 / "ue4ss" / "UE4SS-settings.ini"
    try:
        m = re.search(r"^\s*HookLoadMap\s*=\s*(\d+)", ini.read_text(errors="ignore"), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


def mods_enabled(win64):
    txt = win64 / "ue4ss" / "Mods" / "mods.txt"
    try:
        lines = txt.read_text(errors="ignore").splitlines()
    except OSError:
        return {}
    out = {}
    for l in lines:
        m = re.match(r"\s*([^;:\s][^:]*?)\s*:\s*(\d)", l)
        if m:
            out[m.group(1)] = m.group(2) == "1"
    return out


def run_checks():
    checks = []

    def add(cid, group, label, ok, detail="", fix=None, fix_label=None):
        checks.append({"id": cid, "group": group, "label": label, "ok": ok,
                       "detail": detail, "fix": fix, "fix_label": fix_label})

    add("python", "Python", f"Python {sys.version.split()[0]}", sys.version_info >= (3, 10),
        "" if sys.version_info >= (3, 10) else "needs 3.10 or newer")
    missing = []
    for mod, why in PACKAGES:
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(mod)
    add("packages", "Python", "Python packages", not missing,
        f"missing: {', '.join(missing)}" if missing else "numpy, scikit-learn, xgboost, pad driver",
        "packages" if missing else None, "Install packages")

    if LINUX:
        add("pad", "Controller", "/dev/uinput writable", os.access("/dev/uinput", os.W_OK),
            "needs the uaccess ACL (Steam's udev rules) or the input group")
    else:
        sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        vc = all((sys32 / f).exists() for f in ("vcruntime140_1.dll", "msvcp140_atomic_wait.dll"))
        add("vcredist", "Controller", "Visual C++ runtime", vc,
            "" if vc else "the game mods need the 2015-2022 x64 runtime",
            None if vc else "vcredist", "Download runtime")
        pad_ok, why = False, ""
        try:
            import vgamepad
            vgamepad.VX360Gamepad().reset()
            pad_ok = True
        except Exception as e:
            why = f"{type(e).__name__}: {e}"[:120]
        add("vigem", "Controller", "ViGEmBus virtual controller driver", pad_ok,
            "virtual pad created" if pad_ok else (why or "not installed"),
            None if pad_ok else "vigem", "Download ViGEmBus")

    game = find_game()
    add("game", "Game", "Game folder", game is not None,
        str(game) if game else "not found in your Steam libraries - set it by hand",
        None if game else "game", "Set folder")
    if game:
        win64 = win64_of(game)
        stamp = {}
        try:
            stamp = json.loads((win64 / GAMEFILES_STAMP).read_text())
        except (OSError, ValueError):
            pass
        # AJGAME.exe imports DSOUND.dll, so the loader must be dsound.dll; nothing loads dinput8.dll on
        # many Windows PCs (older game-files releases shipped it under that name, and then no .asi loads).
        old_loader = is_asi_loader(win64 / "dinput8.dll")
        has_loader = (win64 / "dsound.dll").exists()
        add("asi_loader", "Game", "ASI loader (dsound.dll)", has_loader and not old_loader,
            "" if has_loader and not old_loader else
            ("dinput8.dll often isn't loaded, so no .asi mods run" if old_loader else ""),
            "asiloader" if old_loader else "gamefiles",
            "Rename to dsound.dll" if old_loader and not has_loader else
            ("Remove old dinput8.dll" if old_loader else "Install game files"))
        ue = (win64 / "dwmapi.dll").exists() and (win64 / "ue4ss" / "UE4SS.dll").exists()
        add("ue4ss", "Game", "UE4SS", ue, "", "gamefiles", "Install game files")
        hlm = ini_hookloadmap(win64)
        add("hookloadmap", "Game", "UE4SS HookLoadMap = 0", hlm == "0",
            {None: "UE4SS-settings.ini not found", "0": ""}.get(hlm, f"is {hlm}: crashes on map load"),
            "hookloadmap" if ue and hlm not in (None, "0") else ("gamefiles" if not ue else None),
            "Fix setting" if ue else "Install game files")
        en = mods_enabled(win64)
        mods_ok = all((win64 / "ue4ss" / "Mods" / m / "Scripts" / "main.lua").exists() and en.get(m)
                      for m in OUR_MODS)
        add("mods", "Game", "Bot mods (CursorExport, AJProbe)", mods_ok,
            "" if mods_ok else "missing or not enabled in mods.txt",
            "mods" if ue else "gamefiles", "Install mods" if ue else "Install game files")
        add("ajhook", "Game", "ajhook.asi", (win64 / "ajhook.asi").exists(),
            f"release {stamp.get('version')}" if stamp.get("version") else "",
            "gamefiles", "Install game files")
        log = win64 / "ajhook.log"
        if log.exists():
            tail = [l.strip() for l in log.read_text(errors="replace").splitlines() if l.strip()]
            ran = any("hooked" in l for l in tail[-6:])
            add("hook_ran", "Game", "Hook ran last launch", ran, tail[-1][:120] if tail else "")
        else:
            add("hook_ran", "Game", "Hook ran last launch", None, "start the game once to check")

    if game:
        want = icon_names()
        have = [n for n in want if Path("assets/icons", n + ".png").exists()]
        add("icons", "Game", "Character icons (from the game)", len(have) == len(want),
            f"{len(have)} of {len(want)}", None if len(have) == len(want) else "icons",
            "Extract icons")

    pol = Path("models/policy.pkl")
    add("models", "Bot", "Trained model (models/policy.pkl)", pol.exists(),
        f"{pol.stat().st_size // 1024 // 1024} MB" if pol.exists()
        else "download the newest shared model",
        None if pol.exists() else "model", "Download model")
    return checks


# --------------------------------------------------------------------- fixes

def say(msg):
    print(msg, flush=True)


GUI_ASSET = "ajbot-gui.exe"
GAMEFILES_PREFIX = "ajbot-gamefiles"
ICONEXTRACT_ASSET = "ajbot-iconextract.exe"
ICON_IDS = Path("assets/icon_ids.json")


def icon_names():
    try:
        return list(json.loads(ICON_IDS.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def extract_icons(game):
    """The character icons come out of the game's own files (tools/iconextract,
    built by GitHub Actions into every release), so the repo carries no art."""
    exe = Path("cache") / ICONEXTRACT_ASSET
    if not exe.exists():
        say(f"asking GitHub for the latest release of {RELEASE_REPO} ...")
        rel, asset = release_with(ICONEXTRACT_ASSET)
        exe.parent.mkdir(exist_ok=True)
        say(f"downloading {asset['name']} ({asset['size'] // 1024 // 1024} MB) from {rel.get('tag_name')}")
        download(asset["browser_download_url"], exe)
    paks = Path(game) / "HeroGame" / "Content" / "Paks"
    cmd = [str(exe.resolve()), str(paks), str(ICON_IDS.resolve()), str(Path("assets/icons").resolve())]
    say("> " + " ".join(cmd))
    code = subprocess.call(cmd)
    if code:
        raise SystemExit(f"iconextract failed ({code})")


def release_with(prefix):
    """(release, asset) for the newest published release carrying an asset
    whose name starts with `prefix`. The GUI gets a release of its own on
    every build (GitHub Actions), the game files only now and then, so the
    newest release need not hold both."""
    req = urllib.request.Request(f"https://api.github.com/repos/{RELEASE_REPO}/releases?per_page=50",
                                 headers={"User-Agent": "ajbot-setup",
                                          "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        releases = json.load(r)
    for rel in releases:                     # newest first
        if rel.get("draft"):
            continue
        for a in rel.get("assets", []):
            if a["name"].startswith(prefix):
                return rel, a
    raise SystemExit(f"no release on {RELEASES_URL} has {prefix}* yet")


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "ajbot-setup"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got, last = 0, -1
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            pct = got * 100 // total if total else -1
            if pct // 10 != last // 10:
                say(f"  {got // 1024 // 1024} MB" + (f" ({pct}%)" if pct >= 0 else ""))
                last = pct


def set_hookloadmap(win64):
    ini = win64 / "ue4ss" / "UE4SS-settings.ini"
    text = ini.read_text(errors="ignore")
    if re.search(r"^\s*HookLoadMap\s*=", text, re.M):
        text = re.sub(r"^(\s*HookLoadMap\s*=\s*)\d+", r"\g<1>0", text, flags=re.M)
    else:
        text += "\n[Hooks]\nHookLoadMap = 0\n"
    ini.write_text(text)
    say("HookLoadMap set to 0")


def install_mods(win64):
    mods = win64 / "ue4ss" / "Mods"
    mods.mkdir(parents=True, exist_ok=True)
    for m in OUR_MODS:
        src = Path("ue4ss_mods") / m
        if src.is_dir():
            shutil.copytree(src, mods / m, dirs_exist_ok=True)
            say(f"copied mod {m}")
    txt = mods / "mods.txt"
    lines = txt.read_text(errors="ignore").splitlines() if txt.exists() else []
    for m in OUR_MODS:
        pat = re.compile(rf"^\s*{re.escape(m)}\s*:")
        hit = [i for i, l in enumerate(lines) if pat.match(l)]
        if hit:
            lines[hit[0]] = f"{m} : 1"
        else:
            # Keybinds must stay last (UE4SS's own rule): insert before it.
            kb = [i for i, l in enumerate(lines) if l.strip().startswith("Keybinds")]
            lines.insert(kb[0] if kb else len(lines), f"{m} : 1")
    txt.write_text("\n".join(lines) + "\n")
    say("enabled in mods.txt: " + ", ".join(OUR_MODS))


def is_asi_loader(p):
    """True if p is an Ultimate ASI Loader build (any name)."""
    try:
        data = p.read_bytes()
    except OSError:
        return False
    return b"Ultimate-ASI-Loader" in data or "Ultimate-ASI-Loader".encode("utf-16-le") in data


def fix_asi_loader(win64):
    """Old game-files releases shipped the loader as dinput8.dll, which many Windows PCs never load. Move it to
    dsound.dll (AJGAME.exe imports that), or drop the old copy if dsound.dll is already there, so no .asi
    gets loaded twice."""
    old, new = win64 / "dinput8.dll", win64 / "dsound.dll"
    if not is_asi_loader(old):
        return
    if new.exists():
        old.replace(win64 / "dinput8.dll.old")
        say("ASI loader: dsound.dll already there; old dinput8.dll moved to dinput8.dll.old")
    else:
        old.replace(new)
        say("ASI loader: renamed dinput8.dll to dsound.dll")


def install_gamefiles(win64):
    say(f"asking GitHub for the latest release of {RELEASE_REPO} ...")
    rel, asset = release_with(GAMEFILES_PREFIX)
    Path("cache").mkdir(exist_ok=True)
    zpath = Path("cache") / asset["name"]
    say(f"downloading {asset['name']} ({asset['size'] // 1024 // 1024} MB)")
    download(asset["browser_download_url"], zpath)
    keep_ini = (win64 / "ue4ss" / "UE4SS-settings.ini").exists()
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            name = info.filename
            if info.is_dir() or name.startswith(("/", "..")) or ".." in Path(name).parts:
                continue
            if name == "ue4ss/UE4SS-settings.ini" and keep_ini:
                continue                    # keep the user's settings; HookLoadMap fixed below
            if name == "ue4ss/Mods/mods.txt" and (win64 / name).exists():
                continue                    # merged by install_mods
            dest = win64 / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
        say(f"extracted {len(z.infolist())} files into {win64}")
    fix_asi_loader(win64)
    if keep_ini:
        set_hookloadmap(win64)
    install_mods(win64)
    (win64 / GAMEFILES_STAMP).write_text(json.dumps({"version": rel.get("tag_name"),
                                                     "asset": asset["name"]}))
    say(f"game files from release {rel.get('tag_name')} installed")


def fix(fid, path=None):
    if fid == "packages":
        cmd = [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"]
        say("> " + " ".join(cmd))
        sys.exit(subprocess.call(cmd))
    if fid == "vcredist":
        webbrowser.open(VCREDIST_URL)
        say("opened the Visual C++ runtime download - run it, then Check again")
        return
    if fid == "vigem":
        webbrowser.open(VIGEM_URL)
        say("opened the ViGEmBus releases page - install the .exe/.msi, then Check again")
        return
    if fid == "model":
        sys.exit(subprocess.call([sys.executable, "tools/get_model.py"]))
    if fid == "releases":
        webbrowser.open(RELEASES_URL)
        say("opened the releases page")
        return
    if fid == "game":
        if not path or not Path(path, "HeroGame").is_dir():
            raise SystemExit(f"not the game folder (no HeroGame inside): {path}")
        cfg = local.load()
        cfg.setdefault("game", {})["dir"] = str(Path(path))
        local.PATH.write_text(json.dumps(cfg, indent=2))
        say(f"game folder saved to {local.PATH.name}")
        return
    game = find_game()
    if game is None:
        raise SystemExit("set the game folder first")
    if fid == "icons":                  # only reads the game's files: fine while it runs
        extract_icons(game)
        return
    if game_running():
        raise SystemExit("close the game first - its files are in use")
    win64 = win64_of(game)
    if fid == "hookloadmap":
        set_hookloadmap(win64)
    elif fid == "mods":
        install_mods(win64)
    elif fid == "asiloader":
        fix_asi_loader(win64)
    elif fid == "gamefiles":
        install_gamefiles(win64)
    else:
        raise SystemExit(f"unknown fix: {fid}")


def report(checks):
    group = None
    for c in checks:
        if c["group"] != group:
            group = c["group"]
            print(f"\n{group.lower()}")
        mark = {True: "OK ", False: "MISS", None: " -- "}[c["ok"]]
        print(f"  [{mark}] {c['label']}" + (f"  - {c['detail']}" if c["detail"] else "")
              + (f"   (fix: --fix {c['fix']})" if c["ok"] is False and c["fix"] else ""))
    bad = [c for c in checks if c["ok"] is False]
    print("\n" + ("all good" if not bad else "something above needs fixing"))
    return not bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fix", metavar="ID")
    ap.add_argument("--path", help="for --fix game: the game folder")
    a = ap.parse_args()
    if a.fix:
        fix(a.fix, a.path)
        return
    checks = run_checks()
    if a.json:
        print(json.dumps({"checks": checks,
                          "ok": not any(c["ok"] is False for c in checks)}))
        return
    sys.exit(0 if report(checks) else 1)


if __name__ == "__main__":
    main()
