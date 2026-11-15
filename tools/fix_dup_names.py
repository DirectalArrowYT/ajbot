"""One-off: fold '#N' mirror-copy names ('Bakugo#1') into the base name in
stats/matches.jsonl, so the stats and the GUI stop listing them as separate
characters. Writes stats/matches.jsonl.bak_pre_dup first.

    python fix_dup_names.py            # do it
    python fix_dup_names.py --dry      # only count
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root on sys.path and as cwd)
import json
import shutil
import sys
from pathlib import Path

PATH = Path("stats/matches.jsonl")


def strip(name):
    return name.split("#", 1)[0] if isinstance(name, str) else name


def fold_list(items):
    out = []
    for x in items:
        x = strip(x)
        if x not in out:
            out.append(x)
    return out


def fold_dict(d, how):
    out = {}
    for k, v in d.items():
        k2 = strip(k)
        if k2 not in out:
            out[k2] = v
        elif isinstance(v, (int, float)) and isinstance(out[k2], (int, float)):
            out[k2] = min(out[k2], v) if how == "min" else out[k2] + v
    return out


def fix(d):
    changed = False
    for key in ("team", "opponents", "team_ids", "opponent_ids"):
        if isinstance(d.get(key), list):
            new = fold_list(d[key])
            changed |= new != d[key]
            d[key] = new
    # A mirror has two fighters of the same name; keep the lower health (the
    # more damaged copy) for hp, and add up the per-character totals.
    for key, how in (("hp_left", "min"), ("hp_left_enemy", "min"), ("per_char", "sum")):
        if isinstance(d.get(key), dict) and any("#" in k for k in d[key]):
            if key == "per_char":
                out = {}
                for k, v in d[key].items():
                    k2 = strip(k)
                    if k2 not in out:
                        out[k2] = dict(v)
                    else:
                        for f in ("dealt", "taken", "point_s"):
                            out[k2][f] = round(out[k2].get(f, 0) + v.get(f, 0), 3)
                        out[k2]["ko"] = bool(out[k2].get("ko") or v.get("ko"))
                d[key] = out
            else:
                d[key] = fold_dict(d[key], how)
            changed = True
    return changed


def main():
    dry = "--dry" in sys.argv
    lines = PATH.read_text(encoding="utf-8").splitlines()
    out, n = [], 0
    for line in lines:
        if "#" in line and line.strip():
            d = json.loads(line)
            if fix(d):
                n += 1
                line = json.dumps(d)
        out.append(line)
    print(f"{n} of {len(lines)} match lines had '#N' names")
    if dry or not n:
        return
    shutil.copyfile(PATH, PATH.with_suffix(".jsonl.bak_pre_dup"))
    PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("rewritten; backup at stats/matches.jsonl.bak_pre_dup")


if __name__ == "__main__":
    main()
