# IntelSyncFix (`ajfpfix.asi`): online desync fix for Intel vs AMD

In online matches between an **Intel** PC and an **AMD** PC, All's Justice can desync and kick both players, most
often after big knockback (Nana's Plus Ultra is a reliable trigger). AMD vs AMD and Intel vs Intel stay in sync.

**Cause:** the game's physics uses the CPU's fast "approximate reciprocal" math instructions (`rcpps`, `rsqrtps`).
Intel and AMD CPUs return slightly different results for about half of all inputs. Each PC simulates the match
itself, so the two simulations slowly drift apart until the game notices and disconnects.

**Fix:** on Intel CPUs this mod patches every one of those instructions in the game to use AMD's exact results
instead. They come from tables read off an AMD CPU, checked against it on all 4 billion inputs. On AMD CPUs the mod
does nothing.

**Only Intel players need it.** AMD players install nothing.

## Install (Intel players)

1. You need an ASI loader in `HeroGame/Binaries/Win64/`. AJBot's game files include one, and so does the
   stage-select mod. Otherwise use Ultimate ASI Loader, saved there as `dsound.dll`.
   - On Linux/Steam Deck, also set the game's Steam launch options to
     `WINEDLLOVERRIDES="dsound=n,b" %command%`.
2. Copy `ajfpfix.asi` into `<Steam>/steamapps/common/MY HERO ACADEMIA All's Justice/HeroGame/Binaries/Win64/`.
3. Start the game. `ajfpfix.log` appears next to `AJGAME.exe`. It should say `patched 464 rcp/rsqrt sites to AMD
   tables`.

To remove the mod, delete `ajfpfix.asi`.

If a game update changes the code, the mod checks the bytes first. It then patches nothing and writes
`NOT applied: ... (different game build?)` to the log, so it can't break a newer version. It needs rebuilding
(see below) for the new version.

## Tested

| Match | Result |
|---|---|
| Intel i7-9700K, no mod, vs AMD Ryzen AI MAX+ 395 (Zen 5) | desync about 6 s after Nana's Plus Ultra, every time |
| Same PCs, Intel with `ajfpfix` | 3 of 3 matches to victory, every frame identical, Plus Ultras and PU combos included |
| AMD vs AMD, mod forced on one side | full match, identical (the mod's math matches AMD exactly) |

Not tested yet:
- **Older AMD CPUs (Zen 1–4):** the tables come from Zen 5. If an older Ryzen rounds differently, Intel+mod vs that
  Ryzen could still desync. Reports welcome.
- **Intel CPUs with AVX-512** (some 11th gen and server chips) run different game code paths.
- **Two Intel players:** both have the mod, so both compute the AMD way and should stay in sync.

## Build from source

`source/build.sh /path/to/AJGAME.exe` disassembles the exe, finds every rcp/rsqrt instruction, and writes stub code
for each one (`gen/gen.py`). It then builds the ASI. It needs binutils, python3 (plus numpy for `gen/compact.py` only), and Rust with the
`x86_64-pc-windows-gnu` target.

How the files fit together:

| File | What it does |
|---|---|
| `gen/dump.c` | Dumps a CPU's tables |
| `gen/compact.py` | Shrinks the dump into the two small table files |
| `gen/verify.c` | Checks the emulation against the CPU on every input |
| `gen/asmtest.c` | Checks the assembly against the C version |
| `gen/filter_sites.py` | Skips "instructions" that are really data, or the Steam stub section |
| `src/lib.rs` | The ASI itself: maps the stubs after the exe and writes a `jmp` at each site |
