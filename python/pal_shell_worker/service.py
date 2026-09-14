"""Generate user-service definitions without enabling, loading or starting them."""
from pathlib import Path
import plistlib
import sys


def definition(executable, config, platform=None):
    executable, config = Path(executable).resolve(), Path(config).resolve()
    platform = platform or sys.platform
    argv = [str(executable), '--config', str(config)]
    if platform == 'darwin':
        return 'com.pal.shell-worker.plist', plistlib.dumps({
            'Label': 'com.pal.shell-worker', 'ProgramArguments': argv,
            'RunAtLoad': True, 'KeepAlive': False,
            'ProcessType': 'Background', 'Umask': 0o077,
        })
    if platform.startswith('linux'):
        def quote(value):
            return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'
        if any('\n' in value or '\r' in value for value in argv):
            raise ValueError('Service paths must not contain newlines')
        return 'pal-shell-worker.service', ("[Unit]\nDescription=Pal independent native shell worker\n\n"
            "[Service]\nType=simple\nExecStart=" + ' '.join(map(quote, argv)) +
            "\nRestart=no\nUMask=0077\nKillMode=control-group\nTimeoutStopSec=15\n\n"
            "[Install]\nWantedBy=default.target\n").encode()
    raise ValueError('Worker user services support Linux and macOS')
