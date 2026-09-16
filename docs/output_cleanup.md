# Delivery before resource cleanup

After the tool result or resident event is durably recorded in L1, the shell owner
records exactly the captured delivery and schedules cleanup. The caller can start
the next model request without awaiting output release. Output already handed to
the pager remains valid after the native spool/cache is removed.

Terminal release has one task per `(target, runtime_epoch, output_id)`, including
session-zero one-shot commands. Each task attempts at most five times including
the initial attempt, with 1/2/4/8-second exponential delays and ±20% jitter.
Permanent errors stop immediately. Reconnection may shorten an existing delay;
model responses cannot reset or create another retry budget. Existing transport
timeouts apply. Command execution and session deadlines are unchanged.

Output materialization uses the same finite read retry policy. Once a terminal
output failure has been recorded in L1, the output is abandoned and cleanup is
scheduled. This records neither successful byte coverage nor a successful output
delivery. A still-running session or an unknown command outcome is not discarded
by this rule. The command itself is never resubmitted as recovery.

After release succeeds or exhausts its budget, local references, terminal tickets
and download caches are retired. Exhaustion is logged through the existing host
logger; it does not imply that a disconnected worker released remote resources.
The worker's existing lifetime/capacity limits still apply. Closing the shell owner
cancels its cleanup tasks. Cleanup does not wake or rerun a model turn.

`OutputCleanup.tla` models success, failed-output abandonment, lost release replies,
permanent/exhausted failure, owner close and an independent model request. It
complements `HostObservation.tla` and `SessionLifecycle.tla`; the shared checker
runs all three. Runtime tests gate the release operation to verify that L1 commit
and the next model request proceed while release remains blocked, and exercise
five-attempt exhaustion, reconnect, duplicate scheduling and session-zero identity.
