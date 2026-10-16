from pal.execution.runtime_state import ExecutionRuntimeStatePort


class NativeExecutionStatePort(ExecutionRuntimeStatePort):
    def _has_native_work(self):
        owner = self.runtime.shell_owner
        return owner.has_work

    def snapshot_state(self):
        # Persist logical execution state even while native processes or
        # undelivered output remain live. Native process handles are not part
        # of this payload and cannot gate the resident conversation journal.
        return super().snapshot_state()

    def prepare_restore_state(self, payload):
        if self._has_native_work():
            raise RuntimeError("Reset native shell sessions before restoring runtime state.")
        return super().prepare_restore_state(payload)

    async def reset_state(self, reason):
        await self.runtime.shell_owner.reset()
        super().reset_state(reason)
