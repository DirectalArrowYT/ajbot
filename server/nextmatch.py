"""Start the next AJ match directly: write both teams into UBattleSettingWork,
then travel to a stage through ajhook's native OpenLevel ('L').

    python3 server/nextmatch.py                      # random teams, random stage
    python3 server/nextmatch.py --p1 24,13,4 --p2 1,2,3 --stage St401

Needs the offline game with ajhook + AJProbe, in a battle, on its result
screen, or at the main menu (ajhook runs native calls from its frame pump).
Both players are set up as humans; endless.py --boot starts from the menu.
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root + script folders on sys.path)
import argparse, json, os, random, struct, subprocess, sys
from pathlib import Path
import socket


def _recv(s):
    b = s.recv(65536)
    if not b:
        # The offline launcher's bridge accepts before the game's hook
        # listens, then closes: without this the read loop spins forever.
        raise ConnectionError("ajhook closed the connection")
    return b


# Game copy N of the offline launcher (AJ_INSTANCE=N) listens on 47380 + N.
PORT = int(os.environ.get("AJHOOK_PORT") or 47380 + int(os.environ.get("AJ_INSTANCE") or 0))


def call(msg, port=PORT, timeout=10.0):
    """One ajhook TCP request ("<len>\\n" + body framing both ways)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    body = msg.encode()
    s.sendall(f"{len(body)}\n".encode() + body)
    try:
        buf = b""
        while b"\n" not in buf:
            buf += _recv(s)
        n, _, rest = buf.partition(b"\n")
        while len(rest) < int(n):
            rest += _recv(s)
    finally:
        s.close()
    return rest[:int(n)].decode("utf-8", "replace")

HERE = Path(__file__).resolve().parent.parent   # the repo root
_CF = next(p for p in (HERE / "assets/aj_costumes.json", HERE / "costumes.json") if p.exists())
COSTUMES = {int(k): v for k, v in json.loads(_CF.read_text()).items()}
# DT_VsCharSelect in grid order: (ECharacterId, name, category). Story entries
# need story progress (AwakeAFO is bLock) and DLC needs ownership, so random
# picks only use "Normal" unless --all is given.
MENU = [
    (203, "DekuFinal", "Normal"), (250, "DekuHalfNaked", "Story"), (2, "Bakugo", "Normal"),
    (4, "Todoroki", "Normal"), (3, "Uraraka", "Normal"), (5, "Iida", "Normal"),
    (6, "Asui", "Normal"), (8, "Kirishima", "Normal"), (28, "Mineta", "Normal"),
    (10, "Yaoyorozu", "Normal"), (11, "Tokoyami", "Normal"), (7, "Kaminari", "Normal"),
    (57, "DLC2", "Normal"), (9, "Jiro", "Normal"), (27, "Ashido", "Normal"),
    (208, "Shoji", "Normal"), (205, "Ojiro", "Normal"), (204, "Sero", "Normal"),
    (207, "Hagakure", "Normal"), (206, "Sato", "Normal"), (209, "Koda", "Normal"),
    (221, "Monoma", "Normal"), (46, "Kendo", "Normal"), (47, "Tetsutetsu", "Normal"),
    (56, "DLC1", "Normal"), (24, "Mirio", "Normal"), (26, "Amajiki", "Normal"),
    (25, "Nejire", "Normal"), (45, "Hatsume", "Normal"), (22, "Yoarashi", "Normal"),
    (29, "Camie", "Normal"), (30, "Shishikura", "Normal"), (214, "ArmorAllMight", "Normal"),
    (13, "Eraserhead", "Normal"), (58, "DLC3", "Normal"), (59, "DLC4", "Normal"),
    (23, "Endeavor", "Normal"), (43, "Hawks", "Normal"), (211, "Jeanist", "Normal"),
    (212, "Edgeshot", "Normal"), (213, "Mirko", "Normal"), (14, "GranTorino", "Normal"),
    (31, "Nighteye", "Normal"), (33, "FatGum", "Normal"), (32, "GangOrca", "Normal"),
    (215, "ShigarakiFinal", "Normal"), (16, "AllForOne", "Normal"), (216, "AwakeAFO", "Story"),
    (249, "AllForOneShigaraki", "Story"), (217, "DabiFinal", "Normal"), (18, "Toga", "Normal"),
    (37, "Twice", "Normal"), (38, "Compress", "Normal"), (60, "DLC5", "Normal"),
    (48, "Nomu", "Normal"), (61, "HighendNomu1", "Normal"), (34, "Chisaki", "Normal"),
    (36, "Rappa", "Normal"), (20, "Muscular", "Normal"), (19, "Stain", "Normal"),
    (222, "Nagan", "Normal"), (44, "Gentle", "Normal"), (1, "Deku", "Normal"),
    (21, "DekuShoot", "Normal"), (41, "DekuEri", "Normal"), (12, "AllMight", "Normal"),
    (39, "ShigarakiTrench", "Normal"), (17, "Dabi", "Normal"), (224, "Stripe", "DLC"),
    (229, "Shimura", "DLC"), (225, "DekuOverlay", "DLC"),
]
NAME = {i: n for i, n, _ in MENU}


