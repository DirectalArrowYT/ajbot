# IntelSyncFix: online desync fix for Intel vs AMD

In online matches between an **Intel** PC and an **AMD** PC, All's Justice can desync and kick both players, most
often after big knockback (Nana's Plus Ultra is a reliable trigger). AMD vs AMD and Intel vs Intel stay in sync.

**Cause:** the game's physics uses the CPU's fast "approximate reciprocal" math instructions (`rcpps`, `rsqrtps`).
Intel and AMD CPUs return slightly different results for about half of all inputs. Each PC simulates the match
itself, so the two simulations slowly drift apart until the game notices and disconnects.

**Fix:** the mod replaces every one of those instructions with a table lookup, so one PC gets the other brand's
exact results. There are two versions. **In a match, exactly one player uses one of them:**

| You are | Opponent is | Who installs what |
|---|---|---|
| Intel | AMD | the Intel player installs `ajfpfix.asi` (Intel computes like AMD), **or** the AMD player installs `Optional for AMD users/ajfpfix_intelmath.asi` (AMD computes like Intel) |
| Intel | Intel | nobody, or both install `ajfpfix.asi` |
| AMD | AMD | nobody, or both install `Optional for AMD users/ajfpfix_intelmath.asi` |

Don't use both versions in one match: then the Intel PC computes like AMD and the AMD PC like Intel, which is the
same mismatch as no mod at all. A mod on only one side of a same-brand match causes a desync too.

The tables were read off real CPUs (AMD Ryzen AI MAX+ 395 / Zen 5, Intel i7-9700K) and the lookup was checked
against each CPU on all 4 billion inputs. Each version does nothing on the CPU brand it copies: `ajfpfix.asi` only
acts on Intel CPUs, `ajfpfix_intelmath.asi` only on non-Intel CPUs.

## Install

1. You need an ASI loader in `HeroGame/Binaries/Win64/`. AJBot's game files include one, and so does the
   stage-select mod. Otherwise use Ultimate ASI Loader, saved there as `dsound.dll`.
   - On Linux/Steam Deck, also set the game's Steam launch options to
     `WINEDLLOVERRIDES="dsound=n,b" %command%`.
2. Copy **one** `.asi` file into
   `<Steam>/steamapps/common/MY HERO ACADEMIA All's Justice/HeroGame/Binaries/Win64/`:
   - Intel players: `ajfpfix.asi` (this folder).
   - AMD players, only when your Intel opponent doesn't have `ajfpfix.asi`: `Optional for AMD users/ajfpfix_intelmath.asi`.
3. Start the game. A log appears next to `AJGAME.exe` (`ajfpfix.log` or `ajfpfix_intelmath.log`). It should say
   `patched 464 rcp/rsqrt sites to AMD tables` (or `to Intel tables`).

To remove the mod, delete the `.asi` file.

If a game update changes the code, the mod checks the bytes first. It then patches nothing and writes
`NOT applied: ... (different game build?)` to the log, so it can't break a newer version. It needs rebuilding
(see below) for the new version.

## Tested

| Match | Result |
|---|---|
| Intel i7-9700K, no mod, vs AMD Ryzen AI MAX+ 395 (Zen 5) | desync about 6 s after Nana's Plus Ultra, every time |
| Same PCs, Intel with `ajfpfix` | 3 of 3 matches to victory, every frame identical, Plus Ultras and PU combos included |
| AMD vs AMD, mod forced on one side | full match, identical (the mod's math matches AMD exactly) |
| AMD with `ajfpfix_intelmath.asi` vs Intel i7-9700K, no mod | 2 of 2 matches to victory, every frame identical, two Nana Plus Ultras included |

Not tested yet:
- **Older AMD CPUs (Zen 1–4)** and **other Intel generations:** the tables come from one CPU of each brand. If another
  generation rounds differently, matches against it could still desync. Reports welcome.
- **Intel CPUs with AVX-512** (some 11th gen and server chips) run different game code paths.

## Build from source

`source/build.sh /path/to/AJGAME.exe` disassembles the exe, finds every rcp/rsqrt instruction, and writes stub code
for each one (`gen/gen.py`), once with `gen/tables/amd` and once with `gen/tables/intel`. It then builds both ASIs. It needs binutils, python3 (plus numpy for `gen/compact.py` only), and Rust with the
`x86_64-pc-windows-gnu` target.

How the files fit together:

| File | What it does |
|---|---|
| `gen/dump.c` | Dumps a CPU's tables |
| `gen/compact.py` | Shrinks the dump into the two small table files (`gen/tables/<brand>/`) |
| `gen/verify.c` | Checks the emulation against the CPU on every input (run it in that CPU's tables folder) |
| `gen/asmtest.c` | Checks the assembly against the C version |
| `gen/filter_sites.py` | Skips "instructions" that are really data, or the Steam stub section |
| `src/lib.rs` | The ASI itself: maps the stubs after the exe and writes a `jmp` at each site (feature `intel_math` = the AMD-side build) |
