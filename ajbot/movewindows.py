"""When a fighter's moves take their next input, from the game's own montages.

framedata.py keeps one summary per move; the combo trainer needs every stage:

  next    ANS_ShiftChangeAnimation - the window in which pressing the same
          button again goes on to the next stage of a string (A > A > A).
  cancel  ANS_AttackCancelFlag - windows in which the move can be cancelled,
          and into which commands (Unique1, ExMelee, Dash, Dodge...).

All times are real seconds from the montage's start (ANS_AnimSpeed applied,
as in framedata). Montage names per move come from models/framedata.json
("chain"); the montages themselves from the gamedata export
(ajbot_local.json game.gamedata).

    python -m ajbot.movewindows Pl24            # print one fighter's windows
    python -m ajbot.movewindows --build         # bake models/movewindows.json
                                                # (the policy's win.* features)
"""
import json
import re
import sys
from pathlib import Path

from ajbot import framedata as F

_STAGE = re.compile(r"Attack1_(\d+)$")


def montage_windows(path):
    """{"len", "next": [(a, b)], "cancel": [(a, b, [into])]} or None."""
    d = F._load_json(path)
    if not d:
        return None
    by = {o.get("Name"): o for o in d}
    ms = [o for o in d if o.get("Type") == "AnimMontage"]
    if not ms:
        return None
    m = ms[0]["Properties"]
    rows, speeds = [], []
    for n in m.get("Notifies") or []:
        name = n.get("NotifyName") or ""
        a = float(n.get("LinkValue", n.get("TriggerTimeOffset", 0.0)) or 0.0)
        b = a + float(n.get("Duration") or 0.0)
        p = F._props(by, n)
        if name == "ANS_AnimSpeed":
            speeds.append((a, b, float(p.get("Speed", 1.0) or 1.0)))
        rows.append((a, b, name, p))
    real = F._clock(speeds, float(m.get("RateScale", 1.0) or 1.0))
    out = {"len": round(real(float(m.get("SequenceLength") or 0.0)), 4), "next": [], "cancel": []}
    for a, b, name, p in rows:
        if name == "ANS_ShiftChangeAnimation":
            out["next"].append((round(real(a), 4), round(real(b), 4)))
        elif name in ("ANS_AttackCancelFlag", "ANS_DLC2AttackCancelFlag"):
            into = set()
            for c in p.get("CancelParam") or []:
                u = c.get("UseCustomAttackCommand", "EUseAttackId::None")
                into.add((u if u != "EUseAttackId::None" else c.get("Command", "")).split("::")[-1])
            out["cancel"].append((round(real(a), 4), round(real(b), 4), sorted(i for i in into if i)))
    out["next"].sort()
    out["cancel"].sort()
    return out


def for_folder(folder, root=None, fd=None):
    """{label: [windows of each montage in the move's chain, in order]} for one
    fighter folder (Pl24); the Melee string ordered by stage. {} when the
    gamedata export is not on this machine."""
    root = Path(root or F.GAMEDATA)
    anim = F._ci(root, "Character", "Player", folder, "Animation")
    if not anim.is_dir():
        return {}
    am = {c.name.lower(): c for c in anim.glob("AM_*.json")}
    fd = fd if fd is not None else F.get().data
    out = {}
    for label, sides in (fd.get(folder) or {}).items():
        chain = ((sides or {}).get("ground") or {}).get("chain") or []
        if label == "Melee":
            staged = sorted((int(_STAGE.search(k).group(1)), k) for k in chain if _STAGE.search(k))
            chain = [k for _, k in staged] or chain
        ws = []
        for k in chain:
            w = montage_windows(am.get(f"am_{k}.json".lower(), anim / f"AM_{k}.json"))
            if w:
                w["montage"] = k
                ws.append(w)
        if ws:
            out[label] = ws
    return out


class Montages:
    """One fighter's montage windows, parsed on first use (Montages("Pl24").get("at002_Attack1_2"))."""

    def __init__(self, folder, root=None):
        anim = F._ci(Path(root or F.GAMEDATA), "Character", "Player", folder, "Animation")
        self.available = anim.is_dir()
        self.files = ({c.name.lower(): c for c in anim.glob("AM_*.json")}
                      if self.available else {})
        self.cache = {}

    def get(self, montage):
        """Windows of one montage by name, or None when the game data has none."""
        if montage not in self.cache:
            p = self.files.get(f"am_{montage}.json".lower())
            self.cache[montage] = montage_windows(p) if p else None
        return self.cache[montage]


def pick(w, label, prev=None):
    """(open, close) seconds into montage windows `w` for pressing `label`
    next, after `prev` (the move playing): the string's own next-stage window
    when the same button continues it (A > A, Q2 > Q2), else the first cancel
    window that allows it; None if the move cannot follow here."""
    if (label == "Melee" or label == prev) and w["next"]:
        return tuple(w["next"][0])
    for a, b, into in w["cancel"]:
        if label in into:
            return a, b
    return None


