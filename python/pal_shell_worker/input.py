"""Literal finite stdin shared by host, worker and protected helpers."""
from contextlib import contextmanager
import hashlib
import subprocess
import tempfile


def stdin_bytes(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('stdin must be UTF-8 text or null')
    try:
        return value.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ValueError('stdin must be valid UTF-8 text') from exc


def stdin_identity(value):
    raw = stdin_bytes(value)
    return None if raw is None else (len(raw), hashlib.sha256(raw).hexdigest())


@contextmanager
def input_file(value):
    raw = stdin_bytes(value)
    if raw is None:
        yield subprocess.DEVNULL
    else:
        with tempfile.TemporaryFile() as stream:
            stream.write(raw)
            stream.seek(0)
            yield stream
