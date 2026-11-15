"""Champion vs challenger: retrain on what the farm plays, keep a new policy
only when it beats the old one. Runs on the box next to farm.py.

    python3 server/league.py                 # forever (league.log)
    python3 server/league.py --status        # where it stands

The cycle, repeated without anyone at the controls:
  1. ingest   finished recordings from this checkout's botruns/ move into the
              corpus (<server dir>/botruns); archive_low_value.py then
              DELETES every one without progress (a combo beating the combo
              book, a new damaging route, or top-30% combo value) and caps
              the corpus at --max-gb. Human runs/ are never touched.
  2. train    once --min-new recordings survived since the last fit: the
              usual chain (tickcache, mine, combobook, moveinfo, threat,
              train.py --include-bot --cherrypick --quick) with THIS
              checkout's code on the corpus data, nice'd, on --cores cores, in
              a scope capped at --mem-gb so an OOM kills only the trainer.
  3. eval     the new policy goes to models/challenger/ and league.json; farm.py
              restarts the bots. Odd copies play champion vs challenger
              (fair, these decide promotion); even copies spar: the
              challenger vs a boss - the champion with a guard assist that
              leans on a human-only policy. After --eval decided FAIR matches the
              challenger is promoted when it won at least --promote of them,
              and dropped early when it is clearly worse.
  4. promote  challenger files -> models/ (the old champion kept in
              models/prev/, last 2), and the farm restarts on it.
Training for the next generation starts as soon as there is new data, so a
fresh challenger is usually waiting when the current one is decided.
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root + script folders on sys.path)
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent   # the repo root
from ajbot import local as _local          # ajbot_local.json: servers.server
_SRV = _local.server("server")
CORPUS = Path(_SRV["dir"])
PY = _SRV["python"]
LEAGUE = HERE / "league.json"
STATE = HERE / "league_state.json"
STATS = HERE / "stats" / "matches.jsonl"
CHALL_DIR = HERE / "models" / "challenger"
CHALL = "models/challenger/policy.pkl"
FILES = ["policy.pkl", "combobook.json", "moveinfo.json", "combos.json",
         "learned_actions.json", "threat.json", "defense.json", "escape.json",
         "mined_routes.json"]
# The sparring learner's live defence/escape tables (play2 --learn-dir); a
# new challenger takes a snapshot of them, the champion only on promotion.
LEARN = ("defense.json", "escape.json")
QUIET_HOURS = (3.5, 5.0)     # the box's nightly test run
SETTLE_S = 300               # a recording untouched this long is finished


def log(msg):
    line = time.strftime("[%m-%d %H:%M:%S] ") + msg
    print(line, flush=True)
    with open(HERE / "league.log", "a") as f:
        f.write(line + "\n")


def load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def save(path, obj):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(path)


def mem_available_gb():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1e6
    return 0.0


# ---------------------------------------------------------------- ingest

def ingest(a, prune=True):
    """Move finished farm recordings into the corpus, then prune it. While a
    fit runs, prune=False: train.py has already listed the corpus and reads
    those files for minutes, so deleting one mid-fit kills the fit."""
    now = time.time()
    done = [f for f in glob.glob(str(HERE / "botruns" / "*.jsonl"))
            if now - os.path.getmtime(f) > SETTLE_S]
    if not done:
        return 0
    dest = CORPUS / "botruns"
    for f in done:
        os.replace(f, dest / Path(f).name)        # same disk: a rename, mtime kept
    if not prune:
        return len(done)          # counted as kept; the next pass prunes
    r = subprocess.run(["nice", "-n", "10", PY, str(HERE / "server" / "archive_low_value.py"),
                        "--runs", "botruns/*.jsonl", "--apply", "--max-gb", str(a.max_gb),
                        "--purge-archive"],
                       cwd=CORPUS, capture_output=True, text=True,
                       env=dict(os.environ, PYTHONPATH=f"{HERE}:{HERE}/server:{HERE}/training"))
    kept = sum(1 for f in done if (dest / Path(f).name).exists())
    last = (r.stdout.strip().splitlines() or ["?"])
    if r.returncode != 0:
        log(f"ingest: archiver failed: {(r.stderr or '').strip().splitlines()[-1:]}")
    else:
        log(f"ingest: {len(done)} new recordings, ~{kept} kept; "
            f"{' / '.join(l for l in last if l.startswith(('keep', 'deleted', 'cap')))}")
    return kept


# ---------------------------------------------------------------- train

def train_cmd(a):
    # -P: `python -m ajbot.x` run from the corpus would otherwise import the
    # corpus's own (old) ajbot package ahead of PYTHONPATH=HERE.
    py = f"{PY} -u -P"
    steps = [f"{py} -m ajbot.tickcache",
             f"{py} {HERE}/training/mine.py --quiet --top 5",
             f"{py} {HERE}/training/combobook.py",
             f"{py} -m ajbot.moveinfo",
             f"{py} -m ajbot.threat",
             f"{py} -m ajbot.routemine --workers 2",
             f"{py} {HERE}/training/train.py --include-bot --save models/fit {a.train_args}"
             " && rm -rf models/fit/prev"]
    pin = f"nice -n 10 taskset -c 0-{a.cores - 1}"
    chain = " && ".join(f"{pin} {s}" for s in steps)
    # The boss's lean: a fit on human recordings only, redone only when
    # runs/ has something newer than it.
    chain += (f" && if [ ! -f models/human/policy.pkl ] || [ -n \"$(find runs -name '*.jsonl'"
              f" -newer models/human/policy.pkl | head -1)\" ]; then {pin} {py} {HERE}/training/train.py"
              f" --save models/human --quick && rm -rf models/human/prev; fi")
    return ["systemd-run", "--user", "--scope", "-q", "-p", f"MemoryMax={a.mem_gb}G",
            "-p", "MemorySwapMax=0", "bash", "-c",
            f"cd {CORPUS} && mkdir -p models/fit && export PYTHONPATH={HERE}:{HERE}/training "
            # combobook/moveinfo/threat are written to (and the fit reads its
            # features from) models/fit - never over the champion's models/.
            f"AJBOT_DATA_DIR=models/fit OMP_NUM_THREADS={a.cores} "
            f"AJBOT_NO_GPU=1 AJBOT_MEM_CAP_GB={a.mem_gb * 0.7:g} && {chain}"]


def start_train(a, st):
    out = open(HERE / "league_train.log", "ab")
    p = subprocess.Popen(train_cmd(a), stdout=out, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    st["training"] = {"pid": p.pid, "t": time.time(), "gen": st["gen"] + 1}
    st["new_since_train"] = 0
    log(f"train: gen {st['gen'] + 1} started (pid {p.pid}, {a.cores} cores, "
        f"<= {a.mem_gb} GB) - league_train.log")
    return p


def take_trained(st):
    """The fit finished: copy its files aside as the pending challenger."""
    gen = st["training"]["gen"]
    pend = HERE / "models" / f"pending-{gen}"
    pend.mkdir(parents=True, exist_ok=True)
    # The fit saves into models/fit, never models/policy.pkl: models/ is the
    # champion's folder (LIVE/models links to the corpus), so a fit saved
    # there would replace the champion before it is ever challenged.
    # Each challenger carries its own data files: the fit's, else (files the
    # chain does not rebuild) the champion's, and the learner's tables now.
    for f in FILES:
        src = CORPUS / "models" / "fit" / f
        if f in LEARN:
            src = CORPUS / "models" / "learn" / f
        if not src.exists():
            src = CORPUS / "models" / f
        if src.exists():
            shutil.copy2(src, pend / f)
    # train.py rotates the corpus's old policies into models/prev: keep 2.
    prev = sorted(glob.glob(str(CORPUS / "models" / "prev" / "policy-*.pkl")))
    for f in prev[:-2]:
        os.remove(f)
    human = CORPUS / "models" / "human" / "policy.pkl"
    live_h = HERE / "models" / "human" / "policy.pkl"
    if human.exists() and (not live_h.exists() or (
            not os.path.samefile(human, live_h)
            and human.stat().st_mtime > live_h.stat().st_mtime)):
        live_h.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(human, live_h)
        log("train: human-only policy updated (the sparring boss leans on it)")
    st["pending"] = {"gen": gen, "dir": str(pend)}
    log(f"train: gen {gen} done in {(time.time() - st['training']['t']) / 60:.0f} min")


# ---------------------------------------------------------------- eval

def install_challenger(st):
    p = st.pop("pending")
    if CHALL_DIR.exists():
        shutil.rmtree(CHALL_DIR)
    shutil.move(p["dir"], CHALL_DIR)
    st["challenger"] = {"gen": p["gen"], "t": time.time()}
    write_league(st)
    log(f"eval: gen {p['gen']} challenges gen {st['champion_gen']}")


def write_league(st):
    lg = {"gen": st["champion_gen"], "champion": "models/policy.pkl",
          "challenger": CHALL if st.get("challenger") else None,
          "challenger_gen": (st.get("challenger") or {}).get("gen")}
    save(LEAGUE, lg)


def score_challenger(st):
    """(wins, losses) of the challenger's side since it was installed."""
    t0 = st["challenger"]["t"]
    w = l = 0
    try:
        lines = open(STATS).read().splitlines()
    except OSError:
        return 0, 0
    for line in lines[-4000:]:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("t", 0) < t0 or r.get("mode") != "selfplay" or r.get("model") != CHALL:
            continue
        if r.get("result") == "win":
            w += 1
        elif r.get("result") == "loss":
            l += 1
    return w, l


