"""Record (state, action) pairs from live play into JSONL for behaviour cloning.

    python bot/record.py                       # record until Ctrl-C
    python bot/record.py --hz 20 --out runs/session1.jsonl
    python bot/record.py --show                # print snapshots, write nothing

Each line is one tick: every character's state keyed by role, plus the action
your character was executing. `cmd` is the engine's own ECommandId, so the
labels come from the game rather than from guessing what a button did.

Play normally while this runs - it only reads. It keeps going across
matches: each one gets its own runs/ file and a line in stats/matches.jsonl
(mode 'human'), so the command line never needs restarting.
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root + script folders on sys.path)
import argparse
import json
import sys
import time
from pathlib import Path

from ajbot.hook import Hook, HookError
from ajbot import sampler
from ajbot.unknowns import UnknownLog, UnknownProjectileLog, UnknownActionLog
from ajbot.match import MatchTracker, describe
from ajbot.heartbeat import Heartbeat, wait_for_battle


def _trim(rec, places=3):
    return {k: (round(v, places) if isinstance(v, float) else v)
            for k, v in rec.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hz", type=float, default=20.0, help="samples per second")
    ap.add_argument("--out", default=None, help="output .jsonl (default runs/<timestamp>.jsonl)")
    ap.add_argument("--every", type=int, default=6, help="resident chunk cadence, in pumps")
    ap.add_argument("--show", action="store_true", help="print only, do not write")
    ap.add_argument("--no-stats", action="store_true",
                    help="do not append match results to stats/matches.jsonl")
    ap.add_argument("--stop-file", default=None,
                    help="stop cleanly when this file appears (used by the GUI)")
    args = ap.parse_args()

    try:
        hook = Hook()
    except HookError as e:
        print(e)
        return 1

    if not hook.has_state():
        print("not in a battle yet - waiting (start a match whenever you like)")
        if not wait_for_battle(hook, args.stop_file):
            print("stopped before a match started")
            return 0

    sampler.install(hook, every_pumps=args.every)
    sampler.reset(hook)
    roster = sampler.Roster()
    unknowns = UnknownLog(source="record")
    proj_log = UnknownProjectileLog(source="record")
    action_log = UnknownActionLog(source="record")
    next_proj_poll = time.perf_counter()
    print(f"sampling every {args.every} pumps, logging at {args.hz} Hz")

    out = None
    matches = 0
    tracker = MatchTracker(mode="human", write=not (args.show or args.no_stats))
    stall_since = None

    def open_recording():
        if args.show:
            return None
        if args.out:
            base = Path(args.out)
            path = base if matches <= 1 else base.with_name(
                f"{base.stem}.m{matches}{base.suffix}")
        else:
            path = Path("runs") / f"{int(time.time())}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"  writing {path}")
        tracker.recording = path
        return path.open("w", encoding="utf-8")

    def finish_match():
        nonlocal out
        if out:
            out.close()
            out = None
        s = tracker.commit()
        if s:
            print("  " + describe(s))

    period = 1.0 / args.hz
    hb = Heartbeat(enabled=not args.show)
    n = 0
    last_cmd = None
    stalled = False
    roles_seen = set()

    def fmt(rec, key, spec=""):
        v = rec.get(key)
        return "-" if v is None else format(v, spec)

    try:
        nxt = time.perf_counter()
        while True:
            if args.stop_file and Path(args.stop_file).exists():
                try:
                    Path(args.stop_file).unlink()
                except OSError:
                    pass
                print("\nstop requested")
                break
            try:
                snap = sampler.snapshot(hook)
                stall_since = None
                if stalled:
                    print("  ...resumed")
                    stalled = False
            except HookError as e:
                stall_since = stall_since or time.time()
                if tracker.stalled(time.time() - stall_since) == "ended":
                    finish_match()
                # A pause stops the battle scripts, so nothing drains the queue.
                # That is not fatal - wait for play to resume.
                if not stalled:
                    print(f"  ...waiting ({e})")
                    stalled = True
                hb.idle("NO BATTLE (menus, loading or paused)", matches)
                nxt = time.perf_counter() + 0.25
                time.sleep(0.25)
                continue

            roles_seen.update(snap)
            roster.observe(snap)
            t = sampler.teams(snap, roster=roster)
            unknowns.observe(snap, t)
            action_log.observe(snap)
            now = time.perf_counter()
            if now >= next_proj_poll:
                proj_log.poll(hook)
                next_proj_poll = now + 5.0
            event = tracker.update_teams(sampler.dedupe(snap), t)
            if event == "started":
                matches += 1
                print(f"\n=== match {matches} started ===")
                if out:
                    out.close()
                roster.reset()
                roster.observe(snap)
                out = open_recording()
                hb.new_match()
            elif event == "ended":
                finish_match()
                print("  waiting for the next match")
            me = t["me"] if tracker.state == "live" else None
            if me:
                # Store deduped characters at reduced precision: the raw slot
                # dump was ~190 KB/s, most of it float digits and duplicate
                # states for the same fighter.
                # Key by character id, but keep the slot handle for the
                # unnamed ones - collapsing every id='?' under one key merged
                # distinct characters (and lost a team member).
                # Keyed by the dedupe key, which is the character id when it
                # has one and the slot handle otherwise. The pointers below
                # must use the same key: unnamed characters all share id='?',
                # so storing the id left `me` unresolvable on 37% of ticks.
                chars = {}
                for k, c in sampler.dedupe(snap).items():
                    rec = _trim(c)
                    rec["slot"] = k
                    chars[k] = rec
                enemy_key = (t["enemy"] or {}).get("key")
                mine_keys = [c.get("key") for c in t["mine"]]
                theirs_keys = [c.get("key") for c in t["theirs"]]
                # The hook's own state table never forgets a lua_State once
                # seen - across a long session this tick's chars would
                # otherwise carry every earlier match's fighters too (see
                # sampler.prune_stale).
                keep = {k for k in (me.get("key"), enemy_key) if k}
                keep.update(mine_keys)
                keep.update(theirs_keys)
                chars = sampler.prune_stale(chars, keep)
                rec = {"t": round(time.time(), 3), "chars": chars,
                       "me": me.get("key"), "enemy": enemy_key,
                       # live = currently running scripts; roster = the full
                       # line-up, including benched fighters that have gone
                       # quiet and so are not in the live lists.
                       "mine": mine_keys,
                       "theirs": theirs_keys,
                       "roster": roster.sides(),
                       "action": me.get("cmd"),
                       "lever": [me.get("lang"), me.get("lscale"), me.get("lon")]}
                if out:
                    out.write(json.dumps(rec) + "\n")
                    hb.wrote()
                n += 1
                cmd = me.get("cmd")
                if args.show or cmd != last_cmd:
                    en = t["enemy"] or {}
                    print(f"  [{n:6d}] hp={fmt(me,'hp','.3f')} d={fmt(me,'dist','7.1f')} "
                          f"{'gnd' if me.get('ground') else 'air'} "
                          f"act={cmd} lev={fmt(me,'lang','+6.1f')}@{fmt(me,'lscale','5.1f')} "
                          f"| {en.get('id','-')} hp={fmt(en,'hp','.3f')} act={en.get('cmd','-')}")
                    last_cmd = cmd

            if tracker.state == "live":
                hb.live(matches, tracker.recording if out else None,
                        (t["me"] or {}).get("key"), (t["enemy"] or {}).get("key"))
            else:
                hb.idle("WAITING for a match", matches)

            nxt += period
            delay = nxt - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.perf_counter()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        if tracker.close() == "ended":
            finish_match()
        print(f"{matches} match(es) recorded")
        states = 0
        try:
            states = sampler.states_seen(hook)
        except HookError:
            pass
        try:
            sampler.stop(hook)
        except HookError:
            pass
        if out:
            out.close()
        line = unknowns.summary()
        if line:
            print(line)
        unknowns.close()
        line = proj_log.summary()
        if line:
            print(line)
        proj_log.close()
        line = action_log.summary()
        if line:
            print(line)
        action_log.close()
        hook.close()
        print(f"{n} ticks recorded; character slots seen: {len(roles_seen)}")
        sides = roster.sides()
        print(f"  mine  : {', '.join(sorted(sides['mine'])) or '-'}")
        print(f"  theirs: {', '.join(sorted(sides['theirs'])) or '-'}")
        if len(sides["mine"]) > 3 or len(sides["theirs"]) > 3:
            print("  (more than 3 a side - this session spans multiple matches;"
                  " record one match per run for a clean roster)")
        print(f"distinct lua_States seen: {states}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
