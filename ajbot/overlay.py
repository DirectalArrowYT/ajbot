"""A prompt drawn over the game window: the combo trainer's "press this".

A borderless, click-through, always-on-top window that follows the game's
client area (the game runs windowed or borderless; exclusive fullscreen
would cover it). Everything not drawn is the color key, so only the panels
show. Pure Win32 through ctypes - no tkinter (the portable Python has none).

    ov = Overlay()                     # finds AJGAME.exe's window by itself
    ov.show(chain=["A", "A", "CA", "Q1"], k=2, prompt="CA",
            buttons=["B"], stick=None, note="press it to go on")
    ov.show(chain=..., k=4, banner="LANDED", good=True)
    ov.close()

Windows only. On anything else Overlay() raises OSError.
"""
import ctypes
import math
import os
import threading
from ctypes import wintypes as W

if os.name != "nt":
    raise ImportError("ajbot.overlay is Windows only")

u32 = ctypes.WinDLL("user32", use_last_error=True)
g32 = ctypes.WinDLL("gdi32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
ENUMPROC = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", W.UINT), ("style", W.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", W.HINSTANCE), ("hIcon", W.HICON), ("hCursor", W.HANDLE),
                ("hbrBackground", W.HBRUSH), ("lpszMenuName", W.LPCWSTR),
                ("lpszClassName", W.LPCWSTR), ("hIconSm", W.HICON)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", W.HDC), ("fErase", W.BOOL), ("rcPaint", W.RECT),
                ("fRestore", W.BOOL), ("fIncUpdate", W.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


def _sig(f, res, *args):
    f.restype, f.argtypes = res, list(args)


_sig(u32.DefWindowProcW, LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
_sig(u32.RegisterClassExW, W.ATOM, ctypes.POINTER(WNDCLASSEXW))
_sig(u32.CreateWindowExW, W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int,
     ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID)
_sig(u32.SetLayeredWindowAttributes, W.BOOL, W.HWND, W.COLORREF, W.BYTE, W.DWORD)
_sig(u32.GetMessageW, W.BOOL, ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT)
_sig(u32.TranslateMessage, W.BOOL, ctypes.POINTER(W.MSG))
_sig(u32.DispatchMessageW, LRESULT, ctypes.POINTER(W.MSG))
_sig(u32.PostMessageW, W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
_sig(u32.BeginPaint, W.HDC, W.HWND, ctypes.POINTER(PAINTSTRUCT))
_sig(u32.EndPaint, W.BOOL, W.HWND, ctypes.POINTER(PAINTSTRUCT))
_sig(u32.InvalidateRect, W.BOOL, W.HWND, ctypes.c_void_p, W.BOOL)
_sig(u32.SetTimer, ctypes.c_size_t, W.HWND, ctypes.c_size_t, W.UINT, ctypes.c_void_p)
_sig(u32.SetWindowPos, W.BOOL, W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     ctypes.c_int, W.UINT)
_sig(u32.ShowWindow, W.BOOL, W.HWND, ctypes.c_int)
_sig(u32.DestroyWindow, W.BOOL, W.HWND)
_sig(u32.PostQuitMessage, None, ctypes.c_int)
_sig(u32.EnumWindows, W.BOOL, ENUMPROC, W.LPARAM)
_sig(u32.GetWindowThreadProcessId, W.DWORD, W.HWND, ctypes.POINTER(W.DWORD))
_sig(u32.IsWindowVisible, W.BOOL, W.HWND)
_sig(u32.IsWindow, W.BOOL, W.HWND)
_sig(u32.IsIconic, W.BOOL, W.HWND)
_sig(u32.GetClientRect, W.BOOL, W.HWND, ctypes.POINTER(W.RECT))
_sig(u32.ClientToScreen, W.BOOL, W.HWND, ctypes.POINTER(W.POINT))
_sig(u32.FillRect, ctypes.c_int, W.HDC, ctypes.POINTER(W.RECT), W.HBRUSH)
_sig(u32.DrawTextW, ctypes.c_int, W.HDC, W.LPCWSTR, ctypes.c_int, ctypes.POINTER(W.RECT), W.UINT)
_sig(k32.GetModuleHandleW, W.HMODULE, W.LPCWSTR)
_sig(k32.OpenProcess, W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
_sig(k32.CloseHandle, W.BOOL, W.HANDLE)
_sig(k32.QueryFullProcessImageNameW, W.BOOL, W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD))
_sig(g32.CreateSolidBrush, W.HBRUSH, W.COLORREF)
_sig(g32.CreatePen, W.HPEN, ctypes.c_int, ctypes.c_int, W.COLORREF)
_sig(g32.SelectObject, W.HGDIOBJ, W.HDC, W.HGDIOBJ)
_sig(g32.DeleteObject, W.BOOL, W.HGDIOBJ)
_sig(g32.RoundRect, W.BOOL, W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     ctypes.c_int, ctypes.c_int)
_sig(g32.Ellipse, W.BOOL, W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
_sig(g32.SetBkMode, ctypes.c_int, W.HDC, ctypes.c_int)
_sig(g32.SetTextColor, W.COLORREF, W.HDC, W.COLORREF)
_sig(g32.CreateFontW, W.HFONT, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     ctypes.c_int, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD,
     W.LPCWSTR)
_sig(g32.CreateCompatibleDC, W.HDC, W.HDC)
_sig(g32.CreateCompatibleBitmap, W.HBITMAP, W.HDC, ctypes.c_int, ctypes.c_int)
_sig(g32.BitBlt, W.BOOL, W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HDC,
     ctypes.c_int, ctypes.c_int, W.DWORD)
_sig(g32.DeleteDC, W.BOOL, W.HDC)

try:   # physical pixels, so the overlay lines up with the game on a scaled desktop
    u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except (AttributeError, OSError):
    pass

WM_DESTROY, WM_PAINT, WM_TIMER, WM_CLOSE, WM_ERASEBKGND = 0x2, 0xF, 0x113, 0x10, 0x14
WM_APP_REDRAW = 0x8001
WS_POPUP = 0x80000000
WS_EX = 0x80000 | 0x20 | 0x8 | 0x80 | 0x8000000   # layered, transparent, topmost, toolwindow, noactivate
HWND_TOPMOST = W.HWND(-1)
SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x10, 0x40
DT_CENTER, DT_VCENTER, DT_SINGLELINE, DT_CALCRECT = 0x1, 0x4, 0x20, 0x400


def rgb(r, g, b):
    return r | g << 8 | b << 16


KEY = rgb(255, 0, 255)            # never drawn: see-through
PANEL = rgb(18, 20, 28)
EDGE = rgb(70, 76, 96)
TEXT = rgb(235, 238, 245)
DIM = rgb(130, 136, 150)
GOLD = rgb(255, 196, 40)
GOOD = rgb(70, 200, 110)
BAD = rgb(235, 70, 70)
# Xbox face-button colors; the rest are grey
BUTTON_COLORS = {"A": rgb(60, 180, 75), "B": rgb(220, 50, 50), "X": rgb(40, 110, 230),
                 "Y": rgb(240, 190, 20)}
ARROWS = ["→", "↗", "↑", "↖", "←", "↙", "↓", "↘"]


def arrow(stick):
    """The 8-way arrow for a stick (x, y), or '' for neutral."""
    if not stick or math.hypot(*stick) < 0.4:
        return ""
    a = math.degrees(math.atan2(stick[1], stick[0])) % 360
    return ARROWS[int((a + 22.5) // 45) % 8]


def game_window(exe="ajgame.exe"):
    """The biggest visible top-level window of the game process, or None."""
    found = []

    def cb(hwnd, _):
        if not u32.IsWindowVisible(hwnd):
            return True
        pid = W.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = k32.OpenProcess(0x1000, False, pid.value)
        if h:
            buf = ctypes.create_unicode_buffer(520)
            n = W.DWORD(520)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) \
                    and buf.value.lower().endswith(exe):
                r = W.RECT()
                u32.GetClientRect(hwnd, ctypes.byref(r))
                found.append((r.right * r.bottom, hwnd))
            k32.CloseHandle(h)
        return True
    u32.EnumWindows(ENUMPROC(cb), 0)
    return max(found)[1] if found else None


class Overlay:
    def __init__(self, exe="ajgame.exe"):
        self.exe = exe
        self.state = {}
        self.lock = threading.Lock()
        self.hwnd = None
        self.game = None
        self.size = (0, 0)
        self._ready = threading.Event()
        self._fonts = {}
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        if not self._ready.wait(5) or not self.hwnd:
            raise OSError("could not create the overlay window")

    # ------------------------------------------------------------ public
    def show(self, **state):
        """Replace what is drawn. Keys: chain (list of step texts), k (current
        step), prompt (big text), buttons (list of names), stick ((x, y) or
        None), note (small line), banner (big status), good (banner color)."""
        with self.lock:
            self.state = dict(state)
        if self.hwnd:
            u32.PostMessageW(self.hwnd, WM_APP_REDRAW, 0, 0)

    def clear(self):
        self.show()

    def close(self):
        if self.hwnd:
            u32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        self._t.join(2)

    # ------------------------------------------------------------ window
    def _run(self):
        hinst = k32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)        # kept alive with self
        wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=self._proc,
                         hInstance=hinst, lpszClassName="AJBotOverlay")
        u32.RegisterClassExW(ctypes.byref(wc))     # fails harmlessly if registered already
        self.hwnd = u32.CreateWindowExW(WS_EX, "AJBotOverlay", "AJBot trainer", WS_POPUP,
                                        0, 0, 10, 10, None, None, hinst, None)
        if not self.hwnd:
            self._ready.set()
            return
        u32.SetLayeredWindowAttributes(self.hwnd, KEY, 0, 1)   # LWA_COLORKEY
        u32.SetTimer(self.hwnd, 1, 100, None)
        self._follow()
        self._ready.set()
        msg = W.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
        for f in self._fonts.values():
            g32.DeleteObject(f)

    def _follow(self):
        """Sit exactly on the game's client area; hide while it is gone or minimized."""
        if not self.game or not u32.IsWindow(self.game):
            self.game = game_window(self.exe)
        if not self.game or u32.IsIconic(self.game):
            u32.ShowWindow(self.hwnd, 0)
            return
        r = W.RECT()
        u32.GetClientRect(self.game, ctypes.byref(r))
        p = W.POINT(0, 0)
        u32.ClientToScreen(self.game, ctypes.byref(p))
        size = (r.right, r.bottom)
        u32.SetWindowPos(self.hwnd, HWND_TOPMOST, p.x, p.y, size[0], size[1],
                         SWP_NOACTIVATE | SWP_SHOWWINDOW)
        if size != self.size:
            self.size = size
            u32.InvalidateRect(self.hwnd, None, False)

    def _wndproc(self, hwnd, msg, wp, lp):
        if msg == WM_TIMER:
            self._follow()
            return 0
        if msg == WM_APP_REDRAW:
            u32.InvalidateRect(hwnd, None, False)
            return 0
        if msg == WM_ERASEBKGND:
            return 1
        if msg == WM_PAINT:
            ps = PAINTSTRUCT()
            hdc = u32.BeginPaint(hwnd, ctypes.byref(ps))
            try:
                self._paint(hdc)
            finally:
                u32.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        if msg == WM_CLOSE:
            u32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            u32.PostQuitMessage(0)
            return 0
        return u32.DefWindowProcW(hwnd, msg, wp, lp)

    # ------------------------------------------------------------ drawing
    def _font(self, px, bold=True):
        key = (px, bold)
        if key not in self._fonts:
            self._fonts[key] = g32.CreateFontW(-px, 0, 0, 0, 700 if bold else 400, 0, 0, 0, 1,
                                               0, 0, 5, 0, "Segoe UI")   # CLEARTYPE_QUALITY
        return self._fonts[key]

    def _text(self, dc, s, x, y, w, h, px, color, bold=True, flags=DT_CENTER | DT_VCENTER | DT_SINGLELINE):
        g32.SelectObject(dc, self._font(px, bold))
        g32.SetTextColor(dc, color)
        r = W.RECT(int(x), int(y), int(x + w), int(y + h))
        u32.DrawTextW(dc, s, -1, ctypes.byref(r), flags)

    def _measure(self, dc, s, px, bold=True):
        g32.SelectObject(dc, self._font(px, bold))
        r = W.RECT(0, 0, 0, 0)
        u32.DrawTextW(dc, s, -1, ctypes.byref(r), DT_SINGLELINE | DT_CALCRECT)
        return r.right

    def _box(self, dc, x, y, w, h, fill, edge=EDGE, radius=16, width=2):
        br = g32.CreateSolidBrush(fill)
        pen = g32.CreatePen(0, width, edge)
        ob, op = g32.SelectObject(dc, br), g32.SelectObject(dc, pen)
        g32.RoundRect(dc, int(x), int(y), int(x + w), int(y + h), int(radius), int(radius))
        g32.SelectObject(dc, ob)
        g32.SelectObject(dc, op)
        g32.DeleteObject(br)
        g32.DeleteObject(pen)

    def _paint(self, hdc):
        w, h = self.size
        if w <= 0 or h <= 0:
            return
        dc = g32.CreateCompatibleDC(hdc)
        bmp = g32.CreateCompatibleBitmap(hdc, w, h)
        old = g32.SelectObject(dc, bmp)
        key = g32.CreateSolidBrush(KEY)
        u32.FillRect(dc, ctypes.byref(W.RECT(0, 0, w, h)), key)
        g32.DeleteObject(key)
        g32.SetBkMode(dc, 1)      # TRANSPARENT
        with self.lock:
            st = dict(self.state)
        try:
            self._draw(dc, st, w, h)
        finally:
            g32.BitBlt(hdc, 0, 0, w, h, dc, 0, 0, 0x00CC0020)   # SRCCOPY
            g32.SelectObject(dc, old)
            g32.DeleteObject(bmp)
            g32.DeleteDC(dc)

    def _draw(self, dc, st, w, h):
        s = h / 1080.0
        chain = st.get("chain") or []
        k = st.get("k", -1)
        if chain:
            # The route as chips: done green, current gold, the rest grey. A
            # long route shows a window around the current step.
            px, pad, gap = int(30 * s), int(18 * s), int(10 * s)
            lo, hi = 0, len(chain)
            widths = [self._measure(dc, t, px) + 2 * pad for t in chain]
            while sum(widths[lo:hi]) + gap * (hi - lo) > w * 0.9 and hi - lo > 3:
                if k - lo > hi - 1 - k:
                    lo += 1
                else:
                    hi -= 1
            total = sum(widths[lo:hi]) + gap * (hi - lo - 1)
            x, y, ch = (w - total) / 2, 36 * s, 56 * s
            self._box(dc, x - 14 * s, y - 12 * s, total + 28 * s, ch + 24 * s, PANEL, radius=24 * s)
            for i in range(lo, hi):
                if i < k:
                    fill, edge, col = rgb(26, 70, 40), GOOD, TEXT
                elif i == k:
                    fill, edge, col = rgb(80, 62, 10), GOLD, GOLD
                else:
                    fill, edge, col = rgb(34, 37, 48), EDGE, DIM
                self._box(dc, x, y, widths[i], ch, fill, edge, radius=14 * s, width=max(2, int(3 * s)))
                self._text(dc, chain[i], x, y, widths[i], ch, px, col)
                x += widths[i] + gap
        prompt, banner = st.get("prompt"), st.get("banner")
        if prompt:
            # Big panel: the button(s) to press, the stick direction, the move.
            bw, bh = 640 * s, 230 * s
            x, y = (w - bw) / 2, h * 0.60
            self._box(dc, x, y, bw, bh, PANEL, GOLD, radius=30 * s, width=max(2, int(4 * s)))
            icons = list(st.get("buttons") or [])
            arr = arrow(st.get("stick"))
            d = 130 * s
            n = len(icons) + (1 if arr else 0)
            ix = x + (bw - (n * d + (n - 1) * 24 * s)) / 2
            iy = y + 22 * s
            if arr:
                self._box(dc, ix, iy, d, d, rgb(40, 44, 58), DIM, radius=d)
                self._text(dc, arr, ix, iy, d, d, int(84 * s), TEXT)
                ix += d + 24 * s
            for b in icons:
                col = BUTTON_COLORS.get(b, rgb(90, 96, 112))
                if b in BUTTON_COLORS:
                    self._box(dc, ix, iy, d, d, col, TEXT, radius=d, width=max(2, int(4 * s)))
                else:
                    self._box(dc, ix, iy + d * 0.2, d, d * 0.6, col, TEXT, radius=24 * s,
                              width=max(2, int(4 * s)))
                self._text(dc, b, ix, iy, d, d, int((70 if len(b) == 1 else 44) * s), TEXT)
                ix += d + 24 * s
            self._text(dc, prompt, x, y + 158 * s, bw, 44 * s, int(36 * s), GOLD)
            if st.get("wrong"):
                self._text(dc, st["wrong"], x, y - 52 * s, bw, 40 * s, int(30 * s), BAD)
        if banner:
            col = GOOD if st.get("good") else BAD
            bw = self._measure(dc, banner, int(64 * s)) + 80 * s
            x, y = (w - bw) / 2, h * 0.42
            self._box(dc, x, y, bw, 110 * s, PANEL, col, radius=30 * s, width=max(2, int(4 * s)))
            self._text(dc, banner, x, y, bw, 110 * s, int(64 * s), col)
        if st.get("note"):
            nw = self._measure(dc, st["note"], int(24 * s), False) + 40 * s
            x, y = (w - nw) / 2, h - 70 * s
            self._box(dc, x, y, nw, 46 * s, PANEL, EDGE, radius=20 * s)
            self._text(dc, st["note"], x, y, nw, 46 * s, int(24 * s), DIM, False)


if __name__ == "__main__":
    import time
    ov = Overlay()
    chain = ["Jump", "A", "A", "CA", "Q2 x2", "q4", "PU"]
    for k in range(len(chain)):
        ov.show(chain=chain, k=k, prompt=chain[k], buttons=["B"] if chain[k] == "CA" else ["X"],
                stick=(1, 0) if k == 3 else None, note="Back on the pad: quit")
        time.sleep(1.2)
    ov.show(chain=chain, k=len(chain), banner="LANDED", good=True)
    time.sleep(2)
    ov.close()
