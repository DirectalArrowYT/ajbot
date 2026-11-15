"""Download the league champion from the box into models/.

    python training/pull_champion.py           # install if the champion is new
    python training/pull_champion.py --check   # only say what the box has
    python training/pull_champion.py --force   # reinstall even if policy.pkl matches

What it does:
  1. asks the box for champion_gen and the sha256 of policy.pkl + its data files
  2. if the local policy.pkl already matches, stops (nothing to do)
  3. streams the files over ssh into a temp folder and checks every sha256
  4. re-checks the box: if the champion changed during the download, retries
  5. moves the current model into models/pulled-<name>/ (pickable in the GUI,
     e.g. as Bot B), keeps the 3 newest of those, then installs the champion

Uses Windows OpenSSH (the key lives in the Windows agent).
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root + script folders on sys.path)
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from ajbot import local as _local      # ajbot_local.json: servers.server
_SRV = _local.server("server")
HOST = _SRV["host"]
BOX_MODELS = f"{_SRV['dir']}/models"
BOX_STATE = f"{_SRV.get('lockstep', _SRV['dir'])}/league_state.json"
FILES = ["policy.pkl", "combobook.json", "moveinfo.json", "combos.json",
         "learned_actions.json", "threat.json", "defense.json", "escape.json",
         "mined_routes.json"]
# Shared by every model (not per-champion): always from the base models/
# folder, refreshed on every run even when the policy is already current.
SHARED = ["drilled_routes.json"]
SHARED_DIR = f"{_SRV['dir']}/models"
KEEP_BACKUPS = 3
HERE = Path(__file__).resolve().parent.parent   # the repo root
MODELS = HERE / "models"
STAMP = MODELS / "champion.json"     # what was installed last


def ssh_exe():
    win = Path(r"C:\Windows\System32\OpenSSH\ssh.exe")
    return str(win) if win.exists() else "ssh"


def box(script, binary_out=None):
    """Run a bash script on the box (its login shell is fish, so the script
    goes to bash on stdin). Returns stdout text, or streams it to a file."""
    cmd = [ssh_exe(), "-o", "ConnectTimeout=15", HOST, "bash -s"]
    if binary_out is not None:
        with open(binary_out, "wb") as f:
            p = subprocess.run(cmd, input=script.encode(), stdout=f,
                               stderr=subprocess.PIPE)
    else:
        p = subprocess.run(cmd, input=script.encode(), capture_output=True)
    if p.returncode != 0:
        sys.exit(f"ssh to the box failed: {p.stderr.decode(errors='replace').strip()}")
    return None if binary_out is not None else p.stdout.decode()


def box_info():
    out = box(f"""
python3 -c 'import json; print(json.load(open("{BOX_STATE}")).get("champion_gen"))' || echo "?"
cd {BOX_MODELS} 2>/dev/null || {{ echo "no folder {BOX_MODELS} on the box (yet)" >&2; exit 1; }}
for f in {' '.join(FILES)}; do [ -f "$f" ] && sha256sum "$f"; done
true
""")
    lines = out.strip().splitlines()
    gen = lines[0].strip()
    shas = {}
    for ln in lines[1:]:
        h, name = ln.split(None, 1)
        shas[name.strip()] = h
    if "policy.pkl" not in shas:
        sys.exit("the box has no models/policy.pkl right now")
    return gen, shas


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bots_running():
    """play.py / play2.py processes that would keep using (and saving over)
    the old model's files."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
             "ForEach-Object CommandLine"],
            capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return []
    return [l.strip() for l in out.splitlines()
            if "play.py" in l or "play2.py" in l]


def local_label():
    try:
        s = json.loads(STAMP.read_text(encoding="utf-8"))
        g = str(s.get("gen"))
        if g in ("None", "?"):
            return s["sha"][:8]
        return f"gen{g}" if g.isdigit() else g
    except (OSError, ValueError, KeyError):
        pol = MODELS / "policy.pkl"
        return sha256(pol)[:8] if pol.exists() else None


