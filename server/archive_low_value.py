"""Keep only the self-play recordings that move the bot forward; delete the rest.

A bot recording is KEPT when it shows progress:
  - a combo segment that beats the combo book for that character (more hits
    or more damage than its best route), or
  - a new route: a segment whose opening (first 4 commands) the book has not
    seen, at least as long as that character's usual long segment (p75), or
  - combo value density (kept-combo damage per 1000 ticks) in the top
    `1 - bottom_frac` of the batch.
Everything else - and anything train.py can never use (permanent_defect) -
is DELETED, not archived: an archive of runs nobody will look at again is
just disk. On top of that the corpus is capped (--max-gb): when botruns/
grows past it, the lowest-scoring files go first, progress files last.

Never touches runs/, nor a solo botruns/<ts>.jsonl (play.py, one per match:
the bot against a real person or the game's CPU): human recordings are the
scarce, trusted signal. Only self-play (<ts>.<tag>.jsonl, -iN copies) goes.

    python server/archive_low_value.py                        # score botruns/, dry run
    python server/archive_low_value.py --apply                 # delete the losers
    python server/archive_low_value.py --apply --max-gb 8      # and cap botruns/ at 8 GB
    python server/archive_low_value.py --apply --purge-archive # also empty archive/

Every --apply also sweeps cache/ of the digests, facts and training arrays
of recordings that no longer exist (deleted here, or by hand): tickcache
keys them by file name and never cleans up, so without this the cache grew
to ten times the size of the recordings it served.
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root + script folders on sys.path)
import argparse
import glob
import json
import os
import re
import shutil
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from ajbot.parallel import safe_worker_count

WORKERS = min(8, os.cpu_count() or 1)
SCORES = Path("cache") / "run_scores.json"
# The newest book: the league's last fit (models/fit), else the champion's.
BOOK = next((p for p in (Path("models") / "fit" / "combobook.json",
                         Path("models") / "combobook.json") if p.exists()),
            Path("models") / "combobook.json")
PREFIX = 4
SOLO = re.compile(r"^\d+\.jsonl$")      # play.py's file: someone else on the other pad


def prunable(f):
    return not SOLO.match(Path(f).name)

_book = None


def _load_book():
    """Per char: (best hits, best dmg, known openings, p75 segment length)."""
    global _book
    if _book is None:
        _book = {}
        try:
            chars = json.loads(BOOK.read_text(encoding="utf-8")).get("chars") or {}
        except (OSError, ValueError):
            chars = {}
        for ch, page in chars.items():
            routes = page.get("routes") or []
            _book[ch] = (max((r["hits"] for r in routes), default=0),
                         max((r["dmg"] for r in routes), default=0.0),
                         {tuple(r["seq"][:PREFIX]) for r in routes},
                         page.get("seg_p75") or 0)
    return _book


def _score_one(args):
    """Worker: one file's value density and how many progress segments it has."""
    path, min_damage, min_hits, top_frac = args
    from ajbot import dataset as D
    from ajbot.combobook import _segments_from
    from ajbot.combos import Picker
    from ajbot.roster import INDEX, base_name

    try:
        ticks = D.load(path)
    except Exception as e:
        return {"path": path, "err": str(e)}
    if not ticks:
        return {"path": path, "density": 0.0, "ticks": 0, "progress": 0}
    kept = Picker(min_damage=min_damage, min_hits=min_hits, top_frac=top_frac).combos(ticks)
    density = 1000.0 * sum(c.dealt for c in kept) / len(ticks)
    book, progress, why = _load_book(), 0, []
    try:
        segs = list(_segments_from(ticks))
    except Exception:
        segs = []
    for char, cmds, hits, dmg, side in segs:
        if side != "us" or not cmds or cmds == "__tag__" or hits < 3:
            continue
        if isinstance(char, str) and char.startswith("Pl"):
            char = base_name(char)
        elif char not in INDEX:
            continue
        best_h, best_d, openings, p75 = book.get(char, (0, 0.0, set(), 0))
        # A new route only counts with real damage behind it: at least half
        # of the book's best for that character, and longer than usual.
        if dmg > best_d or (hits > best_h and dmg >= 0.5 * best_d):
            progress += 1
            why.append(f"{char} {hits}hit {dmg:.2f} beats book")
        elif (tuple(cmds[:PREFIX]) not in openings and hits >= max(p75 + 2, 6)
              and dmg >= 0.5 * best_d):
            progress += 1
            why.append(f"{char} new route {'>'.join(cmds[:PREFIX])} {hits}hit")
    return {"path": path, "density": round(density, 4), "ticks": len(ticks),
            "progress": progress, "why": why[:3]}


