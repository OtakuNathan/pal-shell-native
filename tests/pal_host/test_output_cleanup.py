"""Model-independent resource retirement, with one lifetime retry budget."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pal.core import PalCore  # Initialize the host before its execution extension.
from pal_shell_native.adapter import Completion
from pal_shell_native.runtime import NativeShellOwner, PendingOutput
from pal_shell_native.remote_contract import RemoteFailure


class Shell:
    def __init__(self):
        self._foreground = {}
        self.execution_work = False
        self._completions = {}
        self.started = asyncio.Event()
        self.gate = asyncio.Event()
        self.attempts = 0
        self.forgotten = []
        self.error = None

    async def acknowledge_completion(self, event):
        self.started.set()
        self.attempts += 1
        await self.gate.wait()
        if self.error:
            raise self.error

    def forget_output(self, raw):
        self.forgotten.append(raw['output_id'])

    async def close(self):
        pass


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = NativeShellOwner()
        self.shell = Shell()
        self.owner._shell = self.shell
        self.raw = dict(session_id=0, target=1, runtime_epoch='epoch', output_id='out',
                        status='exited', returncode=0, operation_id='op')

    async def asyncTearDown(self):
        await self.owner.close()

    def pending(self, call='call', raw=None):
        value = dict(raw or self.raw)
        self.owner.pending[call] = PendingOutput(value, 'turn', prepared=True)
        return Completion(value['session_id'], 'turn', value)

    async def test_commit_does_not_wait_for_release_and_duplicate_commit_shares_task(self):
        event = self.pending()
        await asyncio.wait_for(self.owner.commit('call'), 1)
        await self.shell.started.wait()
        first = tuple(self.owner.observations.acking.values())[0]
        await self.owner.commit('call')
        self.assertIs(self.owner.observations.acknowledge(event), first)
        self.assertTrue(self.owner.pending['call'].delivered)
        self.assertFalse(self.owner.completion_blocked)
        self.assertFalse(first.done())
        self.shell.gate.set()
        await first
        self.assertEqual(self.shell.attempts, 1)
        self.assertFalse(self.owner.pending)
        self.assertIsNone(self.owner.observations.acknowledge(event))

    async def test_exhaustion_retires_local_resources_without_resetting_budget(self):
        event = self.pending()
        self.shell.error = OSError('disconnected')
        self.shell.gate.set()
        with patch('pal_shell_native.recovery.RETRY_DELAYS', (0,)):
            await self.owner.commit('call')
            task = self.owner.observations.acknowledge(event)
            for _ in range(10):
                self.owner.observations.retry_acknowledgements()
                await asyncio.sleep(0)
            await task
        self.assertEqual(self.shell.attempts, 5)
        self.assertEqual(self.shell.forgotten, ['out'])
        self.assertFalse(self.owner.has_work)
        self.owner.observations.retry_acknowledgements()
        self.assertIsNone(self.owner.observations.acknowledge(event))
        self.assertEqual(self.shell.attempts, 5)

    async def test_terminal_read_failure_is_retained_after_error_delivery(self):
        loads = 0
        async def unavailable(raw):
            nonlocal loads
            loads += 1
            raise OSError('output unavailable')
        self.shell.materialize = unavailable
        call = SimpleNamespace(meta={'tool_call': SimpleNamespace(call_id='call'), 'turn_id': 'turn'})
        with patch('pal_shell_native.recovery.RETRY_DELAYS', (0,)):
            result = await self.owner.stage(call, self.raw)
        self.assertEqual(loads, 5)
        self.assertIn('output_error', result.structured)
        self.assertFalse(self.shell.started.is_set())
        await self.owner.commit('call')
        await asyncio.sleep(0)
        self.assertTrue(self.owner.pending['call'].failure)
        self.assertTrue(self.owner.pending['call'].delivered)
        self.assertEqual(self.shell.attempts, 0)
        self.assertFalse(self.owner.observations.acking)
        self.assertFalse(self.owner.observations.covered)

    async def test_output_capacity_failure_does_not_release_original_output(self):
        call = SimpleNamespace(meta={'tool_call': SimpleNamespace(call_id='call'), 'turn_id': 'turn'})
        with patch('pal_shell_native.runtime.REMOTE_PENDING_BYTES', 0):
            result = await self.owner.stage(call, {**self.raw, 'stdout_total': 1})
        self.assertIn('output_error', result.structured)
        self.assertFalse(self.shell.started.is_set())
        await self.owner.commit('call')
        await asyncio.sleep(0)
        self.assertTrue(self.owner.pending['call'].delivered)
        self.assertEqual(self.shell.attempts, 0)
        self.assertTrue(self.owner.has_work)
        self.assertFalse(self.owner.completion_blocked)

    async def test_permanent_error_does_not_retry(self):
        self.pending()
        self.shell.error = RemoteFailure('runtime_changed', 'old runtime')
        self.shell.gate.set()
        await self.owner.commit('call')
        await asyncio.gather(*tuple(self.owner.observations.acking.values()))
        self.assertEqual(self.shell.attempts, 1)
        self.assertFalse(self.owner.pending)

    async def test_local_cleanup_error_does_not_keep_delivery_or_retry_work_alive(self):
        self.pending()
        def fail(raw):
            raise OSError('cache removal failed')
        self.shell.forget_output = fail
        self.shell.gate.set()
        with self.assertLogs('pal_shell_native.recovery', level='ERROR'):
            await self.owner.commit('call')
            await asyncio.gather(*tuple(self.owner.observations.acking.values()))
        self.assertFalse(self.owner.has_work)
        self.assertFalse(self.owner.observations.acking)

    async def test_distinct_oneshot_outputs_do_not_share_cleanup_and_close_cancels(self):
        self.pending('a')
        self.pending('b', {**self.raw, 'output_id': 'other', 'operation_id': 'op2'})
        await self.owner.commit('a')
        await self.owner.commit('b')
        tasks = tuple(self.owner.observations.acking.values())
        self.assertEqual(len(tasks), 2)
        await self.shell.started.wait()
        await self.owner.close()
        self.assertTrue(all(t.done() for t in tasks))
        self.assertFalse(self.owner.pending)
        self.assertFalse(self.owner.observations.acking)
        late = Completion(7, 'turn', {**self.raw, 'session_id': 7, 'status': 'running'})
        self.assertIsNone(self.owner.observations.acknowledge(late))
