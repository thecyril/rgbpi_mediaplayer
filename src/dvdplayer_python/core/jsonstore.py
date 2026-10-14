"""Crash-tolerant reads/writes for the JSON state files on the SD card.

A plain ``Path.write_text`` truncates the target first, so a power cut or a
reboot in the middle of one leaves a half-written — in practice zero-byte —
file behind. The next start then dies in ``json.loads`` before the player has
even created its control socket, and the launcher relaunches RePlay forever.
That is exactly what a reboot during playback did: ``playback_bookmarks.json``
is rewritten every few seconds while a video plays, so it is the file most
likely to be caught mid-write.

Every persistent state file goes through here instead:

* writes land on a temp file that is flushed, fsync'd and then renamed over
  the target, so a reader only ever sees the whole old or the whole new file;
* reads that hit garbage move the bad file aside (``<name>.bad``) and return
  the caller's default, so a corrupt file costs the user that file's settings
  instead of the whole player.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional, Tuple, Type, Union

from .debuglog import log_event

TypeSpec = Union[Type, Tuple[Type, ...]]


def read_json(path: Path, default: Any = None, *, expect: Optional[TypeSpec] = None) -> Any:
    """Return the JSON value at ``path``, or ``default`` if it can't be used.

    ``expect`` is an isinstance() spec: a file holding valid JSON of the wrong
    shape (a list where a dict is expected) is treated as corrupt too, since
    callers would otherwise blow up on it just the same.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except OSError as exc:
        log_event("state_read_failed", path=str(path), error=str(exc))
        return default
    try:
        value = json.loads(text)
    except ValueError as exc:
        _quarantine(path, f"invalid json: {exc}", size=len(text))
        return default
    if expect is not None and not isinstance(value, expect):
        _quarantine(path, f"unexpected type {type(value).__name__}", size=len(text))
        return default
    return value


def write_json(path: Path, data: Any, *, indent: Optional[int] = 2, durable: bool = True,
               mode: Optional[int] = None) -> bool:
    """Serialize ``data`` and write it atomically. Never raises."""
    try:
        payload = json.dumps(data, indent=indent)
    except (TypeError, ValueError) as exc:
        log_event("state_encode_failed", path=str(path), error=str(exc))
        return False
    return write_text_atomic(path, payload, durable=durable, mode=mode)


def write_text_atomic(path: Path, text: str, *, durable: bool = True,
                      mode: Optional[int] = None) -> bool:
    """Write ``text`` to ``path`` via a temp file + rename. Never raises.

    ``durable`` fsyncs the data and the directory entry before returning; pass
    False for throwaway files rewritten many times a second (the runtime status
    export), where the rename alone is enough and the fsync would only add SD
    card wear. ``mode`` is applied to the temp file *before* the rename, so a
    secret never exists at the final path with default permissions.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            if durable:
                os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
        if durable:
            _sync_dir(path.parent)
        return True
    except OSError as exc:
        log_event("state_write_failed", path=str(path), error=str(exc))
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _quarantine(path: Path, reason: str, size: int = -1) -> None:
    """Move a corrupt state file aside so the next start gets a clean slate."""
    bad = path.with_name(path.name + ".bad")
    moved = True
    try:
        os.replace(path, bad)
    except OSError as exc:
        moved = False
        log_event("state_quarantine_failed", path=str(path), error=str(exc))
        try:
            path.unlink()
        except OSError:
            pass
    log_event(
        "state_file_corrupt",
        path=str(path),
        reason=reason,
        size=size,
        quarantined=str(bad) if moved else None,
    )


def _sync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
