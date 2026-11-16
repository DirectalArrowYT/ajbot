"""Call Unreal functions (UFunctions) in the running game through ajhook's 'U'.

UE4SS can find objects and read/write properties in this build, but its
function calls do nothing: the address it scans as ProcessEvent is a folded
`ret 0` stub. ajhook calls the real UObject::ProcessEvent on the game thread.

    python server/uecall.py selftest        # KismetMathLibrary.Add_IntInt(2, 40)

Objects and functions are found by path through UE4SS (AJProbe); addresses
are cached per game session.
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root + script folders on sys.path)
import struct

import nextmatch as nm
from ueprobe import probe

_cache = {}


def find(path):
    """Address of the object at `path` (StaticFindObject), cached."""
    if path not in _cache:
        r = probe('local o = StaticFindObject("%s")\n'
                  'if o and o:IsValid() then out(string.format("0x%%x", o:GetAddress())) '
                  'else out("none") end' % path, timeout=30).strip()
        if not r.startswith("0x"):
            raise LookupError(f"no object {path}")
        _cache[path] = int(r, 16)
    return _cache[path]


def find_first(cls):
    """Address of the first live instance of class `cls` (FindFirstOf), not cached."""
    r = probe('local o = FindFirstOf("%s")\n'
              'if o and o:IsValid() then out(string.format("0x%%x", o:GetAddress())) '
              'else out("none") end' % cls, timeout=30).strip()
    return int(r, 16) if r.startswith("0x") else None


def call(obj, func, parms=b""):
    """ProcessEvent(obj, func, parms) on the game thread; returns the parms
    buffer afterwards (ParmsSize bytes: return value and out params in it)."""
    if isinstance(func, str):
        func = find(func)
    r = nm.call(f"U{obj:#x} {func:#x} {parms.hex()}")
    if r.startswith("!"):
        raise RuntimeError(r)
    return bytes.fromhex(r)


def cdo(cls_path):
    """Class default object of a /Script/Module.Class path (library functions
    are called on it)."""
    mod, _, name = cls_path.rpartition(".")
    return find(f"{mod}.Default__{name}")


def selftest():
    lib = cdo("/Script/Engine.KismetMathLibrary")
    out = call(lib, "/Script/Engine.KismetMathLibrary:Add_IntInt", struct.pack("<ii", 2, 40))
    got = struct.unpack_from("<i", out, 8)[0]
    print(f"Add_IntInt(2, 40) = {got}")
    return got == 42


if __name__ == "__main__":
    if sys.argv[1:] == ["selftest"]:
        sys.exit(0 if selftest() else 1)
    print(__doc__)
