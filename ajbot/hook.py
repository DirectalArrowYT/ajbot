"""Client for the ajhook named pipe - runs Lua inside the running game.

    from ajbot.hook import Hook
    with Hook() as h:
        print(h.eval("return _VERSION"))

Chunks execute on the game thread inside the hooked lua_callk/lua_pcallk.

Wire format is length-prefixed (ASCII decimal, newline, then that many bytes)
so chunk text passes through verbatim - an earlier newline-escaped format
corrupted any Lua containing its own escape sequences.
"""
import os
import socket
import sys
import threading
import time

PIPE = r"\\.\pipe\ajhook"
# Off Windows (the game under Proton) a native Python cannot open Wine's named
# pipe, so ajhook also listens on loopback TCP; AJHOOK_PORT moves both ends.
# Game copy N of the offline launcher (AJ_INSTANCE=N) is on 47380 + N.
INSTANCE = int(os.environ.get("AJ_INSTANCE") or 0)
TCP_PORT = int(os.environ.get("AJHOOK_PORT") or 47380 + INSTANCE)
DEFAULT = PIPE if sys.platform == "win32" else ("127.0.0.1", TCP_PORT)


def wine_path(path):
    """A path the DLL (a Wine process) can open. Wine maps the Linux root to
    Z:, so /home/x/runs/a.jsonl becomes Z:\\home\\x\\runs\\a.jsonl. Relative
    paths and Windows hosts are left alone."""
    p = str(path)
    if sys.platform != "win32" and p.startswith("/"):
        return "Z:" + p.replace("/", "\\")
    return p


class _Sock:
    """Just the read/write/close a pipe file object offers, over a socket."""

    def __init__(self, addr):
        self.s = socket.create_connection(addr, timeout=2.0)
        self.s.settimeout(None)
        self.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def write(self, data):
        self.s.sendall(data)

    def read(self, n):
        return self.s.recv(n)

    def close(self):
        self.s.close()


class HookError(RuntimeError):
    pass


