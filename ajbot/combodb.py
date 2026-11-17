"""The combo library: combos/<Character>.json, shared on GitHub.

Combos people write down, in the notation of ajbot/notation.py, sorted into
three tiers:

  authored      works every time: fact-checked by people who play the
                character
  potential     works, with a catch: only midscreen, only on some
                characters, a tight link that drops now and then
  hypothetical  nobody has proven it yet: the game's own CPU combo lists,
                routes the bot found in recordings, ideas to try. Tested in
                training mode (or by the bot's practice), it moves up a tier
                or is removed

One file per character, so contributions rarely touch the same file:

    {"char": "Bakugo",
     "combos": [{"id": "bakugo-3f9a1c", "tier": "authored",
                 "notation": "AAA > Q1 > Dodge Cancel > AA > Q1",
                 "name": "midscreen BnB", "source": "community",
                 "author": "someone", "damage": 0.42,
                 "requires": {"awake": 1}, "partners": ["Todoroki"],
                 "conditions": "midscreen only", "downsides": "...",
                 "notes": "...", "evidence": "video link / bot landed 19/20",
                 "tested": {"attempts": 20, "landed": 19, "by": "bot",
                            "date": "2026-11-15"}}]}

Only `tier` and `notation` are required (`id` is filled in from the
notation). `source`: community | game-cpu | ml | draft.

    python -m ajbot.combodb check          # validate every file (CI runs this)
    python -m ajbot.combodb render         # write the .md pages
    python -m ajbot.combodb seed           # add CPU combos + mined routes as
                                           # hypothetical (needs models/)
    python -m ajbot.combodb fill-ids       # give new entries their id

With --routes the bot also follows authored and potential combos
(routes.RouteFollower), ranked above the CPU's lists.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

DIR = Path("combos")
TIERS = ["authored", "potential", "hypothetical"]
TIER_INFO = {
    "authored": "Works 100% of the time. Fact-checked by people who play the character.",
    "potential": "Works, with a catch: position (walls, midscreen), certain opponents, "
                 "or a link that is not consistent. The catch is written next to it.",
    "hypothetical": "Not proven yet: the game's own CPU combo lists, routes the bot found "
                    "in recordings, ideas. Test them and report back: each one moves up "
                    "a tier or gets removed.",
}
SOURCES = {"community", "game-cpu", "ml", "draft"}
FIELDS = {"id", "tier", "notation", "name", "source", "author", "damage", "requires",
          "partners", "conditions", "downsides", "notes", "evidence", "tested"}
ICON_IDS = Path("assets/icon_ids.json")


def roster():
    return list(json.loads(ICON_IDS.read_text(encoding="utf-8")))


def norm(text):
    return re.sub(r"\s+", " ", re.sub(r"\s*>\s*", " > ", text.strip())).lower()


def make_id(char, notation):
    slug = re.sub(r"[^a-z0-9]+", "", char.lower())
    return f"{slug}-{hashlib.sha1(norm(notation).encode()).hexdigest()[:6]}"


def path_of(char, root=DIR):
    return root / f"{char}.json"


def load(char, root=DIR):
    p = path_of(char, root)
    if not p.exists():
        return {"char": char, "combos": []}
    return json.loads(p.read_text(encoding="utf-8"))


def save(data, root=DIR):
    root.mkdir(parents=True, exist_ok=True)
    order = {t: i for i, t in enumerate(TIERS)}
    data["combos"].sort(key=lambda c: (order.get(c.get("tier"), 9), c.get("name") or "",
                                       len(c.get("notation", "")), c.get("notation", "")))
    path_of(data["char"], root).write_text(
        json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def chars_with_files(root=DIR):
    return sorted(p.stem for p in root.glob("*.json"))


# ------------------------------------------------------------------ check

def check(root=DIR, parse=True):
    """Problems as strings; empty when every file is fine."""
    from . import notation as N
    names = set(roster())
    errs = []
    for p in sorted(root.glob("*.json")):
        where = p.as_posix()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as e:
            errs.append(f"{where}: not valid JSON ({e})")
            continue
        char = data.get("char")
        if char != p.stem:
            errs.append(f"{where}: \"char\" is {char!r}, the file name says {p.stem!r}")
        if p.stem not in names:
            errs.append(f"{where}: {p.stem!r} is not a roster name (assets/icon_ids.json)")
            continue
        seen = {}
        for i, c in enumerate(data.get("combos") or []):
            at = f"{where} combo {i + 1}"
            if not isinstance(c, dict):
                errs.append(f"{at}: not an object")
                continue
            extra = set(c) - FIELDS
            if extra:
                errs.append(f"{at}: unknown field(s) {sorted(extra)}")
            if c.get("tier") not in TIERS:
                errs.append(f"{at}: tier must be one of {TIERS}, not {c.get('tier')!r}")
            text = c.get("notation")
            if not isinstance(text, str) or not text.strip():
                errs.append(f"{at}: no notation")
                continue
            if c.get("source") and c["source"] not in SOURCES:
                errs.append(f"{at}: source must be one of {sorted(SOURCES)}")
            if c.get("id") and c["id"] != make_id(p.stem, text) and not c["id"].startswith(
                    re.sub(r"[^a-z0-9]+", "", p.stem.lower()) + "-"):
                errs.append(f"{at}: id {c['id']!r} does not belong to {p.stem}")
            key = norm(text)
            if key in seen:
                errs.append(f"{at}: same notation as combo {seen[key]}")
            seen[key] = i + 1
            for k in ("partners",):
                bad = [x for x in c.get(k) or [] if x not in names]
                if bad:
                    errs.append(f"{at}: {k} not on the roster: {bad}")
            if parse:
                try:
                    N.parse(text, p.stem, use_routes=False)
                except Exception as e:      # NotationError, unknown partner ...
                    errs.append(f"{at}: notation does not read: {e}")
    return errs


def fill_ids(root=DIR):
    n = 0
    for char in chars_with_files(root):
        data = load(char, root)
        for c in data["combos"]:
            if not c.get("id"):
                c["id"] = make_id(char, c["notation"])
                n += 1
        save(data, root)
    return n


# ------------------------------------------------------------------ seed

def seed(root=DIR, mined_per_char=5):
    """Hypothetical entries from this machine's models/: the CPU's combo lists
    (models/ai_routes.json), the best mined routes and the draft routes.
    Existing entries (any tier) are never touched or duplicated."""
    from . import notation as N
    ids = json.loads(ICON_IDS.read_text(encoding="utf-8"))
    by_folder = {v: k for k, v in ids.items()}
    new = {}

    def offer(char, labels, **fields):
        text = N.round_trips(labels, char)
        if text:
            new.setdefault(char, []).append({"notation": text, **fields})

    ai = Path("models/ai_routes.json")
    if ai.exists():
        for folder, routes in json.loads(ai.read_text(encoding="utf-8"))["folders"].items():
            char = by_folder.get(folder)
            for r in routes if char else []:
                offer(char, r["steps"], source="game-cpu",
                      notes="From the game's own CPU combo list.")
    mined = Path("models/mined_routes.json")
    if mined.exists():
        for char, routes in json.loads(mined.read_text(encoding="utf-8"))["chars"].items():
            if char not in ids:
                continue
            best = sorted(routes, key=lambda r: -(r.get("mined") or {}).get("dmg", 0))
            for r in best[:mined_per_char]:
                m = r.get("mined") or {}
                offer(char, r["steps"], source="ml", damage=m.get("dmg"),
                      notes=f"Found in recordings: landed {m.get('seen', 1)} time(s).")
    drafts = Path("models/authored_routes.json")
    if drafts.exists():
        for char, routes in json.loads(drafts.read_text(encoding="utf-8"))["chars"].items():
            for r in routes:
                fields = {"source": "draft", "name": re.sub(r"\s*\(DRAFT\)", "", r.get("name", ""))}
                if r.get("requires"):
                    fields["requires"] = r["requires"]
                if r.get("notes"):
                    fields["notes"] = r["notes"]
                offer(char, r["steps"], **fields)
    added = 0
    for char, items in new.items():
        data = load(char, root)
        have = {norm(c["notation"]) for c in data["combos"]}
        for it in items:
            if norm(it["notation"]) in have:
                continue
            have.add(norm(it["notation"]))
            data["combos"].append({"id": make_id(char, it["notation"]), "tier": "hypothetical",
                                   **{k: v for k, v in it.items() if v not in (None, "")}})
            added += 1
        save(data, root)
    return added


# ------------------------------------------------------------------ render

REPO = "DirectalArrowYT/ajbot"
ISSUE = f"https://github.com/{REPO}/issues/new?template=combo.yml"


def _cell(v):
    return str(v).replace("|", "\\|").replace("\n", " ") if v not in (None, "") else ""


def _tested(t):
    if not t:
        return ""
    s = f"{t.get('landed', '?')}/{t.get('attempts', '?')}"
    if t.get("by"):
        s += f" ({t['by']}"
        s += f", {t['date']})" if t.get("date") else ")"
    return s


def render_char(data):
    char = data["char"]
    lines = [f"# {char} combos", "",
             f"[All characters](README.md) - [suggest a combo]({ISSUE}&character={char})"
             f" - data: [{char}.json]({char}.json)", ""]
    for tier in TIERS:
        rows = [c for c in data["combos"] if c.get("tier") == tier]
        lines += [f"## {tier.capitalize()} ({len(rows)})", "", f"_{TIER_INFO[tier]}_", ""]
        if not rows:
            lines += ["None yet.", ""]
            continue
        lines += ["| Combo | Notes | Source | Tested |", "|---|---|---|---|"]
        for c in rows:
            name = f"**{_cell(c['name'])}**<br>" if c.get("name") else ""
            req = c.get("requires") or {}
            notes = "; ".join(filter(None, [
                "needs Rising" if req.get("awake") else "",
                f"partners: {', '.join(c['partners'])}" if c.get("partners") else "",
                c.get("conditions"), f"downside: {c['downsides']}" if c.get("downsides") else "",
                f"{c['damage']:.0%} damage" if isinstance(c.get("damage"), (int, float)) else "",
                c.get("notes")]))
            src = c.get("source") or ""
            if c.get("author"):
                src += f" ({c['author']})"
            lines.append(f"| {name}`{_cell(c['notation'])}` | {_cell(notes)} | {_cell(src)} "
                         f"| {_cell(_tested(c.get('tested')))} |")
        lines.append("")
    return "\n".join(lines)


def render(root=DIR):
    chars = chars_with_files(root)
    rows = []
    for char in chars:
        data = load(char, root)
        (root / f"{char}.md").write_text(render_char(data), encoding="utf-8")
        n = {t: sum(1 for c in data["combos"] if c.get("tier") == t) for t in TIERS}
        rows.append(f"| [{char}]({char}.md) | {n['authored']} | {n['potential']} | {n['hypothetical']} |")
    head = [
        "# Combo library", "",
        "Combos for every character, written in the bot's combo notation and sorted by how sure "
        "we are that they work. The bot and the GUI's Combos page read the same files, so a "
        "combo added here can be watched, practiced and (with `--routes`) followed by the bot.", "",
        *[f"- **{t.capitalize()}**: {TIER_INFO[t]}" for t in TIERS], "",
        "## Adding a combo", "",
        f"- **No git needed:** [suggest a combo]({ISSUE}) and fill in the form.",
        "- **Pull request:** edit `combos/<Character>.json` (only `tier` and `notation` are "
        "required) and run `python -m ajbot.combodb check`. The pages here are rebuilt "
        "automatically after a merge.",
        "- Tested a hypothetical one? Report it with the form (landed / attempts, where on the "
        "stage), so it can move up or come out.", "",
        "Notation: `A` attack, `CA` counter attack, `GC` guard-cancel counter, `UB` unblockable, "
        "`Q1`/`Q2` quirks, `TQ1`/`TQ2` tilt quirks, `PU` Plus Ultra, `j.X` jump then X, `d.X` "
        "dash then X, `t.NAME.X` tag in NAME, `[X]` charge, `Dodge Cancel`, `Jump`, `Rising`, "
        "`EA`. Full list: [ajbot/notation.py](../ajbot/notation.py).", "",
        "| Character | Authored | Potential | Hypothetical |", "|---|---|---|---|",
    ]
    (root / "README.md").write_text("\n".join(head + rows) + "\n", encoding="utf-8")
    return len(chars)


# ------------------------------------------------------------------ routes

def routes_for_bot(root=DIR, tiers=("authored", "potential"), landed=False):
    """{char: [route]} in routes.py's format, for the tiers the bot may follow.
    With `landed`, also any combo the drill landed at least once (tested)."""
    from . import notation as N
    out = {}
    if not root.is_dir():
        return out
    for char in chars_with_files(root):
        try:
            data = load(char, root)
        except ValueError:
            continue
        for c in data.get("combos") or []:
            if c.get("tier") not in tiers and not (
                    landed and ((c.get("tested") or {}).get("landed") or 0) > 0):
                continue
            try:
                steps = [s["label"] for s in N.parse(c["notation"], char, use_routes=False)]
            except Exception:
                continue
            if "Tag" in steps:
                continue            # the follower drives one fighter
            r = {"name": f"{c.get('tier')}: {c.get('name') or c['notation']}", "steps": steps}
            if c.get("requires"):
                r["requires"] = c["requires"]
            out.setdefault(char, []).append(r)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["check", "render", "seed", "fill-ids"])
    ap.add_argument("--dir", default=str(DIR))
    ap.add_argument("--mined", type=int, default=5, help="seed: mined routes per character")
    a = ap.parse_args(argv)
    root = Path(a.dir)
    if a.cmd == "check":
        errs = check(root)
        print("\n".join(errs) if errs else f"{len(chars_with_files(root))} files OK")
        sys.exit(1 if errs else 0)
    if a.cmd == "render":
        print(f"rendered {render(root)} character pages + README.md")
    elif a.cmd == "seed":
        print(f"added {seed(root, a.mined)} hypothetical combos")
    elif a.cmd == "fill-ids":
        print(f"filled {fill_ids(root)} ids")


if __name__ == "__main__":
    main()