def score(files, min_damage=0.05, min_hits=2, top_frac=0.5, workers=WORKERS):
    files = list(files)
    if not files:
        return []
    n = safe_worker_count([Path(f).stat().st_size for f in files], workers)
    jobs = [(f, min_damage, min_hits, top_frac) for f in files]
    if len(jobs) > 1 and n > 1:
        with ProcessPoolExecutor(max_workers=n) as ex:
            return list(ex.map(_score_one, jobs))
    return [_score_one(j) for j in jobs]


def load_scores():
    try:
        return json.loads(SCORES.read_text())
    except (OSError, ValueError):
        return {}


def save_scores(s):
    SCORES.parent.mkdir(parents=True, exist_ok=True)
    tmp = SCORES.with_suffix(".tmp")
    tmp.write_text(json.dumps(s))
    tmp.replace(SCORES)


def book_time():
    try:
        return BOOK.stat().st_mtime
    except OSError:
        return 0.0


def fresh(sc, book_t):
    """Progress still news: scored against a book older than the current one.
    Once a newer book exists it was built from this file too, so the combo
    that beat the old book is in it and the file is worth only its density.
    (Progress was cached forever before, which protected 737 of 738 files and
    left the cap nothing to delete but the newest recordings.)"""
    return bool(sc.get("progress")) and sc.get("at", 0) > book_t


def judge(results, bottom_frac, book_t=0.0):
    """Split newly scored results into (keep, drop). Fresh progress stays;
    the rest must be in the top (1 - bottom_frac) of the batch by density.
    Only the new batch: run over the whole corpus every pass this would cut
    70% of every older plain file again and again."""
    ok = [r for r in results if not r.get("err")]
    plain = sorted((r for r in ok if not fresh(r, book_t)), key=lambda r: r["density"])
    cut = int(round(len(plain) * bottom_frac))
    drop = plain[:cut] + [r for r in results if r.get("err")]
    dropped = {r["path"] for r in drop}
    return [r for r in ok if r["path"] not in dropped], drop


def cap(top, max_gb, scores, book_t=0.0):
    """Paths to delete so `top`/*.jsonl fits in max_gb: lowest density first,
    files with fresh progress last. Unscored files are left alone."""
    files = [(f, os.path.getsize(f)) for f in glob.glob(f"{top}/*.jsonl") if prunable(f)]
    total = sum(s for _, s in files)
    limit = max_gb * 1e9
    if total <= limit:
        return []
    ranked = sorted((f for f, _ in files if Path(f).name in scores),
                    key=lambda f: (fresh(scores[Path(f).name], book_t),
                                   scores[Path(f).name]["density"]))
    size = dict(files)
    out = []
    for f in ranked:
        if total <= limit:
            break
        out.append(f)
        total -= size[f]
    return out


def sweep_cache(apply=True):
    """Delete cache files whose recording is gone. -> (files, bytes).

    cache/ticks and cache/meta are named '<file>.jsonl.<...>'; the training
    arrays in cache/ itself '<stem>.<kind>...'. A cache file is kept while any
    recording in runs/, botruns/ or archive/ could own it (when unsure, keep)."""
    live = {Path(f).name for f in glob.glob("runs/*.jsonl") + glob.glob("botruns/*.jsonl")
            + glob.glob("archive/**/*.jsonl", recursive=True)}
    if not live:
        return 0, 0                  # wrong folder, or everything moved: touch nothing
    stems = {n[:-len(".jsonl")] for n in live}
    n = size = 0
    root = Path("cache")
    for sub in ("ticks", "meta"):
        d = root / sub
        if not d.is_dir():
            continue
        for e in os.scandir(d):
            i = e.name.find(".jsonl.")
            if i < 0 or e.name[:i + 6] in live:
                continue
            n += 1
            size += e.stat().st_size
            if apply:
                os.unlink(e.path)
    if root.is_dir():
        for e in os.scandir(root):
            if not e.is_file() or not e.name.endswith((".npz", ".json")) \
                    or not e.name[:1].isdigit():
                continue
            parts = e.name.split(".")
            # '<ts>.<kind>...' or '<ts>.<tag>.<kind>...': owned if either stem lives
            if parts[0] in stems or ".".join(parts[:2]) in stems:
                continue
            n += 1
            size += e.stat().st_size
            if apply:
                os.unlink(e.path)
    return n, size


