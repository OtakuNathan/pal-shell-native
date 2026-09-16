"""Native-owned tool guidance; importing contracts does not load native binaries."""
from pal.execution.tool_facade import NextToolHint, ToolGuidance


RUN_GUIDANCE = ToolGuidance(
    purpose="Run a shell command; return its result or a retained session snapshot.",
    use_when=(
        "Execute commands, builds or tests. Prefer rg for repository text search and rg --files for file"
        " enumeration; use alternatives only when rg is unavailable or unsuitable. Run tests and builds"
        " directly to preserve full output. wait_ms controls response waiting (default five minutes,"
        " one second for a PTY), not process lifetime; timeout_ms sets an optional hard deadline."
        " Use tty=true for interactive terminal input. A session_id identifies retained execution; use status to distinguish running from terminal output."
        " Returning control while waiting does not complete the task. Continue independent reads or reasoning allowed by the current write gate; "
        " otherwise yield until a relevant event. Completion is delivered separately while the session is watched."
        " Success requires status=exited and returncode=0."
    ),
    do_not_use_when=(
        "A dedicated file or Pal runtime/module/Bunshin introspection tool directly handles the task."
        " For Pal self-maintenance follow pal.self.maintenance and the active runtime policy."
        " Do not pipe long-running"
        " tests/builds through head, tail or grep to shorten output; result budgeting handles it. Do not rerun a command"
        " that returned a live session or repeatedly poll it just to wait for completion."
    ),
    failure_next_steps=(
        "Inspect status, returncode, stdout and stderr before deciding whether repetition is safe."
        " Follow live-session affordances. If output is unavailable, inspect the reported execution state;"
        " never repeat a command to retrieve output. A missing session does not prove it never ran."
    ),
    next_tool_hints=(
        NextToolHint(name="shell_session", use_when="Inspect progress, send PTY input, resize or terminate a returned session."),
    ),
)

SESSION_GUIDANCE = ToolGuidance(
    purpose="Inspect or control an existing shell session without rerunning its command.",
    use_when=(
        "Use the returned session_id. read returns a full snapshot; wait_ms waits for exit (default zero,"
        " maximum five minutes). Read for progress or interactive prompts, not in a short polling loop."
        " watch(wait_ms, extend_by_ms=0) immediately arms one background decision event, optionally extending the "
        " existing deadline atomically. extend(extend_by_ms) adds to the existing finite deadline only. "
        " unwatch disables future unsolicited notifications without stopping the process; watch restores attention. "
        " A read never extends a deadline or rearms notifications. No deadline means no extension is needed. "
        " write queues exact PTY text (include a newline to submit); acceptance does not prove processing."
        " resize changes a live PTY. terminate requests cancellation; read terminal status to confirm exit."
        " release discards completed output."
    ),
    do_not_use_when=(
        "Do not invent IDs or use zero. Do not write or resize a non-PTY session, or release a running one."
        " Delivered terminal output is released automatically; use read_tool_result for its result_handle."
    ),
    failure_next_steps=(
        "For invalid_session, consult the previous result/result_handle; reset or consumption"
        " may have retired it. Do not rerun the command automatically. For live-session precondition errors,"
        " read current status. After uncertain input delivery, inspect output before resending input."
    ),
)

REMOTE_RUN_GUIDANCE = RUN_GUIDANCE.model_copy(update={
    "purpose": "Run a shell command locally or on a configured remote target; return its result or a retained session snapshot.",
    "use_when": (
        "When the task already requires remote execution and the user has not specified a connection method, "
        "prefer run_shell(target=...) for configured targets. Use list_remote if the target mapping is unknown. "
        "The task determines the execution location; configured remotes do not change the local default. "
        "Honor an explicit request for SSH. "
    ) + RUN_GUIDANCE.use_when + (
        " On Linux remote targets, sudo=true supports only apt/apt-get update or apt/apt-get install PACKAGE... "
        "without extra flags or shell operators; each requires approval. Inspect list_remote for configured management support."
    ),
    "do_not_use_when": RUN_GUIDANCE.do_not_use_when + (
        " Local file tools do not access remote paths. Use the selected target shell for remote files "
        "when no corresponding file capability exists."
    ),
    "failure_next_steps": RUN_GUIDANCE.failure_next_steps,
    "next_tool_hints": RUN_GUIDANCE.next_tool_hints + (
        NextToolHint(name="list_remote", use_when="The task needs remote execution and the configured target ID is unknown."),
        NextToolHint(name="shell_status", use_when="A shell is blocked or retained output/completion needs diagnosis."),
    ),
})

RESIDENT_SESSION_GUIDANCE = SESSION_GUIDANCE.model_copy(update={
    "failure_next_steps": SESSION_GUIDANCE.failure_next_steps + " Use shell_status when the session ID or retained state is unknown.",
})
