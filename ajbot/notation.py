"""Combo notation, the way players write it, turned into a practice route.

    AAA > Q1 > Dodge Cancel > AA > Q1 > Jump > Q1 > Q2 (Jump Followup)

Steps are split on ">" or " - " (also "->", "," and "~"). Words in
(brackets) are notes: kept with the step, not pressed - except a move right
after TQ2, which is its follow-up: TQ2(a) = TQ2 with the attack follow-up.

    A - TQ1 - Dodge (Cancel) - A - TQ1 - jump (cancel) - AAAA - CA - TQ1 - TQ2(a) - PU

    A          attack (jab); AAA = three jabs, "A x3" / "3A" work too
    CA         counter attack, the yellow one (stick + attack)
    GC         guard-cancel counter (guard + quirk 1 while blocking)
    UB         unblockable (the red one)
    Q1 / Q2    quirk 1 / quirk 2
    TQ1 / Q3   tilt quirk 1 (stick + quirk 1)
    TQ2 / Q4   tilt quirk 2 (stick + quirk 2)
    PU         Plus Ultra (PU1 / PU2 for a character's second one)
    j.X        jump, then X in the air        (j.Q1, j.A)
    d.X        dash in, then X                (d.A); bd.X = back dash first
    r.X        roll / dodge, then X           (r.A)
    t.NAME.X   tag in NAME, then X with them  (t.Todoroki.Q1)
    [X]        charge X: hold the button      ([Q1], [Q1]:40 = 40 frames)
    Q2 (x3)    press Q2 three times during one Q2 (Mirio's Q2 goes on while
               you press). Not the same as Q2x3 / Q2 x3, three Q2s in a row
    air > ...  the combo starts in the air: jump first (same as j.X on the
               first move: "air > AA > CA" = "j.AA > CA")
    Q2 (early) Aizawa's Q2 is a grab, so the next step normally waits for it
               to let go; (early) - or (no wait), (cancel) - presses the next
               step off the Q2 itself instead
    Jump, Dash, Back Dash, Dodge, Dodge Cancel, Roll, Guard / Block,
    Just Guard / JG, Escape, Rising, Extreme Action / EA   - written out

Timing is not in the notation, so every step gets a first-guess gap and
practice.py searches it from there. A Bakugo move written right after TQ2
(TQ2 > A) is the TQ2 follow-up on that button.

    python -m ajbot.notation "AAA > Q1 > Dodge Cancel > AA" --char Bakugo
    python -m ajbot.notation "..." --char Bakugo --save "Bakugo pro BnB"
    python -m ajbot.notation "..." --char Bakugo --json      # for the GUI
"""
import argparse
import json
import re
import sys

from . import demo

# First-guess frames from the previous step registering to this one. Learned
# per character from real play when models/notation_gaps.json has the pair
# (python -m ajbot.notation --learn-gaps ...), else these.
GAP = 14
GAPS_PATH = demo.DEMO_PATH.parent / "notation_gaps.json"
# Recorded gaps longer than this were the player waiting (for a bounce, a
# read), which only slows practice's search down; the guess does better there.
LINK_MAX = 24
SEARCH_REACH = 38          # practice.SEARCH's largest late offset
GAP_AFTER = {"Jump": 8, "DashForward": 6, "DashBack": 6, "Dodge": 10, "Tag": 30}