# ------------------------------------------------- baked table + features
# The policy learns from these too, on the training server, which has no
# gamedata export: build() bakes every fighter's montage windows into
# models/movewindows.json (built locally, travels with the code like
# movedata.json).
PATH = Path("models") / "movewindows.json"


def build(root=None, fd=None):
    """{folder: {"labels": {label: [montage...]}, "mont": {montage: windows}}}"""
    root = Path(root or F.GAMEDATA)
    fd = fd if fd is not None else F.get().data
    out = {}
    for folder, moves in fd.items():
        anim = F._ci(root, "Character", "Player", folder, "Animation")
        if not anim.is_dir():
            continue
        am = {c.name.lower(): c for c in anim.glob("AM_*.json")}
        labels, mont = {}, {}
        for label, sides in (moves or {}).items():
            names = []
            for side in ("ground", "air"):
                for k in ((sides or {}).get(side) or {}).get("chain") or []:
                    if k in names:
                        continue
                    p = am.get(f"am_{k}.json".lower())
                    w = montage_windows(p) if p else None
                    if w and (w["next"] or w["cancel"]):
                        mont[k] = w
                    names.append(k)
            if names:
                labels[label] = names
        if mont:
            out[folder] = {"labels": labels, "mont": mont}
    return out


_CACHE = {"mtime": None, "data": {}, "by": {}}


def table(path=PATH):
    """(baked table, {folder: {montage: label}}), reloaded when the file changes."""
    p = Path(path)
    try:
        m = p.stat().st_mtime
    except OSError:
        return {}, {}
    if _CACHE["mtime"] != m:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        _CACHE.update(mtime=m, data=d, by={
            f: {k: lab for lab, ks in (v.get("labels") or {}).items() for k in ks}
            for f, v in d.items()})
    return _CACHE["data"], _CACHE["by"]


# What the policy sees: for us, which follow-ups the game would take right
# now (a string's next stage or a cancel), or how soon it will; for the
# opponent, whether its move can be cancelled now (a "safe" string that may
# continue). Labels as framedata.FEAT_LABELS.
FEAT_LABELS = list(F.FEAT_LABELS)
N_FEATS = len(FEAT_LABELS) + 4
FEATURE_NAMES = ([f"win.me.{l}" for l in FEAT_LABELS]
                 + ["win.me.left", "win.me.known", "win.en.open", "win.en.in"])
SOON_S = 0.5      # a window opening within this reads as "coming" (0..0.5)
RATE = 16.7       # sampler pumps a second (dataset.BOT_ACT_RATE)


def _where(rec, data, by):
    """(windows, seconds into the montage, label playing) or None."""
    folder = F.folder_of(F._move_key(rec or {}))
    t = data.get(folder)
    if not t:
        return None
    mont, n = rec.get("mont"), rec.get("mont_n")
    if mont and isinstance(n, (int, float)):
        w = t["mont"].get(mont)
        return (w, n / RATE, by.get(folder, {}).get(mont)) if w else None
    # Recordings from before the sampler read montages: the move's first
    # montage, timed from the act.
    lab = F.label_of_act(rec.get("act"))
    n = rec.get("act_n")
    if not lab or not isinstance(n, (int, float)):
        return None
    for k in (t["labels"].get(lab) or []):
        if k in t["mont"]:
            return t["mont"][k], n / RATE, lab
    return None


def _spans(w, label, playing):
    """The windows in which `label` would be taken during montage `w`."""
    if label == playing and w["next"]:
        yield from w["next"]
    for a, b, into in w["cancel"]:
        if label in into:
            yield a, b


def features(me, en):
    """N_FEATS values; 0 throughout when there is no data for the move."""
    data, by = table()
    out = [0.0] * N_FEATS
    if not data:
        return out
    hit = _where(me, data, by) if me else None
    if hit:
        w, t, playing = hit
        left = 0.0
        for i, lab in enumerate(FEAT_LABELS):
            v = 0.0
            for a, b in _spans(w, lab, playing):
                if a <= t <= b:
                    v, left = 1.0, max(left, b - t)
                elif t < a < t + SOON_S:
                    v = max(v, 0.5 * (1.0 - (a - t) / SOON_S))
            out[i] = v
        out[len(FEAT_LABELS)] = min(left, 1.0)
        out[len(FEAT_LABELS) + 1] = 1.0
    hit = _where(en, data, by) if en else None
    if hit:
        w, t, _ = hit
        spans = list(w["next"]) + [(a, b) for a, b, _ in w["cancel"]]
        out[-2] = 1.0 if any(a <= t <= b for a, b in spans) else 0.0
        ahead = [a - t for a, _ in spans if a > t]
        out[-1] = min(min(ahead), 1.0) if ahead else 1.0
    else:
        out[-1] = 1.0
    return out


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--build":
        d = build()
        PATH.write_text(json.dumps(d, separators=(",", ":")), encoding="utf-8")
        print(f"{PATH}: {len(d)} fighters, {sum(len(v['mont']) for v in d.values())} montages")
        return
    folder = sys.argv[1] if len(sys.argv) > 1 else "Pl24"
    print(json.dumps(for_folder(folder), indent=1))


if __name__ == "__main__":
    main()
