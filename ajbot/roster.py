"""Character-select roster in on-screen display order.

Generated from DT_VsCharSelect. The table's FILE order is the grid order -
do not sort it. Verified against the live screen.
"""

DISPLAY_ORDER = [
    "DekuFinal",            # key  0  緑谷 出久 OFA
    "DekuHalfNaked",        # key 66  緑谷 出久 ライジング
    "Bakugo",               # key  1  爆豪 勝己
    "Todoroki",             # key  3  轟 焦凍
    "Uraraka",              # key  2  麗日 お茶子
    "Iida",                 # key  4  飯田 天哉
    "Asui",                 # key  5  蛙吸 梅雨
    "Kirishima",            # key  7  切島 鋭児郎
    "Mineta",               # key 12  峰田 実
    "Yaoyorozu",            # key  9  八百万 百
    "Tokoyami",             # key 10  常闇 踏陰
    "Kaminari",             # key  6  上鳴 電気
    "DLC2",                 # key 13  青山 優雅
    "Jiro",                 # key  8  耳郎 響香
    "Ashido",               # key 11  芦戸 三奈
    "Shoji",                # key 18  障子 目蔵
    "Ojiro",                # key 15  尾白 猿夫
    "Sero",                 # key 16  瀬呂 範太
    "Hagakure",             # key 19  葉隠 透
    "Sato",                 # key 14  砂藤 力道
    "Koda",                 # key 17  口田 甲司
    "Monoma",               # key 63  DLC　物間 寧人
    "Kendo",                # key 23  拳藤 一佳
    "Tetsutetsu",           # key 24  鉄哲 徹鐵
    "DLC1",                 # key 28  心操 人使
    "Mirio",                # key 25  通形 ミリオ
    "Amajiki",              # key 26  天喰 環
    "Nejire",               # key 27  波動 ねじれ
    "Hatsume",              # key 29  発目 明
    "Yoarashi",             # key 43  夜嵐 イナサ
    "Camie",                # key 45  現見 ケミィ
    "Shishikura",           # key 44  肉倉 精児
    "ArmorAllMight",        # key 31  アーマードオールマイト
    "Eraserhead",           # key 32  相澤 消太
    "DLC3",                 # key 39  プレゼント・マイク
    "DLC4",                 # key 65  ミッドナイト
    "Endeavor",             # key 34  エンデヴァー
    "Hawks",                # key 38  ホークス
    "Jeanist",              # key 40  ベストジーニスト
    "Edgeshot",             # key 41  エッジショット
    "Mirko",                # key 42  ミルコ
    "GranTorino",           # key 33  グラントリノ
    "Nighteye",             # key 35  サー・ナイトアイ
    "FatGum",               # key 37  ファットガム
    "GangOrca",             # key 36  ギャングオルカ
    "ShigarakiFinal",       # key 47  死柄木 弔 AFO
    "AllForOne",            # key 48  オール・フォーワン
    "AwakeAFO",             # key 49  オール・フォー・ワン 巻き戻し
    "AllForOneShigaraki",   # key 67  オール・フォー・ワン 混沌
    "DabiFinal",            # key 51  荼毘 死炎
    "Toga",                 # key 52  トガヒミコ
    "Twice",                # key 53  トゥワイス
    "Compress",             # key 54  Mr.コンプレス
    "DLC5",                 # key 55  黒霧
    "Nomu",                 # key 57  脳無
    "HighendNomu1",         # key 58  ハイエンド脳無
    "Chisaki",              # key 59  治崎 廻
    "Rappa",                # key 60  乱波 肩動
    "Muscular",             # key 56  マスキュラー
    "Stain",                # key 61  ステイン
    "Nagan",                # key 64  レディ・ナガン
    "Gentle",               # key 62  ジェントル＆ラブラバ
    "Deku",                 # key 20  緑谷 出久
    "DekuShoot",            # key 21  緑谷 出久 シュートスタイル
    "DekuEri",              # key 22  緑谷 出久 フルカウル100％
    "AllMight",             # key 30  オールマイト
    "ShigarakiTrench",      # key 46  死柄木 弔
    "Dabi",                 # key 50  荼毘
    "Stripe",               # key 69  DLC　スターアンドストライプ
    "Shimura",              # key 70  DLC　志村菜奈
    "DekuOverlay",          # key 71  DLC　緑谷 出久 オーバーレイフルスタイル
]