MOVES = {
    # CA: in recordings of a Bakugo main writing "CA", the move is the
    # stick + attack one (ExMelee); the guard-cancel counter is "GC".
    "A": "Melee", "CA": "ExMelee", "TA": "ExMelee", "GC": "CounterAttack",
    "UB": "Unguardable",
    "Q1": "Unique1", "Q2": "Unique2",
    "TQ1": "ExUnique1", "Q3": "ExUnique1", "TQ2": "ExUnique2", "Q4": "ExUnique2",
    "PU": "PlusUltra1", "PU1": "PlusUltra1", "PU2": "PlusUltra2",
}
# Written-out steps (lower case, spaces squeezed).
WORDS = {
    "jump": "Jump", "dash": "DashForward", "dash forward": "DashForward",
    "forward dash": "DashForward", "back dash": "DashBack", "backdash": "DashBack",
    "dash back": "DashBack", "dodge": "Dodge", "dodge cancel": "Dodge", "dc": "Dodge",
    "roll": "Dodge", "roll cancel": "Dodge", "guard": "Guard", "block": "Guard",
    "block cancel": "Guard", "guard cancel": "Guard", "just guard": "JustGuard",
    "jg": "JustGuard", "escape": "Escape", "rising": "Awaking", "awaken": "Awaking",
    "extreme action": "ExtremeAction", "ea": "ExtremeAction",
    "air": "Jump", "in the air": "Jump", "airborne": "Jump", "air start": "Jump",
}
# A note on a move saying how many times to press it while it plays out:
# Q2 (x3), Q2 (3x), Q2 (3 times), Q2 (x3 while in q2).
PRESSES = re.compile(r"(?i)^\s*(?:[x×*]\s*(\d+)|(\d+)\s*(?:x|×|times|presses)\b)")
REPEAT_EVERY = 12          # frames between the extra presses
PREFIX = {"j": "Jump", "d": "DashForward", "bd": "DashBack", "r": "Dodge"}
# Bakugo's TQ2 follow-ups: the move written next is pressed on that button.
TQ2_FOLLOW = {"Melee": "ExUnique2FollowX", "Unique1": "ExUnique2FollowY",
              "Jump": "ExUnique2FollowA", "Unique2": "ExUnique2FollowB",
              "ExUnique2": "ExUnique2FollowB", "DashForward": "ExUnique2FollowDash"}
CHARGE_FRAMES = 40
# Characters whose Q2 is a grab: the next step waits for the release.
GRAB_Q2 = {"Eraserhead"}
GRAB_GAP = 2
# A note on the grab saying not to wait for it: Q2 (early) -> the next press is
# timed from the Q2 itself, so it cuts the grab short.
NO_WAIT = re.compile(r"(?i)^\s*(early|no ?wait|don'?t wait|cancel|quick|fast)\s*$")


class NotationError(ValueError):
    pass


def _split(text):
    out = []
    for c in re.split(r"\s*(?:->|>|,|~|-)\s*", text):
        c = c.strip()
        if c:
            out += [c] if _known(c) else _split_spaces(c)
    return out


_PREFIXES = re.compile(r"(?i)^(?:(?:bd|j|d|r)\.\s*|t\.\s*[A-Za-z][\w ]*?\s*\.\s*)*")


def _known(chunk):
    """Whether a chunk reads as one step (so its spaces are part of it)."""
    body = re.sub(r"\([^)]*\)", "", chunk).strip()
    body = _PREFIXES.sub("", body)
    body = re.sub(r"^\[\s*(.+?)\s*\](?::\d+)?$", r"\1", body)
    if not body:
        return True
    try:
        _move(body, chunk)
        return True
    except NotationError:
        return False


def _split_spaces(chunk):
    """'AAA CA Q1 Q2' -> steps, keeping 'Dodge Cancel', 'j. A', 'TQ2 (a)' whole."""
    toks = re.findall(r"\([^)]*\)|\[[^\]]*\](?::\d+)?|\S+", chunk)
    out, i, lead = [], 0, ""
    while i < len(toks):
        t = toks[i]
        if t.startswith("(") and out:
            out[-1] += " " + t                 # a note or TQ2 follow-up
            i += 1
            continue
        if t.endswith("."):                    # j. d. t.Name. go with the next
            lead += t
            i += 1
            continue
        for j in range(min(len(toks), i + 3), i, -1):   # longest known phrase
            if all(not x.startswith("(") for x in toks[i + 1:j]) and \
                    _known(" ".join(toks[i:j])):
                break
        out.append(lead + " ".join(toks[i:j]))
        lead, i = "", j
    if lead:
        out.append(lead)
    return out


