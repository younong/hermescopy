"""Import-light liveness checks for canonical local Unix sockets."""
from __future__ import annotations

import errno
import socket
import stat
from pathlib import Path


def canonical_unix_peer_is_absent(socket_path: Path) -> bool:
    """Return true only when a canonical local Unix peer is conclusively absent."""
    if not socket_path.exists():
        return True
    try:
        if not stat.S_ISSOCK(socket_path.stat().st_mode):
            return False
    except OSError:
        return False
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(0.25)
        client.connect(str(socket_path))
    except FileNotFoundError:
        return True
    except ConnectionRefusedError:
        return True
    except OSError as exc:
        return exc.errno in {errno.ENOENT, errno.ECONNREFUSED}
    else:
        return False
    finally:
        client.close()
