"""Retrain on another machine, so this PC stays free for the game and self-play.

    python remote_retrain.py            # sync, run the full retrain there, pull the policy back
    python remote_retrain.py --sync     # only copy code / digests / caches over
    python remote_retrain.py --pull     # only fetch the remote's latest policy
    python remote_retrain.py --args "--cherrypick"   # train.py flags instead of the quick default
    python remote_retrain.py --host laptop           # the old target

Targets (servers in ajbot_local.json): "server" (default) is a Linux box - 32 threads,
shared with LLMs, image gen and a nightly test run, so training there runs
under nice, pinned to TRAIN_CORES cores, and keeps off the GPU while an LLM
is loaded. "laptop" is the old Windows/MSYS target. ("The laptop" below
means whichever remote is in use.)

Recordings MOVE to the remote (since 2026-11-09): each finished, usable
recording is digested here (ajbot.tickcache) and its raw-JSON facts cached,
then the recording itself is copied over (mtime kept, so every cache key
still matches), checked by sha256 on both sides, and only then deleted
here. So runs/ and botruns/ here only ever hold what has not been trained on
yet, and the remote holds the corpus. Recordings permanent_defect() rejects
are never sent - they stay here for you to archive or delete
(archive_junk.py). --keep-local copies without deleting.

Older remotes got an empty placeholder per recording plus its real size and
mtime in cache/stubs.json; any placeholder left there for a recording that
was not sent is dropped on the next sync.

The laptop (i7-1065G7, 8 threads, no CUDA) is several times slower than this
PC for training, but it is otherwise idle: the point is that a retrain no
longer competes with the game, self-play recording or builds here.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from ajbot import local as _local      # ajbot_local.json: servers
HOSTS = _local.servers()
TRAIN_CORES = 16
# llama-swap on the server: lists the loaded models (none -> no "model" key).
LLM_RUNNING = "curl -s -m 3 localhost:8080/running"
# The server's nightly test run (~03:46); a retrain should not overlap it.
QUIET_HOURS = (3.5, 5.0)

HOST = REMOTE = PY = None
SSH = []
LINUX = False


def use_host(name):
    global HOST, REMOTE, PY, SSH, LINUX
    h = HOSTS.get(name) or _local.server(name)
    HOST, REMOTE, PY, LINUX = h["host"], h["dir"], h["python"], h["linux"]
    SSH = ["C:/Windows/System32/OpenSSH/ssh.exe", "-o", "BatchMode=yes", HOST]
HERE = Path(__file__).resolve().parent

CODE_EXCLUDE = ("runs", "botruns", "cache", "archive", "gui", "ajhook", "__pycache__",
                "models/prev", ".git", "stats/control_modes")
REC_DIRS = ("runs", "botruns")
STUBS = "cache/stubs.json"
PULL = ["models/policy.pkl", "models/combobook.json", "models/moveinfo.json",
        "models/combos.json", "models/learned_actions.json", "models/threat.json"]


def ssh(cmd, check=True, capture=False):
    """Run a bash command line there. It goes in on stdin to `bash -s`: the
    server's login shell is fish, which does not speak bash ($?, quoting), so
    the login shell only ever sees `bash -s` / `bash -c '...'`."""
    r = subprocess.run(SSH + ["bash -s"], input=cmd.encode("utf-8"),
                       capture_output=capture)
    if check and r.returncode != 0:
        raise SystemExit(f"remote command failed ({r.returncode}): {cmd}")
    return r.stdout.decode("utf-8", "replace") if capture else r.returncode


def push(paths, label):
    """tar the given relative paths here and unpack them in REMOTE."""
    if not paths:
        print(f"  {label}: nothing new")
        return
    lst = HERE / "cache" / ".push_list.txt"
    lst.parent.mkdir(exist_ok=True)
    lst.write_text("\n".join(paths) + "\n", encoding="utf-8")
    size = sum((HERE / p).stat().st_size for p in paths)
    print(f"  {label}: {len(paths)} files, {size / 1e6:.0f} MB ...", flush=True)
    t0 = time.time()
    tar = subprocess.Popen(["tar", "-C", str(HERE), "-czf", "-", "-T", str(lst)],
                           stdout=subprocess.PIPE)
    rem = subprocess.run(SSH + [f"bash -c 'mkdir -p {REMOTE} && tar -C {REMOTE} -xzf -'"],
                         stdin=tar.stdout)
    tar.stdout.close()
    tar.wait()
    if rem.returncode != 0 or tar.returncode != 0:
        raise SystemExit(f"copy failed ({label})")
    print(f"    done in {time.time() - t0:.0f}s", flush=True)


def local_files(top):
    out = {}
    for d in top:
        base = HERE / d
        if not base.exists():
            continue
        for root, dirs, files in os.walk(base):
            for f in files:
                p = Path(root) / f
                rel = p.relative_to(HERE).as_posix()
                if rel.endswith((".tmp", ".push_list.txt")) or ".tmp" in Path(rel).suffixes:
                    continue
                out[rel] = p.stat().st_size
    return out


def remote_files(top):
    txt = ssh(f"cd {REMOTE} 2>/dev/null && find {' '.join(top)} -type f -printf '%p\\t%s\\n' 2>/dev/null; true",
              check=False, capture=True) or ""
    out = {}
    for line in txt.splitlines():
        if "\t" in line:
            p, s = line.rsplit("\t", 1)
            out[p] = int(s) if s.isdigit() else -1
    return out


def code_files():
    out = []
    for root, dirs, files in os.walk(HERE):
        rel_root = Path(root).relative_to(HERE).as_posix()
        # .claude/worktrees holds whole copies of the project, recordings included
        if (rel_root != "." and rel_root.startswith(".")) or any(rel_root == e or rel_root.startswith(e + "/") for e in CODE_EXCLUDE):
            dirs[:] = []
            continue
        for f in files:
            if f.endswith((".py", ".bat", ".json", ".jsonl", ".txt", ".md")) and not f.endswith(".log"):
                rel = (Path(root) / f).relative_to(HERE).as_posix()
                if rel != "models/policy.pkl":
                    out.append(rel)
    return out


def recordings():
    """Finished recordings - not the ones self-play is writing right now."""
    from ajbot.tickcache import LIVE_S
    now = time.time()
    return sorted(p.relative_to(HERE).as_posix() for d in REC_DIRS
                  for p in (HERE / d).glob("*.jsonl") if now - p.stat().st_mtime > LIVE_S)


def _facts(rel):
    """The per-recording facts that read the JSON itself - they cannot be
    computed from a stub on the laptop."""
    import train as T
    T.permanent_defect(rel)
    T._opponent_ai_fraction(rel)
    T._passive_fraction(rel)
    return rel


def prepare(recs):
    """Digest new recordings and cache their raw-JSON facts, here."""
    from concurrent.futures import ProcessPoolExecutor
    from ajbot import tickcache
    from ajbot.parallel import safe_worker_count
    print("digesting new recordings here ...", flush=True)
    t0 = time.time()
    if subprocess.run([sys.executable, "-m", "ajbot.tickcache"], cwd=HERE).returncode != 0:
        raise SystemExit("digesting failed")
    with ProcessPoolExecutor(max_workers=safe_worker_count(tickcache.src_sizes(recs))) as ex:
        list(ex.map(_facts, recs, chunksize=8))
    print(f"  ready in {time.time() - t0:.0f}s", flush=True)


def make_stubs():
    """Run ON THE LAPTOP (remote_retrain.py --make-stubs): an empty file for
    every recording in cache/stubs.json that is not there yet, and drop stubs
    of recordings that were archived on the main PC since."""
    rows = json.loads((HERE / STUBS).read_text(encoding="utf-8"))
    made = dropped = 0
    for d in REC_DIRS:
        (HERE / d).mkdir(exist_ok=True)
        for p in (HERE / d).glob("*.jsonl"):
            rel = p.relative_to(HERE).as_posix()
            if p.stat().st_size == 0 and rel not in rows:
                p.unlink()
                dropped += 1
    for rel in rows:
        p = HERE / rel
        if not p.exists():
            p.touch()
            made += 1
    print(f"  stubs: {made} new, {dropped} dropped, {len(rows)} recordings")


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def remote_recordings():
    """{rel: (size, int mtime)} of the real (non-empty) recordings there."""
    txt = ssh(f"cd {REMOTE} 2>/dev/null && find {' '.join(REC_DIRS)} -maxdepth 1 -name '*.jsonl' "
              "-size +0 -printf '%p\\t%s\\t%T@\\n' 2>/dev/null; true", check=False, capture=True) or ""
    out = {}
    for line in txt.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[1].isdigit():
            out[parts[0]] = (int(parts[1]), int(float(parts[2])))
    return out


def move_recordings(recs, keep_local=False):
    """Copy recordings there, verify, then delete them here."""
    rem = remote_recordings()
    send = [r for r in recs if rem.get(r, (None,))[0] != (HERE / r).stat().st_size]
    push(send, "recordings")
    rem = remote_recordings()
    # mtime is part of every cache key; tar keeps it, but make sure.
    fix = []
    for r in recs:
        st = (HERE / r).stat()
        if r in rem and rem[r][1] != int(st.st_mtime):
            fix.append(f"touch -m -d @{int(st.st_mtime)} '{r}'")
    if fix:
        ssh(f"cd {REMOTE} && " + " && ".join(fix))
    there = [r for r in recs if rem.get(r, (None,))[0] == (HERE / r).stat().st_size]
    missing = len(recs) - len(there)
    if missing:
        print(f"  {missing} recordings did not arrive - kept here, retried next sync")
    if not there:
        return
    print(f"  verifying {len(there)} recordings (sha256 both sides) ...", flush=True)
    listing = "\n".join(there)
    txt = ssh(f"cd {REMOTE} && xargs -d '\\n' sha256sum <<'EOF_LIST'\n{listing}\nEOF_LIST",
              check=False, capture=True) or ""
    remote_sum = {}
    for line in txt.splitlines():
        if "  " in line:
            h, p = line.split("  ", 1)
            remote_sum[p.strip()] = h
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:
        local_sum = dict(zip(there, ex.map(lambda r: _sha256(HERE / r), there)))
    ok = [r for r in there if remote_sum.get(r) == local_sum[r]]
    bad = len(there) - len(ok)
    if bad:
        print(f"  {bad} recordings differ there - kept here, resent next sync")
    if keep_local:
        print(f"  {len(ok)} recordings verified there (--keep-local: not deleted here)")
        return
    freed = 0
    for r in ok:
        p = HERE / r
        freed += p.stat().st_size
        p.unlink()
    print(f"  moved {len(ok)} recordings ({freed / 1e9:.2f} GB) - they now live only on {HOST}")


def sync(keep_local=False):
    import train as T
    print(f"sync to {HOST}:")
    recs = [r for r in recordings() if (HERE / r).stat().st_size > 0]
    prepare(recs)
    junk = [r for r in recs if T.permanent_defect(r)[1]]
    good = [r for r in recs if r not in set(junk)]
    if junk:
        print(f"  {len(junk)} recordings are permanently unusable - kept here, not sent "
              "(python archive_junk.py lists them)")
    push(code_files(), "code, settings, stats")
    loc = local_files(["cache"])
    rem = remote_files(["cache"])
    # Cache files named after a recording only go along with that recording.
    # Once a recording lives only there, the server's archiver may sweep its
    # caches; resending the leftovers here re-sent ~1.9 GB every sync.
    here_ids = {Path(r).stem for r in recs}

    def wanted(p):
        m = re.match(r"(\d{9,})\.", Path(p).name)
        return m is None or m.group(1) in here_ids
    push(sorted(p for p, s in loc.items() if rem.get(p) != s and wanted(p)),
         "digests, caches")
    move_recordings(good, keep_local)
    # No more placeholders: every recording there is real. An empty table
    # makes --make-stubs drop the placeholders of recordings never sent.
    r = subprocess.run(SSH + [f"bash -c 'mkdir -p {REMOTE}/cache && cat > {REMOTE}/{STUBS}'"],
                       input=b"{}")
    if r.returncode != 0:
        raise SystemExit("could not write the stub table there")
    ssh(f"cd {REMOTE} && {PY} remote_retrain.py --make-stubs")


def _quiet_hours():
    lt = time.localtime()
    h = lt.tm_hour + lt.tm_min / 60
    return QUIET_HOURS[0] <= h < QUIET_HOURS[1]


def train(args, force=False):
    if LINUX and _quiet_hours() and not force:
        raise SystemExit(f"{QUIET_HOURS[0]:g}-{QUIET_HOURS[1]:g} h is the server's nightly test "
                         "window - retry later, or pass --force")
    py = f"{PY} -u"
    steps = [
        f"{py} -m ajbot.tickcache",
        f"{py} mine.py --quiet --top 5",
        f"{py} combobook.py",
        f"{py} -m ajbot.moveinfo",
        # Contact chance / timing per move, combo-damage-to-come, escape and
        # resource success by state: the rule layers (defence, emergency
        # dodge, Plus Ultra / Rising) read it. models/movedata.json is built
        # locally from the FModel export and travels with the code.
        f"{py} -m ajbot.threat",
        f"{py} train.py --include-bot --save models {args}",
    ]
    pre = f"cd {REMOTE}"
    if LINUX:
        # Nice, pinned to TRAIN_CORES cores (joblib, xgboost and the worker
        # pools all size themselves from the affinity mask), and off the GPU
        # while an LLM holds it - the iGPU's memory is the LLMs' memory.
        # ROCm runtime from the venv, for a HIP xgboost build if one is ever
        # installed. The AMD fork built 2026-11-06 (build/xgboost_hip_pkg_broken)
        # trained wrong trees on gfx1151 (train acc 40% vs CPU 100% on the same
        # data) and crashed on multi-output trees, so the venv runs the CPU wheel.
        pre += (f" && export LD_LIBRARY_PATH={REMOTE}/venv/lib/python3.11/site-packages/"
                "_rocm_sdk_core/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}")
        pre += (f" && export OMP_NUM_THREADS={TRAIN_CORES}"
                f" && if {LLM_RUNNING} | grep -q model; then export AJBOT_NO_GPU=1;"
                " echo 'an LLM is loaded: training on the CPU only'; fi")
        steps = [f"nice -n 10 taskset -c 0-{TRAIN_CORES - 1} {st}" for st in steps]
    cmd = pre + " && " + " && ".join(steps)
    print(f"\nretraining on {HOST} ({args.strip()}):", flush=True)
    t0 = time.time()
    if ssh(cmd, check=False) != 0:
        raise SystemExit("remote retrain FAILED - local policy left alone")
    print(f"remote retrain finished in {(time.time() - t0) / 60:.1f} min", flush=True)


def pull():
    sys.path.insert(0, str(HERE))
    from train import _rotate
    print("\npulling the new policy:")
    for rel in PULL:
        dest = HERE / rel
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as fh:
            r = subprocess.run(SSH + [f"cat {REMOTE}/{rel}"], stdout=fh)
        if r.returncode == 0 and tmp.stat().st_size > 0:
            if rel == "models/policy.pkl":
                _rotate(dest)              # only once the new one is safely here
            os.replace(tmp, dest)
            print(f"  {rel}  {dest.stat().st_size / 1e6:.1f} MB")
        else:
            tmp.unlink(missing_ok=True)
            print(f"  {rel}: not there, kept the local one")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--pull", action="store_true")
    ap.add_argument("--make-stubs", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--host", choices=sorted(HOSTS) or None, default="server")
    ap.add_argument("--force", action="store_true",
                    help="train even in the server's nightly quiet hours")
    ap.add_argument("--keep-local", action="store_true",
                    help="copy recordings there but do not delete them here")
    ap.add_argument("--args", default="--cherrypick --quick",
                    help="train.py flags (besides --include-bot --save models)")
    a = ap.parse_args()
    if a.make_stubs:
        make_stubs()
        return
    use_host(a.host)
    if a.pull:
        pull()
        return
    sync(a.keep_local)
    if a.sync:
        return
    train(a.args, a.force)
    pull()
    print("\ndone - the new policy is in models/policy.pkl (previous one in models/prev)")


if __name__ == "__main__":
    main()
