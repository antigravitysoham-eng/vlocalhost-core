"""A way for a second launch to talk to the copy already running.

Pressing the hotkey when nothing is running has to *start* the app; pressing it
again has to reach the app that is now running rather than start a second one.
Those are two different problems, and this module is the second half.

**Why not the mutex.** :mod:`singleton` deliberately is not a single-instance
lock -- it says so, and it is right: "the tray plus a CLI run" is a case people
have. A mutex can answer *is something running*, and cannot carry "so toggle
your recording", which is the thing that actually needs saying.

**Why a loopback socket.** It is the one mechanism that behaves the same on all
three platforms and needs no dependency. A Windows named pipe would be neater
on Windows and absent everywhere else; a file the app polls would add latency
to the one feature whose entire purpose is not making the user wait.

**On security.** The socket binds to 127.0.0.1 only, so nothing off the machine
can reach it. That still leaves every other process running as any user on the
same machine, so the port is paired with a random token written beside it in
the per-user data directory, and a command without the token is refused. This
is not a hardened channel and does not need to be -- the entire vocabulary is
"toggle a recording" -- but an unauthenticated local port that starts a
microphone would be a genuinely bad thing to ship.
"""

import json
import os
import secrets
import socket
import threading

#: Beside settings and notes, not in the program folder: the program folder is
#: replaced by every update and may not be writable at all.
FILE = "control.json"

#: Long enough to survive a busy machine, short enough that a stale file does
#: not make the hotkey feel broken while we wait to find out.
TIMEOUT = 1.5

#: The whole vocabulary.
TOGGLE = "toggle"
START = "start"
STOP = "stop"
PING = "ping"


def _path() -> str:
    from integrations import store

    return os.path.join(store.data_dir(), FILE)


# ---------------------------------------------------------------------------
# the running app's side
# ---------------------------------------------------------------------------
class Server:
    """Listens on loopback for commands from a second launch.

    ``handlers`` maps a command name to a callable taking no arguments. It is
    called on this module's accept thread, so a handler must hand real work to
    its own event loop rather than doing it here -- the same contract the
    hotkey has.
    """

    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.token = secrets.token_hex(16)
        self.port = 0
        self.error = ""
        self._sock = None
        self._thread = None
        self._stop = threading.Event()

    def start(self) -> bool:
        """Bind, publish the port, and listen. False if unavailable."""
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # No SO_REUSEADDR: two copies must not share a port, and a bind
            # failure here is information -- it means something already has it.
            self._sock.bind(("127.0.0.1", 0))
            self._sock.listen(4)
            self.port = self._sock.getsockname()[1]
        except OSError as e:
            self.error = f"could not open the control port: {e}"
            return False

        try:
            self._publish()
        except OSError as e:
            self.error = f"could not write {FILE}: {e}"
            self._sock.close()
            return False

        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="control")
        self._thread.start()
        return True

    def _publish(self):
        path = _path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        blob = json.dumps({"port": self.port, "token": self.token,
                           "pid": os.getpid()})
        # Written then moved, so a reader never sees half a file. Same
        # directory, so the move stays on one filesystem and is atomic.
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(blob)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass            # best effort; Windows ACLs do not work this way

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return      # closed by stop()
            with conn:
                try:
                    conn.settimeout(TIMEOUT)
                    raw = conn.recv(512).decode("utf-8", "replace").strip()
                    conn.sendall(self._handle(raw).encode("utf-8"))
                except Exception:  # noqa: BLE001 - one bad client, not a crash
                    pass

    def _handle(self, raw: str) -> str:
        try:
            msg = json.loads(raw)
        except ValueError:
            return "error: not json"
        if msg.get("token") != self.token:
            return "error: refused"
        command = msg.get("command", "")
        if command == PING:
            return "ok"
        handler = self.handlers.get(command)
        if handler is None:
            return f"error: unknown command {command!r}"
        handler()
        return "ok"

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except Exception:  # noqa: BLE001
            pass
        # Leaving the file behind would make the next launch try a dead port,
        # wait for the timeout, and only then start -- a slow, mysterious
        # first press.
        try:
            os.remove(_path())
        except OSError:
            pass


# ---------------------------------------------------------------------------
# the second launch's side
# ---------------------------------------------------------------------------
def send(command: str) -> bool:
    """Ask a running copy to do something. False if there isn't one.

    False covers every way this can fail -- no file, stale file, dead port,
    wrong token -- because the caller's next move is the same for all of them:
    start the app itself.
    """
    try:
        with open(_path(), encoding="utf-8") as f:
            info = json.load(f)
        port, token = int(info["port"]), str(info["token"])
    except (OSError, ValueError, KeyError):
        return False

    try:
        with socket.create_connection(("127.0.0.1", port), TIMEOUT) as s:
            s.settimeout(TIMEOUT)
            s.sendall(json.dumps({"token": token,
                                  "command": command}).encode("utf-8"))
            return s.recv(64).decode("utf-8", "replace").strip() == "ok"
    except OSError:
        # A stale file from a copy that was killed rather than closed. Clear it
        # so the next press does not pay the timeout again.
        try:
            os.remove(_path())
        except OSError:
            pass
        return False


def running() -> bool:
    """True if a copy is up and answering."""
    return send(PING)