def junk(files):
    import train as T
    out = []
    for f in files:
        _, reason = T.permanent_defect(f)
        if reason:
            out.append(f)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="botruns/*.jsonl",
                    help="glob to score; never point this at runs/")
    ap.add_argument("--bottom-frac", type=float, default=0.7,
                    help="share of the no-progress recordings to delete, lowest value first")
    ap.add_argument("--max-gb", type=float, default=None,
                    help="cap the folder of --runs at this size (deletes lowest scores)")
    ap.add_argument("--min-damage", type=float, default=0.05)
    ap.add_argument("--min-hits", type=int, default=2)
    ap.add_argument("--top-frac", type=float, default=0.5)
    ap.add_argument("--rescore", action="store_true", help="ignore cached scores")
    ap.add_argument("--purge-archive", action="store_true",
                    help="also delete archive/low_value and archive/junk")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete; default is a dry run")
    args = ap.parse_args()
    if "runs/" in args.runs and "botruns/" not in args.runs:
        raise SystemExit("refusing to prune human recordings (runs/)")

    files = sorted(f for f in glob.glob(args.runs) if prunable(f))
    scores = {} if args.rescore else load_scores()
    bad = set(junk(files))
    todo = [f for f in files if f not in bad and Path(f).name not in scores]
    print(f"{len(files)} recordings, {len(bad)} unusable, {len(todo)} to score")
    book_t, now = book_time(), __import__("time").time()
    new = []
    for r in score(todo, args.min_damage, args.min_hits, args.top_frac):
        if not r.get("err"):
            scores[Path(r["path"]).name] = dict({k: r[k] for k in ("density", "ticks", "progress", "why")}, at=now)
        new.append(r)
    results = [dict(scores[Path(f).name], path=f) for f in files
               if f not in bad and Path(f).name in scores]
    _, drop = judge([dict(r, at=now) for r in new], args.bottom_frac, book_t)
    doomed = sorted(bad | {r["path"] for r in drop})
    keep = [r for r in results if r["path"] not in set(doomed)]
    prog = sum(1 for r in keep if fresh(r, book_t))
    print(f"keep {len(keep)} ({prog} with progress), delete {len(doomed)}")
    for r in sorted(keep, key=lambda r: -r["progress"])[:5]:
        if r["progress"]:
            print(f"  progress: {Path(r['path']).name}: {'; '.join(r['why'])}")
    if args.max_gb:
        top = str(Path(args.runs).parent)
        over = [f for f in cap(top, args.max_gb, scores, book_t) if f not in doomed]
        print(f"cap {args.max_gb:g} GB: {len(over)} more go")
        doomed += over
    mb = sum(os.path.getsize(f) for f in doomed) / 1e6
    if not args.apply:
        print(f"dry run - would delete {len(doomed)} files ({mb:.0f} MB)")
        n, size = sweep_cache(apply=False)
        print(f"dry run - cache: would delete {n} stale files ({size / 1e6:.0f} MB)")
        return
    for f in doomed:
        Path(f).unlink(missing_ok=True)
        scores.pop(Path(f).name, None)
    save_scores(scores)
    print(f"deleted {len(doomed)} recordings ({mb:.0f} MB)")
    if args.purge_archive:
        for d in (Path("archive") / "low_value", Path("archive") / "junk"):
            if d.exists():
                shutil.rmtree(d)
                print(f"deleted {d}")
    n, size = sweep_cache()
    print(f"deleted {n} stale cache files ({size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
