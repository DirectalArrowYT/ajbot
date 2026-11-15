"""The combo book: what continues a combo, per character, mined from recordings.

Every recording holds combos by the game's own rule (ajbot.chain): human play
in runs/, the bot's play in botruns/, and - from either - the OPPONENT's
combos on us, which is how a Lv5 CPU's routes get learned too. Each combo is
cut into segments, one per fighter, and each segment is the sequence of
commands that fighter used while the hits kept coming.

From those this builds, per character:

  next     (previous command, current command) -> the command that followed,
           with how many hits the segment went on to reach. The live policy
           uses it to pick the continuation that historically keeps a combo
           going longest, instead of whatever the model rates highest.
  routes   the best complete segments seen (command sequence, hits, damage).
  seg      how long this character's segments usually get before the combo
           drops - the prior for when it should tag the combo onward.

    python combobook.py                       # build models/combobook.json
    python combobook.py --show Eraserhead     # print one character's page
"""
import collections
import glob
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from . import dataset as D, tickcache
from .chain import ChainTracker
from .parallel import Progress, safe_worker_count
from .roster import base_name, INDEX

BOOK_PATH = Path("models") / "combobook.json"
IGNORE = {None, "None", "DamageEscape"}
# Marker for a Change Combo record in _segments_from (see there).
TAG = "__tag__"
# Minimum ticks into an action before a human's tag from it counts as a
# deliberate tag point rather than a buffered press from the previous move.
TAG_MIN_ACT_N = 3


def _segments_from(ticks):
    """Yield (char, [cmds], hits, damage, source_side) for every segment."""
    chain = ChainTracker()
    # Per open combo: the command sequence of its CURRENT segment.
    seqs = {}
    last_cmd = {}

    def cmd_of(key, tick):
        return ((tick.get("chars") or {}).get(key) or {}).get("cmd")

    def act_of(key, tick):
        return ((tick.get("chars") or {}).get(key) or {}).get("act")

    last_act = {}

    for t in ticks:
        me, en, mine, theirs = D.resolve(t)
        now = t.get("t")
        if not isinstance(now, (int, float)):
            continue
        before = {id(c): (c, len(c.segments)) for c in chain.open.values()}
        closed = chain.update(t.get("chars") or {}, mine, theirs, me, en, now)

        for c, side, attacker in ((chain.by_us, "us", me), (chain.on_us, "them", en)):
            if c is None or not attacker:
                continue
            state = seqs.setdefault(id(c), {"seg": 0, "cmds": [], "c": c, "side": side})
            if len(c.segments) != state["seg"] and state["cmds"]:
                # A tag handed the combo on: the previous segment is complete.
                prev = c.segments[state["seg"] - 1] if state["seg"] >= 1 else None
                if prev:
                    yield prev[0], state["cmds"], prev[1], prev[2], side
                state["cmds"] = []
            state["seg"] = len(c.segments)
            cmd = cmd_of(attacker, t)
            if cmd not in IGNORE and (not state["cmds"] or state["cmds"][-1] != cmd):
                if last_cmd.get(attacker) != cmd or not state["cmds"]:
                    state["cmds"].append(cmd)
            last_cmd[attacker] = cmd
            # A jump or dash mid-combo is part of the route even though the
            # game's `cmd` never reports it - read it off the act that starts.
            act = act_of(attacker, t)
            if act != last_act.get(attacker):
                tok = D.MOVE_LABELS.get(act)
                if tok and state["cmds"] and state["cmds"][-1] != tok:
                    state["cmds"].append(tok)
            last_act[attacker] = act

        # Combos that just ended: their last segment is complete too.
        for cid in list(seqs):
            st = seqs[cid]
            if st["c"] in chain.finished:
                if st["c"].segments and st["cmds"]:
                    s = st["c"].segments[-1]
                    yield s[0], st["cmds"], s[1], s[2], st["side"]
                del seqs[cid]
        # Change Combos into combos that just closed: the outgoing fighter's
        # action at the tag and how far the chain went on afterwards. Yielded
        # with a TAG marker in the cmds slot so the two record kinds share one
        # generator (see _build_one_raw).
        for c in chain.finished:
            side = "us" if c.victim in theirs else "them" if c.victim in mine else None
            if side is None or not c.tag_points:
                continue
            for p in c.tag_points:
                p = dict(p, dmg_after=round(max(0.0, c.damage - p.get("dmg_at", c.damage)), 4))
                yield p["char"], TAG, p, c.hits - p["hits_at"], side
        chain.drain()


