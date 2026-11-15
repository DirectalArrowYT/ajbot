"""Sizing a ProcessPoolExecutor against actual memory, not just CPU count.

Naively using os.cpu_count() workers to parse recordings in parallel is what
took the corpus scan from "slow" to "memory maxed out, CPU barely doing
anything" (thrashing, not computing) - 20 workers each loading their own
multi-hundred-MB recording at once can exceed a 16GB machine's RAM even
when every individual file is unremarkable, and catastrophically so if one
file is oversized (see tools/prune_stale_chars.py for how that happened here).
Workers should scale with how much memory the files actually in flight need,
capped by cores as a ceiling, not the other way around.
"""
import ctypes
import os
import time

# Parsing a compact JSON line into nested Python dicts/lists costs several
# times its byte size - dict/list/str object overhead, key strings repeated
# per line rather than interned. Not measured precisely here; conservative
# on purpose, because guessing low means slower, guessing high means the
# exact thrashing this exists to prevent.
MEMORY_MULTIPLIER = 8
RAM_FRACTION = 0.5     # leave headroom for the main process and the OS


def available_ram_bytes():
    """Free physical memory, or None if it cannot be determined (the
    Windows-only fast path here; callers fall back to CPU count alone)."""
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return stat.ullAvailPhys
    except (AttributeError, OSError):
        pass
    try:                                    # Linux (the training server)
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    free = int(line.split()[1]) * 1024
                    # league.py trains inside a memory-capped scope: the
                    # box's free RAM is not ours to fill.
                    cap = os.environ.get("AJBOT_MEM_CAP_GB")
                    return min(free, int(float(cap) * 1e9)) if cap else free
    except OSError:
        pass
    return None


def cpu_count():
    """Cores this process may use: the affinity mask where there is one
    (the server runs training under `taskset` to leave room for its other
    jobs), else os.cpu_count()."""
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def safe_worker_count(file_sizes, max_workers=None):
    """How many of these files (by byte size) can safely be processed at
    once, given free RAM - never more than max_workers (default: cpu_count).

    Greedily sums the LARGEST files first, since those dominate peak memory:
    if the biggest 3 already fill the budget, running a 4th at the same time
    does not help even if a hundred small ones are waiting behind them.
    """
    cap = max_workers or cpu_count()
    sizes = sorted(file_sizes, reverse=True)
    if not sizes:
        return max(1, min(cap, 1))
    ram = available_ram_bytes()
    if ram is None:
        return max(1, min(cap, len(sizes)))
    budget = ram * RAM_FRACTION
    total = 0
    n = 0
    for s in sizes:
        total += s * MEMORY_MULTIPLIER
        if total > budget and n >= 1:
            break
        n += 1
    return max(1, min(cap, n, len(sizes)))


class Progress:
    """A single self-overwriting console line with an ETA.

    The "scanner" steps (mine.py, combobook.py) used to run silently for
    minutes with no way to tell how much was left - this gives every one of
    them the same line for free: done/total, percent, elapsed, ETA.
    """

    def __init__(self, total, label="progress"):
        self.total = total
        self.label = label
        self.done = 0
        self.start = time.perf_counter()

    def update(self, n=1):
        self.done += n
        elapsed = time.perf_counter() - self.start
        frac = self.done / self.total if self.total else 1.0
        eta = (elapsed / frac - elapsed) if frac > 0 else 0.0
        width = 24
        filled = int(width * frac)
        bar = "#" * filled + "-" * (width - filled)
        end = "\n" if self.done >= self.total else ""
        print(f"\r  {self.label} [{bar}] {self.done}/{self.total} "
              f"({100*frac:.0f}%)  elapsed {elapsed:.0f}s  ETA {eta:.0f}s   ",
              end=end, flush=True)