def _move(code, chunk):
    """One move code ("A", "TQ2", "Q3", ...) -> [labels] (AAA -> three)."""
    c = code.upper().replace(" ", "")
    m = re.fullmatch(r"(\d+)A|A[X*](\d+)|(A+)", c)
    if m:
        n = int(m.group(1) or m.group(2) or len(m.group(3)))
        return ["Melee"] * n
    m = re.fullmatch(r"([A-Z]+\d?)[X*](\d+)", c)
    if m and m.group(1) in MOVES:
        return [MOVES[m.group(1)]] * int(m.group(2))
    if c in MOVES:
        return [MOVES[c]]
    w = re.sub(r"\s+", " ", code.strip().lower())
    if w in WORDS:
        return [WORDS[w]]
    raise NotationError(f"don't know '{code}' (in '{chunk}')")


def _names():
    try:
        import sys
        from pathlib import Path
        srv = str(Path(__file__).resolve().parent.parent / "server")
        if srv not in sys.path:
            sys.path.append(srv)          # nextmatch.py lives in server/
        import nextmatch as nm
        return [n for _, n, _ in nm.MENU]
    except ImportError:
        return []


# What players call them -> the menu name.
NICKNAMES = {
    "aizawa": "Eraserhead", "eraser": "Eraserhead", "eraser head": "Eraserhead",
    "shoto": "Todoroki", "ochaco": "Uraraka", "tenya": "Iida", "tsuyu": "Asui",
    "froppy": "Asui", "momo": "Yaoyorozu", "kacchan": "Bakugo", "katsuki": "Bakugo",
    "denki": "Kaminari", "kyoka": "Jiro", "mina": "Ashido", "fumikage": "Tokoyami",
    "eijiro": "Kirishima", "red riot": "Kirishima", "lemillion": "Mirio",
    "tamaki": "Amajiki", "kendou": "Kendo", "shigaraki": "ShigarakiFinal",
    "afo": "AllForOne", "all for one": "AllForOne", "overhaul": "Chisaki",
    "fat gum": "FatGum", "gran torino": "GranTorino", "best jeanist": "Jeanist",
    "all might": "AllMight", "himiko": "Toga", "lady nagant": "Nagan",
}


def _char(name):
    """A menu character from a written name (any case, a unique prefix, or
    what players call them: Aizawa -> Eraserhead)."""
    names = _names()
    low = name.strip().lower()
    low = NICKNAMES.get(low, low).lower()
    hits = [n for n in names if n.lower() == low] or \
           [n for n in names if n.lower().startswith(low)]
    if len(hits) == 1 or (hits and not names):
        return hits[0]
    if not names:
        return name.strip()
    if not hits:
        raise NotationError(f"no character called '{name}' on the menu")
    raise NotationError(f"'{name}' could be {', '.join(hits[:5])}")


def _learned_gaps():
    try:
        return json.loads(GAPS_PATH.read_text())
    except (OSError, ValueError):
        return {}


# A label the game reports again while one press plays out (a TQ1 shows up
# two or three times): repeats closer than this are the same press.
REPEAT_F = 60
SAME_PRESS = {"ExUnique1", "ExUnique2", "ExMelee", "Unique1", "Unique2", "CounterAttack"}


