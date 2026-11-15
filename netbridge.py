"""Tiny TCP <-> unix socket forwarder for offline_game.sh.

The game runs in a network namespace with only loopback. Two kinds of
traffic still have to cross into or out of it, always as loopback-to-loopback:

  hook:   the bot (host) -> ajhook's TCP port (inside)
  steam:  the game (inside) -> the Steam client's 127.0.0.1 ports (host)

Each crossing is a unix socket in a directory both sides can see.

  netbridge.py tcp2unix PORT SOCK   listen on 127.0.0.1:PORT, forward to SOCK
  netbridge.py unix2tcp SOCK PORT   listen on SOCK, forward to 127.0.0.1:PORT

Several pairs can be given in one call, e.g.
  netbridge.py unix2tcp a.sock 1 tcp2unix 2 b.sock
Plain stdlib so it runs in the Flatpak runtime too.
"""
import os
import socket
import sys
import threading


def pump(src, dst):
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass
    for s in (src, dst):
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def serve(listener, connect):
    while True:
        client, _ = listener.accept()
        try:
            upstream = connect()
        except OSError:
            client.close()
            continue
        for a, b in ((client, upstream), (upstream, client)):
            threading.Thread(target=pump, args=(a, b), daemon=True).start()


def tcp2unix(port, path):
    lst = socket.create_server(("127.0.0.1", int(port)), reuse_port=False)
    def connect():
        s = socket.socket(socket.AF_UNIX)
        s.connect(path)
        return s
    return lst, connect


def unix2tcp(path, port):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    lst = socket.socket(socket.AF_UNIX)
    lst.bind(path)
    lst.listen(16)
    return lst, lambda: socket.create_connection(("127.0.0.1", int(port)))


def main(args):
    modes = {"tcp2unix": tcp2unix, "unix2tcp": unix2tcp}
    pairs = []
    while args:
        if len(args) < 3 or args[0] not in modes:
            sys.exit(__doc__)
        pairs.append(modes[args[0]](args[1], args[2]))
        args = args[3:]
    if not pairs:
        sys.exit(__doc__)
    threads = [threading.Thread(target=serve, args=p, daemon=True) for p in pairs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main(sys.argv[1:])