def _build_one(args):
    """Cached per recording and min_hits (ajbot.tickcache) - see _build_one_raw."""
    from . import tickcache
    path, min_hits = args
    # version 2: tag points (Change Combo mining) joined the per-file result.
    # A recording can vanish between listing and mining (the farm archives
    # or moves files while a retrain runs): skip it rather than fail the build.
    if not Path(path).exists():
        return None
    return tickcache.cached_pickle(path, f"combobook{min_hits}",
                                   lambda p: _build_one_raw((p, min_hits)), version=3)


def _build_one_raw(args):
    """Worker: one file's segment stats, flattened for pickling - a nested
    defaultdict with lambda factories (see build()) cannot cross a process
    boundary. Module-level so it can run in a subprocess.

    Returns (nexts, routes, seglens, n_seg) where nexts is a flat list of
    (char, key, next_cmd, count, hits) tuples instead of the nested shape.
    """
    path, min_hits = args
    try:
        ticks = D.load(path)
    except (OSError, ValueError):
        return [], [], [], 0, [], []

    nexts = collections.defaultdict(lambda: collections.defaultdict(
        lambda: collections.defaultdict(lambda: [0, 0.0])))
    routes = []           # (char, seq, hits, dmg, side)
    seglens = []          # (char, hits)
    tagouts = []          # (char, act, act_n, stall, hits_after, side)
    handoffs = []         # (out, in, air, opener, hits_after, dmg_after)
    n_seg = 0
    for char, cmds, hits, dmg, side in _segments_from(ticks):
        if isinstance(char, str) and char.startswith("Pl"):
            char = base_name(char)
        elif char not in INDEX:
            char = None           # unnamed slot handle: several fighters mixed
        if not char or not cmds:
            continue
        if cmds == TAG:
            # A Change Combo: `hits` is the tag point dict, `dmg` the hits the
            # chain went on to land after it.
            p, after = hits, dmg
            inc = p.get("in")
            if isinstance(inc, str) and inc.startswith("Pl"):
                inc = base_name(inc)
            if inc and inc in INDEX:
                handoffs.append((char, inc, p.get("air"), p.get("opener"),
                                 int(after), float(p.get("dmg_after") or 0.0)))
            if p.get("act") and isinstance(p.get("act_n"), (int, float)) \
                    and p["act_n"] >= TAG_MIN_ACT_N:
                tagouts.append((char, p["act"], int(p["act_n"]), float(p["stall"]),
                                int(after), side))
            continue
        n_seg += 1
        seglens.append((char, hits))
        if hits >= min_hits:
            routes.append((char, tuple(cmds[:10]), hits, round(dmg, 4), side))
        seq = [None, None] + cmds
        for i in range(2, len(seq) - 1):
            key = f"{seq[i - 1]}|{seq[i]}"
            arm = nexts[char][key][seq[i + 1]]
            arm[0] += 1
            arm[1] += hits
    nexts_flat = [(char, key, nxt, arm[0], arm[1])
                  for char, kv in nexts.items()
                  for key, nv in kv.items()
                  for nxt, arm in nv.items()]
    return nexts_flat, routes, seglens, n_seg, tagouts, handoffs


