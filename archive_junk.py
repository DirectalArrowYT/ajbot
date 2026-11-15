"""Archive recordings that can NEVER be trained on - not short, structurally
broken: no opponent identified for the whole run, or an old schema with no
movement fields. train.py's permanent_defect() re-derives this fresh on
every retrain, forever, for files that will never pass; this moves them out
of the way once instead.

    python archive_junk.py                  # runs/ and botruns/, dry run
    python archive_junk.py --apply           # actually move the files
    python archive_junk.py --apply --delete  # remove them outright instead

Nothing here is a judgement call the way cherry-picking thresholds are - a
recording with no opponent is not "somewhat useful", so unlike combo-quality
filtering this is safe to act on permanently. Defaults to a dry run because
moving files is still worth seeing before it happens.
"""
import argparse
import glob
import shutil
from pathlib import Path

import train as T

ARCHIVE_DIR = Path("archive") / "junk"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="runs/*.jsonl,botruns/*.jsonl",
                    help="comma-separated globs to scan")
    ap.add_argument("--apply", action="store_true",
                    help="actually move (or delete) files; default is a dry run")
    ap.add_argument("--delete", action="store_true",
                    help="delete instead of archiving (only with --apply)")
    args = ap.parse_args()

    files = []
    for pat in args.runs.split(","):
        files.extend(sorted(glob.glob(pat.strip())))
    if not files:
        print("no recordings found")
        return

    print(f"scanning {len(files)} recordings...")
    junk = []
    for f in files:
        n, reason = T.permanent_defect(f)
        if reason:
            junk.append((f, n, reason))

    if not junk:
        print("nothing to archive - every recording passes permanent_defect()")
        return

    total_mb = sum(Path(f).stat().st_size for f, _, _ in junk) / 1e6
    print(f"\n{len(junk)}/{len(files)} recordings are permanently unusable "
          f"({total_mb:.0f} MB):")
    for f, n, reason in junk:
        print(f"  {f}  ({n} ticks) - {reason}")

    if not args.apply:
        print("\ndry run - nothing moved. Re-run with --apply to act on this.")
        return

    if args.delete:
        for f, _, _ in junk:
            Path(f).unlink()
        print(f"\ndeleted {len(junk)} recordings ({total_mb:.0f} MB freed)")
        return

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    for f, _, _ in junk:
        src = Path(f)
        # Keep the source subfolder (runs/ vs botruns/) so nothing collides
        # and it is obvious where each file came from if you want it back.
        dest_dir = ARCHIVE_DIR / src.resolve().parent.name
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest_dir / src.name))
    print(f"\nmoved {len(junk)} recordings to {ARCHIVE_DIR} ({total_mb:.0f} MB)")


if __name__ == "__main__":
    main()
