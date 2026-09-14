"""Build a standalone directory bundle for the current OS/architecture.

Run in an isolated build environment with pal-shell-native and PyInstaller installed.
The privileged helper requires separate root-owned installation, never setuid.
"""
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent
subprocess.run([sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm', '--onedir',
    '--name', 'pal-shell-worker', '--hidden-import', '_pal_shell_runtime',
    '--hidden-import', '_pal_shell_rpc', '--collect-all', 'pal_shell_worker',
    str(root / 'worker_entry.py')], check=True)

# Keep the root monitor separate from the user service; installation never elevates it.
import importlib.metadata
import shutil
helper = Path(importlib.metadata.distribution('pal-shell-native').locate_file('libexec/pal-shell-privileged'))
if helper.is_file():
    shutil.copy2(helper, Path('dist/pal-shell-worker/pal-shell-privileged'))
shutil.copy2(root.parent / 'THIRD_PARTY.md', Path('dist/pal-shell-worker/THIRD_PARTY.md'))