INDEX = {cid: i for i, cid in enumerate(DISPLAY_ORDER)}

# Characters whose scripts have no Lua namespace: the sampler cannot tell
# them apart (they show up as slot handles like 'c22'), so nothing recorded
# with them on point is usable. Measured over every recording: only this one.
NO_NAMESPACE = {"DekuShoot"}

# Alternate forms: a character that transforms mid-match (Rising, a quirk
# mode) or brings a partner gets a SECOND Lua namespace with its own id. They
# are the same fighter, and counting them separately split one character's
# stats in two - and let the tag policy treat Dabi's burning form as a benched
# partner of Dabi. Form id (without the Pl<NN> prefix) -> roster entry.
# The first four were seen live on the same side as their base character;
# the rest follow from the name.
FORMS = {
    "DabiFullBodyBurn": "DabiFinal",
    "ShigarakiHandArmor": "ShigarakiFinal",
    "Chisaki2": "Chisaki",
    "FatGumSkinny": "FatGum",
    "Chisaki3": "Chisaki",
    "StripeAtmosphere": "Stripe",
    "KirishimaUnbreakable": "Kirishima",
    "TokoyamiBaldur": "Tokoyami",
    "DarkShadow": "Tokoyami",
    "MuscularMax": "Muscular",
    "DekuFinal2": "DekuFinal",
    "DekuEriCarrier": "DekuEri",
    "AllForOneYouth": "AwakeAFO",
    "AllForOneBoy": "AwakeAFO",
    "LaBrava": "Gentle",
}


def strip_dup(cid):
    """'Pl02Bakugo#1' -> 'Pl02Bakugo'. sampler.dedupe gives the second copy of a
    character in a mirror match a '#1' key so both can exist in one snapshot;
    it is the same character for every purpose that is not telling the two
    apart in that snapshot (vocabularies, stats, combo books, feature
    one-hots)."""
    return cid.split("#", 1)[0] if isinstance(cid, str) else cid


def base_name(cid):
    """Roster entry for a Lua id, following alternate forms, or None.

    'Pl02Bakugo' -> 'Bakugo'; 'Pl247DabiFullBodyBurn' -> 'DabiFinal'.
    Anything unnamed ('c220', '?') or not a playable character -> None.
    """
    if not isinstance(cid, str) or not cid.startswith("Pl"):
        return None
    name = strip_dup(cid)[2:].lstrip("0123456789")
    if name in INDEX:
        return name
    if name in FORMS:
        return FORMS[name]
    # Extra states a character runs under its own name plus a suffix -
    # Pl07KaminariState is Kaminari on point. Longest roster name that the id
    # starts with, so "Nomu1" is Nomu but "HighendNomu1" stays itself above.
    best = None
    for base in INDEX:
        if name.startswith(base) and (best is None or len(base) > len(best)):
            best = base
    return best


def move_char(rec, key=None):
    """Whose MOVES a fighter record is using right now, as a roster name.

    Normally the fighter's own (base_name of its key). Toga while transformed
    fights with the copied character's moves - sampler.dedupe() stamps that
    as `copy_of` - so move reach, frame data and combo routes come from the
    copy instead of from Toga's own tables.
    """
    rec = rec or {}
    k = key or rec.get("key") or rec.get("id")
    if rec.get("char_mode") == 1 and rec.get("copy_of"):
        b = base_name(rec["copy_of"])
        if b:
            return b
    return base_name(k) or k


def toga_mode(rec):
    """None, 'blood' (copied an opponent: only their jabs, counter and
    unblockable) or 'pu' (Plus Ultra into her partner: the full moveset)."""
    rec = rec or {}
    if rec.get("char_mode") != 1 or not rec.get("copy_of"):
        return None
    return "pu" if rec.get("copy_pu") else "blood"


def index_of(character_id):
    """Linear display index (row = i // 6, col = i % 6)."""
    try:
        return INDEX[character_id]
    except KeyError:
        raise KeyError(
            f"unknown character {character_id!r}; known: {sorted(INDEX)}"
        ) from None