def decide(a, st):
    w, l = score_challenger(st)
    n = w + l
    gen = st["challenger"]["gen"]
    if n and n != st["challenger"].get("seen"):
        st["challenger"]["seen"] = n
        if n % 10 == 0:
            log(f"eval: gen {gen} {w}-{l} vs gen {st['champion_gen']}")
    rate = w / n if n else 0.0
    if n >= a.eval:
        verdict = "promoted" if rate >= a.promote else "dropped"
    elif n >= a.eval // 2 and rate < 0.35:
        verdict = "dropped"
    else:
        return
    st["history"].append({"gen": gen, "vs": st["champion_gen"], "wins": w, "losses": l,
                          "verdict": verdict, "t": time.time()})
    log(f"eval: gen {gen} {verdict} ({w}-{l}, {rate:.0%})")
    if verdict == "promoted":
        prev = HERE / "models" / "prev"
        prev.mkdir(exist_ok=True)
        shutil.copy2(HERE / "models" / "policy.pkl",
                     prev / f"champion-gen{st['champion_gen']}.pkl")
        for f in sorted(glob.glob(str(prev / "champion-gen*.pkl")),
                        key=os.path.getmtime)[:-2]:
            os.remove(f)
        for f in FILES:
            src = CHALL_DIR / f
            if src.exists():
                shutil.copy2(src, HERE / "models" / f)
        st["champion_gen"] = gen
    shutil.rmtree(CHALL_DIR, ignore_errors=True)
    st["challenger"] = None
    write_league(st)


