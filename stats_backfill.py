"""Add the matches inside existing recordings to stats/matches.jsonl.

    python stats_backfill.py                 # every file in runs/ and botruns/
    python stats_backfill.py --dry           # print, write nothing
    python stats_backfill.py --runs "botruns/1789970386.*.jsonl"
    python stats_backfill.py --redo-since 1792871000   # re-measure existing lines

--redo-since re-replays the recordings behind lines that are already in the
stats file (matches starting at or after that epoch time) and replaces their
measured fields - teams, damage, KOs, events - with what the current
MatchTracker makes of them, keeping the result, times and model as recorded
live. The old file is kept as matches.jsonl.bak.

The live recorders now write a stats line per match as they go. Recordings
made before that still contain the matches - this replays them through the
same MatchTracker so they count too. Files already present in the stats file
(by recording path) are skipped, so it is safe to run repeatedly.

Only recordings made since the action wrappers went in carry the intro and
victory poses; older ones can still end a match on a team KO, and otherwise
come out as a single 'none' session (training mode, or no clear result).
"""
import argparse
import glob
import json
import sys
from pathlib import Path

from ajbot import dataset as D
from ajbot.match import MatchTracker, STATS_PATH, describe


def mode_of(tick, path):
    if tick.get("selfplay") and tick.get("side"):
        return "selfplay", tick.get("side")
    if tick.get("bot") or Path(path).resolve().parent.name == "botruns":
        return "bot", None
    return "human", None


def replay(path, write):
    ticks = D.load(path)
    if not ticks:
        return []
    mode, side = mode_of(ticks[0], path)
    tr = MatchTracker(mode=mode, side=side, write=write)
    tr.recording = Path(path)
    found = []
    last_t = None
    for t in ticks:
        now = t.get("t")
        if not isinstance(now, (int, float)):
            continue
        if last_t is not None and now - last_t > 20 and tr.stalled(now - last_t, now) == "ended":
            found.append(tr.commit({"backfill": True}))
        last_t = now
        me, en, mine, theirs = D.resolve(t)
        if tr.update(t.get("chars", {}), mine, theirs, me, now) == "ended":
            found.append(tr.commit({"backfill": True}))
    if tr.state == "live" and tr.t0 is not None:
        tr._end("none", "recording ended", last_t or 0.0)
        found.append(tr.commit({"backfill": True}))
    return [f for f in found if f]


REMEASURED = ("team", "opponents", "team_ids", "opponent_ids", "team_roster",
              "opponents_roster", "hp_left", "hp_left_enemy", "kos_taken", "kos_dealt",
              "damage_dealt", "damage_taken", "per_char", "events", "enemy_events")


def redo(since, min_s):
    lines = STATS_PATH.read_text(encoding="utf-8").splitlines()
    out, changed, cache = [], 0, {}
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        rec = d.get("recording")
        if not rec or (d.get("t") or 0) < since or not Path(rec).exists():
            out.append(line)
            continue
        if rec not in cache:
            cache[rec] = [s for s in replay(rec, write=False) if s.get("duration_s", 0) >= min_s]
        found = cache[rec]
        if not found:
            out.append(line)
            continue
        new = found[0]
        for k in REMEASURED:
            if k in new:
                d[k] = new[k]
        d["remeasured"] = True
        out.append(json.dumps(d))
        changed += 1
        print(f"{rec}: {d.get('result')}  dealt {d.get('damage_dealt')} taken {d.get('damage_taken')} "
              f"KOs {d.get('kos_dealt')}-{d.get('kos_taken')}  {'/'.join(d.get('team_roster') or [])}")
    bak = STATS_PATH.with_suffix(".jsonl.bak")
    bak.write_text("\n".join(lines) + "\n", encoding="utf-8")
    STATS_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"\nre-measured {changed} line(s); previous file kept as {bak}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="runs/*.jsonl,botruns/*.jsonl")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--min-s", type=float, default=30.0,
                    help="ignore 'matches' shorter than this")
    ap.add_argument("--redo-since", type=float, default=None,
                    help="re-measure stats lines for matches from this epoch time on")
    args = ap.parse_args()
    if args.redo_since is not None:
        return redo(args.redo_since, args.min_s)

    done = set()
    if STATS_PATH.exists():
        for line in STATS_PATH.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line).get("recording")
            except ValueError:
                continue
            if rec:
                done.add(str(Path(rec)))

    files = []
    for pat in args.runs.split(","):
        files += sorted(glob.glob(pat.strip()))
    total = 0
    for f in files:
        if str(Path(f)) in done:
            print(f"skip {f} (already in stats)")
            continue
        found = replay(f, write=False)
        found = [s for s in found if s.get("duration_s", 0) >= args.min_s]
        for s in found:
            print(f"{f}: " + describe(s))
        if not args.dry and found:
            STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with STATS_PATH.open("a", encoding="utf-8") as fh:
                for s in found:
                    fh.write(json.dumps(s) + "\n")
        total += len(found)
    print(f"\n{total} match(es) {'found' if args.dry else 'added to ' + str(STATS_PATH)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
