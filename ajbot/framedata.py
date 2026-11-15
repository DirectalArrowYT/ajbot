"""Real frame data, read from the game's own files (FModel export).

moveinfo.py learns reach and commitment statistically from recordings; this
reads what the game itself says. For every fighter folder (Pl02 = Bakugo...)
and attack action (Melee, Unique2, ExUnique2...), ground and air:

  startup   s from the action starting to its first hit (melee window opening,
            projectile spawned, grab applied)
  active    s the first melee window stays open (0 for a projectile)
  free      s until the fighter can act again (AN_ReturnToNormal, else the
            montage's AnimEnd)
  cancel    s when the first attack-cancel window opens, and into what
  armor     whether the move has super armor
  proj      whether it fires a projectile (anywhere in its montages)
  reach     the move's homing distance (ActionAttackParam 追従.最大距離) and
            where it stops short of the target (停止距離)

Times are real seconds: montage time is divided by every ANS_AnimSpeed
window's Speed. Hit-stop (Group_HitSlow) is not included, so on hit a move
runs a little longer than this says - which is the safe direction for
"how long is the opponent stuck".

Which montages a move plays comes from its script,
Script/Character/Player/PlNN/Battle/Action/Attack/ActionAttack<Move>.lua
("EPl02BakugoAnimationId::sa021_AttackUnique2_1"); functions whose name
contains "Air" are the air version.

    python -m ajbot.framedata                        # writes models/framedata.json
    python -m ajbot.framedata --show Pl13            # one fighter's table
"""
import argparse
import glob
import json
import re
import sys
from pathlib import Path

PATH = Path("models") / "framedata.json"
# fmcli export of the game's Content folder (ajbot_local.json: game.gamedata)
GAMEDATA = Path(__import__("ajbot.local", fromlist=["get"]).get("game.gamedata")
                or "aj-gamedata/HeroGame/Content")

HIT_POINTS = ("AN_AttackMelee", "AN_ReserveProjectileGenerate", "AN_SetGrabDamage",
              "AN_SetGrabDamageByState", "AN_GenerateHomingShotForPl02", "AN_GrabThrowing")


def _props(by, n):
    obj = n.get("NotifyStateClass") or n.get("Notify")
    if not obj:
        return {}
    name = obj.get("ObjectName", "").split(":")[-1].rstrip("'")
    return (by.get(name) or {}).get("Properties") or {}


def _clock(windows, rate=1.0):
    """montage time -> real seconds, given [(a, b, speed)] ANS_AnimSpeed windows."""
    ws = sorted((a, b, s) for a, b, s in windows if s and s > 0 and b > a)

    def real(tm):
        out, cur = 0.0, 0.0
        for a, b, s in ws:
            if a >= tm:
                break
            if a > cur:
                out += a - cur
                cur = a
            seg_end = min(b, tm)
            if seg_end > cur:
                out += (seg_end - cur) / s
                cur = seg_end
        if tm > cur:
            out += tm - cur
        return out / (rate or 1.0)
    return real


