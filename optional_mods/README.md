# Optional game mods (All's Justice)

AJBot does not need any of these. Each folder is one mod. To install one, copy that folder's files into the game's
`HeroGame/Content/Paks/~mods/` folder (create `~mods` if it doesn't exist). To remove it, delete those files again.

All of them are unsigned paks, so the game needs the UniversalSigBypasser (the same one the stage mod needs).
**Offline only:** don't take modded games online. Exceptions: `IntelSyncFix` and `HostRoomLagFix` are meant for online play (see below).

| Folder | Files | Status | What it does |
|---|---|---|---|
| `StageExpand` | `zzz_StageExpand_P.*` | done | 5x4 free-battle stage select: 19 stages plus Random |
| `DeluxeEdition` | - | moved | Moved to its own private repo, `AJ-DeluxeEdition` (2026-11-19). Install and update notes are in its README. |
| `ShigarakiBS_Pl15` | - | moved | Moved to the private `AJ-DeluxeEdition` repo (2026-11-19). |
| `IntelSyncFix` | `ajfpfix.asi`, `Optional for AMD users/ajfpfix_intelmath.asi` | test | **Not a pak.** Fixes online desyncs between Intel and AMD PCs. One player per match copies one file into `HeroGame/Binaries/Win64/` (needs the ASI loader): the Intel player `ajfpfix.asi`, or the AMD player `Optional for AMD users/ajfpfix_intelmath.asi`. See `IntelSyncFix/README.md`. |
| `HostRoomLagFix` | `ajdirect.asi` | test | **Not a pak.** Less lag in rooms where the host only spectates: the two fighters send their inputs straight to each other instead of only through the host. Both fighters (Steam friends) copy it into `HeroGame/Binaries/Win64/` (needs the ASI loader). On the host it adds a fallback that helps even when the fighters don't have it (a little more input delay instead of pauses). Tested on one PC only, not yet in a real room. See `HostRoomLagFix/README.md`. |

Every mod here goes into `HeroGame/Content/Paks/~mods/` (Windows:
`<Steam>\steamapps\common\MY HERO ACADEMIA All's Justice\HeroGame\Content\Paks\~mods\`).

The `.asi` mods need the ASI loader saved as `dsound.dll` in `HeroGame/Binaries/Win64/`. If yours is
`dinput8.dll` (older AJBot game files), rename it: many Windows PCs never load `dinput8.dll`, so no `.asi` runs.
Only Ultimate ASI Loader (ThirteenAG) is supported; other loaders such as "Universal ASI Loader" are not.

### Deluxe Edition

Deluxe Edition moved to its own private repo, `AJ-DeluxeEdition`, on 2026-11-19 (full history kept).
Install notes, the `-deluxe` launch option, updates and the Steam library entry are in that repo's README.

Sources and build scripts for the mods live outside this repo (in a private build folder; notes in its `FINDINGS.md`).
