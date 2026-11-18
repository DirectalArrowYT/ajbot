# HostRoomLagFix: less lag when the room host only watches

In a room where the host spectates and two other players fight, All's Justice lags for everyone: the fighters,
the host and any other spectators all see constant hitches. When the host fights, there's no problem.

**Cause:** the game runs in lockstep, so a frame can't run until both fighters' inputs for it have arrived. Both
fighters are connected only to the host, so every input goes fighter A → host → fighter B: two network trips plus a
host frame, often through Steam's relay servers. That's often longer than the game's small input delay, so the
match keeps pausing to wait. Because everyone runs the same frames, the whole room pauses with it.

**Fix:** `ajdirect.asi` sends each fighter's inputs straight to the other fighter as well, over Steam networking.
The other fighter's game takes whichever copy comes first. The host's copy still arrives as usual, so a lost direct
packet costs nothing.

- **Only the two fighters need it.** The host and spectators don't. On the host it does nothing.
- **The two fighters must be Steam friends** (that's how it finds the other player). No ports or settings needed.
- **It can't change what happens in the match.** The mod only uses the other player once 240 frames of their direct
  inputs exactly match the copy the host delivered. After that it checks every directly sent frame against the
  host's copy when that arrives, and logs any mismatch.
- When the host fights (normal 1v1), it has no effect.

## Install

1. You need an ASI loader in `HeroGame/Binaries/Win64/`, named **`dsound.dll`**. AJBot's game files include one,
   and so does the stage-select mod. Otherwise use Ultimate ASI Loader, saved there as `dsound.dll`.
   - **Only Ultimate ASI Loader (ThirteenAG) is supported.** Other loaders, such as "Universal ASI Loader",
     are not: replace them with Ultimate ASI Loader saved as `dsound.dll`.
   - The name matters: the game always loads `dsound.dll`, but on many Windows PCs it never loads `dinput8.dll`.
     If your loader is `dinput8.dll` (older AJBot game files) and no `.log` appears, rename it to `dsound.dll`.
   - On Linux/Steam Deck, also set the game's Steam launch options to
     `WINEDLLOVERRIDES="dsound=n,b" %command%`.
2. Copy `ajdirect.asi` into
   `<Steam>/steamapps/common/MY HERO ACADEMIA All's Justice/HeroGame/Binaries/Win64/`.
3. Start the game. A log appears next to `AJGAME.exe` named `ajdirect.log`. It should say `ajdirect: hooks at ...`.
   In a match with the host spectating it should then show `bonded to Steam(...)` a few seconds in, and every
   600 frames a `stats[periodic]` line:
   - `stall_ticks`: frames the game waited for input. Lower is better.
   - `direct first on X/Y frames`: how often the direct copy beat the host's.
   - `MISMATCH`: should always be 0.

To remove the mod, delete the `.asi` file. To turn it off without deleting it, create `ajdirect.ini` next to it
with the line `enabled=0`.

If a game update changes the code, the mod checks the bytes first. It then patches nothing and writes
`NOT applied: ... (different game build?)` to the log, so it can't break a newer version. It needs rebuilding for
the new version.

## Tested

On one PC, three copies of the game: the host spectating and two bot-driven fighters. Each network trip was
delayed on purpose. One full match per row, all played to the victory screen:

| Delay per trip | Mod | Fighter frames stalled | Host stall warnings | Mismatched inputs |
|---|---|---|---|---|
| 40 ms | off | 1.1% | 1377 | - |
| 40 ms | on | 0.3% (all before the fighters connected) | 92 | 0 of ~7,500 frames |
| 80 ms | off | 45.6% | 6624 | - |
| 80 ms | on | 9.7% | 882 | 0 of ~7,300 frames |

The direct copy arrived first on every frame, by 2 frames on average at 40 ms and 4 frames at 80 ms.

Not tested yet:
- **Between real PCs over Steam.** The test above sent the direct inputs over a local connection. The Steam path
  (Steam networking messages to friends playing AJ) is written but hasn't been tried in a real room.
- Rooms with more than one spectator.

## Options (`ajdirect.ini`, optional)

| Line | Default | Meaning |
|---|---|---|
| `enabled=0` | 1 | turn the mod off |
| `steam=0` | 1 | don't use Steam networking |
| `steam_peer=<SteamID64>` | none | also try this player even if they aren't a Steam friend (both sides need it) |
| `peer=<ip>:<port>` | none | LAN/testing: send over UDP to this address (opens UDP port `listen`) |
| `listen=<port>` | 7790 | UDP port for `peer=` |
| `stats_every=<frames>` | 600 | how often the stats line is logged |

## How it works / build from source

`source/src/lib.rs` hooks three functions in `AJGAME.exe`:
- The client's `ServerTransitKeyHistory` send wrapper, to copy its own inputs out.
- `ClientTransitKeyHistory_Implementation`, to see the host's copy and to feed the direct copy in the same way.
- The battle controller's `TickActor`, to send, receive and count stalls once per frame.

`source/build.sh` builds `ajdirect.asi`. It needs Rust with the `x86_64-pc-windows-gnu` target.