def montage(path):
    """Timing of one AM_*.json, in real seconds, or None."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    by = {o.get("Name"): o for o in d}
    ms = [o for o in d if o.get("Type") == "AnimMontage"]
    if not ms:
        return None
    m = ms[0]["Properties"]
    length = float(m.get("SequenceLength") or 0.0)
    speeds, rows = [], []
    for n in m.get("Notifies") or []:
        name = n.get("NotifyName") or ""
        a = float(n.get("LinkValue", n.get("TriggerTimeOffset", 0.0)) or 0.0)
        b = a + float(n.get("Duration") or 0.0)
        p = _props(by, n)
        if name == "ANS_AnimSpeed":
            speeds.append((a, b, float(p.get("Speed", 1.0) or 1.0)))
        rows.append((a, b, name, p))
    real = _clock(speeds, float(m.get("RateScale", 1.0) or 1.0))
    hits, cancels, armor, ret, end, proj = [], [], False, [], None, False
    for a, b, name, p in rows:
        if name == "ANS_AttackMelee":
            hits.append((real(a), real(b)))
        elif name in HIT_POINTS:
            hits.append((real(a), real(a)))
            proj = proj or "Projectile" in name or "Shot" in name
        elif name in ("ANS_AttackCancelFlag", "ANS_DLC2AttackCancelFlag"):
            into = []
            for c in p.get("CancelParam") or []:
                u = c.get("UseCustomAttackCommand", "EUseAttackId::None")
                into.append((u if u != "EUseAttackId::None" else c.get("Command", "")).split("::")[-1])
            cancels.append((real(a), real(b), sorted(set(i for i in into if i))))
        elif name in ("ANS_EnableSuperArmor", "AN_EnableSuperArmor"):
            armor = True
        elif name == "AN_ReturnToNormal":
            ret.append(real(a))
        elif name == "AN_AnimEnd" and end is None:
            end = real(a)
    hits.sort()
    total = real(length)
    first = hits[0] if hits else None
    after = [r for r in ret if not first or r >= first[0]]
    free = min(after) if after else (end if end is not None else total)
    return {"len": round(total, 3),
            "startup": round(first[0], 3) if first else None,
            "active": round(first[1] - first[0], 3) if first else None,
            "last_hit": round(max(h[1] for h in hits), 3) if hits else None,
            "hits": len(hits),
            "free": round(free, 3),
            "cancel": round(min(c[0] for c in cancels), 3) if cancels else None,
            "cancel_into": sorted({i for c in cancels for i in c[2]}),
            "armor": armor, "proj": proj}


_FUNC = re.compile(r"^\s*(?:function\s+([A-Za-z0-9_]+)\s*\(|([A-Za-z0-9_]+)\s*=\s*\(\s*function)", re.M)
_ANIM = re.compile(r"AnimationId::([A-Za-z0-9_]+)")


def script_montages(lua_text):
    """{"ground": [keys...], "air": [keys...]} in the order the script names them."""
    out = {"ground": [], "air": []}
    marks = [(m.start(), m.group(1) or m.group(2)) for m in _FUNC.finditer(lua_text)]
    for am in _ANIM.finditer(lua_text):
        fn = ""
        for pos, name in marks:
            if pos > am.start():
                break
            fn = name
        key = am.group(1)
        side = "air" if ("Air" in fn or "Air" in key) else "ground"
        if key not in out[side]:
            out[side].append(key)
    return out


def _reach(param, label, side):
    """(max homing distance, stop distance) from ActionAttackParam.json."""
    node = (param or {}).get(label) or {}
    lst = node.get("空中" if side == "air" else "地上") or []
    if isinstance(lst, dict):
        lst = [lst]
    for stage in lst:
        f = (stage or {}).get("追従") or {}
        try:
            mx = float(f.get("最大距離"))
            st = float(f.get("停止距離", 0) or 0)
        except (TypeError, ValueError):
            continue
        return mx, st
    return None, None


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _ci(base, *names):
    """base/names..., matching each name case-insensitively when the exact
    spelling is absent. The pak spells some fighter folders in capitals
    (Character/Player/PL12, PL212, PL46...) while Script/ and DB/ say Pl12;
    Windows never noticed, a Linux export does."""
    p = Path(base)
    for n in names:
        q = p / n
        if not q.exists() and p.is_dir():
            low = n.lower()
            q = next((c for c in p.iterdir() if c.name.lower() == low), q)
        p = q
    return p


def build(root=GAMEDATA):
    root = Path(root)
    out = {}
    for lua_dir in sorted(glob.glob(str(root / "Script/Character/Player/Pl*/Battle/Action/Attack"))):
        folder = Path(lua_dir).parts[-4]
        anim_dir = _ci(root, "Character", "Player", folder, "Animation")
        db = _ci(root, "DB", "Character", "Player", folder, "Attack")
        # montage files by lower-cased name, for the same reason as _ci
        am = ({c.name.lower(): c for c in anim_dir.glob("AM_*.json")}
              if anim_dir.is_dir() else {})
        moves = {}
        for lua in sorted(Path(lua_dir).glob("ActionAttack*.lua")):
            label = lua.stem[len("ActionAttack"):]
            try:
                keys = script_montages(lua.read_text(encoding="utf-8-sig", errors="replace"))
            except OSError:
                continue
            param = _load_json(_ci(db, label, "ActionAttackParam.json"))
            entry = {}
            for side, ks in keys.items():
                # Montage names number the sequence (sa040 < sa041 < sa042);
                # the script mentions them in whatever order its branches do.
                ks = sorted([k for k in ks if not k.endswith("_Awaking")] or ks)
                timed = []
                for k in ks:
                    t = montage(am.get(f"am_{k}.json".lower(), anim_dir / f"AM_{k}.json"))
                    if t:
                        timed.append((k, t))
                if not timed:
                    continue
                # Montages before the first one that hits are lead-ins - the
                # homing run toward the target, a wind-up, a held charge. How
                # long they play depends on distance or the button, so the
                # timings below are from the hitting montage's own start and
                # `lead` is only their nominal length.
                lead = 0.0
                main = None
                for k, t in timed:
                    if t["startup"] is not None:
                        main = (k, t)
                        break
                    lead += t["free"]
                if main is None:
                    main, lead = timed[0], 0.0
                k, t = main
                mx, st = _reach(param, label, side)
                entry[side] = {"montage": k, "lead": round(lead, 3),
                               "startup": t["startup"],
                               "active": t["active"], "hits": t["hits"],
                               "free": t["free"],
                               "cancel": t["cancel"],
                               "cancel_into": t["cancel_into"], "armor": t["armor"],
                               "proj": any(tt["proj"] for _, tt in timed),
                               "reach": mx, "stop": st,
                               "chain": [kk for kk, _ in timed]}
            if entry:
                moves[label] = entry
        if moves:
            out[folder] = moves
    return out


# ---------------------------------------------------------------- runtime
_FOLDER = re.compile(r"^(Pl\d+)")


def _move_key(rec):
    """Key whose frame data applies: Toga's copy while she is transformed."""
    if rec.get("char_mode") == 1 and rec.get("copy_of"):
        return rec["copy_of"]
    return rec.get("key") or rec.get("id")