class Hook:
    def __init__(self, path=DEFAULT, timeout=5.0):
        self.path = path
        self.timeout = timeout
        self.f = None
        self._lock = threading.RLock()     # one request/response at a time
        self.open()

    def open(self):
        deadline = time.time() + self.timeout
        last = None
        while time.time() < deadline:
            try:
                if isinstance(self.path, tuple):
                    self.f = _Sock(self.path)
                else:
                    self.f = open(self.path, "r+b", buffering=0)
                return
            except OSError as e:
                last = e
                time.sleep(0.15)
        where = "%s:%d" % self.path if isinstance(self.path, tuple) else self.path
        raise HookError(
            f"could not open {where}: {last}. Is the game running with ajhook.asi loaded?"
        )

    # ---- transport ----
    def _send(self, opcode, payload):
        with self._lock:
            return self._send_locked(opcode, payload)

    def _send_locked(self, opcode, payload):
        if self.f is None:
            self.open()
        body = opcode.encode() + payload.encode("utf-8")
        header = f"{len(body)}\n".encode()
        try:
            self.f.write(header + body)
        except OSError:
            # Windows raw pipe I/O can throw a transient EINVAL under rapid
            # back-to-back writes (seen during tight polling loops) even
            # though the pipe itself is fine - one reconnect-and-retry
            # clears it without needing to chase the exact Win32 semantics.
            self.f = None
            self.open()
            self.f.write(header + body)
        return self._recv()

    def _recv(self):
        header = b""
        while not header.endswith(b"\n"):
            b = self.f.read(1)
            if not b:
                raise HookError("pipe closed by game")
            header += b
        want = int(header.decode().strip())
        chunks, got = [], 0
        while got < want:
            part = self.f.read(want - got)
            if not part:
                raise HookError("pipe closed mid-message")
            chunks.append(part)
            got += len(part)
        out = b"".join(chunks).decode("utf-8", "replace")
        if out.startswith("!"):
            raise HookError(out[1:])
        return out

    # ---- api ----
    def eval(self, src):
        """Run a Lua chunk now and return its result as a string."""
        return self._send("E", src)

    def set_resident(self, src):
        """Install a chunk the hook re-runs on its pump cadence ('' clears)."""
        return self._send("R", src)

    def set_interval(self, every_pumps):
        """Run the resident chunk every N pumps (lower = fresher, costlier)."""
        return self._send("I", str(every_pumps))

    def samples(self):
        """Aggregated sampler output, collected DLL-side across all lua_States."""
        return self._send("S", "")

    def clear_samples(self):
        return self._send("C", "")

    def start_recording(self, path):
        """Have the DLL itself assemble and write ticks natively to `path`
        (a resident sampler must already be installed via set_resident/
        set_interval - this only controls whether pump() also writes)."""
        return self._send("W", wine_path(path) if path else "")

    def stop_recording(self):
        return self._send("X", "")

    def recording_status(self):
        """Raw "recording=<0|1> ticks=<n> path=<path>" string from the DLL."""
        return self._send("T", "")

    def xinput_slots(self):
        """XInput slots the game itself sees as connected (0 == Player 1)."""
        return {int(t) for t in self._send("G", "").split()}

    def pad_state(self, side="1P"):
        """Everything ajhook reports for one side from a single request:
        pos/row/col/page/banner/complete/random/combo (cursor and screen flags),
        rows (live Normal/Manual toggle per control-mode row), crow (cursor row
        on the control-mode screen), ctl (committed settings), age_ms. Missing
        keys are simply absent; None when nothing is pushed at all."""
        raw = self._send("K", "").strip()
        if raw == "none":
            return None
        tag = "p1" if side == "1P" else "p2"
        out = {}
        for tok in raw.split():
            k, _, v = tok.partition("=")
            if k == "age_ms":
                try:
                    out["age_ms"] = int(v)
                except ValueError:
                    pass
            elif k.startswith(tag + "_"):
                name = k[len(tag) + 1:]
                if name == "rows":
                    try:
                        out["rows"] = [int(x) for x in v.split(",")]
                    except ValueError:
                        pass
                elif name == "ctl":
                    try:
                        out["ctl"] = [tuple(int(x) for x in e.split(",")) for e in v.split(";")]
                    except ValueError:
                        pass
                else:
                    try:
                        out[name] = int(v)
                    except ValueError:
                        pass
        if "pos" in out:
            out["row"], out["col"] = divmod(out["pos"], 6)
        return out

    def cursor(self, side="1P"):
        """Character-select cursor for one player ("1P" or "2P"), read by ajhook
        straight from the widget at the address the UE4SS CursorExport mod
        pushes. Returns a dict (pos, row, col, page, banner, complete, random,
        age_ms), or None when there is no reading (not on the screen, mod not
        running, or that player's struct could not be read)."""
        raw = self._send("K", "").strip()
        if raw == "none":
            return None
        fields = {}
        for tok in raw.split():
            k, _, v = tok.partition("=")
            try:
                fields[k] = int(v)
            except ValueError:
                pass
        tag = "p1" if side == "1P" else "p2"
        if f"{tag}_pos" not in fields:
            return None
        out = {k[len(tag) + 1:]: v for k, v in fields.items() if k.startswith(tag + "_")}
        out["age_ms"] = fields.get("age_ms", 0)
        out["row"], out["col"] = divmod(out["pos"], 6)
        return out

    def control_modes(self, side="1P"):
        """Committed control-mode settings for one side's three team slots, as
        [(mode, auto_change_combo, auto_plus_ultra), ...] (mode 0=Normal,
        1=Manual), read from the widget's ComboModeSettings array. It updates
        when the control-mode screen is confirmed. None when unavailable."""
        raw = self._send("K", "").strip()
        tag = "p1" if side == "1P" else "p2"
        for tok in raw.split():
            if tok.startswith(f"{tag}_ctl="):
                try:
                    return [tuple(int(x) for x in e.split(",")) for e in tok.split("=", 1)[1].split(";")]
                except ValueError:
                    return None
        return None

    def control_rows(self, side="1P"):
        """Live Normal/Manual toggle of each control-mode row on one side, as a
        list of 0/1 (1 = toggled to Manual). Flips on every Left/Right press and
        holds the remembered value when the screen opens, so it can be read
        while the screen is open. None when the screen/objects are unavailable."""
        raw = self._send("K", "").strip()
        tag = "p1" if side == "1P" else "p2"
        for tok in raw.split():
            if tok.startswith(f"{tag}_rows="):
                try:
                    return [int(x) for x in tok.split("=", 1)[1].split(",")]
                except ValueError:
                    return None
        return None

    def has_state(self):
        """True only when battle Lua is actually running.

        Both "no lua state" (never entered a match) and "not pumping" (paused,
        or sitting in a menu like character select) mean the same thing to a
        caller, so neither raises.
        """
        try:
            self.eval("return 1")
            return True
        except HookError as e:
            msg = str(e)
            if "no lua state" in msg or "not pumping" in msg:
                return False
            raise

    def close(self):
        if self.f:
            self.f.close()
            self.f = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
