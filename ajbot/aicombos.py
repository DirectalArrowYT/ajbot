"""The game's own CPU combos, as routes the bot can follow.

Every character folder in the FModel export carries AIComboList.json: the
combos the in-game CPU runs, written by the developers. Each entry is a short
command list (Melee x4, Unique1, Plus Ultra ...) plus a LINK list naming the
entries that may follow it (連動コンボリスト) - Bakugo's runs Melee x2 ->
Unique1 -> air Melee x3 -> Counter + Unique2, for example. Following the
links gives full-length combo routes for every character, including the 100+
that no human route or combo-book page covers.

    python -m ajbot.aicombos                 # writes models/ai_routes.json
    python -m ajbot.aicombos --show Pl02

The steps are decision labels (the names agent.ACTION_MAP presses), so
routes.RouteFollower can follow them like the hand-written routes. They rank
below those: a human route always wins, and the combo book still overrides a
CPU step whenever human play says another continuation went better (see
routes.RouteFollower.suggest).
"""
import argparse
import json
import re
from pathlib import Path

PATH = Path("models") / "ai_routes.json"
VERSION = 1
from .framedata import GAMEDATA as _GAMEDATA   # ajbot_local.json: game.gamedata
EXPORT = _GAMEDATA / "DB/Character/Player"

# AI command -> decision label. Anything else ends the route at that point
# (partner calls, team Plus Ultras and NPC actions have no single label).
CMD = {
    "Melee": "Melee", "ExMelee": "ExMelee",
    "Unique1": "Unique1", "Unique2": "Unique2",
    "ExUnique1": "ExUnique1", "ExUnique2": "ExUnique2",
    "UniqueCharge1": "UniqueCharge1", "UniqueCharge2": "UniqueCharge2",
    "PlusUltra1": "PlusUltra1", "PlusUltra2": "PlusUltra2",
    "ExtremeAction": "ExtremeAction", "Unguardable": "Unguardable",
    "Awaking": "Awaking", "Jump": "Jump",
    "Dash": "Dash", "DashCancel": "Dash",
}
SKIP = {"None", "Guard"}       # no press of their own inside a route
MAX_LINKS = 6                  # combos chained into one route
MAX_ROUTES = 24                # per character folder
MIN_STEPS = 3
MAX_LOOP = 3


def _load(p):
    try:
        b = Path(p).read_bytes()
    except OSError:
        return None
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return json.loads(b.decode(enc))
        except (UnicodeDecodeError, ValueError):
            continue
    return None


def _lv5(levels):
    return isinstance(levels, list) and 5 in levels


def _steps(combo):
    """Labels of one combo's parts, cut at the first part with no label."""
    out = []
    for p in combo.get("コンボパーツ") or []:
        cmd = str(p.get("コマンド") or "None").replace("ECommandId::", "")
        if cmd in SKIP:
            continue
        lab = CMD.get(cmd)
        if lab is None:
            return out, False
        try:
            loops = int(float(p.get("ループ回数") or 0))
        except (TypeError, ValueError):
            loops = 0
        out.extend([lab] * (1 + max(0, min(loops, MAX_LOOP))))
    return out, True


def routes_of(data):
    combos = [c for c in (data or {}).get("AIコンボリスト") or []
              if isinstance(c, dict) and not c.get("トレーニング固定攻撃専用コンボ")
              and not str(c.get("コンボ名", "")).startswith("トレーニング用")
              and _lv5(c.get("コンボ選択可能なAIレベル"))]
    by_name = {}
    for c in combos:
        by_name.setdefault(c.get("コンボ名"), c)
    found = []

    def walk(c, steps, names, awake):
        st, whole = _steps(c)
        steps = steps + st
        names = names + [c.get("コンボ名")]
        awake = awake or bool(c.get("覚醒時限定"))
        nxt = []
        if whole and len(names) < MAX_LINKS:
            for ln in c.get("連動コンボリスト") or []:
                n = (ln or {}).get("連動コンボ名")
                if n and n in by_name and n not in names and _lv5(ln.get("連動可能なAIレベル") or [5]):
                    nxt.append(by_name[n])
        if not nxt:
            found.append((steps, names, awake))
        for n in nxt:
            walk(n, steps, names, awake)

    for c in combos:
        if not c.get("連動コンボ専用"):
            walk(c, [], [], False)
    # Longest first; drop duplicates and routes that are a prefix of a kept one.
    found.sort(key=lambda r: (-len(r[0]), r[2]))
    kept = []
    for steps, names, awake in found:
        if len(steps) < MIN_STEPS:
            continue
        if any(k["steps"][:len(steps)] == steps and bool(k.get("requires")) == awake
               for k in kept):
            continue
        r = {"name": "CPU: " + " > ".join(names), "steps": steps, "ai": True}
        if awake:
            r["requires"] = {"awake": 1}
        kept.append(r)
        if len(kept) >= MAX_ROUTES:
            break
    return kept


def build(root=EXPORT):
    out = {}
    for pl in sorted(Path(root).glob("Pl*")):
        if not re.fullmatch(r"Pl\d+", pl.name):
            continue
        rs = routes_of(_load(pl / "AIComboList.json"))
        if rs:
            out[pl.name] = rs
    return {"version": VERSION, "folders": out}


def load(path=PATH):
    d = _load(path)
    return d if isinstance(d, dict) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", default=str(EXPORT))
    ap.add_argument("--show", default=None, help="print one folder (Pl02)")
    a = ap.parse_args()
    if a.show:
        for r in ((load() or {}).get("folders") or {}).get(a.show) or []:
            req = " [Rising]" if r.get("requires") else ""
            print(f"  {len(r['steps']):>2}{req} {' > '.join(r['steps'])}\n      {r['name']}")
        return
    if not Path(a.export).is_dir():
        raise SystemExit(f"no export at {a.export}")
    out = build(a.export)
    PATH.parent.mkdir(exist_ok=True)
    PATH.write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
    n = sum(len(v) for v in out["folders"].values())
    print(f"wrote {PATH}: {len(out['folders'])} characters, {n} routes")


if __name__ == "__main__":
    main()
