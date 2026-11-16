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


def pick(w, label):
    """(open, close) seconds into montage windows `w` for pressing `label`
    next: the string's own next-stage window for Melee, else the first
    cancel window that allows it; None if the move cannot follow here."""
    if label == "Melee" and w["next"]:
        return tuple(w["next"][0])
    for a, b, into in w["cancel"]:
        if label in into:
            return a, b
    return None


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else "Pl24"
    print(json.dumps(for_folder(folder), indent=1))


if __name__ == "__main__":
    main()
