"""Draw our own widget inside the game through its UI system (UMG).

The combo trainer's key prompt, drawn by the game's own UI system (UMG):
the title screen's brushed gold menu bar (T_ui_title_menubg) under the game's
operation-instructions widget, which turns markup like {Melee,} into that
action's key icon for the current device. Built from engine widgets through
uecall (ajhook 'U' -> UObject::ProcessEvent); see Prompt for the tree.

    python server/uewidget.py "{Guard,}+{Melee,}  Guard Break"   # show it
    python server/uewidget.py --hide
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root + script folders on sys.path)
import re
import struct

import nextmatch as nm
import uecall as U
from ueprobe import probe

BAR = "/Game/GUI/Outgame/Title/Texture/T_ui_title_menubg.T_ui_title_menubg"
HOST = "/Script/LevelSequence.LevelSequenceBurnIn"
# Reflected offsets (UE4SS ForEachProperty on this build).
USERWIDGET_TREE = 0x278
TREE_ROOT = 0x30
VISIBLE, HIDDEN, HIT_TEST_INVISIBLE = 0, 2, 3
FILL, CENTER = 0, 2

q = lambda *v: struct.pack(f"<{len(v)}Q", *v)
d = lambda *v: struct.pack(f"<{len(v)}d", *v)
f32 = lambda *v: struct.pack(f"<{len(v)}f", *v)


def ret_obj(out, off):
    return struct.unpack_from("<Q", out, off)[0]


def lib(fn):
    """(CDO, function path) of a static library function '/Script/M.Class:Fn'."""
    return U.cdo(fn.split(":")[0]), fn


def load(path):
    """The asset at `path`, loading it if this level has not (KismetSystemLibrary
    MakeSoftObjectPath -> Conv_SoftObjPathToSoftObjRef -> LoadAsset_Blocking;
    UE4SS's LoadAsset does nothing here)."""
    r = probe('local o = StaticFindObject("%s")\n'
              'if o and o:IsValid() then out(string.format("0x%%x", o:GetAddress())) else out("none") end'
              % path, timeout=30).strip()
    if r.startswith("0x"):
        return int(r, 16)
    k = "/Script/Engine.KismetSystemLibrary"
    o = U.cdo(k)
    sp = U.call(o, f"{k}:MakeSoftObjectPath", gstring(path))[0x10:0x30]
    ref = U.call(o, f"{k}:Conv_SoftObjPathToSoftObjRef", sp)[0x20:0x48]
    a = ret_obj(U.call(o, f"{k}:LoadAsset_Blocking", ref), 0x28)
    if not a:
        raise LookupError(f"could not load {path}")
    return a


def gstring(s):
    """An FString's bytes pointing at a copy of `s` in game memory (leaked;
    prompts are short and few)."""
    w = (s + "\0").encode("utf-16-le")
    p = nm.alloc(len(w))
    nm.wr(p, w)
    n = len(s) + 1
    return q(p) + struct.pack("<ii", n, n)


def text(s):
    """FText bytes for `s` (KismetTextLibrary.Conv_StringToText)."""
    o, fn = lib("/Script/Engine.KismetTextLibrary:Conv_StringToText")
    return U.call(o, fn, gstring(s))[0x10:0x28]


OPERATION = ("/Game/GUI/Common/OperationInstructions/WBP_OperationInstructions."
             "WBP_OperationInstructions_C")
BOTTOM = 3
STYLE = "header"         # white, outlined row of DT_RichTextStyleCommandList
SCALE = 1.5
OP_PENDING_TEXT = 0x610  # WidgetOperationInstructions private pending text (see set_text)


def styled(markup, style=STYLE):
    """Wrap the plain-text runs of `markup` (everything outside {Action,}
    icons and runs already tagged <row>...</>) in <style>...</>; untagged
    text uses the table's black Default row."""
    out = []
    for part in re.split(r"(\{[A-Za-z0-9_]+,\}|<[A-Za-z0-9_]+>.*?</>)", markup):
        if part.startswith(("{", "<")) or not part.strip():
            out.append(part)
        else:
            out.append(f"<{style}>{part}</>")
    return "".join(out)


class Prompt:
    """The trainer's key prompt: the game's own operation-instructions widget
    (what the battle action log and command list draw with) on the title menu
    bar, bottom centre.

    show() takes the game's markup: {Melee,} {Jump,} {Dash,} {Guard,}
    {Unique1,} {Unique2,} {MoveUp,} {Sidekick1,} {Sidekick2,} {RStickPush,}
    become that action's icon for the current device and bindings, e.g.
    "{Guard,}+{Melee,}  Guard Break".

        UserWidget (HOST, full viewport: its viewport slot keeps the default)
          Overlay                  root, fills the screen
            Overlay                bottom centre, padded up
              Image                the bar
              OperationInstructions
    """

    def __init__(self):
        self.w = self.pc = self.op = None
        self.shown = False

    def alive(self):
        """Still on screen: same world (a level load drops our widget with
        the old PlayerController) and the object still exists."""
        if not self.w:
            return False
        if U.find_first("PlayerController") != self.pc:
            return False
        r = probe('local hit = "no"\n'
                  'for _, w in ipairs(FindAllOf("%s") or {}) do\n'
                  '  if w:GetAddress() == %d then hit = "yes" end end\n'
                  'out(hit)' % (HOST.rsplit(".", 1)[1], self.w), timeout=30).strip()
        return r == "yes"

    def set_text(self, markup):
        # RefreshText lays out a private pending FText (+0x610, which it moves
        # to the current text at +0x600); DefaultText is only read when the
        # widget is constructed. The FText from text() holds a reference
        # nothing else owns, so its bytes are handed over as they are (FText
        # is 0x10 bytes in this build). Never copy an FText the game owns this
        # way: that crashed the game.
        nm.wr(self.op + OP_PENDING_TEXT, text(styled(markup))[:0x10])
        U.call(self.op, "/Script/UIModule.WidgetOperationInstructions:RefreshText")

    def build(self):
        pc = U.find_first("PlayerController")
        if not pc:
            raise RuntimeError("no PlayerController")
        o, fn = lib("/Script/UMG.WidgetBlueprintLibrary:Create")
        create = lambda cls: ret_obj(U.call(o, fn, q(pc, cls, pc)), 0x18)
        w = create(U.find(HOST))
        op = create(load(OPERATION))
        if not (w and op):
            raise RuntimeError("CreateWidget failed")
        tree = struct.unpack("<Q", nm.rd(w + USERWIDGET_TREE, 8))[0]
        so, sfn = lib("/Script/Engine.GameplayStatics:SpawnObject")
        new = lambda cls: ret_obj(U.call(so, sfn, q(U.find("/Script/UMG." + cls), tree)), 0x10)
        root, box, img = new("Overlay"), new("Overlay"), new("Image")
        if not (tree and root and box and img):
            raise RuntimeError("widget construction failed")
        nm.wr(tree + TREE_ROOT, q(root))

        def add(parent, child, h, v):
            slot = ret_obj(U.call(parent, "/Script/UMG.Overlay:AddChildToOverlay", q(child)), 8)
            U.call(slot, "/Script/UMG.OverlaySlot:SetHorizontalAlignment", bytes([h]))
            U.call(slot, "/Script/UMG.OverlaySlot:SetVerticalAlignment", bytes([v]))
            return slot

        # No SetAnchors/Alignment/DesiredSizeInViewport: the default viewport
        # slot fills the screen; pinning it to a point collapsed it to the
        # top-left corner in battle.
        bs = add(root, box, CENTER, BOTTOM)
        U.call(bs, "/Script/UMG.OverlaySlot:SetPadding", f32(0, 0, 0, 230))
        add(box, img, FILL, FILL)
        add(box, op, CENTER, CENTER)
        U.call(img, "/Script/UMG.Image:SetBrushFromTexture", q(load(BAR)) + b"\1")
        U.call(op, "/Script/UMG.Widget:SetRenderScale", d(SCALE, SCALE))
        U.call(w, "/Script/UMG.Widget:SetVisibility", bytes([HIT_TEST_INVISIBLE]))
        U.call(w, "/Script/UMG.UserWidget:AddToViewport", struct.pack("<i", 50))
        self.w, self.pc, self.op = w, pc, op
        self.shown = True

    def show(self, markup, check=True):
        """Show `markup`. check=False skips the liveness check (UE4SS probes,
        which only answer while frames run and take ~0.1 s each): for prompts
        that must go up within a frame or two, after ensure()."""
        if not self.w or (check and not self.alive()):
            self.build()
        self.set_text(markup)
        if not self.shown:
            U.call(self.w, "/Script/UMG.Widget:SetVisibility", bytes([HIT_TEST_INVISIBLE]))
            self.shown = True

    def ensure(self):
        """Rebuild if a level load dropped the widget (frames must be running)."""
        if not self.alive():
            self.build()

    def hide(self, check=True):
        if self.w and self.shown and (not check or self.alive()):
            U.call(self.w, "/Script/UMG.Widget:SetVisibility", bytes([HIDDEN]))
        self.shown = False


if __name__ == "__main__":
    if sys.argv[1:] == ["--hide"]:
        w = U.find_first(HOST.rsplit(".", 1)[1])
        if w:
            U.call(w, "/Script/UMG.Widget:RemoveFromParent")
    else:
        p = Prompt()
        p.show(" ".join(sys.argv[1:]) or "{Melee,}  Melee")
        print(f"widget {p.w:#x}")
