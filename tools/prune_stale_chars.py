"""Drop chars[] entries no training code ever reads.

A long play2.py --auto session leaks: something never resets fighter/roster
state between matches (see the linked follow-up task), so every later tick
in that session carries EVERY fighter from EVERY earlier match too - one
file on disk hit 78 "characters" per tick, 100KB/line, 12GB total, when a
real match has ~6.

This is lossless, not a judgement call like cherry-picking: dataset.py's
resolve()/_rec()/_bench()/stabilize()/rewards() only ever read a chars[]
entry if it is named by me/enemy/mine/theirs for THAT tick, or is fresher
than BENCH_AGE (3000) - everything else already sits there unread by any
consumer. Stripping it changes nothing about what training sees.

Streams line by line, so even a many-GB file never has to fit in memory at
once. Originals are archived (gzip) before being replaced, never deleted.

    python prune_stale_chars.py                    # dry run, default >=50MB
    python prune_stale_chars.py --apply
    python prune_stale_chars.py --apply --min-size-mb 10
"""
import _root  # noqa: F401,E402  (repo root on sys.path and as cwd)
import argparse
import glob
import gzip
import json
import shutil
from pathlib import Path

from ajbot.dataset import BENCH_AGE

ARCHIVE_DIR = Path("archive") / "oversized_originals"


def _prune_tick(d):
    chars = d.get("chars")
    if not chars:
        return 0, 0
    keep_keys = {k for k in (d.get("me"), d.get("enemy")) if k}
    keep_keys.update(d.get("mine") or [])
    keep_keys.update(d.get("theirs") or [])
    before = len(chars)
    pruned = {k: r for k, r in chars.items()
             if k in keep_keys or (r.get("age", 1e9) <= BENCH_AGE)}
    if len(pruned) != before:
        d["chars"] = pruned
    return before, len(pruned)


def process(path):
    """Stream `path` into a sibling .pruned file; returns stats or None if
    nothing changed (still writes the .pruned file - caller decides)."""
    tmp = Path(str(path) + ".pruned")
    total_before = total_after = lines = 0
    changed = False
    with open(path, encoding="utf-8") as src, tmp.open("w", encoding="utf-8") as dst:
        for line in src:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if not d.get("summary"):
                before, after = _prune_tick(d)
                total_before += before
                total_after += after
                lines += 1
                changed = changed or (after != before)
            dst.write(json.dumps(d) + "\n")
    if not changed:
        tmp.unlink()
        return None
    return {
        "path": path, "old_size": Path(path).stat().st_size,
        "new_size": tmp.stat().st_size, "lines": lines,
        "avg_before": total_before / max(lines, 1),
        "avg_after": total_after / max(lines, 1), "tmp": tmp,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="runs/*.jsonl,botruns/*.jsonl")
    ap.add_argument("--min-size-mb", type=float, default=50,
                    help="only check files at least this big")
    ap.add_argument("--apply", action="store_true",
                    help="actually replace files; default is a dry run")
    args = ap.parse_args()

    files = []
    for pat in args.runs.split(","):
        files.extend(sorted(glob.glob(pat.strip())))
    files = [f for f in files if Path(f).stat().st_size >= args.min_size_mb * 1e6]
    print(f"checking {len(files)} file(s) >= {args.min_size_mb:.0f}MB...")

    results = []
    for f in files:
        r = process(f)
        if r:
            results.append(r)
        else:
            print(f"  {f}: nothing to prune")

    if not results:
        print("\nno files needed pruning")
        return

    total_old = sum(r["old_size"] for r in results)
    total_new = sum(r["new_size"] for r in results)
    print(f"\n{len(results)} file(s) have prunable bloat:")
    for r in results:
        print(f"  {r['path']}: {r['old_size']/1e6:8.1f}MB -> {r['new_size']/1e6:7.1f}MB "
              f"({r['avg_before']:.0f} -> {r['avg_after']:.1f} chars/tick avg)")
    print(f"\ntotal: {total_old/1e9:.2f}GB -> {total_new/1e9:.2f}GB "
          f"({100*(1-total_new/total_old):.0f}% smaller)")

    if not args.apply:
        for r in results:
            r["tmp"].unlink()
        print("\ndry run - nothing changed. Re-run with --apply.")
        return

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    for r in results:
        src = Path(r["path"])
        archived = ARCHIVE_DIR / (src.name + ".gz")
        with src.open("rb") as fin, gzip.open(archived, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        src.unlink()
        r["tmp"].rename(src)
        print(f"  {src}: original archived to {archived}")
    print(f"\napplied - originals archived (gzip) under {ARCHIVE_DIR}")


if __name__ == "__main__":
    main()
