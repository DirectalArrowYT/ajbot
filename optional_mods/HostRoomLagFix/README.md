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

- **The two fighters get the most from it.** Spectators don't need it.
- **The host can install it too.** It then helps even when the fighters don't have the mod (see "Host mode" below).
- **The two fighters must be Steam friends** (that's how it finds the other player). No ports or settings needed.
- **It can't change what happens in the match.** The mod only uses the other player once 240 frames of their direct
  inputs exactly match the copy the host delivered. After that it checks every directly sent frame against the
  host's copy when that arrives, and logs any mismatch.
- When the host fights (normal 1v1), it has no effect.

## Host mode (when the fighters don't have the mod)

Installed on a host that spectates, the mod does two things each time the host forwards a fighter's inputs:

- **It sends them right away** instead of at the end of the host's frame.
- **It tells both fighters to use a little more input delay when they keep stalling in the fight.** The game has
  this setting, but a spectating host never passes it on, so the fighters stay at the minimum. The mod measures
  the fighters' stalls after loading and the intro, and adds 1 frame at a time, at most `host_max` (4) frames.
  A raised delay can't be lowered until the match ends.

The cost is a few frames more input delay on a slow connection, instead of the match pausing.

The game also raises its own value on the host, but mostly during loading and the intro, where everyone waits
anyway: in a real room it reached 9 extra frames that way, which felt like input lag. The mod doesn't pass that value
on unless `host_game=1`. When the host
fights, or the fighters have the mod, host mode changes nothing that matters: the direct copy still arrives first.

The host's log shows a `host stats[...]` line every 1200 forwards:
- `fighter_stall`: how far the fighters fell behind full speed, as the host sees it. Lower is better.
- `delay +N`: the extra input delay the mod added on top of the game's own.

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

Host mode, with the mod **only on the host** (fighters without it):

| Delay per trip | Host mode | Fighter frames stalled | Host stall warnings | Input delay the fighters ended on |
|---|---|---|---|---|
| 40 ms | off | 1.1% | 1377 | 6 frames |
| 40 ms | on | 0.4% | 112 | 7 frames |
| 80 ms | off | 45.6% | 6624 | 6 frames |
| 80 ms | on | 1.0% | 167 | 12-13 frames |
| 80 ms | on, `host_flush=0` | 1.6% | 193 | 12 frames |

Most of the gain comes from the input delay reaching the fighters; sending right away adds a little. These runs
used the first version, which also passed the game's own value on (`host_game=1` now).

In a real room (host only, Steam), that first version left almost no lag but noticeable input lag: the fighters
ended on 15 frames, 9 of them from the game's own value. The current version passes on only its own value. It
hasn't been tested yet.

Not tested yet:
- **Between real PCs over Steam.** The test above sent the direct inputs over a local connection. The Steam path
  (Steam networking messages to friends playing AJ) is written but hasn't been tried in a real room.
- Rooms with more than one spectator.
- Host mode between real PCs, and how much the extra input delay is noticed in play.

## Options (`ajdirect.ini`, optional)

| Line | Default | Meaning |
|---|---|---|
| `enabled=0` | 1 | turn the mod off |
| `steam=0` | 1 | don't use Steam networking |
| `steam_peer=<SteamID64>` | none | also try this player even if they aren't a Steam friend (both sides need it) |
| `peer=<ip>:<port>` | none | LAN/testing: send over UDP to this address (opens UDP port `listen`) |
| `listen=<port>` | 7790 | UDP port for `peer=` |
| `stats_every=<frames>` | 600 | how often the stats line is logged |
| `host=0` | 1 | turn host mode off |
| `host_max=<frames>` | 4 | most extra input delay host mode may add (the game's own limit is 18 extra) |
| `host_floor=<frames>` | 0 | extra input delay host mode always adds |
| `host_up_pct=<percent>` | 5 | add 1 frame when a fighter stalls at least this much over 2 seconds |
| `host_flush=0` | 1 | don't send forwarded inputs right away |
| `host_warmup=<frames>` | 1200 | ignore stalls in a fighter's first frames of a match (loading and intro) |
| `host_game=1` | 0 | also pass on the game's own input delay (smoother, but more input lag) |

## How it works / build from source

`source/src/lib.rs` hooks three functions in `AJGAME.exe`:
- The client's `ServerTransitKeyHistory` send wrapper, to copy its own inputs out.
- `ClientTransitKeyHistory_Implementation`, to see the host's copy and to feed the direct copy in the same way.
- The battle controller's `TickActor`, to send, receive and count stalls once per frame.
- On the host: the `ClientTransitKeyHistory` send wrapper, to pass on the input delay (byte 3 of the first entry's
  header, the game's `AdditionalHistoryCount`) and flush the connection after each forward.

`source/build.sh` builds `ajdirect.asi`. It needs Rust with the `x86_64-pc-windows-gnu` target.
