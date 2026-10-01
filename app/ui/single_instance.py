"""Keep one running copy per Windows user.

Because the app keeps running in the system tray, double-clicking the
shortcut again must bring the existing window forward rather than start a
second copy against the same database. The first instance listens on a
per-user local socket (a named pipe on Windows); later launches send it
"show" and exit.
"""

from __future__ import annotations

import getpass
import re

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from app.config import APP_NAME

_MESSAGE = b"show"
_TIMEOUT_MS = 500


def server_name() -> str:
    """Return a per-user socket name, safe for a Windows pipe name."""
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = "user"
    return f"{APP_NAME}-{re.sub(r'[^A-Za-z0-9_.-]', '_', user)}"


class SingleInstance(QObject):
    """Detect an already running instance and accept "show" requests."""

    activation_requested = Signal()

    def __init__(self, name: str | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.name = name or server_name()
        self._server: QLocalServer | None = None

    def notify_existing(self) -> bool:
        """Ask a running instance to show itself; ``True`` if one answered."""
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        if not socket.waitForConnected(_TIMEOUT_MS):
            return False
        socket.write(_MESSAGE)
        socket.flush()
        socket.waitForBytesWritten(_TIMEOUT_MS)
        socket.disconnectFromServer()
        return True

    def listen(self) -> bool:
        """Start accepting activation requests from later launches."""
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
        socket.readyRead.connect(lambda: self._read(socket))
        socket.disconnected.connect(socket.deleteLater)
        if socket.bytesAvailable():
            self._read(socket)

    def _read(self, socket: QLocalSocket) -> None:
        if bytes(socket.readAll().data()).startswith(_MESSAGE):
            self.activation_requested.emit()
