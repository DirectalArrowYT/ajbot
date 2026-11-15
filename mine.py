"""Mine the recordings for combos that worked, and report what the bot knows.

    python mine.py                          # everything, human and bot runs
    python mine.py --bot                    # only the bot's own runs
    python mine.py --character Bakugo       # drill into one fighter
    python mine.py --top 20                 # the single best combos found
    python mine.py --save models/combos.json
    python mine.py --min-damage .08 --top-frac .3     # a stricter cut

This is the inspection side of ajbot.combos. It answers three questions:

  what works   - per character, which command sequences keep producing damage
  how much     - what a given selection policy would actually feed to training
  what is thin - which characters have too few successful examples to learn from

The last one is the point of doing this at all. A 71-strong roster cannot be
covered by recording human matches, so the bot has to supply its own examples;
this says where it still has none.
"""
import argparse
import collections
import glob
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from ajbot import combos as C, dataset as D, tickcache
from ajbot.parallel import Progress, safe_worker_count

BLIND_LIMIT = 0.5       # reject a run this blind to its opponent
MIN_TICKS = 500


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default=None,
                    help="glob (default: runs/ and botruns/ together)")
    ap.add_argument("--bot", action="store_true", help="only botruns/")
    ap.add_argument("--human", action="store_true", help="only runs/")
    ap.add_argument("--min-damage", type=float, default=0.05)
    ap.add_argument("--min-hits", type=int, default=2)
    ap.add_argument("--top-frac", type=float, default=0.5,
                    help="fraction of each character's combos to keep")
    ap.add_argument("--per-char-cap", type=int, default=None,
                    help="hard cap on combos kept per character")
    ap.add_argument("--allow-losses", action="store_true",
                    help="keep combos that cost more health than they won")
    ap.add_argument("--break-s", type=float, default=C.BREAK_S)
    ap.add_argument("--lead-s", type=float, default=C.LEAD_S)
    ap.add_argument("--min-count", type=int, default=2,
                    help="times a sequence must recur to count as known")
    ap.add_argument("--top", type=int, default=10, help="best combos to list")
    ap.add_argument("--character", default=None, help="drill into one character")
    ap.add_argument("--blind-limit", type=float, default=BLIND_LIMIT)
    ap.add_argument("--min-ticks", type=int, default=MIN_TICKS)
    ap.add_argument("--save", metavar="FILE", default=None,
                    help="write the repertoire as JSON")
    ap.add_argument("--quiet", action="store_true", help="skip the per-file table")
    return ap.parse_args()


def sources(args):
    if args.runs:
        return sorted(glob.glob(args.runs))
    pats = []
    if not args.bot:
        pats.append("runs/*.jsonl")
    if not args.human:
        pats.append("botruns/*.jsonl")
    return [f for p in pats for f in sorted(glob.glob(p))]


def _scan_one(job):
    """Cached per recording and settings (ajbot.tickcache) - see _scan_one_raw."""
    from ajbot import tickcache
    name = "mine" + "_".join(str(x) for x in job[1:]).replace(".", "p").replace(" ", "")
    r = tickcache.cached_pickle(job[0], name[:120], lambda p: _scan_one_raw((p,) + tuple(job[1:])))
    # The cached tuple holds the path as spelled where it was built ('runs\\x'
    # on Windows); the caller's spelling is the one scan() sorts by.
    return (job[0],) + tuple(r[1:])


def _scan_one_raw(job):
    """Worker: one recording's (path, n_ticks, found, kept, reject_reason).
    Module-level so it can be pickled and run in a subprocess."""
    (path, min_ticks, blind_limit, break_s, lead_s, min_damage, min_hits,
     allow_losses, top_frac, per_char_cap) = job
    ticks = D.load(path)
    if len(ticks) < min_ticks:
        return path, len(ticks), None, None, f"only {len(ticks)} ticks"
    blind = C.blind_fraction(ticks)
    if blind > blind_limit:
        return path, len(ticks), None, None, f"no opponent for {blind*100:.0f}% of ticks"
    cs = C.find(ticks, break_s=break_s, lead_s=lead_s)
    ks = C.select(cs, min_damage=min_damage, min_hits=min_hits,
                 require_net=not allow_losses, top_frac=top_frac,
                 per_char_cap=per_char_cap)
    return path, len(ticks), cs, ks, None


def scan(files, picker, args):
    """(kept, all, rejected, per_file) across every usable recording.

    Each file's scan is independent, so - like train.py's feature build -
    this runs across a process pool sized against free RAM (a scan loads
    every tick of a file into memory, same as training's cache build) with
    a progress line, instead of one file at a time with no feedback.
    """
    found, kept, rejected, per_file = [], [], [], []
    if not files:
        return found, kept, rejected, per_file
    jobs = [(f, args.min_ticks, args.blind_limit, args.break_s, args.lead_s,
             args.min_damage, args.min_hits, args.allow_losses, args.top_frac,
             args.per_char_cap) for f in files]
    sizes = tickcache.src_sizes(files)
    n_workers = safe_worker_count(sizes)
    progress = Progress(len(files), "scanning")

    def handle(result):
        path, n, cs, ks, reason = result
        progress.update()
        if reason:
            rejected.append((path, reason))
            return
        found.extend(cs)
        kept.extend(ks)
        per_file.append((path, n, cs, ks))

    if len(jobs) > 1 and n_workers > 1:
        with ProcessPoolExecutor(max_workers=n_workers) as ex:
            # as_completed, not map(): map yields in SUBMISSION order, so one
            # slow file at the front of the list would stall every progress
            # update behind it even while later, faster files keep finishing.
            futures = [ex.submit(_scan_one, j) for j in jobs]
            for fut in as_completed(futures):
                handle(fut.result())
    else:
        for j in jobs:
            handle(_scan_one(j))
    order = {f: i for i, f in enumerate(files)}
    per_file.sort(key=lambda pf: order[pf[0]])
    return found, kept, rejected, per_file


