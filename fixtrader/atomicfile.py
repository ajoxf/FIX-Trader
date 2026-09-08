"""Write a file so a reader never sees half of it.

`open(path, 'w')` truncates first. A reader in that window sees an empty or
partial file — and in front of a read-modify-write save, that read an EMPTY
config and wrote it back, deleting every account. This is one small module so
that no caller has to remember the sequence.
"""

import json
import os
import tempfile
from typing import Any


def write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    """Replace `path` with `text` atomically."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".swap")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)            # atomic on POSIX and on Windows
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: str, data: Any, indent: int = 2) -> None:
    write_text(path, json.dumps(data, indent=indent, sort_keys=False,
                                default=str) + "\n")


def read_json(path: str, default: Any = None) -> Any:
    """Read, returning `default` when the file is missing.

    A file that exists but will not parse RAISES: a corrupt config that reads
    as an empty one is how a system starts up flat and reports everything at
    the venue as an orphan.
    """
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if not text.strip():
        return default
    return json.loads(text)
