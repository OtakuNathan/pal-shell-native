"""Apply the pending upstream fix, tolerating a repeated CMake configure."""
from pathlib import Path
import subprocess
import sys

git, source, patch = sys.argv[1:]
command = [git, '-C', source, 'apply']
patch = str(Path(patch).resolve())
if subprocess.run([*command, '--reverse', '--check', patch], capture_output=True).returncode:
    subprocess.run([*command, '--check', patch], check=True)
    subprocess.run([*command, patch], check=True)
