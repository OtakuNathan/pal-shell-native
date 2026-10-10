"""Native-owned tool guidance; importing contracts does not load native binaries."""
from pal.execution.tool_facade import NextToolHint, ToolGuidance


SHELL_RESULT_GUIDANCE = (
    "kind=complete means a shell result was returned, not that the command succeeded. "
    "effect=applied means execution or control took effect; it does not confirm task success. "
    "Check status, returncode, signal and error before deciding what happened."
)
NONZERO_EXIT_GUIDANCE = (
    "Read stdout and stderr before changing or repeating the command: a non-zero exit is the command's "
    "reported result, not necessarily a mistake. rg/grep exit 1 means no matches, diff exit 1 means "
    "differences, and test runners can report failing tests. Earlier command steps may already have "
    "changed state; do not repeat them automatically."
)
RUNNING_SESSION_GUIDANCE = (
    "This shell is still running in the background. You may continue calling tools. "
    "Avoid changing its input files or writing to the same output locations with other operations: "
    "results may become inconsistent. If relevant inputs change, the result may need to be verified again."
)


RUN_GUIDANCE = ToolGuidance(
    search_objects=("command", "commands"),
    purpose="Run a shell command; return its result or a retained session snapshot.",
    use_when=(
        "Execute commands, builds or tests. Prefer rg for repository text search and rg --files for file"
        " enumeration; use alternatives only when rg is unavailable or unsuitable. Search saved output snapshots with rg. "
        "Output snapshot paths are local to the Pal host, including copies of remote output; inspect them locally (target=0 where available). "
        "Read the saved file rather than rerunning its producer. Run tests and builds"
        " directly to preserve full output. wait_ms controls response waiting (default five minutes,"
        " one second for a PTY), not process lifetime; timeout_ms sets an optional hard deadline."
        " Use tty=true for interactive terminal input. A session_id identifies retained execution; use status to distinguish running from terminal output."
        " Returning control while waiting does not complete the task. Continue independent work or yield until a relevant event. "
        "Completion is delivered separately while the session is watched. "
        + RUNNING_SESSION_GUIDANCE +
        " Command success requires status=exited and returncode=0. "
    ) + SHELL_RESULT_GUIDANCE + " " + NONZERO_EXIT_GUIDANCE,
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
        NextToolHint(name="manage_shell_session", use_when="Inspect progress, send PTY input, resize or terminate a returned session."),
    ),
)

SESSION_GUIDANCE = ToolGuidance(
    search_objects=("session", "sessions", "output"),
    search_enum_fields=("action",),
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
        " Controls by action: read accepts optional wait_ms; write requires text; resize requires rows and columns;"
        " watch requires positive wait_ms and accepts optional extend_by_ms; extend requires positive extend_by_ms;"
        " terminate/release/unwatch accept no controls. Omit controls not listed for the action."
        " Supply exactly one of session_id or output_ref. output_ref supports only read/release with no controls, including wait_ms."
        " Deadline extension requires an unexpired, extendable finite deadline. "
    ) + SHELL_RESULT_GUIDANCE,
    do_not_use_when=(
        "Do not invent IDs or use zero. Do not write or resize a non-PTY session, or release a running one."
        " Large output is preserved in an immutable local snapshot; search its file or use read_file."
    ),
    failure_next_steps=(
        "For invalid_session, consult the previous result/output snapshot; reset or consumption"
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
        "Honor an explicit request for SSH. If a newly reachable SSH host is not a configured remote target, "
        "offer to enroll it after obtaining the user's consent; use search_skills/inject_skill for pal.remote.setup. "
        "SSH success alone is neither remote enrollment nor permission to install a worker or edit target configuration. "
    ) + RUN_GUIDANCE.use_when + (
        " On Linux remote targets, sudo=true supports only apt/apt-get update or apt/apt-get install PACKAGE... "
        "without extra flags or shell operators; requires tty=false, remote target>0, and approval for each command. Inspect list_remote for configured management support."
    ),
    "do_not_use_when": RUN_GUIDANCE.do_not_use_when + (
        " Local file tools do not access remote paths. Use the selected target shell for remote files "
        "when no corresponding file capability exists."
    ),
    "failure_next_steps": RUN_GUIDANCE.failure_next_steps,
    "next_tool_hints": RUN_GUIDANCE.next_tool_hints + (
        NextToolHint(name="read_shell_session", use_when="A fresh session snapshot or export of retained output is needed; never rerun its producer."),
        NextToolHint(name="write_shell_session", use_when="Send input to a live PTY; inspect output before resending uncertain input."),
        NextToolHint(name="terminate_shell_session", use_when="Cancellation is required; confirm terminal status afterwards."),
        NextToolHint(name="search_tools", use_when="With user consent, discover search_skills/inject_skill and use pal.remote.setup to enroll a newly reachable SSH host as a remote target."),
        NextToolHint(name="list_remote", use_when="The task needs remote execution and the configured target ID is unknown."),
        NextToolHint(name="inspect_shell_status", use_when="A shell is blocked or retained output/completion needs diagnosis."),
    ),
})

RESIDENT_SESSION_GUIDANCE = SESSION_GUIDANCE.model_copy(update={
    "failure_next_steps": SESSION_GUIDANCE.failure_next_steps + " Use inspect_shell_status when the session ID or retained state is unknown.",
})
