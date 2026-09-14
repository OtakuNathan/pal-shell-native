"""Apply the pending upstream fix, tolerating a repeated CMake configure."""
from pathlib import Path
import os
import subprocess
import sys

git, source, patch = sys.argv[1:]
# Archive dependencies are not repositories. Prevent git apply from discovering
# the enclosing project and silently skipping paths outside its current prefix.
env = os.environ.copy()
for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
    env.pop(key, None)
env['GIT_CEILING_DIRECTORIES'] = str(Path(source).resolve().parent)
command = [git, '-C', source, 'apply']
# Source archives use LF. Accept older Windows checkouts whose patch file was
# converted to CRLF, including bare empty context lines Git accepts with LF.
patch_bytes = Path(patch).read_bytes().replace(b'\r\n', b'\n')
if subprocess.run([*command, '--reverse', '--check', '-'], input=patch_bytes, capture_output=True, env=env).returncode:
    subprocess.run([*command, '--check', '-'], input=patch_bytes, check=True, env=env)
    subprocess.run([*command, '-'], input=patch_bytes, check=True, env=env)