def costume(cid):
    """Default costume string. Characters without DT_ItemCostume rows use the
    exe's generic "co_pl%s_%03d" form (the menu writes co_pl58_001 for DLC3)."""
    return COSTUMES[cid][0] if cid in COSTUMES else f"co_pl{cid:02d}_001"


# Free Battle stages (DT_StageSelect bUseFree) -> EStageId. The id goes into
# the setup too: the game reads the stage's camera/area data by id, not by
# the loaded map, so a stale id leaves the camera parked at the origin. The
# id -> map table is the exe's static TMap at 0x14a4796b0 ("/Game/Stage/St%s/St%s").
STAGE_IDS = {"St809": 21, "St401": 4, "St702": 6, "St701": 5, "St704": 8,
             "St703_d": 7, "St706": 10, "St801": 13, "St707": 11}
STAGES = list(STAGE_IDS)
ENTRY, BLOCK = 0x590, 0x1A0


def rd(a, n):
    r = call(f"D{a:#x} {n}")
    if r.startswith("!"):
        raise RuntimeError(f"read {a:#x}: {r}")
    return bytes.fromhex(r)


def wr(a, b):
    r = call(f"M{a:#x} {b.hex()}")
    if not r.startswith("wrote"):
        raise RuntimeError(f"write {a:#x}: {r}")


def addrs():
    lua = ('out(string.format("0x%x 0x%x", FindFirstOf("BattleSettingWork"):GetAddress(), '
           'FindFirstOf("PlayerController"):GetAddress()))')
    r = subprocess.run([sys.executable, str(HERE / "server" / "ueprobe.py"), lua], capture_output=True,
                       text=True, timeout=30)
    out = r.stdout.split()
    if len(out) < 2:
        why = (r.stderr.strip().splitlines() or [""])[-1]
        raise RuntimeError(f"ueprobe gave no addresses ({why or 'game not in a battle/result?'})")
    return int(out[0], 16), int(out[1], 16)


def set_char(blk, cid, costume):
    arr, n, _ = struct.unpack_from("<Qii", rd(blk + 8, 16))
    if n < 1:
        # Setup filled straight from the main menu has empty equipment
        # arrays; the game then loads the default costume.
        wr(blk, bytes([cid]))
        return
    s, _, smax = struct.unpack("<Qii", rd(arr, 16))
    if len(costume) + 1 > smax:
        raise RuntimeError(f"costume {costume} does not fit ({smax})")
    wr(s, (costume + "\0").encode("utf-16-le"))
    wr(arr + 8, struct.pack("<i", len(costume) + 1))
    wr(blk, bytes([cid]))


TIMER = 300  # round timer the menu writes for a free battle


def prep_setup(work, players, stage):
    """Fields the menu fills in that a menu-free start has to set itself.
    Entry tail +0x4e0: byte 0 = player number, bytes 2-3 = 00 02 for a human
    (a CPU entry reads 01 04 there). Both stay human: the two ML bots each
    drive a pad. Then the round timer (+0x34; 99 at a fresh main menu) and the
    setup status (+0x190; 0 at the main menu, 2 for a set-up match), and the
    EStageId byte at +0x3d."""
    for p in range(2):
        wr(players + p * ENTRY + 0x4E0, bytes([p + 1, 1, 0, 2]))
    wr(work + 0x34, struct.pack("<i", TIMER))
    wr(work + 0x190, bytes([2]))
    wr(work + 0x3D, bytes([STAGE_IDS[stage]]))


FREE_MODE_LUA = ('local o = FindFirstOf("VsCharSelectControl") '
                 'if o and o:IsValid() then o.VsCharMode = 1 out(tostring(o.VsCharMode)) '
                 'else out("none") end')


