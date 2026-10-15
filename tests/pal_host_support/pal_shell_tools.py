"""Compatibility imports for the standalone acceptance suite."""
from pal_shell_native.tools import *  # noqa: F401,F403

from pal_shell_native.tools import session_affordances as native_session_affordances
from pal.execution.tool_facade import NextToolHint


# This standalone acceptance host deliberately registers its own multiplexer.
# Production guidance points at simple tools that this fixture does not mount.
RUN_GUIDANCE = RUN_GUIDANCE.model_copy(update={
    "next_tool_hints": (NextToolHint(
        name="manage_shell_session", use_when="Inspect or control the returned prototype session."),),
})


def session_affordances(result):
    """Route recovery through the prototype's registered multiplexer."""
    return [hint.model_copy(update={"arguments": {
        "name": "manage_shell_session", "args": {**hint.arguments["args"], "action": "read"},
    }}) for hint in native_session_affordances(result)]
