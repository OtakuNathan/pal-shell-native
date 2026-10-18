"""Restore the caller's loader environment before a bundled worker spawns tools."""
from __future__ import annotations

import os
import sys


def restore_subprocess_environment() -> None:
    """Run once at startup, after importing the worker's bundled dependencies.

    PyInstaller prepends its own libraries to LD_LIBRARY_PATH. Native execv
    inherits this environment too, so subprocess-only Python wrappers cannot
    protect commands launched by the extension.
    """
    if not sys.platform.startswith('linux') or not getattr(sys, 'frozen', False):
        return
    if 'LD_LIBRARY_PATH_ORIG' in os.environ:
        os.environ['LD_LIBRARY_PATH'] = os.environ['LD_LIBRARY_PATH_ORIG']
    else:
        os.environ.pop('LD_LIBRARY_PATH', None)
