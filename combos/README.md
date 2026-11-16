# Combo library

Combos for every character, written in the bot's combo notation and sorted by how sure we are that they work. The bot and the GUI's Combos page read the same files, so a combo added here can be watched, practiced and (with `--routes`) followed by the bot.

- **Authored**: Works 100% of the time. Fact-checked by people who play the character.
- **Potential**: Works, with a catch: position (walls, midscreen), certain opponents, or a link that is not consistent. The catch is written next to it.
- **Hypothetical**: Not proven yet: the game's own CPU combo lists, routes the bot found in recordings, ideas. Test them and report back: each one moves up a tier or gets removed.

## Adding a combo

- **No git needed:** [suggest a combo](https://github.com/DirectalArrowYT/ajbot/issues/new?template=combo.yml) and fill in the form.
- **Pull request:** edit `combos/<Character>.json` (only `tier` and `notation` are required) and run `python -m ajbot.combodb check`. The pages here are rebuilt automatically after a merge.
- Tested a hypothetical one? Report it with the form (landed / attempts, where on the stage), so it can move up or come out.

Notation: `A` attack, `CA` counter attack, `GC` guard-cancel counter, `UB` unblockable, `Q1`/`Q2` quirks, `TQ1`/`TQ2` tilt quirks, `PU` Plus Ultra, `j.X` jump then X, `d.X` dash then X, `t.NAME.X` tag in NAME, `[X]` charge, `Dodge Cancel`, `Jump`, `Rising`, `EA`. Full list: [ajbot/notation.py](../ajbot/notation.py).

| Character | Authored | Potential | Hypothetical |
|---|---|---|---|
| [AllForOne](AllForOne.md) | 0 | 0 | 15 |
| [AllForOneShigaraki](AllForOneShigaraki.md) | 0 | 0 | 3 |
| [Amajiki](Amajiki.md) | 0 | 0 | 13 |
| [ArmorAllMight](ArmorAllMight.md) | 0 | 0 | 17 |
| [Ashido](Ashido.md) | 0 | 0 | 12 |
| [Asui](Asui.md) | 0 | 0 | 10 |
| [AwakeAFO](AwakeAFO.md) | 0 | 0 | 7 |
| [Bakugo](Bakugo.md) | 0 | 0 | 21 |
| [Camie](Camie.md) | 0 | 0 | 7 |
| [Chisaki](Chisaki.md) | 0 | 0 | 20 |
| [Compress](Compress.md) | 0 | 0 | 10 |
| [DLC1](DLC1.md) | 0 | 0 | 7 |
| [DLC2](DLC2.md) | 0 | 0 | 16 |
| [DLC3](DLC3.md) | 0 | 0 | 18 |
| [DLC4](DLC4.md) | 0 | 0 | 13 |
| [DLC5](DLC5.md) | 0 | 0 | 6 |
| [Dabi](Dabi.md) | 0 | 0 | 15 |
| [DabiFinal](DabiFinal.md) | 0 | 0 | 7 |
| [DekuEri](DekuEri.md) | 0 | 0 | 6 |
| [DekuFinal](DekuFinal.md) | 0 | 0 | 15 |
| [DekuHalfNaked](DekuHalfNaked.md) | 0 | 0 | 16 |
| [DekuOverlay](DekuOverlay.md) | 0 | 0 | 5 |
| [DekuShoot](DekuShoot.md) | 0 | 0 | 24 |
| [Edgeshot](Edgeshot.md) | 0 | 0 | 8 |
| [Eraserhead](Eraserhead.md) | 0 | 0 | 22 |
| [FatGum](FatGum.md) | 0 | 0 | 10 |
| [GangOrca](GangOrca.md) | 0 | 0 | 5 |
| [Gentle](Gentle.md) | 0 | 0 | 7 |
| [GranTorino](GranTorino.md) | 0 | 0 | 11 |
| [Hagakure](Hagakure.md) | 0 | 0 | 10 |
| [Hatsume](Hatsume.md) | 0 | 0 | 8 |
| [Hawks](Hawks.md) | 0 | 0 | 18 |
| [HighendNomu1](HighendNomu1.md) | 0 | 0 | 6 |
| [Iida](Iida.md) | 0 | 0 | 13 |
| [Jeanist](Jeanist.md) | 0 | 0 | 14 |
| [Jiro](Jiro.md) | 0 | 0 | 16 |
| [Kaminari](Kaminari.md) | 0 | 0 | 9 |
| [Kirishima](Kirishima.md) | 0 | 0 | 23 |
| [Koda](Koda.md) | 0 | 0 | 14 |
| [Mineta](Mineta.md) | 0 | 0 | 17 |
| [Mirio](Mirio.md) | 0 | 0 | 27 |
| [Mirko](Mirko.md) | 0 | 0 | 8 |
| [Monoma](Monoma.md) | 0 | 0 | 9 |
| [Muscular](Muscular.md) | 0 | 0 | 8 |
| [Nagan](Nagan.md) | 0 | 0 | 4 |
| [Nejire](Nejire.md) | 0 | 0 | 14 |
| [Nighteye](Nighteye.md) | 0 | 0 | 6 |
| [Nomu](Nomu.md) | 0 | 0 | 10 |
| [Ojiro](Ojiro.md) | 0 | 0 | 12 |
| [Rappa](Rappa.md) | 0 | 0 | 3 |
| [Sato](Sato.md) | 0 | 0 | 13 |
| [Sero](Sero.md) | 0 | 0 | 7 |
| [ShigarakiFinal](ShigarakiFinal.md) | 0 | 0 | 7 |
| [ShigarakiTrench](ShigarakiTrench.md) | 0 | 0 | 10 |
| [Shimura](Shimura.md) | 0 | 0 | 12 |
| [Shishikura](Shishikura.md) | 0 | 0 | 5 |
| [Shoji](Shoji.md) | 0 | 0 | 25 |
| [Stain](Stain.md) | 0 | 0 | 16 |
| [Tetsutetsu](Tetsutetsu.md) | 0 | 0 | 24 |
| [Todoroki](Todoroki.md) | 0 | 0 | 11 |
| [Toga](Toga.md) | 0 | 0 | 8 |
| [Tokoyami](Tokoyami.md) | 0 | 0 | 1 |
| [Twice](Twice.md) | 0 | 0 | 9 |
| [Uraraka](Uraraka.md) | 0 | 0 | 18 |
| [Yaoyorozu](Yaoyorozu.md) | 0 | 0 | 9 |
| [Yoarashi](Yoarashi.md) | 0 | 0 | 22 |
