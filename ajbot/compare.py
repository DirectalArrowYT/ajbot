"""Head-to-head comparison of two models from bot-vs-bot matches.

bot/play2.py --model NEW --model-b OLD --matches 10 tags every stats line
with a run id, stops after 10 matches and writes one line to
stats/compare.jsonl with what this module works out. That is the local
stand-in for the server's league eval: anyone with the game can check
whether a retrain beat the model it replaced.

    python -m ajbot.compare            # the latest comparison again
    python -m ajbot.compare --run ID   # rebuild one from stats/matches.jsonl
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

MATCHES = Path("stats/matches.jsonl")
COMPARE = Path("stats/compare.jsonl")


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def _binom_p(k, n):
    """Two-sided exact binomial test against a coin flip."""
    if n == 0:
        return 1.0
    lo = min(k, n - k)
    tail = sum(math.comb(n, i) for i in range(lo + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def per_model(rows):
    """rows: stats/matches.jsonl dicts (one per bot per match)."""
    out = {}
    for r in rows:
        m = r.get("model") or "?"
        d = out.setdefault(m, {"rows": []})
        d["rows"].append(r)
    for m, d in out.items():
        rs = d.pop("rows")
        res = [r.get("result") for r in rs]
        cs = [r.get("combo_stats") or {} for r in rs]
        d.update({
            "matches": len(rs),
            "wins": res.count("win"),
            "losses": res.count("loss"),
            "draws": len(rs) - res.count("win") - res.count("loss"),
            "damage_dealt": _mean(r.get("damage_dealt") for r in rs),
            "damage_taken": _mean(r.get("damage_taken") for r in rs),
            "kos_dealt": _mean(r.get("kos_dealt") for r in rs),
            "kos_taken": _mean(r.get("kos_taken") for r in rs),
            "hp_left": _mean(sum((r.get("hp_left") or {}).values()) for r in rs),
            "combos": _mean(c.get("combos", 0) for c in cs),
            "max_hits": _mean(c.get("max_hits") for c in cs if c.get("combos")),
            "change_combos": _mean(c.get("change_combos", 0) for c in cs),
            "duration_s": _mean(r.get("duration_s") for r in rs),
        })
    return out


def verdict(a, b, stats):
    """One sentence on whether `a` beat `b`, honest about small samples."""
    sa = stats.get(a) or {}
    sb = stats.get(b) or {}
    wa, wb = sa.get("wins", 0), sb.get("wins", 0)
    n = wa + wb
    na, nb = Path(a).stem, Path(b).stem
    if n == 0:
        return "no decided matches", None
    p = _binom_p(wa, n)
    if p < 0.05:
        best = na if wa > wb else nb
        text = f"{best} is better ({max(wa, wb)}-{min(wa, wb)}, p={p:.2f})"
    elif wa == wb:
        text = f"dead even ({wa}-{wb})"
    else:
        lead = na if wa > wb else nb
        text = (f"{lead} leads {max(wa, wb)}-{min(wa, wb)}, but {n} matches "
                f"cannot tell them apart yet (p={p:.2f}); play more")
    return text, round(p, 3)


def build(run, a, b, rows):
    stats = per_model(rows)
    text, p = verdict(a, b, stats)
    return {"t": round(time.time(), 1), "run": run, "a": a, "b": b,
            "matches": max((s["matches"] for s in stats.values()), default=0),
            "models": stats, "verdict": text, "p": p}


def save(entry, path=COMPARE):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


ROWS = [("wins", "wins", "{:d}"), ("losses", "losses", "{:d}"),
        ("draws", "draws", "{:d}"),
        ("damage_dealt", "damage dealt", "{:.2f}"),
        ("damage_taken", "damage taken", "{:.2f}"),
        ("kos_dealt", "KOs dealt", "{:.2f}"), ("kos_taken", "KOs taken", "{:.2f}"),
        ("hp_left", "health left", "{:.2f}"), ("combos", "combos", "{:.2f}"),
        ("max_hits", "longest combo", "{:.1f}"),
        ("change_combos", "change combos", "{:.2f}")]


def report(entry):
    a, b = entry["a"], entry["b"]
    sa, sb = entry["models"].get(a, {}), entry["models"].get(b, {})
    na, nb = Path(a).stem, Path(b).stem
    w = max(len(na), len(nb), 8)
    lines = [f"=== {entry['matches']} matches: {na} vs {nb} (per match) ===",
             f"  {'':<14} {na:>{w}} {nb:>{w}}"]
    for key, label, fmt in ROWS:
        va, vb = sa.get(key), sb.get(key)
        f = lambda v: "-" if v is None else fmt.format(v)
        lines.append(f"  {label:<14} {f(va):>{w}} {f(vb):>{w}}")
    lines.append(f"  -> {entry['verdict']}")
    return "\n".join(lines)


def load_rows(run, path=MATCHES):
    rows = []
    if path.exists():
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get("run") == run:
                rows.append(r)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", help="rebuild this run from stats/matches.jsonl")
    a = ap.parse_args()
    if a.run:
        rows = load_rows(a.run)
        models = sorted({r.get("model") for r in rows if r.get("model")})
        if len(models) != 2:
            sys.exit(f"run {a.run}: expected two models, found {models or 'none'}")
        print(report(build(a.run, models[0], models[1], rows)))
        return
    if not COMPARE.exists():
        sys.exit("no comparisons yet: run bot vs bot with --model-b and --matches")
    last = COMPARE.read_text(encoding="utf-8").strip().splitlines()[-1]
    print(report(json.loads(last)))


if __name__ == "__main__":
    main()
