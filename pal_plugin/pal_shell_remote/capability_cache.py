"""Last verified capabilities, never live readiness or execution authority."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile

from pal_shell_contracts import resolve_target

logger = logging.getLogger(__name__)


class CapabilityCache:
    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, target):
        # Bind observations to the full configured endpoint/identity, not an ID
        # that may later name another machine. No credential contents are stored.
        binding = resolve_target([target], target.target)['binding']
        return self.directory / (binding + '.json')

    def load(self, target):
        try:
            data = json.loads(self._path(target).read_text())
            if (isinstance(data, dict) and data.get('version') == 1
                    and isinstance(data.get('capabilities'), dict)):
                capabilities = data['capabilities']
                if all(key not in capabilities or isinstance(capabilities[key], dict)
                       for key in ('privilege', 'power')):
                    installation = capabilities.get('privilege', {}).get('installation')
                    if installation is None or isinstance(installation, dict):
                        return capabilities
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.warning('Cannot read remote capability cache for target %s', target.target)
        return None

    def save(self, target, capabilities):
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix='.capabilities-', dir=self.directory)
            with os.fdopen(fd, 'w') as output:
                json.dump({'version': 1, 'capabilities': capabilities}, output, ensure_ascii=False)
            os.replace(temporary, self._path(target))
        except OSError:
            logger.warning('Cannot persist remote capability cache for target %s', target.target)
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    logger.warning('Cannot remove temporary capability cache for target %s', target.target)


def capability_summary(config, observed, *, fresh=False):
    declared = config.static.get('privilege', {})
    privilege = (observed or {}).get('privilege')
    source = 'probe' if fresh else 'last_known' if observed else 'configured' if declared else 'unknown'
    facts = privilege if privilege is not None else declared
    installation = facts.get('installation')
    supported = privilege.get('supported') if privilege is not None else None
    if supported and installation is not None and not installation.get('ok'):
        supported = False
    power = (observed or {}).get('power')
    shutdown = None if power is None else bool(power.get('shutdown') and power.get('policy') != 'disabled')
    if shutdown and installation is not None and not installation.get('ok'):
        shutdown = False
    return {
        'privilege': {key: facts.get(key) for key in (
            'mode', 'configured', 'allowed_actions', 'installation', 'approval_required')} | {
                'supported': supported,
                'source': source, 'observed_at': (observed or {}).get('observed_at'),
            },
        'shutdown': {'supported': shutdown, 'source': source,
                     'observed_at': (observed or {}).get('observed_at'),
                     'reason': 'Refresh to verify shutdown support' if shutdown is None else
                               ('' if shutdown else 'Shutdown is not available under the observed policy/installation')},
    }