def print_files(per_file):
    print(f"\n{'recording':<28}{'ticks':>8}{'combos':>8}{'kept':>7}"
          f"{'damage':>9}{'best':>7}")
    for f, n, cs, ks in per_file:
        name = Path(f).name
        print(f"  {name:<26}{n:>8}{len(cs):>8}{len(ks):>7}"
              f"{sum(c.dealt for c in ks):>9.2f}"
              f"{max((c.dealt for c in ks), default=0):>7.2f}")


def print_coverage(kept, found):
    """Per character, with the thin ones called out."""
    cov = C.coverage(kept)
    seen = C.coverage(found)
    print(f"\n{'character':<24}{'kept':>6}{'found':>7}{'damage':>9}"
          f"{'best':>7}{'hits/combo':>12}{'tagged':>8}")
    rows = sorted(cov.items(), key=lambda kv: -kv[1]["combos"])
    for name, s in rows:
        print(f"  {name:<22}{s['combos']:>6}{seen.get(name, {}).get('combos', 0):>7}"
              f"{s['dealt']:>9.2f}{s['best']:>7.3f}"
              f"{s['hits']/s['combos']:>12.1f}{s['tagged']:>8}")

    thin = [n for n, s in cov.items() if s["combos"] < 3]
    missing = sorted(set(seen) - set(cov))
    if thin:
        print(f"\n  thin (under 3 kept combos): {', '.join(sorted(thin))}")
    if missing:
        print(f"  seen but nothing kept:      {', '.join(missing)}")
    print(f"\n  {len(cov)} characters have usable examples; "
          f"{len(seen)} appeared at all")


def print_repertoire(kept, args):
    reps = C.repertoire(kept, min_count=args.min_count)
    if args.character:
        reps = [r for r in reps if r["character"].lower() == args.character.lower()]
    if not reps:
        print(f"\nno sequence recurred {args.min_count}+ times yet - "
              f"either lower --min-count or record more")
        return reps
    print(f"\nknown combos (a sequence seen {args.min_count}+ times):")
    cur = None
    for r in sorted(reps, key=lambda r: (r["character"], -r["mean_damage"])):
        if r["character"] != cur:
            cur = r["character"]
            print(f"\n  {cur}")
        seq = " > ".join(r["sequence"])
        tag = "  [tag]" if r["tagged"] else ""
        print(f"    x{r['count']:<3} {r['mean_damage']:.3f} avg  "
              f"{r['best_damage']:.3f} best  {r['mean_hits']:.1f} hits   "
              f"{seq}{tag}")
    return reps


def print_best(kept, args):
    best = sorted(kept, key=lambda c: -c.dealt)
    if args.character:
        best = [c for c in best
                if C.short(c.character).lower() == args.character.lower()]
    print(f"\nbest {min(args.top, len(best))} combos:")
    for c in best[:args.top]:
        chain = (" via " + " > ".join(C.short(a) for a in c.attackers)
                 if len(c.attackers) > 1 else "")
        print(f"  {C.short(c.character):<18}{c.dealt:.3f} dmg  {c.hits:>2} hits  "
              f"{c.duration:>5.1f}s  net{c.net:+.3f}   "
              f"{' > '.join(c.actions[:5])}{chain}")


def save(path, reps, kept, files, picker):
    cov = C.coverage(kept)
    chars = {}
    for name, s in cov.items():
        chars[name] = dict(s, sequences=[
            {k: (list(v) if k == "sequence" else v) for k, v in r.items()
             if k != "character"}
            for r in reps if r["character"] == name])
    out = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "policy": picker.tag,
        "recordings": len(files),
        "combos_kept": len(kept),
        "characters": chars,
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwrote {p}  ({len(chars)} characters, "
          f"{sum(len(c['sequences']) for c in chars.values())} known sequences)")


def main():
    args = parse_args()
    files = sources(args)
    if not files:
        print("no recordings found")
        return 1
    picker = C.Picker(min_damage=args.min_damage, min_hits=args.min_hits,
                      top_frac=args.top_frac, per_char_cap=args.per_char_cap,
                      require_net=not args.allow_losses,
                      break_s=args.break_s, lead_s=args.lead_s)
    print(f"scanning {len(files)} recordings with {picker.tag}")

    found, kept, rejected, per_file = scan(files, picker, args)
    if rejected:
        print(f"\nrejected {len(rejected)} recordings:")
        for f, why in rejected:
            print(f"  {Path(f).name:<28}{why}")
    if not found:
        print("\nno combos found in anything usable")
        return 1

    if not args.quiet:
        print_files(per_file)
    print(f"\nfound: {C.summarize(found)}")
    print(f"kept:  {C.summarize(kept)}")
    ticks_kept = sum(c.end_i - c.start_i + 1 for c in kept)
    ticks_all = sum(n for _, n, _, _ in per_file)
    print(f"       {ticks_kept} of {ticks_all} ticks "
          f"({100*ticks_kept/max(ticks_all,1):.1f}%) would reach training")

    print_coverage(kept, found)
    reps = print_repertoire(kept, args)
    print_best(kept, args)
    if args.save:
        save(args.save, reps, kept, files, picker)
    return 0


if __name__ == "__main__":
    sys.exit(main())
