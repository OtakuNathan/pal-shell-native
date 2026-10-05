"""Compatibility imports for the standalone acceptance suite."""
from pal_shell_native.tools import *  # noqa: F401,F403

from pal_shell_native.tools import session_affordances as native_session_affordances


def session_affordances(result):
    """Route recovery through the prototype's registered multiplexer."""
    return [hint.model_copy(update={"arguments": {
        "name": "manage_shell_session", "args": {**hint.arguments["args"], "action": "read"},
    }}) for hint in native_session_affordances(result)]
