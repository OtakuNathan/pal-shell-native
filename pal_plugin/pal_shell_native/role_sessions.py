"""Deliver native shell completions at a role's existing LLM safe points."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace



class BunshinShellSessions:
    def __init__(self, runtime):
        self.runtime = runtime
        self.owner = runtime.shell_owner
        self.ready = asyncio.Event()
        self.owner.defer_delivery = True
        self.owner.require_output_delivery = True
        self.owner.on_ready = self.ready.set
        self.completions = self.owner.observations.pending
        self.failures = self.owner.observations.failures

    @property
    def has_work(self):
        return self.owner.completion_blocked

    @property
    def resources_live(self):
        return self.owner.has_work

    def handles_capability(self, name):
        from pal.bunshin.v2.verification_builder import SHELL_EVIDENCE_CAPABILITIES
        return name in SHELL_EVIDENCE_CAPABILITIES | {"op_exec_shell", "op_exec_session"}

    def execution_delegate(self, runtime, check_cancel):
        async def execute(call, **kwargs):
            return await self.run_tool(runtime.execute_tool_async(call, **kwargs), check_cancel)
        return SimpleNamespace(execute_tool_async=execute)

    async def run_tool(self, operation, check_cancel):
        task = asyncio.create_task(operation)
        try:
            while not task.done():
                await check_cancel()
                await asyncio.wait({task}, timeout=.25)
            return await task
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    def _collect(self):
        self.owner.observations.collect()

    async def before_model(self, memory, turn_id, check_cancel, **_legacy):
        """Compatibility entrypoint: only project what is ready right now."""
        await check_cancel()
        self.owner.observations.project(self.runtime, memory, SimpleNamespace(turn_id=turn_id, delivery_binding=None))

    async def wait_after_response(self, check_cancel):
        """Yield a normal text response before deciding task completion.

        park_or_ready registers its waiter in the same owner transition that
        checks event eligibility. State refreshes cannot satisfy this waiter.
        """
        observations = self.owner.observations
        observations.refresh()
        while self.has_work:
            await check_cancel()
            if observations.failures:
                return "Shell output is unavailable; use the delivered execution state and error."
            waiter = observations.park_or_ready()
            try:
                while not waiter.done():
                    await check_cancel()
                    if not self.has_work:
                        return ""
                    await asyncio.wait({waiter}, timeout=.25)
                await waiter
                observations.collect()
                if any(observations.eligible(sid, ready=True) for sid in observations.pending):
                    return ""
            finally:
                if not waiter.done():
                    waiter.cancel()
        return ""

    def retry_note(self):
        if not self.has_work:
            return ""
        return ("This role still owns shell work or undelivered output and cannot finish yet. "
                "An eligible observation is ready for this request. Use its state and output to continue; "
                "do not repeat the command. Use shell_session only when execution control or a fresh read is needed.")