# ---------------------------------------------------------------- main

def quiet_hours():
    lt = time.localtime()
    h = lt.tm_hour + lt.tm_min / 60
    return QUIET_HOURS[0] <= h < QUIET_HOURS[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-new", type=int, default=60,
                    help="kept recordings needed before the next fit")
    ap.add_argument("--eval", type=int, default=40, help="decided matches per challenger")
    ap.add_argument("--promote", type=float, default=0.55, help="win rate to promote")
    ap.add_argument("--max-gb", type=float, default=8.0, help="corpus botruns/ cap")
    ap.add_argument("--cores", type=int, default=12)
    ap.add_argument("--mem-gb", type=int, default=14)
    ap.add_argument("--no-quiet", action="store_true",
                    help="train through QUIET_HOURS too")
    ap.add_argument("--train-args", default="--cherrypick --human-share 0.6")
    ap.add_argument("--poll", type=float, default=60)
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()

    st = load(STATE, None) or {"gen": 0, "champion_gen": 0, "challenger": None,
                               "pending": None, "training": None,
                               "new_since_train": 0, "history": []}
    if a.status:
        print(json.dumps({k: v for k, v in st.items() if k != "history"}, indent=1))
        for h in st["history"][-10:]:
            print(f"  gen {h['gen']} vs {h['vs']}: {h['wins']}-{h['losses']} {h['verdict']}")
        return

    log(f"league: champion gen {st['champion_gen']}, eval {a.eval} matches, "
        f"promote at {a.promote:.0%}, corpus cap {a.max_gb:g} GB")
    st["training"] = None          # a fit from a previous run is not ours to wait on
    write_league(st)
    proc = None
    while True:
        st["new_since_train"] += ingest(a, prune=proc is None)
        if proc is not None and proc.poll() is not None:
            if proc.returncode == 0:
                st["gen"] = st["training"]["gen"]
                take_trained(st)
            else:
                log(f"train: gen {st['training']['gen']} FAILED (code {proc.returncode}) "
                    f"- see league_train.log")
            st["training"] = proc = None
        if st.get("challenger"):
            decide(a, st)
        if not st.get("challenger") and st.get("pending"):
            install_challenger(st)
        if (proc is None and not st.get("pending") and st["new_since_train"] >= a.min_new
                and (a.no_quiet or not quiet_hours())):
            if mem_available_gb() >= a.mem_gb + 2:
                proc = start_train(a, st)
            elif int(time.time()) % 1800 < a.poll:
                log(f"train: waiting for memory ({mem_available_gb():.0f} GB free, "
                    f"need {a.mem_gb + 2})")
        save(STATE, st)
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