def build(files, min_hits=2):
    """Each file's segment mining is independent, so - like train.py's
    feature build and mine.py's combo scan - this runs across a process
    pool sized against free RAM, with a progress line, and the per-file
    results are merged afterward."""
    nexts = collections.defaultdict(lambda: collections.defaultdict(
        lambda: collections.defaultdict(lambda: [0, 0.0])))
    routes = collections.defaultdict(list)
    seglens = collections.defaultdict(list)
    tagouts = collections.defaultdict(list)   # char -> [(act, act_n, stall, after, side)]
    handoff = {}          # "out|in|air" -> [n, hits_after_sum, dmg_after_sum]
    opener = {}           # "in|air" -> {cmd: [n, hits_after_sum]}
    n_seg = 0
    n_gone = 0

    if files:
        jobs = [(f, min_hits) for f in files]
        sizes = tickcache.src_sizes(files)
        n_workers = safe_worker_count(sizes)
        progress = Progress(len(files), "mining combos")

        def handle(result):
            nonlocal n_seg, n_gone
            if result is None:
                n_gone += 1
                progress.update()
                return
            nexts_flat, r, s, n = result[:4]
            tg = result[4] if len(result) > 4 else []
            for char, key, nxt, count, hits in nexts_flat:
                arm = nexts[char][key][nxt]
                arm[0] += count
                arm[1] += hits
            for char, seq, hits, dmg, side in r:
                routes[char].append((seq, hits, dmg, side))
            for char, hits in s:
                seglens[char].append(hits)
            for char, act, act_n, stall, after, side in tg:
                tagouts[char].append((act, act_n, stall, after, side))
            for out, inc, air, op, after, dmg in (result[5] if len(result) > 5 else []):
                ak = "air" if air else "gnd" if air is not None else "?"
                e = handoff.setdefault(f"{out}|{inc}|{ak}", [0, 0.0, 0.0])
                e[0] += 1
                e[1] += after
                e[2] += dmg
                if op:
                    o = opener.setdefault(f"{inc}|{ak}", {}).setdefault(op, [0, 0.0])
                    o[0] += 1
                    o[1] += after
            n_seg += n
            progress.update()

        if len(jobs) > 1 and n_workers > 1:
            with ProcessPoolExecutor(max_workers=n_workers) as ex:
                # as_completed, not map(): map yields in submission order, so
                # one slow file at the front would stall every progress
                # update behind it even while later files keep finishing.
                futures = [ex.submit(_build_one, j) for j in jobs]
                for fut in as_completed(futures):
                    try:
                        handle(fut.result())
                    except FileNotFoundError:
                        handle(None)      # vanished mid-read
        else:
            for j in jobs:
                try:
                    handle(_build_one(j))
                except FileNotFoundError:
                    handle(None)
        if n_gone:
            print(f"  {n_gone} recording(s) vanished during mining (moved/archived) - skipped")

    # Change Combo hand-offs (ajbot.tagbook): per (outgoing, incoming,
    # victim airborne) the mean hits and damage the chain went on to land,
    # and per incoming fighter the mean hits after each opener command.
    book = {"segments": n_seg, "chars": {},
            "handoff": {k: [e[0], round(e[1] / e[0], 2), round(e[2] / e[0], 4)]
                        for k, e in handoff.items()},
            "opener": {k: {c: [e[0], round(e[1] / e[0], 2)] for c, e in v.items()}
                       for k, v in opener.items()}}
    for char in set(seglens) | set(routes) | set(tagouts):
        lens = sorted(seglens[char])
        # Tag points: per action the outgoing fighter was in when a partner
        # was tagged in mid-combo - how often, how far into the action (ticks
        # at the sampler's rate), how long after the last hit, and how many
        # hits the chain went on to land. The live TagPolicy treats an action
        # seen here 2+ times with the chain continuing as a learned cue to
        # Change Combo from it; "any" pools every action for the timing.
        tagout = {}
        for act, act_n, stall, after, side in tagouts[char]:
            for key in (act, "any"):
                e = tagout.setdefault(key, {"count": 0, "kept": 0, "after": 0.0,
                                            "act_n": [], "stall": []})
                e["count"] += 1
                e["kept"] += 1 if after > 0 else 0
                e["after"] += after
                if key == act:
                    e["act_n"].append(act_n)
                e["stall"].append(stall)
        for e in tagout.values():
            n = e["count"]
            e["after"] = round(e["after"] / n, 2)
            ns, ss = sorted(e["act_n"]), sorted(e["stall"])
            e["act_n"] = [ns[len(ns) // 10], ns[len(ns) // 2]] if ns else None   # p10, p50
            e["stall"] = round(ss[len(ss) // 2], 2) if ss else None             # p50
        best = {}
        for seq, hits, dmg, side in routes[char]:
            b = best.setdefault(seq, {"seq": list(seq), "count": 0, "hits": 0, "dmg": 0.0, "from": set()})
            b["count"] += 1
            b["hits"] = max(b["hits"], hits)
            b["dmg"] = max(b["dmg"], dmg)
            b["from"].add(side)
        top = sorted(best.values(), key=lambda b: (-b["hits"], -b["count"]))[:12]
        for b in top:
            b["from"] = sorted(b["from"])
        book["chars"][char] = {
            "segments": len(lens),
            "seg_p50": lens[len(lens) // 2] if lens else 0,
            "seg_p75": lens[(3 * len(lens)) // 4] if lens else 0,
            "routes": top,
            "next": {k: {nxt: [a[0], round(a[1] / a[0], 2)] for nxt, a in v.items()}
                     for k, v in nexts[char].items()},
            "tagout": tagout,
        }
    return book


def load(path=None):
    """The book: by default the active model folder's copy (ajbot.datadir)."""
    from . import datadir
    path = path or datadir.path("combobook.json")
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class ComboPolicy:
    """Picks the combo continuation the book says goes longest.

    Only active while WE are comboing (the opponent is in hitstun in a chain
    we started) - neutral play stays with the model. Overrides the model's
    pick when the book has a continuation seen at least `min_count` times that
    historically led to clearly longer segments than the model's choice.
    """

    def __init__(self, book, min_count=3, margin=1.0):
        self.chars = (book or {}).get("chars", {})
        self.min_count = min_count
        self.margin = margin
        self.hist = {}                    # our key -> [prev, cur] (commands + movement)
        self.last_act = {}                # our key -> last act seen
        self.last_cmd = {}                # our key -> last cmd seen
        self.overrides = 0

    def seg_prior(self):
        """Per-character segment targets for TagPolicy (a bit under the median)."""
        return {c: max(3, min(10, int(round(p["seg_p50"] * 0.9))))
                for c, p in self.chars.items() if p.get("segments", 0) >= 5}

    def tag_prior(self, min_count=2):
        """Per character: the actions players Change Combo out of, for
        TagPolicy - {char: {act: {"count", "kept", "after", "act_n", "stall"}}}
        keeping only actions seen `min_count`+ times where the chain usually
        went on (kept > count/2). Books built before tag mining have no
        "tagout" page and yield {} - the policy then falls back to its
        segment-target rule."""
        out = {}
        for c, p in self.chars.items():
            page = {}
            for act, e in (p.get("tagout") or {}).items():
                if e.get("count", 0) >= min_count and e.get("kept", 0) * 2 >= e["count"]:
                    page[act] = e
            if page:
                out[c] = page
        return out

    def observe(self, me):
        key = (me or {}).get("key")
        if not key:
            return
        h = self.hist.setdefault(key, [None, None])
        # Movement tokens (Jump / Dash), read off the act that just started -
        # the same tokens combobook mines into routes.
        act = (me or {}).get("act")
        if act != self.last_act.get(key):
            self.last_act[key] = act
            tok = D.MOVE_LABELS.get(act)
            if tok and h[1] not in (None, tok):
                h[0], h[1] = h[1], tok
        cmd = (me or {}).get("cmd")
        if cmd in IGNORE:
            return
        if cmd != self.last_cmd.get(key):
            self.last_cmd[key] = cmd
            if cmd != h[1]:
                h[0], h[1] = h[1], cmd

    def suggest(self, me, model_pick, available):
        """A better continuation than `model_pick`, or None to keep it."""
        key = (me or {}).get("key")
        from .roster import move_char
        page = self.chars.get(move_char(me, key))
        if not page:
            return None
        h = self.hist.get(key, [None, None])
        arms = page["next"].get(f"{h[0]}|{h[1]}") or page["next"].get(f"None|{h[1]}")
        if not arms:
            return None
        good = [(nxt, cnt, mean) for nxt, (cnt, mean) in arms.items()
                if cnt >= self.min_count and available(nxt)]
        if not good:
            return None
        nxt, cnt, mean = max(good, key=lambda x: x[2])
        model_mean = arms.get(model_pick, [0, 0.0])[1]
        if nxt != model_pick and mean >= model_mean + self.margin:
            self.overrides += 1
            return nxt
        return None


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", default="runs/*.jsonl,botruns/*.jsonl")
    from . import datadir
    ap.add_argument("--out", default=str(datadir.out("combobook.json")))
    ap.add_argument("--show", default=None, help="print one character's page")
    args = ap.parse_args(argv)

    if args.show:
        book = load(args.out)
        page = (book or {}).get("chars", {}).get(args.show)
        if not page:
            print(f"no page for {args.show}")
            return 1
        print(f"{args.show}: {page['segments']} segments, typical length "
              f"{page['seg_p50']} hits (p75 {page['seg_p75']})")
        for r in page["routes"]:
            print(f"  {r['hits']:>2} hits  x{r['count']:<3} {' > '.join(r['seq'])}"
                  f"   ({'/'.join(r['from'])})")
        if page.get("tagout"):
            print("  Change Combo tag points (action the fighter was in when a "
                  "partner came in mid-combo):")
            for act, e in sorted(page["tagout"].items(), key=lambda kv: -kv[1]["count"]):
                print(f"    {act:<22} x{e['count']:<3} chain kept {e['kept']:<3} "
                      f"+{e['after']} hits after  act_n p10/p50 {e['act_n']}  "
                      f"stall p50 {e['stall']}s")
        return 0

    files = []
    for pat in args.runs.split(","):
        files += sorted(glob.glob(pat.strip()))
    print(f"mining {len(files)} recordings...")
    book = build(files)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(book, indent=1), encoding="utf-8")
    chars = book["chars"]
    print(f"{book['segments']} combo segments, {len(chars)} characters -> {args.out}")
    for c, p in sorted(chars.items(), key=lambda kv: -kv[1]["segments"])[:15]:
        best = p["routes"][0] if p["routes"] else None
        print(f"  {c:<18} {p['segments']:>4} segs  typical {p['seg_p50']} hits"
              + (f"  best {best['hits']}: {' > '.join(best['seq'][:6])}" if best else ""))
    return 0