def folder_of(key):
    """'Pl222Nagan' / 'Pl02Bakugo#1' -> 'Pl222' / 'Pl02'."""
    m = _FOLDER.match(str(key or ""))
    return m.group(1) if m else None


def label_of_act(act):
    """Recorded act name -> script label: 'AttackUnique2' -> 'Unique2'."""
    if act and act.startswith("Attack"):
        return act[len("Attack"):]
    return None


class FrameData:
    def __init__(self, data=None):
        self.data = data or {}

    def __bool__(self):
        return bool(self.data)

    def move(self, key, label, airborne=False):
        moves = self.data.get(folder_of(key)) or {}
        m = moves.get(label)
        if not m:
            return None
        return m.get("air" if airborne else "ground") or m.get("ground") or m.get("air")

    def profile(self, rec):
        """Frame data of the move this record is in, or None."""
        lab = label_of_act((rec or {}).get("act"))
        if not lab:
            return None
        return self.move(_move_key(rec), lab, not (rec.get("ground") or 0))

    def fastest(self, key, airborne, ok=None):
        """Quickest startup among the character's attacks (ok: labels allowed)."""
        best = None
        for lab, m in (self.data.get(folder_of(key)) or {}).items():
            if ok is not None and lab not in ok:
                continue
            p = m.get("air" if airborne else "ground")
            if p and p.get("startup") is not None and p.get("hits"):
                best = p["startup"] if best is None else min(best, p["startup"])
        return best


# The attacks the per-move features cover, and the game's "allowed now" flag
# for each (sampler ok_*), used to find the quickest punish actually available.
FEAT_LABELS = {"Melee": "ok_melee", "ExMelee": "ok_exmelee", "Unique1": "ok_u1",
               "Unique2": "ok_u2", "ExUnique1": "ok_exu1", "ExUnique2": "ok_exu2",
               "Unguardable": "ok_ub", "PlusUltra1": "ok_pu1"}
