"""Combo drill: the farm's game copies practice long combos instead of playing.

    python3 bot/drill.py                         # copies 0-3, every long route once
    python3 bot/drill.py --copies 2 --min-steps 10 --max-attempts 200
    python3 bot/drill.py --only Mirio --rounds 3
    python3 bot/drill.py --sources library --max-attempts 40 --confirm 3
                                                 # every library combo, each character

Routes, longest first:
  * models/drill_routes.json - combos written by people, in combo notation
    (ajbot/notation.py), e.g. the Mirio route found by players:
        {"routes": [{"char": "Mirio", "name": "Mirio optimal",
                     "notation": "jump > AA > CA > Q2 (x2) > q4 > Q2 (x2) > q4 > pu"}]}
  * models/mined_routes.json - the champion's routes mined from human play
    (ajbot.routemine), the ones with at least --min-steps steps. Each is
    rebuilt from the recording it came from (demo.from_combo): the player's
    own gaps, stick angles, drift and starting distance. A bare label list
    with one guessed gap and no stick drops almost every long route - the
    first drill round landed 3 of 17 that way. Older mined files carry no
    source; then the recordings under --corpus are mined again to find it.
    Only true combos are drilled: a mined route is an engagement (presses no
    more than 2.5 s apart that did damage), and in most of them the opponent
    was out of hitstun for seconds - guarding, attacking back, running -
    which no dummy in training mode reproduces. A route whose opponent was
    free for more than FREE_MAX_S is skipped.
  * the combo library, combos/<Character>.json (ajbot.combodb), with
    --sources library: every combo of the --tiers given, in its notation.
    Each result goes back into its library entry as "tested" (attempts,
    landed, best steps, against the guarding dummy); the tier stays for
    people to change. Combos that need a gauge or awakening ("requires")
    are skipped - the drill does not set those up.
  * true combos found in the recordings (ajbot.combos), rebuilt the same way,
    the --per-char biggest per character with at least --min-steps steps.
    Both kinds must open from neutral: one the person started off a sidekick
    assist or a tag-in (the opponent already stunned) drops wherever that
    stun wears off, since the drill starts every attempt from the reset.

Each copy takes the next route and runs practice.py on it (a training-mode
match, d-pad Left resets between attempts) until it is down, given up, or
--max-attempts. Timing offsets carry over between rounds (models/
demo_routes.json). A route that landed goes into models/drilled_routes.json,
which the route reward in training reads: the policy is rewarded when its
own picks follow a landed route, so the bots learn to go for it in real
games. (Matches only press routes outright with play.py/play2.py --routes.)

Stop the farm first (it uses the same copies); stop the drill with Ctrl-C,
SIGTERM, or by creating drill.stop. Progress goes to drill.log, each copy's
practice output to drill-N.log.
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root + script folders on sys.path)
import argparse
import fcntl
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent   # the repo root
sys.path.insert(0, str(HERE))

from ajbot import combodb, demo, notation  # noqa: E402
from ajbot import dataset as D             # noqa: E402
from ajbot import routemine                # noqa: E402
from ajbot import agent as A               # noqa: E402

NOTATION_PATH = HERE / "models" / "drill_routes.json"
MINED_PATH = HERE / "models" / "mined_routes.json"
DRILLED_PATH = HERE / "models" / "drilled_routes.json"
STOP = HERE / "drill.stop"
_lock = threading.Lock()


def log(msg):
    line = time.strftime("[%H:%M:%S] ") + msg
    with _lock:
        print(line, flush=True)
        with open(HERE / "drill.log", "a") as f:
            f.write(line + "\n")


def menu_chars():
    os.environ.setdefault("AJ_INSTANCE", "0")
    import nextmatch as nm
    return {n for _, n, _ in nm.MENU}


FREE_MAX_S = 0.4         # longest the opponent may stand free inside a "combo"
HITSTUN = ("Damage", "Down", "Bound", "Stun")


def free_time(ticks, i0, i1):
    """Longest stretch (s) in ticks[i0..i1] where the opponent was not in
    hitstun, a knockdown or a stun - i.e. could have acted."""
    best, start = 0.0, None
    for i in range(i0, i1 + 1):
        t = ticks[i].get("t")
        act = (D._rec(ticks[i], "enemy") or {}).get("act") or ""
        if not isinstance(t, (int, float)):
            continue
        if act.startswith(HITSTUN):
            start = None
        else:
            start = t if start is None else start
            best = max(best, t - start)
    return best


def opened_from_neutral(ticks, i0):
    """Whether a combo starting at tick i0 can be replayed from the training
    reset: the opponent was not already in hitstun (a sidekick assist or a
    tag-in hit them first) and our fighter was not mid-tag. Both are things
    the drill cannot set up, and the chain drops where they wear off."""
    i = demo.lead_in(ticks, i0)
    for j in range(max(0, i - 3), i0 + 1):
        me = D._rec(ticks[j], "me") or {}
        if (me.get("act") or "").startswith("Sidekick"):
            return False
    en = D._rec(ticks[i], "enemy") or {}
    return not (en.get("act") or "").startswith(HITSTUN)


def _ticks(path):
    """A recording loaded the way routemine and demo read it: the human on
    "me" (a bot recording with a person as the opponent is swapped)."""
    ticks = D.load(str(path))
    if not ticks:
        return None
    D.calibrate_frame(ticks)
    if "botruns" in Path(path).parts:
        if not routemine._human_enemy(ticks):
            return None
        ticks = D.swap_sides(ticks)
    return ticks


def _true_combos(path, min_steps=8):
    """[route] for every true combo of at least min_steps steps in one recording."""
    from ajbot import combos as C
    try:
        ticks = _ticks(path)
        if not ticks:
            return []
        out = []
        for c in C.find(ticks):
            if c.hits < 4:
                continue
            route = demo.from_combo(ticks, c, source="combo")
            if not route or len(route["steps"]) < min_steps:
                continue
            free = free_time(ticks, c.start_i, c.end_i)
            if free > FREE_MAX_S or not opened_from_neutral(ticks, c.start_i):
                continue
            route["free_s"] = round(free, 2)
            route["src"] = {"file": str(path), "t0": ticks[c.start_i].get("t"),
                            "t1": ticks[c.end_i].get("t"), "dmg": round(c.dealt, 3)}
            out.append(route)
        return out
    except Exception:
        return []


def true_combos(corpus, min_steps, per_char, workers=2):
    """{char: [route]} the biggest true combos in the human recordings."""
    from concurrent.futures import ProcessPoolExecutor
    from functools import partial
    old = os.getcwd()
    os.chdir(corpus)
    try:
        paths = routemine.files()
        with ProcessPoolExecutor(max_workers=workers) as ex:
            found = [r for rs in ex.map(partial(_true_combos, min_steps=min_steps), paths)
                     for r in rs]
    finally:
        os.chdir(old)
    by = {}
    for r in sorted(found, key=lambda r: -(r.get("dealt") or 0)):
        labels = tuple(s["label"] for s in r["steps"])
        items = by.setdefault(r["char"], [])
        if len(items) < per_char and all(labels != tuple(s["label"] for s in o["steps"])
                                         for o in items):
            items.append(r)
    return by


def find_sources(corpus, workers=2):
    """{(char, steps): src} by mining the recordings under `corpus` again."""
    old = os.getcwd()
    os.chdir(corpus)
    try:
        chars = routemine.pick(routemine.mine(routemine.files(), workers), per_char=50)
    finally:
        os.chdir(old)
    return {(c, tuple(r["steps"])): r["src"] for c, rs in chars.items() for r in rs}


class _Span:
    """The bit of ajbot.combos.Combo that demo.from_combo reads."""
    def __init__(self, i0, i1, dealt):
        self.start_i, self.end_i, self.hits, self.dealt = i0, i1, None, dealt


def from_recording(src, name, corpus):
    """The human's own route (timing, stick, drift, start distance) from the
    recording span a mined route came from, or None."""
    path = Path(corpus) / src["file"]
    try:
        ticks = _ticks(path)
    except Exception as e:
        log(f"  {name}: cannot read {path}: {e}")
        return None
    if not ticks:
        return None
    ts = [t.get("t") if isinstance(t.get("t"), (int, float)) else -1 for t in ticks]
    i0 = next((i for i, t in enumerate(ts) if t >= src["t0"] - 0.02), None)
    i1 = max((i for i, t in enumerate(ts) if t <= src["t1"] + 0.02), default=None)
    if i0 is None or i1 is None or i1 <= i0:
        return None
    route = demo.from_combo(ticks, _Span(i0, i1, src.get("dmg") or 0.0), name=name,
                            source="mined")
    if route:
        route["src"] = src
        route["free_s"] = round(free_time(ticks, i0, i1), 2)
        route["neutral"] = opened_from_neutral(ticks, i0)
    return route


def candidates(a):
    """[route dict] to practice, longest first."""
    menu = menu_chars()
    out, seen = [], set()

    def add(route, source):
        steps = route["steps"]
        bad = [s["label"] for s in steps if s["label"] not in A.ACTION_MAP and s["label"] != "Tag"]
        if route["char"] not in menu:
            log(f"skip {route['name']}: {route['char']} is not on the character menu")
            return
        if bad:
            log(f"skip {route['name']}: no button for {bad}")
            return
        if a.only and route["char"] not in a.only.split(","):
            return
        key = (route["char"], tuple(s["label"] for s in steps))
        if key in seen:
            return
        seen.add(key)
        # Timing learned earlier carries over only onto the same steps and
        # gaps (a route rebuilt from its recording starts over).
        old = demo.get(route["name"])
        shape = [(s["label"], s.get("gap"), s.get("lead"), s.get("presses")) for s in steps]
        if (old and old.get("name") == route["name"] and old.get("learned")
                and [(s["label"], s.get("gap"), s.get("lead"), s.get("presses")) for s in old["steps"]] == shape):
            route["learned"] = old["learned"]
        route["drill_source"] = source
        out.append(route)

    sources = set(a.sources.split(","))
    try:
        written = json.loads(NOTATION_PATH.read_text()).get("routes") or []
    except (OSError, ValueError):
        written = []
    if "notation" not in sources:
        written = []
    for w in written:
        try:
            add(notation.route(w["notation"], w["char"], w.get("name")), "notation")
        except (notation.NotationError, KeyError) as e:
            log(f"skip {w.get('name')}: {e}")
    if "library" in sources:
        tiers = set(a.tiers.split(","))
        for char in combodb.chars_with_files(HERE / combodb.DIR):
            if a.only and char not in a.only.split(","):
                continue
            for c in combodb.load(char, HERE / combodb.DIR).get("combos") or []:
                if c.get("tier") not in tiers:
                    continue
                cid = c.get("id") or combodb.make_id(char, c["notation"])
                if c.get("requires"):
                    log(f"skip library {cid}: needs {c['requires']}")
                    continue
                try:
                    r = notation.route(c["notation"], char, f"lib {cid}: {c['notation']}")
                except (notation.NotationError, KeyError) as e:
                    log(f"skip library {cid}: {e}")
                    continue
                if c.get("partners"):
                    r["partners"] = list(dict.fromkeys((r.get("partners") or []) + c["partners"]))
                r["lib"] = {"char": char, "id": cid}
                add(r, "library")
    try:
        mined = json.loads(MINED_PATH.read_text()).get("chars") or {}
    except (OSError, ValueError):
        mined = {}
    if "mined" not in sources:
        mined = {}
    long = [(char, r) for char, items in mined.items() for r in items
            if len(r.get("steps") or []) >= a.min_steps
            and (not a.only or char in a.only.split(","))]
    found = {}
    if any(not r.get("src") for _, r in long):
        log("mined routes carry no source recording - mining the recordings again")
        found = find_sources(a.corpus)
    for char, r in long:
        name = f"drill {char}: {' > '.join(r['steps'])}"
        src = r.get("src") or found.get((char, tuple(r["steps"])))
        route = from_recording(src, name, a.corpus) if src else None
        if route and route["free_s"] > FREE_MAX_S:
            log(f"  {name}: not a true combo - the opponent was free for "
                f"{route['free_s']:.1f} s in the middle, skipped")
            continue
        if route and not route["neutral"]:
            log(f"  {name}: opened off a tag or assist the drill cannot set up, skipped")
            continue
        if route:
            got = [s["label"] for s in route["steps"]]
            log(f"  {name}: from {src['file']} - {len(got)} steps, gaps "
                f"{[s['gap'] for s in route['steps']]}")
        else:
            log(f"  {name}: no recording found, guessing {a.gap}-frame gaps")
            route = demo.from_labels(r["steps"], char, name, gap=a.gap, source="mined")
        add(route, "mined")
    for char, items in (true_combos(a.corpus, a.min_steps, a.per_char).items()
                        if "combo" in sources else ()):
        if a.only and char not in a.only.split(","):
            continue
        for r in items:
            r["name"] = f"combo {char}: {' > '.join(s['label'] for s in r['steps'])}"
            log(f"  {r['name']}: true combo from {r['src']['file']}, {r['dealt']:.2f} dmg")
            add(r, "combo")
    # People's routes first, then the library, then the longest mined ones.
    rank = {"notation": 0, "library": 1}
    out.sort(key=lambda r: (rank.get(r["drill_source"], 2), -len(r["steps"])))
    return out


def record(route):
    """Put a route that landed into drilled_routes.json (the follower's list)."""
    saved = demo.get(route["name"]) or route
    learned = saved.get("learned") or {}
    if not learned.get("landed"):
        return False
    labels = [s["label"] for s in saved["steps"]]
    entry = {"name": saved["name"], "steps": labels,
             "drilled": {"landed": learned["landed"], "attempts": learned.get("attempts"),
                         "source": route.get("drill_source"), "updated": round(time.time())}}
    lock = DRILLED_PATH.with_suffix(".lock")
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            data = json.loads(DRILLED_PATH.read_text())
        except (OSError, ValueError):
            data = {"version": 1, "chars": {}}
        items = [r for r in data["chars"].get(route["char"], []) if r.get("steps") != labels]
        items.append(entry)
        data["chars"][route["char"]] = items
        tmp = DRILLED_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(DRILLED_PATH)
    return True


def record_library(route):
    """Write a library combo's drill result into its combos/<Character>.json
    entry ("tested"). Returns the entry's new "tested", or None."""
    lib = route.get("lib")
    saved = demo.get(route["name"]) or {}
    learned = saved.get("learned") or {}
    if not lib or not learned.get("attempts"):
        return None
    tested = {"attempts": learned["attempts"], "landed": learned.get("landed", 0),
              "best_steps": learned.get("best", 0), "steps": len(route["steps"]),
              "dummy": "guards", "by": "bot", "date": time.strftime("%Y-%m-%d")}
    root = HERE / combodb.DIR
    with open(root / ".drill.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        data = combodb.load(lib["char"], root)
        for c in data.get("combos") or []:
            if (c.get("id") or combodb.make_id(lib["char"], c["notation"])) == lib["id"]:
                c["tested"] = tested
                combodb.save(data, root)
                return tested
    return None


def worker(n, work, a, procs):
    env = dict(os.environ, AJ_INSTANCE=str(n))
    while not STOP.exists():
        try:
            route = work.get_nowait()
        except queue.Empty:
            return
        argv = [a.python, str(HERE / "bot" / "practice.py"), "--route-json", json.dumps(route),
                "--instance", str(n), "--launch", "--stop-file", str(STOP),
                "--max-attempts", str(a.max_attempts), "--confirm", str(a.confirm),
                "--speed", str(a.speed)]
        log(f"copy {n}: {route['name']} ({route['char']}, {len(route['steps'])} steps)")
        t0 = time.time()
        with open(HERE / f"drill-{n}.log", "a") as out:
            p = subprocess.Popen(argv, cwd=HERE, env=env, stdout=out, stderr=subprocess.STDOUT,
                                 start_new_session=True)
            procs[n] = p
            p.wait()
            procs[n] = None
        saved = demo.get(route["name"]) or {}
        learned = saved.get("learned") or {}
        kept = record(route)
        record_library(route)
        log(f"copy {n}: done in {(time.time() - t0) / 60:.1f} min (exit {p.returncode}) - "
            f"best {learned.get('best', 0)}/{len(route['steps'])} steps, landed "
            f"{learned.get('landed', 0)}/{learned.get('attempts', 0)}"
            + (" -> drilled_routes.json" if kept else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--copies", type=int, default=4)
    ap.add_argument("--first", type=int, default=0, help="first game copy")
    ap.add_argument("--min-steps", type=int, default=8, help="shortest mined route to drill")
    ap.add_argument("--only", help="only these characters (comma list)")
    ap.add_argument("--rounds", type=int, default=1, help="passes over the route list")
    ap.add_argument("--max-attempts", type=int, default=150, help="per route per round")
    ap.add_argument("--confirm", type=int, default=5)
    ap.add_argument("--gap", type=int, default=14, help="first-guess frames between mined steps")
    ap.add_argument("--speed", type=float, default=100)
    ap.add_argument("--per-char", type=int, default=3,
                    help="true combos per character from the recordings")
    ap.add_argument("--corpus", default=str(HERE),
                    help="folder with runs/ and botruns/ (the human recordings)")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--sources", default="notation,mined,combo",
                    help="comma list of route sources: notation (models/drill_routes.json), "
                         "mined, combo (recordings), library (combos/*.json)")
    ap.add_argument("--tiers", default=",".join(combodb.TIERS),
                    help="library tiers to drill (with --sources library)")
    ap.add_argument("--list", action="store_true", help="print the routes, run nothing")
    a = ap.parse_args()

    routes = candidates(a)
    for r in routes:
        best = (r.get("learned") or {}).get("best")
        log(f"  {r['drill_source']:8s} {r['char']:16s} {len(r['steps']):2d} steps"
            + (f" (best so far {best})" if best else "") + f"  {r['name']}")
    if a.list or not routes:
        return
    STOP.unlink(missing_ok=True)
    procs = {}

    def on_signal(*_):
        STOP.touch()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    for rnd in range(a.rounds):
        work = queue.Queue()
        for r in routes:
            work.put(r)
        log(f"round {rnd + 1}/{a.rounds}: {len(routes)} routes on copies "
            f"{a.first}-{a.first + a.copies - 1}")
        threads = [threading.Thread(target=worker, args=(n, work, a, procs), daemon=True)
                   for n in range(a.first, a.first + a.copies)]
        for k, t in enumerate(threads):
            t.start()
            time.sleep(20 if k + 1 < len(threads) else 0)   # stagger the game boots
        for t in threads:
            while t.is_alive():
                t.join(5)
        if STOP.exists():
            break
    log("drill finished" if not STOP.exists() else "drill stopped")
    STOP.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