def backup_current():
    label = local_label()
    if not label:
        return None
    dest = MODELS / f"pulled-{label}"
    if dest.exists():
        dest = MODELS / f"pulled-{label}-{time.strftime('%Y%m%d-%H%M%S')}"
    dest.mkdir(parents=True)
    for name in FILES + ["champion.json"]:
        src = MODELS / name
        if src.exists():
            shutil.copy2(src, dest / name)
    # Keep the newest few backups this script made.
    olds = sorted((d for d in MODELS.glob("pulled-*") if d.is_dir()),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    # Never one the GUI is set to play (e.g. Bot B in a duel).
    try:
        gui = (HERE / "ajbot_gui_settings.json").read_text(encoding="utf-8")
    except OSError:
        gui = ""
    for d in olds[KEEP_BACKUPS:]:
        if f"models/{d.name}/" in gui.replace("\\\\", "/").replace("\\", "/"):
            print(f"  kept {d.name} (the GUI uses it)")
            continue
        shutil.rmtree(d, ignore_errors=True)
        print(f"  removed old backup {d.name}")
    return dest


def download(names, tmp):
    tar_path = tmp / "files.tar"
    print(f"downloading {len(names)} files ...", flush=True)
    t0 = time.time()
    box(f"cd {BOX_MODELS} && tar cf - {' '.join(names)}\n", binary_out=tar_path)
    print(f"  {tar_path.stat().st_size / 1e6:.0f} MB in {time.time() - t0:.0f}s")
    out = tmp / "x"
    out.mkdir()
    with tarfile.open(tar_path) as t:
        t.extractall(out, filter="data")
    return out


def pull_shared():
    """Combo routes the drill landed on the box (drilled_routes.json): the
    route follower plays them in matches, whatever the model."""
    out = box(f"cd {SHARED_DIR} && for f in {' '.join(SHARED)}; do "
              f"[ -f \"$f\" ] && sha256sum \"$f\"; done; true\n")
    for ln in out.strip().splitlines():
        h, name = ln.split(None, 1)
        name = name.strip()
        dst = MODELS / name
        if dst.exists() and sha256(dst) == h:
            continue
        with tempfile.TemporaryDirectory(dir=HERE, prefix=".pull_tmp_") as td:
            tmp = Path(td) / name
            box(f"cat {SHARED_DIR}/{name}\n", binary_out=tmp)
            if sha256(tmp) != h:
                print(f"  {name}: checksum mismatch, kept the old one")
                continue
            shutil.copy2(tmp, dst)
        print(f"  updated {name} (drilled combo routes)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only report, change nothing")
    ap.add_argument("--force", action="store_true", help="reinstall even if up to date")
    ap.add_argument("--from", dest="src", metavar="FOLDER", default=None,
                    help="install the model in box models/FOLDER (e.g. userfit) "
                         "instead of the league champion")
    args = ap.parse_args()

    global BOX_MODELS
    if args.src:
        BOX_MODELS = f"{BOX_MODELS}/{args.src.strip('/')}"
    gen, shas = box_info()
    if args.src:
        gen = args.src.strip("/")        # not a league generation
        print(f"(box folder models/{gen}, not the league champion)")
    local = MODELS / "policy.pkl"
    have = sha256(local) if local.exists() else None
    print(f"box {'folder ' + gen if args.src else 'champion: gen ' + gen}  "
          f"policy {shas['policy.pkl'][:8]}")
    print(f"this PC:      policy {have[:8] if have else 'none'}")
    if not args.check:
        pull_shared()
    if have == shas["policy.pkl"] and not args.force:
        print("already up to date - nothing to do")
        return 0
    if args.check:
        print("a newer champion is available (run without --check to install)")
        return 0

    running = bots_running()
    if running:
        print("\n!! a bot is running - stop it in the GUI first, or it keeps playing")
        print("   the old model and may save its learning over the new files:")
        for r in running:
            print("   ", r[:120])
        return 1

    for attempt in range(3):
        # Next to models/ (same drive, and C: is small).
        with tempfile.TemporaryDirectory(dir=HERE, prefix=".pull_tmp_") as td:
            got = download(list(shas), Path(td))
            bad = [n for n, h in shas.items() if sha256(got / n) != h]
            if bad:
                print(f"  checksum mismatch: {', '.join(bad)} - retrying")
                continue
            gen2, shas2 = box_info()
            if args.src:
                gen2 = gen
            if shas2 != shas:
                print(f"  the champion changed during the download (now gen {gen2}) - "
                      f"downloading again")
                gen, shas = gen2, shas2
                continue
            print("  all checksums match the box")
            kept = backup_current()
            if kept:
                print(f"  previous model kept in models/{kept.name}/")
            for name in shas:
                shutil.copy2(got / name, MODELS / name)
            STAMP.write_text(json.dumps({
                "gen": gen, "sha": shas["policy.pkl"],
                "installed": time.strftime("%Y-%m-%d %H:%M:%S"),
                "files": shas}, indent=1), encoding="utf-8")
            what = f"models/{gen} from the box" if args.src else f"champion gen {gen}"
            print(f"\ninstalled {what} into models/ - restart the bot to use it")
            return 0
    print("gave up after 3 tries - nothing was changed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
