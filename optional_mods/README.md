# Optional game mods (All's Justice)

AJBot does not need any of these. Each folder is one mod. To install one, copy that folder's files into the game's
`HeroGame/Content/Paks/~mods/` folder (create `~mods` if it doesn't exist). To remove it, delete those files again.

All of them are unsigned paks, so the game needs the UniversalSigBypasser (the same one the stage mod needs).
**Offline only:** don't take modded games online. Exception: `IntelSyncFix` is meant for online play (see below).

| Folder | Files | Status | What it does |
|---|---|---|---|
| `StageExpand` | `zzz_StageExpand_P.*` | done | 5x4 free-battle stage select: 19 stages plus Random |
| `BakugoIntroWin_Pl02` | `zzz_Pl02Demo_P.*` | test | Bakugo gets his OJ2 battle intro (replaces AJ's 0.9 s generic one) and his OJ2 win pose, as **win pose 01** (equip it in Customize). Bakugo-only demo SFX that AJ dropped use his closest attack sounds. No OJ2 cinematic cameras. |
| `ShigarakiBS_Pl15` | `zzz_Pl15_P.*`, `zzz_Pl15loose_P.*` | WIP | OJ2 Shigaraki (black-suit/Pl15) as a playable character. Known issues: broken hand vertices, the get-up ("rising") doesn't work, the customize menu is wrong. |
| `IntelSyncFix` | `ajfpfix.asi`, `Optional for AMD users/ajfpfix_intelmath.asi` | test | **Not a pak.** Fixes online desyncs between Intel and AMD PCs. One player per match copies one file into `HeroGame/Binaries/Win64/` (needs the ASI loader): the Intel player `ajfpfix.asi`, or the AMD player `Optional for AMD users/ajfpfix_intelmath.asi`. See `IntelSyncFix/README.md`. |

Linux (Flatpak Steam library path on this PC):

```bash
cp optional_mods/BakugoIntroWin_Pl02/* "/mnt/AllForMe/Steam/steamapps/common/MY HERO ACADEMIA All's Justice/HeroGame/Content/Paks/~mods/"
```

Windows: copy the files into `<Steam>\steamapps\common\MY HERO ACADEMIA All's Justice\HeroGame\Content\Paks\~mods\`.

Sources and build scripts for the mods live outside this repo (`/mnt/OneForAll/tmp/ajmods/`, notes in its `FINDINGS.md`).