SIDE_NAMES = ["in_move", "to_hit", "hitting", "left", "progress"]
N_FEATS = 2 * len(SIDE_NAMES) + 2 * len(FEAT_LABELS) + 1
FEATURE_NAMES = ([f"fd.me.{n}" for n in SIDE_NAMES] + [f"fd.en.{n}" for n in SIDE_NAMES]
                 + [f"fd.startup.{l}" for l in FEAT_LABELS] + [f"fd.reach.{l}" for l in FEAT_LABELS]
                 + ["fd.punish"])


def _side(fd, rec, dt):
    """Where a fighter is inside its current attack, from the game's timings:
    in an attack with data, time until it hits (/1.5 s), in its hit window
    now, time until it can act again (/2 s), and how far through it is."""
    p = fd.profile(rec) if rec else None
    if not p or dt is None:
        return [0.0] * len(SIDE_NAMES), None
    st = p["startup"] if p["startup"] is not None else 0.0
    hit_end = st + (p["active"] or 0.0)
    left = max(0.0, p["free"] - dt)
    return [1.0,
            min(max(st - dt, 0.0), 1.5) / 1.5,
            1.0 if p["hits"] and st - 0.05 <= dt <= hit_end + 0.05 else 0.0,
            min(left, 2.0) / 2.0,
            min(dt / max(p["free"], 0.1), 2.0) / 2.0], left


def features(me, en, me_dt, en_dt):
    """N_FEATS values; me_dt / en_dt are seconds since each side's current
    act began (None if unknown).

    per side   SIDE_NAMES above
    startup    each FEAT_LABELS move's startup for our character (/1.5 s,
               1 = no data), ground or air version by where we are
    reach      distance / that move's homing reach (/3; 1 = no homing data)
    punish     the opponent's recovery left minus our quickest available
               startup (+ = a free punish), /1 s, clipped to -1..1
    """
    fd = get()
    if not fd or not me:
        return [0.0] * N_FEATS
    a, _ = _side(fd, me, me_dt)
    b, en_left = _side(fd, en, en_dt)
    out = a + b
    key = _move_key(me)
    air = not me.get("ground")
    dist = me.get("dist")
    starts, reaches = [], []
    for lab in FEAT_LABELS:
        p = fd.move(key, lab, air)
        st = p.get("startup") if p else None
        starts.append(min(st, 1.5) / 1.5 if st is not None else 1.0)
        r = p.get("reach") if p else None
        reaches.append(min(dist / r, 3.0) / 3.0 if r and isinstance(dist, (int, float)) else 1.0)
    out += starts + reaches
    ok = [lab for lab, flag in FEAT_LABELS.items() if me.get(flag, 1)]
    quick = fd.fastest(key, air, ok)
    if en_left is None or quick is None:
        out.append(0.0)
    else:
        out.append(max(-1.0, min(1.0, en_left - quick)))
    return out


_CACHE = {"mtime": None, "obj": FrameData()}


def get(path=PATH):
    p = Path(path)
    try:
        m = p.stat().st_mtime
    except OSError:
        return _CACHE["obj"]
    if _CACHE["mtime"] != m:
        try:
            _CACHE["obj"] = FrameData(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            _CACHE["obj"] = FrameData()
        _CACHE["mtime"] = m
    return _CACHE["obj"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(GAMEDATA))
    ap.add_argument("--show", default=None, help="fighter folder, e.g. Pl13")
    a = ap.parse_args()
    if a.show:
        for lab, m in sorted((get().data.get(a.show) or {}).items()):
            for side, p in m.items():
                print(f"{lab:16} {side:6} startup={p['startup']} active={p['active']} hits={p['hits']} "
                      f"free={p['free']} cancel={p['cancel']}->{','.join(p['cancel_into'])} "
                      f"reach={p['reach']} stop={p['stop']} armor={p['armor']}  [{' '.join(p['chain'])}]")
        return
    out = build(a.root)
    if not out:
        print(f"no fighter data under {a.root} - kept {PATH} as it was")
        return
    PATH.parent.mkdir(exist_ok=True)
    PATH.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    n = sum(len(v) for v in out.values())
    print(f"wrote {PATH}: {len(out)} fighters, {n} moves")


if __name__ == "__main__":
    main()