def set_free_mode():
    """EVsCharMode FreeBattle (1) on the char-select control. A load from the
    main menu rebuilds that control with mode 0, and the pause menu then
    opens as Training. Call once the battle is up."""
    r = subprocess.run([sys.executable, str(HERE / "server" / "ueprobe.py"), FREE_MODE_LUA],
                       capture_output=True, text=True, timeout=30).stdout.strip()
    return r == "1"


def set_speed(n):
    """Run the game n times faster than real time, same logic per frame.
    The engine runs a fixed timestep (GameEngine.bUseFixedFrameRate, 60):
    every frame advances the world 1/FixedFrameRate s, then the engine
    sleeps to keep real time. FixedFrameRate = 60n makes it sleep n times
    less; WorldSettings.DemoPlayTimeDilation = n stretches each frame back
    to 1/60 s of game time. The game's own slow-motion writes
    WorldSettings.TimeDilation, which UE multiplies with this one, so it
    keeps working. If the machine cannot make 60n frames a second the game
    simply runs as fast as it can (each frame is still 1/60 s).
    WorldSettings belongs to the level: call again after every travel.
    Returns True once set."""
    lua = (f'local w = FindFirstOf("WorldSettings") '
           f'if not w or not w:IsValid() then out("none") return end '
           f'w.DemoPlayTimeDilation = {float(n)} '
           f'FindFirstOf("GameEngine").FixedFrameRate = {60.0 * n} '
           f'out(tostring(w.DemoPlayTimeDilation))')
    try:
        r = subprocess.run([sys.executable, str(HERE / "server" / "ueprobe.py"), lua],
                           capture_output=True, text=True, timeout=30).stdout.strip()
        return abs(float(r) - n) < 1e-3
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return False  # game busy loading, or gone


HUB = "/Game/Stage/st_MS01/st_MS01"  # main-menu hub the title's Game Start opens


def travel(path, pc=None):
    """Native FCommonStatics::OpenLevel through ajhook. Returns its reply."""
    if pc is None:
        pc = addrs()[1]
    r = call(f"L{pc:#x} {path}")
    if not r.startswith("open"):
        raise RuntimeError(f"OpenLevel {path}: {r}")
    return r


def pool_from(path):
    """Character names with mined combo routes (ajbot.routemine's file), or
    None when the file is missing or has no pickable character."""
    try:
        names = set((json.loads(Path(path).read_text(encoding="utf-8")).get("chars") or {}))
    except (OSError, ValueError):
        return None
    return names or None


def start(p1=None, p2=None, stage=None, all_=False, log=print, mirror=False, names=None):
    """Write both teams and travel to the stage. p1/p2: lists of ids or None
    for random menu picks; mirror=True gives player 2 player 1's team; names
    limits random picks to those characters (when 3+ of them are pickable).
    Returns (teams, stage). Raises RuntimeError."""
    pool = [i for i, _, cat in MENU if all_ or cat == "Normal"]
    if names:
        only = [i for i, n, cat in MENU if n in names and (all_ or cat == "Normal")]
        if len(only) >= 3:
            pool = only
    teams = [list(t) if t else random.sample(pool, 3) for t in (p1, p2)]
    if mirror:
        teams[1] = list(teams[0])
    stage = stage or random.choice(STAGES)
    if stage not in STAGE_IDS:
        raise RuntimeError(f"unknown stage {stage} (known: {', '.join(STAGES)})")
    work, pc = addrs()
    players, num, _ = struct.unpack_from("<Qii", rd(work + 0x50, 16))
    if num != 2:
        raise RuntimeError(f"expected 2 players in the setup, found {num}")
    for p, team in enumerate(teams):
        for i, cid in enumerate(team):
            set_char(players + p * ENTRY + i * BLOCK, cid, costume(cid))
    prep_setup(work, players, stage)
    for p, team in enumerate(teams):
        log(f"p{p + 1}: " + ", ".join(f"{NAME.get(c, '?')}({c}) {costume(c)}" for c in team))
    log(f"stage: {stage}")
    log(travel(f"/Game/Stage/{stage.split('_')[0]}/{stage}", pc))
    return teams, stage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--p1"); ap.add_argument("--p2"); ap.add_argument("--stage")
    ap.add_argument("--all", action="store_true", help="random picks include Story/DLC menu entries")
    a = ap.parse_args()
    ids = lambda t: [int(x) for x in t.split(",")] if t else None
    try:
        start(ids(a.p1), ids(a.p2), a.stage, a.all)
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
