"""Keep one running copy per Windows user.

Because the app keeps running in the system tray, double-clicking the
shortcut again must bring the existing window forward rather than start a
second copy against the same database. The first instance listens on a
per-user local socket (a named pipe on Windows); later launches talk to it:

* ``hello <build>``: a normal launch. If the running copy is the same build
  it shows itself (``shown``). If it is an older or newer build (the app was
  reinstalled while a copy kept running in the tray) it answers ``restart``
  and quits, and the new launch takes over. Otherwise the user would keep
  getting the stale copy, whose files were replaced underneath it.
* ``quit``: sent by ``AkremMobile.exe --quit`` (the installer uses it to
  close the app before replacing its files).
* ``show``: the original one-word protocol, still understood.
"""

from __future__ import annotations

import getpass
import logging
import re
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from app.config import APP_NAME, APP_VERSION

_SHOW = b"show"
_QUIT = b"quit"
_HELLO = b"hello "
_SHOWN = b"shown"
_RESTART = b"restart"
_TIMEOUT_MS = 800
_TAKEOVER_WAIT_S = 15.0

_LOG = logging.getLogger(__name__)


def server_name() -> str:
    """Return a per-user socket name, safe for a Windows pipe name."""
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = "user"
    return f"{APP_NAME}-{re.sub(r'[^A-Za-z0-9_.-]', '_', user)}"


def build_id() -> str:
    """Identify this exact build: version plus the executable's timestamp when frozen."""
    if getattr(sys, "frozen", False):
        try:
            return f"{APP_VERSION}-{int(Path(sys.executable).stat().st_mtime)}"
        except OSError:
            return APP_VERSION
    return f"{APP_VERSION}-dev"


def allow_other_process_to_take_focus() -> None:
    """Let the running copy bring its window to the front (Windows foreground rules).

    The process the user just started may take the foreground; it hands that
    right to the running copy before asking it to show itself.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(0xFFFFFFFF)  # ASFW_ANY
    except (AttributeError, OSError):
        _LOG.debug("AllowSetForegroundWindow failed", exc_info=True)


class SingleInstance(QObject):
    """Detect an already running instance and accept requests from later launches."""

    activation_requested = Signal()
    quit_requested = Signal()

    def __init__(self, name: str | None = None, parent: QObject | None = None, *, build: str | None = None) -> None:
        super().__init__(parent)
        self.name = name or server_name()
        self.build = build or build_id()
        self._server: QLocalServer | None = None

    # ----------------------------------------------------------- client side
    def notify_existing(self) -> bool:
        """Ask a running copy to show itself; ``True`` means this launch should exit.

        Returns ``False`` when nothing is running, or when the running copy is
        a different build that agreed to quit (this launch then takes over).
        """
        reply = self._send(_HELLO + self.build.encode("utf-8"), expect_reply=True)
        if reply is None:
            return False
        if reply.startswith(_RESTART):
            _LOG.info("A different build was running; it is closing so this one can start")
            self._wait_until_free()
            return False
        return True

    def request_quit(self) -> bool:
        """Ask a running copy to quit; ``True`` if one was running."""
        return self._send(_QUIT, expect_reply=False) is not None

    def _send(self, message: bytes, *, expect_reply: bool) -> bytes | None:
        """Send one message; ``None`` when no copy is running."""
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        if not socket.waitForConnected(_TIMEOUT_MS):
            return None
        allow_other_process_to_take_focus()
        socket.write(message + b"\n")
        socket.flush()
        socket.waitForBytesWritten(_TIMEOUT_MS)
        reply = b""
        if expect_reply and socket.waitForReadyRead(_TIMEOUT_MS * 2):
            reply = bytes(socket.readAll().data())
        socket.disconnectFromServer()
        # An old build only understands "show": no reply means it showed itself.
        return reply or _SHOWN

    def _wait_until_free(self) -> None:
        """Wait for the previous copy to finish quitting (bounded)."""
        deadline = time.monotonic() + _TAKEOVER_WAIT_S
        while time.monotonic() < deadline:
            probe = QLocalSocket()
            probe.connectToServer(self.name)
            if not probe.waitForConnected(200):
                return
            probe.disconnectFromServer()
            time.sleep(0.25)
        _LOG.warning("The previous copy did not quit in time; starting anyway")

    # ----------------------------------------------------------- server side
    def listen(self) -> bool:
        """Start accepting requests from later launches."""
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self.name):
            # A crashed instance can leave a stale socket file behind (Unix).
            QLocalServer.removeServer(self.name)
            if not server.listen(self.name):
                return False
        server.newConnection.connect(self._on_connection)
        self._server = server
        return True

    def close(self) -> None:
        """Stop listening (on quit)."""
        if self._server is not None:
            self._server.close()
            self._server = None

    def _on_connection(self) -> None:
        if self._server is None:
            return
        socket = self._server.nextPendingConnection()
        if socket is None:
            return
        # The server owns its sockets and deletes them with itself; deleting
        # them here as well (deleteLater) double-frees once the server goes.
        socket.readyRead.connect(lambda: self._read(socket))
        if socket.bytesAvailable():
            self._read(socket)

    def _read(self, socket: QLocalSocket) -> None:
        message = bytes(socket.readAll().data()).strip()
        if message.startswith(_QUIT):
            socket.close()
            self.quit_requested.emit()
            return
        if message.startswith(_HELLO) and message[len(_HELLO):].decode("utf-8", "replace") != self.build:
            socket.write(_RESTART)
            _flush(socket)
            socket.close()
            self.quit_requested.emit()
            return
        if message.startswith((_HELLO, _SHOW)):
            socket.write(_SHOWN)
            _flush(socket)
            self.activation_requested.emit()
        socket.close()


def _flush(socket: QLocalSocket) -> None:
    """Push a reply out unless the caller already hung up."""
    socket.flush()
    if socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
        socket.waitForBytesWritten(_TIMEOUT_MS)
