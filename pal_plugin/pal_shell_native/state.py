from pal.execution.runtime_state import ExecutionRuntimeStatePort
from dataclasses import dataclass


@dataclass(frozen=True)
class _PreparedNativeState:
    execution: object
    remote_files: dict


class NativeExecutionStatePort(ExecutionRuntimeStatePort):
    def _has_native_work(self):
        owner = self.runtime.shell_owner
        return owner.has_work

    def snapshot_state(self):
        # Persist logical execution state even while native processes or
        # undelivered output remain live. Native process handles are not part
        # of this payload and cannot gate the resident conversation journal.
        return {**super().snapshot_state(), 'remote_files': self.runtime.shell_owner.files.snapshot()}

    def prepare_restore_state(self, payload):
        if self._has_native_work():
            raise RuntimeError("Reset native shell sessions before restoring runtime state.")
        prepared = super().prepare_restore_state(payload)
        self.runtime.shell_owner.files.validate_snapshot(dict(payload.get('remote_files') or {}))
        return _PreparedNativeState(prepared, dict(payload.get('remote_files') or {}))

    def install_prepared_state(self, prepared):
        super().install_prepared_state(prepared.execution)
        self.runtime.shell_owner.files.restore(prepared.remote_files)

    def finish_restore_state(self, ports):
        self.runtime.shell_owner.files.detach_histories()
        super().finish_restore_state(ports)
        self.runtime.shell_owner.files.reconcile_restored_history()

    async def reset_state(self, reason):
        await self.runtime.shell_owner.reset()
        super().reset_state(reason)
