"""What every character's moves ARE, from the game's own data (FModel export
of DB/Character/Player/PlNN/...): which moves are projectiles, which cannot
be guarded, which carry super armour, how much they hit for, how far a
projectile travels. Static facts, so the rule layers stop guessing them from
act names:

  * the defence answered every quirk skill inside 450 units as if it were a
    punch - a projectile from 1200 away was never answered, a punch at 500
    was; with `proj` and `reach` it knows which is which;
  * Unguardable is a move name, but several Plus Ultras and Rising moves are
    unguardable too (DamageParam Unguardable=1) - guarding those is a loss;
  * armoured moves (スーパーアーマー) eat a mash: guard or dodge, do not trade.

    python -m ajbot.movedata                     # writes models/movedata.json
    python -m ajbot.movedata --show Pl02

framedata.py reads the same export's MONTAGES (startup / active / recovery /
cancel windows / homing reach - model features); this reads DamageParam and
the projectile files, which framedata does not: unguardable, damage, guard
break, hit-slow, projectile speed x lifetime.

The export is only needed to BUILD the table; models/movedata.json is what
the bot reads, so machines without the export still get it.
"""
import argparse
import json
import re
from pathlib import Path

PATH = Path("models") / "movedata.json"
VERSION = 2
from .framedata import GAMEDATA as _GAMEDATA   # ajbot_local.json: game.gamedata
EXPORT = _GAMEDATA / "DB/Character/Player"
MAX_REACH = 4000.0


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _load(p):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _walk(o):
    """Every (key, value) pair in a nested JSON value."""
    if isinstance(o, dict):
        for k, v in o.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _damage(d):
    """Summary of a DamageParam.json: groups are the top-level dicts that
    carry a Group_Basic."""
    out = {"dmg": 0.0, "hits": 0, "ub": 0, "proj": 0, "gbreak": 0.0, "kb": 0.0,
           "gslow": 0.0, "hslow": 0.0}
    fb, pu, stun, addf = [], [], [], []
    if not isinstance(d, dict):
        return out
    for g in d.values():
        if not isinstance(g, dict) or "Group_Basic" not in g:
            continue
        b = g.get("Group_Basic") or {}
        hits = int(_f((g.get("連続ヒット") or {}).get("ヒット回数"), 1.0)) or 1
        dv = _f(b.get("DamageValue"))
        out["dmg"] = max(out["dmg"], dv * hits)
        out["hits"] = max(out["hits"], hits)
        if str(b.get("Unguardable", "0")).strip() not in ("0", "", "False", "false"):
            out["ub"] = 1
        kinds = b.get("DamageKind") or []
        if isinstance(kinds, str):
            kinds = [kinds]
        if any("Projectile" in str(k) for k in kinds):
            out["proj"] = 1
        out["gbreak"] = max(out["gbreak"], _f(b.get("GuardBreakValue")))
        out["kb"] = max(out["kb"], _f(b.get("KnockBackTime")))
        hs = g.get("Group_HitSlow") or {}
        out["gslow"] = max(out["gslow"], _f(hs.get("HitSlowSecAttackAtGuard")))
        out["hslow"] = max(out["hslow"], _f(hs.get("HitSlowSecAttack")))
        # Per hit: down value (ForcedBlowValue - the victim's ForcedBlow gauge
        # blows them out of the combo at 100), Plus Ultra gain, stun, and
        # extra hitstun frames.
        fb.append(_f(b.get("ForcedBlowValue")))
        pu.append(_f(b.get("PlusUltraValue")))
        stun.append(_f(b.get("StunValue")))
        addf.append(_f(b.get("AdditionalFrame")))
    if fb:
        med = lambda v: sorted(v)[len(v) // 2]
        out["fb"] = round(med(fb), 2)
        out["fb_max"] = round(max(fb), 2)
        out["pu"] = round(med(pu), 2)
        out["stun"] = round(med(stun), 2)
        out["addf"] = round(med(addf), 2)
    return out


def _gauges(path):
    """Per-character gauges that matter live: the victim's ForcedBlow (down
    value) cap and the partner (Sidekick) recharge."""
    d = _load(path)
    if not isinstance(d, dict):
        return {}
    out = {}
    fbg = d.get("ForcedBlow") or {}
    if fbg:
        out["fb_cap"] = _f(fbg.get("ゲージ消費量"), 100.0) or 100.0
        out["fb_max"] = _f(fbg.get("ゲージ最大量"), 150.0)
    sk = d.get("Sidekick") or {}
    if sk:
        mx = _f(sk.get("ゲージ最大量"), 10.0) or 10.0
        unit = _f(sk.get("リロード単位"), 10.0) or 10.0
        per = _f(sk.get("リロード単位辺り、何秒でリロードするか"), 25.0)
        # Seconds from empty to full; the gauge is spent whole on a call.
        out["sk_full_s"] = round(per * mx / unit, 2)
        out["sk_init"] = round(_f(sk.get("ゲージ初期値"), 0.0) / mx, 3)
        out["sk_teamup_s"] = _f(sk.get("チームアップ時のリロード時間"), 0.0)
        out["sk_rescue_s"] = _f(sk.get("レスキュー後のリロード時間"), 0.0)
    hp = d.get("Health") or {}
    if hp:
        out["hp_max"] = _f(hp.get("ゲージ最大量"), 0.0)
    return out


def _projectile(p):
    """(reach, speed, life) of one projectile file."""
    d = _load(p)
    if not isinstance(d, dict):
        return None
    par = d.get("パラメータ") or {}
    life = _f(par.get("寿命"))
    speed = 0.0
    hom = d.get("ホーミングパラメータ") or {}
    acc = hom.get("加速性能") or {}
    speed = max(speed, _f(acc.get("開始性能")), _f(acc.get("最終性能")))
    for ch in d.get("ChangeableHomingParam") or []:
        if not isinstance(ch, dict):
            continue
        a = ch.get("加速性能") or {}
        speed = max(speed, _f(a.get("開始性能")), _f(a.get("最終性能")))
        life = max(life, _f(ch.get("寿命")))
    radius = 0.0
    for c in d.get("コリジョン") or []:
        if isinstance(c, dict):
            radius = max(radius, _f(c.get("半径")))
    reach = min(MAX_REACH, speed * life + radius) if life > 0 else radius
    return reach, speed, life


def build(root=EXPORT):
    root = Path(root)
    moves, chars = {}, {}
    for pl in sorted(root.glob("Pl*")):
        if not re.fullmatch(r"Pl\d+", pl.name):
            continue
        atk = pl / "Attack"
        table = {}
        if atk.is_dir():
            for mv in sorted(atk.iterdir()):
                if not mv.is_dir() or mv.name in ("CharaCommon",):
                    continue
                e = _damage(_load(mv / "DamageParam.json"))
                aap = _load(mv / "ActionAttackParam.json")
                armor, shots = 0, []
                for k, v in _walk(aap):
                    if k == "スーパーアーマー" and str(v).lower() == "true":
                        armor = 1
                    elif k == "弾" and isinstance(v, list):
                        for s in v:
                            if isinstance(s, dict) and s.get("弾名"):
                                shots.append(s["弾名"])
                e["armor"] = armor
                e["shots"] = sorted(set(shots))
                table[mv.name] = e
        # Projectiles: by the names a move fires, else by file-name prefix.
        prj = {}
        if (pl / "Projectile").is_dir():
            for p in (pl / "Projectile").glob("*.json"):
                r = _projectile(p)
                if r:
                    prj[p.stem] = r
        names = sorted(table, key=len, reverse=True)
        for stem, (reach, speed, life) in prj.items():
            owner = None
            for mv, e in table.items():
                if stem in e["shots"]:
                    owner = mv
                    break
            if owner is None and stem.startswith("Prj"):
                rest = stem[3:]
                owner = next((mv for mv in names if rest.startswith(mv)), None)
            if owner is None:
                continue
            e = table[owner]
            if speed > 0 and life > 0:
                e["proj"] = 1
            e["reach"] = round(max(e.get("reach", 0.0), reach), 1)
            e["prj_speed"] = round(max(e.get("prj_speed", 0.0), speed), 1)
            e["prj_life"] = round(max(e.get("prj_life", 0.0), life), 3)
        for e in table.values():
            e.pop("shots", None)
            for k in ("dmg", "gbreak", "kb", "gslow", "hslow"):
                e[k] = round(e[k], 3)
        if table:
            moves[pl.name] = table
        g = _gauges(pl / "GaugeParam.json")
        if g:
            chars[pl.name] = g
    return {"version": VERSION, "moves": moves, "chars": chars}


# ------------------------------------------------------------------ lookup --

_FOLDER = re.compile(r"^Pl(\d+)")


class MoveData:
    def __init__(self, data=None):
        self.moves = (data or {}).get("moves") or {}
        self.chars = (data or {}).get("chars") or {}
        self._names = {k: sorted(v, key=len, reverse=True) for k, v in self.moves.items()}

    def __bool__(self):
        return bool(self.moves)

    def _folder(self, key, tables):
        if not isinstance(key, str):
            return None
        m = _FOLDER.match(key)
        if not m:
            return None
        folder = f"Pl{m.group(1)}"
        if folder in tables:
            return folder
        # Lua ids pad to two digits ('Pl02'); folders may not ('Pl104').
        folder = f"Pl{int(m.group(1)):02d}"
        return folder if folder in tables else None

    def char(self, key):
        """Gauge facts for Lua id `key`: fb_cap (down value that ends a
        combo), sk_full_s (partner recharge, s), sk_init, hp_max ... or {}."""
        f = self._folder(key, self.chars)
        return self.chars.get(f) or {} if f else {}

    def fb(self, key, act, default=3.0):
        """Down value one hit of `act` adds to the victim (median per hit)."""
        e = self.move(key, act)
        v = (e or {}).get("fb")
        return float(v) if isinstance(v, (int, float)) else default

    def move(self, key, act):
        """The record for Lua id `key` ('Pl02Bakugo', 'Pl247DabiFullBodyBurn')
        doing `act` ('AttackExUnique1'), or None."""
        if not isinstance(key, str) or not isinstance(act, str):
            return None
        folder = self._folder(key, self.moves)
        if folder is None:
            return None
        table = self.moves[folder]
        name = act[6:] if act.startswith("Attack") else act
        e = table.get(name)
        if e is not None:
            return e
        for mv in self._names[folder]:
            if name.startswith(mv):
                return table[mv]
        return None


_CACHE = {"mtime": None, "obj": MoveData()}


def get(path=PATH):
    p = Path(path)
    try:
        m = p.stat().st_mtime
    except OSError:
        return _CACHE["obj"]
    if _CACHE["mtime"] != m:
        try:
            _CACHE["obj"] = MoveData(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            _CACHE["obj"] = MoveData()
        _CACHE["mtime"] = m
    return _CACHE["obj"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", default=str(EXPORT))
    ap.add_argument("--show", default=None, help="print one folder (Pl02)")
    a = ap.parse_args()
    if a.show:
        for mv, e in sorted((get().moves.get(a.show) or {}).items()):
            print(f"  {mv:22} {e}")
        return
    if not Path(a.export).is_dir():
        raise SystemExit(f"no export at {a.export} - models/movedata.json is built from "
                         f"the FModel export of DB/Character/Player")
    out = build(a.export)
    PATH.parent.mkdir(exist_ok=True)
    PATH.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    n = sum(len(v) for v in out["moves"].values())
    np_ = sum(e.get("proj", 0) for v in out["moves"].values() for e in v.values())
    nu = sum(e.get("ub", 0) for v in out["moves"].values() for e in v.values())
    na = sum(e.get("armor", 0) for v in out["moves"].values() for e in v.values())
    print(f"wrote {PATH}: {len(out['moves'])} characters, {n} moves "
          f"({np_} projectile, {nu} unguardable, {na} armoured)")


if __name__ == "__main__":
    main()