def learn_gaps(files, char, side="me", max_gap_s=0.9, exact=False, combo=False):
    """Median frames between consecutive steps of `char`'s strings in
    recordings -> {"prev>label": frames}."""
    from . import dataset as D
    from .roster import base_name
    pairs = {}
    for f in files:
        ticks = D.load(str(f))
        D.calibrate_frame(ticks)
        if side == "enemy":
            ticks = D.swap_sides(ticks)
        prev = last = None
        for t in ticks:
            me = D._rec(t, "me")
            if not me or base_name(me.get("id")) != char:
                prev = last = None
                continue
            lab = D.label_of(prev, me)
            if not lab and demo.fresh_attack(prev, me):
                lab = me.get("cmd")
            prev = me
            if lab in demo.NOISE:
                continue
            now = t.get("t")
            if last and now - last[1] <= max_gap_s:
                gap = int(round((now - last[1]) * 60))
                if lab == last[0] and lab in SAME_PRESS and gap < REPEAT_F:
                    continue            # the same press reported again
                pairs.setdefault(f"{last[0]}>{lab}", []).append(gap)
            last = (lab, now)
    if exact:      # a recording of the combo itself: every pair, as played
        return {k: sorted(v)[len(v) // 2] for k, v in pairs.items()}
    if combo:      # practicing a combo: links inside strings, seen twice or more
        return {k: sorted(v)[len(v) // 2] for k, v in pairs.items() if len(v) >= 2}
    # The quick end of what they did: medians mix in neutral play, where a
    # player waits after a move; inside a combo they go about this fast.
    return {k: sorted(v)[len(v) // 3] for k, v in pairs.items() if len(v) >= 3}


def _key(text):
    return re.sub(r"\s+", " ", text.strip().lower())


def _free(en):
    """The opponent is out of our combo: not hit, held or knocked down."""
    act = str(en.get("act") or "")
    return not (en.get("kb") or en.get("stun") or en.get("grab")
                or any(w in act for w in ("Damage", "Down", "Blow", "Grab", "Catch")))


def learn_route(files, text, char, side="me", max_free=3):
    """Per-step gaps of `text` from recordings of the player doing it.

    Only clean tries count: every step present in order (an extra jab the game
    reports is skipped), and once the first hit lands the opponent is never
    free for more than `max_free` ticks between two steps. Returns ({"gaps": [median per step],
    "repeat": {step: [frames from it coming out to each extra (x3) press landing]}}, tries)."""
    from . import dataset as D
    from .roster import base_name
    plan = parse(text, char, use_routes=False)
    want = [s["label"] for s in plan]
    presses = [s.get("presses") or 0 for s in plan]
    tries, reps = [], []
    for f in files:
        ticks = D.load(str(f))
        D.calibrate_frame(ticks)
        if side == "enemy":
            ticks = D.swap_sides(ticks)
        ev, prev, last, free, hit = [], None, None, 0, False
        en_hp = hit_t = None             # the opponent's health, when it last dropped
        for t in ticks:
            me, en = D._rec(t, "me"), D._rec(t, "enemy") or {}
            if not me or base_name(me.get("id")) != char:
                prev = last = None
                ev.append(None)              # a break: no try spans it
                continue
            n_atk = me.get("atkcount") or 0
            if prev and n_atk >= 2 and n_atk > (prev.get("atkcount") or 0):
                ev.append(("_count", t["t"], int(n_atk), False, None))   # a "Q2 (x3)" press landing
            lab = D.label_of(prev, me)
            if not lab and demo.fresh_attack(prev, me):
                lab = me.get("cmd")
            prev = me
            hp = en.get("hp")
            if isinstance(hp, (int, float)):
                if en_hp is not None and hp < en_hp - 1e-4:
                    hit_t = t["t"]
                en_hp = hp
            if _free(en):
                free += 1
            else:
                hit = True
            if not lab or lab in demo.NOISE:
                continue
            if last and lab == last[0] and lab in SAME_PRESS and (t["t"] - last[1]) * 60 < REPEAT_F:
                continue
            ev.append((lab, t["t"], free, hit, bool(me.get("ground")), hit_t))
            free, hit, last = 0, False, (lab, t["t"])
        for i, e in enumerate(ev):
            if not e or e[0] != want[0]:
                continue
            if want[0] != "Jump" and not e[4]:
                continue          # a ground combo starts on the ground (air ones say air/j.)
            # The combo is on from the first stretch where they are hit; the
            # opener's whiffs and startup before that do not count as free.
            got, j, on, rep = [e], i + 1, False, {}
            while len(got) < len(want) and j < len(ev) and ev[j]:
                lab, tt, fr, hit = ev[j][:4]
                j += 1
                cur = len(got) - 1           # the step that came out last
                if lab == "_count":
                    if presses[cur] and fr >= 2:
                        rep.setdefault(cur, {})[fr] = int(round((tt - got[-1][1]) * 60))
                    continue
                # A long move keeps hitting past 2.5 s (Mirio's q4 hits for
                # ~3.5 s): time the gap from its last hit, as routemine does.
                if tt - max(got[-1][1], ev[j - 1][5] or 0) > 2.5:
                    break
                if on and fr > max_free:
                    break                    # they got out: a dropped try
                on = on or hit
                if lab == want[len(got)] or (lab, want[len(got)]) == ("Dash", "DashForward"):
                    got.append(ev[j - 1])
                elif presses[cur] and lab == want[cur]:
                    continue                 # the same move pressed again (x3)
                elif lab != "Melee":
                    break                    # a different move: not this combo
            # "Q2 (x3)" means three presses landed: the game's attack count for
            # that move has to reach exactly 3 (x2: 2), else it is another combo.
            if len(got) == len(want) and any(
                    n > 1 and max(rep.get(k, {}) or {1: 0}) != n for k, n in enumerate(presses)):
                continue
            if len(got) == len(want):
                tries.append([0] + [int(round((b[1] - a[1]) * 60)) for a, b in zip(got, got[1:])])
                reps.append(rep)
    if not tries:
        return None, []
    med = []
    for c in zip(*tries):
        c = sorted(c)
        m = c[len(c) // 2] if len(c) % 2 else int(round((c[len(c) // 2 - 1] + c[len(c) // 2]) / 2))
        # A try with a pause in it (they waited, it still landed): the quick
        # tries show the link works fast, so start from the quickest.
        if c[-1] > 1.5 * c[0] + 20:
            m = c[0]
        med.append(m)
    # When each extra press of a "Q2 (x3)" step landed, from the step coming out.
    repeat = {}
    for k, n in enumerate(presses):
        if n > 1:
            seen = [[r[k][c] for r in reps if c in r.get(k, {})] for c in range(2, n + 1)]
            if all(seen):
                repeat[k] = [sorted(v)[len(v) // 2] for v in seen]
    return {"gaps": med, "repeat": repeat}, tries


def parse(text, char, use_routes=True):
    """Notation -> list of steps {label, gap, rel, note?, hold_frames?, tag?}."""
    steps = []
    point = char
    learned = _learned_gaps()

    def add(label, chunk, **kw):
        prev = steps[-1]["label"] if steps else None
        rec = lambda l: "Dash" if l == "DashForward" else l   # recordings say Dash
        pair = f"{rec(prev)}>{rec(label)}"
        seen = (learned.get(point) or {}).get(pair)
        if seen is not None and seen > LINK_MAX:
            seen = None        # a pause in their play, not a combo link
        gap = seen if seen is not None else GAP_AFTER.get(prev, GAP)
        # A recording of this very combo: take its gap where it is longer than
        # practice's search reaches from the guess (a move that has to finish,
        # like Aizawa's Q4 before the last CA). Shorter ones the search finds,
        # and training-mode play is often slower than the combo needs.
        exact = ((learned.get("_exact") or {}).get(point) or {}).get(pair)
        # A recording of the player practicing combos (--learn-gaps --combo):
        # how fast they link these two inside a string - the best first guess.
        combo = ((learned.get("_combo") or {}).get(point) or {}).get(pair)
        if combo is not None:
            gap, exact = combo, None
        early = bool(steps and steps[-1].get("no_wait"))
        if exact is not None and exact > gap + SEARCH_REACH + 20 and not early:
            gap = exact        # (early) says not to wait the move out
        gap = 0 if not steps else gap
        st = {"label": label, "gap": gap, "rel": None, "src": chunk}
        if label == "Dodge":
            st["rel"] = 0.0            # cancels go toward the opponent
        if label == "DashBack":
            st["rel"] = 180.0
        st.update(kw)
        if st.get("after_grab"):
            st["gap"] = GRAB_GAP       # counted from the release, not the grab
        steps.append(st)

    for chunk in _split(text):
        # TQ2(a): the follow-up written in brackets is a step of its own.
        m = re.fullmatch(r"(?i)(.*?\b(?:TQ2|Q4))\s*\(\s*([A-Za-z0-9. ]+?)\s*\)", chunk)
        follow = None
        if m and point == "Bakugo":
            try:
                if len(_move(m.group(2), chunk)) == 1:
                    chunk, follow = m.group(1), m.group(2)
            except NotationError:
                pass
        notes = re.findall(r"\(([^)]*)\)", chunk)
        body = re.sub(r"\([^)]*\)", " ", chunk).strip()
        if not body:
            if steps and notes:
                steps[-1]["note"] = "; ".join(filter(None, [steps[-1].get("note"), *notes]))
            continue
        first = len(steps)
        no_wait = any(NO_WAIT.match(n) for n in notes)
        presses = next((int(m.group(1) or m.group(2)) for m in map(PRESSES.match, notes) if m), 0)
        # Prefixes: j. d. bd. r. t.NAME.
        while True:
            m = re.match(r"(?i)t\.\s*([A-Za-z][\w ]*?)\s*\.\s*", body)
            if m:
                point = _char(m.group(1))
                add("Tag", chunk, tag=point)
                body = body[m.end():]
                continue
            m = re.match(r"(?i)(bd|j|d|r)\.\s*", body)
            if m:
                add(PREFIX[m.group(1).lower()], chunk)
                body = body[m.end():]
                continue
            break
        charge = None
        m = re.fullmatch(r"\[\s*(.+?)\s*\](?::(\d+))?", body)
        if m:
            body, charge = m.group(1), int(m.group(2) or CHARGE_FRAMES)
        if body:
            for lab in _move(body, chunk):
                prev = steps[-1]["label"] if steps else None
                kw = {"hold_frames": charge} if charge else {}
                if point == "Bakugo" and prev == "ExUnique2" and lab in TQ2_FOLLOW:
                    kw["alias"] = [lab]
                    lab = TQ2_FOLLOW[lab]
                if point in GRAB_Q2 and prev == "Unique2" and not steps[-1].get("no_wait"):
                    # Q2 is a grab: wait for it to let go, then press.
                    kw["after_grab"] = True
                    kw.setdefault("note", "after the grab lets go")
                if lab == "ExMelee" and re.fullmatch(r"(?i)CA\d*", body.strip()):
                    # The yellow registers as ExMelee, but mid-string only
                    # guard + quirk 1 brings it out (stick + attack keeps jabbing).
                    kw["press"] = "CounterAttack"
                    kw["alias"] = ["CounterAttack"]
                add(lab, chunk, **kw)
        elif len(steps) == first:
            raise NotationError(f"nothing to press in '{chunk}'")
        if notes:
            steps[-1]["note"] = "; ".join(notes)
        if no_wait and len(steps) > first:
            steps[-1]["no_wait"] = True
        if presses > 1 and len(steps) > first:
            steps[-1]["presses"] = presses
        if follow:
            lab = _move(follow, chunk)[0]
            add(TQ2_FOLLOW.get(lab, lab), f"{chunk}({follow})", alias=[lab],
                note="TQ2 follow-up")
    if not steps:
        raise NotationError("no steps")
    # This exact combo learned from the player's clean tries (--learn-gaps
    # --notation): its own gap per step beats every pair-wise guess.
    own = ((learned.get("_routes") or {}).get(char) or {}).get(_key(text)) if use_routes else None
    if isinstance(own, list):
        own = {"gaps": own}
    if own and len(own.get("gaps") or []) == len(steps):
        for st, g in zip(steps[1:], own["gaps"][1:]):
            if not st.get("after_grab"):
                st["gap"] = g
        for k, offs in (own.get("repeat") or {}).items():
            if steps[int(k)].get("presses"):
                steps[int(k)]["repeat_at"] = offs
    return steps


def route(text, char, name=None):
    """A demo route (ajbot.demo format) from notation."""
    char = _char(char)
    steps = parse(text, char)
    tags = [s["tag"] for s in steps if s["label"] == "Tag"]
    r = {"name": name or f"{char}: {text.strip()}", "char": char, "source": "notation",
         "notation": text.strip(), "start_dist": None, "hits": None, "dealt": None,
         "steps": steps}
    if tags:
        r["partners"] = list(dict.fromkeys(t for t in tags if t != char))
    return r


def describe(step):
    s = step["label"] + (f" -> {step['tag']}" if step.get("tag") else "")
    if step.get("hold_frames"):
        s += f" (charge {step['hold_frames']}f)"
    if step.get("presses"):
        s += f" x{step['presses']} presses"
    if step.get("note"):
        s += f"  ({step['note']})"
    return s


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("notation", nargs="?")
    ap.add_argument("--char", required=True, help="who starts the combo (menu name)")
    ap.add_argument("--learn-gaps", nargs="+", metavar="RECORDING",
                    help="learn --char's timing between moves from recordings "
                         "into models/notation_gaps.json")
    ap.add_argument("--exact", action="store_true",
                    help="with --learn-gaps: the recordings are of this combo being "
                         "done - keep every gap as played, long ones too")
    ap.add_argument("--notation", dest="learn_notation", metavar="TEXT",
                    help="with --learn-gaps: learn this combo's own timing, step by "
                         "step, from the clean tries of it in the recordings")
    ap.add_argument("--combo", action="store_true",
                    help="with --learn-gaps: the recordings are of combos being "
                         "practiced - learn the links inside strings (under 2 s); "
                         "these win over every other guess")
    ap.add_argument("--side", choices=("me", "enemy"), default="me",
                    help="with --learn-gaps: the recorder's fighter or the opponent's")
    ap.add_argument("--save", metavar="NAME", help="save as a demo route for practice.py")
    ap.add_argument("--json", action="store_true", help="print the route as JSON")
    a = ap.parse_args(argv)
    if a.learn_gaps and a.learn_notation:
        char = _char(a.char)
        med, tries = learn_route(a.learn_gaps, a.learn_notation, char, a.side)
        for t in tries:
            print("  clean try:", t)
        if not med:
            sys.exit(f"no clean try of that combo in the recording ({char})")
        data = _learned_gaps()
        data.setdefault("_routes", {}).setdefault(char, {})[_key(a.learn_notation)] = med
        GAPS_PATH.parent.mkdir(parents=True, exist_ok=True)
        GAPS_PATH.write_text(json.dumps(data, indent=1, sort_keys=True))
        print(f"{len(tries)} clean tries -> {med} saved to {GAPS_PATH}")
        return 0
    if a.learn_gaps:
        char = _char(a.char)
        data = _learned_gaps()
        if a.exact or a.combo:
            key = "_exact" if a.exact else "_combo"
            gaps = learn_gaps(a.learn_gaps, char, a.side, max_gap_s=4.0 if a.exact else 2.0,
                              exact=a.exact, combo=a.combo)
            ex = data.setdefault(key, {})
            ex[char] = {**ex.get(char, {}), **gaps}
        else:
            gaps = learn_gaps(a.learn_gaps, char, a.side)
            data[char] = {**data.get(char, {}), **gaps}
        GAPS_PATH.parent.mkdir(parents=True, exist_ok=True)
        GAPS_PATH.write_text(json.dumps(data, indent=1, sort_keys=True))
        for k, v in sorted(gaps.items()):
            print(f"  {k:<34} {v:3d} frames")
        print(f"{len(gaps)} {char} gaps saved to {GAPS_PATH}")
        return 0
    if not a.notation:
        ap.error("give a notation (or --learn-gaps)")
    try:
        r = route(a.notation, a.char, a.save)
    except NotationError as e:
        if a.json:
            print(json.dumps({"error": str(e)}))
            return 1
        sys.exit(str(e))
    if a.save:
        old = demo.get(a.save)
        if old and old.get("name") == a.save and old.get("notation") == r["notation"]:
            r["learned"] = old.get("learned")      # same combo: keep what was learned
            for st, o in zip(r["steps"], old.get("steps") or []):   # and what steps register as
                if o.get("label") == st["label"] and o.get("alias"):
                    st["alias"] = sorted(set(st.get("alias") or []) | set(o["alias"]))
        demo.put(r)
    if a.json:
        print(json.dumps(r))
        return 0
    for k, s in enumerate(r["steps"]):
        print(f"{k:2d}  {describe(s)}")
    if a.save:
        print(f"saved '{a.save}' to {demo.DEMO_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
