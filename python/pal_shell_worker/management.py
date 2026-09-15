"""Linux management policy shared by preparation and the protected verifier."""
import re
import shlex
from .protocol import RemoteError


def literal_package_selector(name):
    # apt-get otherwise falls back to an unanchored regex for names such as
    # "bash." and interprets trailing +/- as operations. Only our escaped,
    # anchored selector reaches APT; the user still approves literal names.
    if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]+', name):
        raise RemoteError('privilege_command_unsupported', 'Invalid package name')
    return '^' + re.escape(name) + '$'


def parse_apt(command):
    if any(c in command for c in '\n\r\x00'):
        raise RemoteError('privilege_command_unsupported', 'Use one apt update or apt install command')
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise RemoteError('privilege_command_unsupported', 'Invalid APT command') from exc
    if len(tokens) < 2 or tokens[0] not in ('apt', 'apt-get'):
        raise RemoteError('privilege_command_unsupported', 'Linux sudo supports apt update or apt install PACKAGE... only')
    if tokens[1:] == ['update']:
        return {'action': 'apt_update', 'packages': []}
    if tokens[1] == 'install' and 2 < len(tokens) <= 130 and all(re.fullmatch(r'[a-z0-9][a-z0-9+.-]+', p) for p in tokens[2:]):
        return {'action': 'apt_install', 'packages': list(dict.fromkeys(tokens[2:]))}
    raise RemoteError('privilege_command_unsupported', 'Use apt update or apt install PACKAGE... without options, paths, or shell operators')


def normalize(args):
    if args['action'] == 'shutdown':
        return {'action': 'shutdown', 'packages': []}
    if args['action'] != 'sudo' or args.get('cwd') or args.get('tty'):
        raise RemoteError('privilege_command_unsupported', 'Linux management does not accept cwd, PTY, or arbitrary root commands')
    return parse_apt(args['cmd'])
