"""How strong is the bot, per policy version? Reads stats/matches.jsonl.

    python benchmark.py                 # every policy version, vs CPU / human / self-play
    python benchmark.py --since 2026-10-01

A policy version is the policy.pkl file's modification time at the time of
the match (recorded as `model_mtime` from now on; older rows are grouped by
the retrain dates in models/prev). Results the tracker could not have got
right (train.suspect_result: a "win" with nothing dealt) are left out and
counted separately.

The numbers worth watching after a retrain:
  win%        wins / (wins + losses), suspect rows excluded
  dealt-taken mean health bars dealt minus taken per match (3 bars a team)
  combo       mean best-combo damage per match
"""
import argparse
import collections
import json
import time
from pathlib import Path

from train import suspect_result


def retrain_times():
    """Policy versions as [(start_time, label)] from models/prev and the current file."""
    out = []
    prev = Path("models/prev")
    for f in sorted(prev.glob("policy-*.pkl")):
        stamp = f.stem.split("-", 1)[1]                 # file was moved aside at this time
        try:
            t = time.mktime(time.strptime(stamp, "%Y%m%d-%H%M%S"))
        except ValueError:
            continue
        out.append(t)
    cur = Path("models/policy.pkl")
    marks = sorted(set(out))
    if cur.exists():
        marks.append(cur.stat().st_mtime)
    return sorted(set(marks))


def version_of(t, marks):
    v = None
    for m in marks:
        if t >= m:
            v = m
    return time.strftime("%m-%d %H:%M", time.localtime(v)) if v else "before"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="YYYY-MM-DD")
    a = ap.parse_args()
    since = time.mktime(time.strptime(a.since, "%Y-%m-%d")) if a.since else 0
    marks = retrain_times()
    rows = [json.loads(l) for l in open("stats/matches.jsonl", encoding="utf-8") if l.strip()]
    agg = collections.defaultdict(lambda: {"w": 0, "l": 0, "sus": 0, "net": [], "best": []})
    for r in rows:
        t = r.get("t", 0)
        if t < since or r.get("result") not in ("win", "loss"):
            continue
        mode = r.get("mode") or "?"
        key = (version_of(t, marks), mode)
        g = agg[key]
        if suspect_result(r):
            g["sus"] += 1
            continue
        g["w" if r["result"] == "win" else "l"] += 1
        g["net"].append((r.get("damage_dealt") or 0) - (r.get("damage_taken") or 0))
        cs = r.get("combo_stats") or {}
        if isinstance(cs, dict) and cs.get("max_damage") is not None:
            g["best"].append(cs["max_damage"])
    print(f"{'policy from':<14}{'mode':<10}{'W-L':>8}{'win%':>7}{'dealt-taken':>13}{'best combo':>12}{'suspect':>9}")
    for (v, mode), g in sorted(agg.items()):
        n = g["w"] + g["l"]
        if not n and not g["sus"]:
            continue
        net = sum(g["net"]) / len(g["net"]) if g["net"] else 0.0
        best = sum(g["best"]) / len(g["best"]) if g["best"] else float("nan")
        print(f"{v:<14}{mode:<10}{g['w']:>4}-{g['l']:<3}{(100 * g['w'] / n if n else 0):>6.0f}%"
              f"{net:>+13.2f}{best:>12.3f}{g['sus']:>9}")
    print("\nmode: bot = play.py (vs CPU or a human), selfplay = play2.py, human = your recordings")


if __name__ == "__main__":
    main()
