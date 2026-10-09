"""Bounded host-only recovery. Never accepts a command submission callback."""
import asyncio
import logging
import random
import sys
import traceback

from pal_shell_contracts import RemoteFailure
from pal.shared.diagnostics import diagnostic_text

LOGGER = logging.getLogger(__name__)
MAX_ATTEMPTS = 5
RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
PERMANENT = frozenset({'invalid_output', 'output_capacity', 'runtime_changed',
                       'authorization_required', 'invalid_operation', 'invalid_session'})


def exception_report(exc):
    """Keep the full redacted cause chain; let Pal's output pager budget it.

    Use the existing redactor with an explicit size so this plugin also works
    with resident Pal versions predating the unbounded diagnostic helper.
    """
    report = traceback.TracebackException.from_exception(
        exc, max_group_width=sys.maxsize, max_group_depth=sys.maxsize)
    text = ''.join(report.format()).rstrip()
    return diagnostic_text(text, limit=sys.maxsize)


async def retry_read(operation, *, stage, identity, wakeup=None):
    """One bounded recovery item; reconnect may shorten delay, never reset its budget."""
    for attempt in range(MAX_ATTEMPTS):
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            permanent = isinstance(exc, (ValueError, TypeError)) or (
                isinstance(exc, RemoteFailure) and exc.code in PERMANENT)
            if permanent or attempt == MAX_ATTEMPTS - 1:
                LOGGER.warning('shell recovery exhausted stage=%s identity=%s error=%s',
                               stage, identity, type(exc).__name__)
                raise
            LOGGER.debug('shell recovery retry stage=%s identity=%s attempt=%s error=%s',
                         stage, identity, attempt + 1, type(exc).__name__)
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)] * random.uniform(.8, 1.2)
            if wakeup is None:
                await asyncio.sleep(delay)
            else:
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                wakeup.clear()
