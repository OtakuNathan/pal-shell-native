"""Bounded read-only readiness and identity-bound capability observations."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from pal_shell_contracts import Target
from pal_shell_remote.capability_cache import CapabilityCache
from pal_shell_remote.hub import RemoteHub
from pal_shell_remote.slot import RemoteSlot
from pal_shell_native.capabilities import ListRemoteInput, _target_summary
from pal_shell_worker.protocol import RemoteError


READY = {'worker_id': 'worker', 'runtime_epoch': 'epoch', 'observed_at': '2026-11-17T12:00:00Z',
         'shell_ready': True, 'draining': False,
         'privilege': {'mode': 'signed_sudoers', 'configured': True, 'supported': True,
                       'allowed_actions': ['shell_command', 'shutdown'], 'installation': {'ok': True}},
         'power': {'shutdown': True, 'policy': 'approval'}}


class RemoteReadinessTests(unittest.IsolatedAsyncioTestCase):
    def slot(self, *, cache=None, target=None):
        target = target or Target(1, 'desktop', 'worker', 'pal', '/key', '/worker.sock')
        # Probes below inject transport outcomes; no real host is contacted.
        return RemoteSlot(target, executor=object(), capability_cache=cache)

    async def test_transient_boot_failures_retry_only_metadata(self):
        slot = self.slot()
        slot.request = AsyncMock(side_effect=[RemoteError('transport_lost', 'RPC peer closed'), READY])
        result = await slot.describe(True, wait_ready_ms=2000)
        self.assertEqual(result['readiness'], 'ready')
        self.assertEqual(result['probe_attempts'], 2)
        self.assertFalse(result['wait_timed_out'])
        for call in slot.request.await_args_list:
            self.assertEqual(call.args, ('metadata', {'refresh': True}))

    async def test_authentication_failure_is_not_retried(self):
        slot = self.slot()
        slot.request = AsyncMock(side_effect=RemoteError('ssh_authentication_failed', 'Permission denied'))
        result = await slot.describe(True, wait_ready_ms=2000)
        slot.request.assert_awaited_once()
        self.assertFalse(result['wait_timed_out'])
        self.assertEqual(result['probe_details']['code'], 'ssh_authentication_failed')

    async def test_wait_deadline_and_cancellation_do_not_leave_probe_running(self):
        stopped = asyncio.Event()
        async def probe(*args):
            try:
                await asyncio.sleep(60)
            finally:
                stopped.set()
        slot = self.slot()
        slot.request = probe
        result = await asyncio.wait_for(slot.describe(True, wait_ready_ms=20), 1)
        self.assertTrue(result['wait_timed_out'])
        self.assertTrue(stopped.is_set())
        self.assertEqual(result['probe_details']['code'], 'readiness_timeout')
        stopped.clear()
        task = asyncio.create_task(slot.describe(True, wait_ready_ms=300000))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())

    async def test_cached_capabilities_survive_restart_but_never_establish_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CapabilityCache(Path(directory))
            first = self.slot(cache=cache)
            first.request = AsyncMock(return_value=READY)
            await first.describe(True)
            second = self.slot(cache=cache)
            second.request = AsyncMock(side_effect=RemoteError('transport_lost', 'offline'))
            result = await second.describe()
            second.request.assert_not_awaited()
            self.assertIsNone(result['reachable'])
            self.assertIsNone(result['dynamic'])
            self.assertEqual(result['readiness'], 'unverified')
            summary = _target_summary(result)
            self.assertTrue(summary['management']['shutdown']['supported'])
            self.assertEqual(summary['management']['privilege']['source'], 'last_known')
            failed = await second.describe(True)
            self.assertFalse(failed['reachable'])
            self.assertEqual(failed['management']['privilege']['source'], 'last_known')
            changed = self.slot(cache=cache, target=replace(first.config, worker_id='other'))
            self.assertIsNone((await changed.describe())['management']['shutdown']['supported'])

    async def test_configured_privilege_is_not_verified_support(self):
        slot = self.slot(target=Target(1, 'desktop', 'worker', 'pal', '/key', '/worker.sock',
                        static={'privilege': {'mode': 'signed_sudoers', 'configured': True,
                                             'allowed_actions': ['apt_install']}}))
        result = await slot.describe()
        privilege = result['management']['privilege']
        self.assertTrue(privilege['configured'])
        self.assertIsNone(privilege['supported'])
        self.assertEqual(privilege['source'], 'configured')

    async def test_start_is_executed_once_and_failed_start_is_not_probed(self):
        hub = RemoteHub([])
        slot = self.slot()
        hub.slots[1] = slot
        slot.start = AsyncMock(return_value={'returncode': 0})
        slot.request = AsyncMock(side_effect=[RemoteError('transport_lost', 'booting'), READY])
        result = await hub.call('start', {'target': 1, 'action': 'wake', 'wait_ready_ms': 2000})
        self.assertEqual(result['result']['readiness'], 'ready')
        slot.start.assert_awaited_once_with('wake')
        slot.start.return_value = {'returncode': 1}
        slot.request.reset_mock()
        result = await hub.call('start', {'target': 1, 'action': 'wake', 'wait_ready_ms': 2000})
        self.assertEqual(result['result']['readiness'], 'unverified')
        slot.request.assert_not_awaited()

    def test_wait_requires_explicit_refresh(self):
        from jsonschema import Draft202012Validator
        schema = Draft202012Validator(ListRemoteInput.model_json_schema())
        with self.assertRaises(ValueError):
            ListRemoteInput(wait_ready_ms=1000)
        self.assertFalse(schema.is_valid({'wait_ready_ms': 1000}))
        self.assertFalse(schema.is_valid({'wait_ready_ms': 1000, 'refresh': False}))
        self.assertTrue(schema.is_valid({'wait_ready_ms': 1000, 'refresh': True}))
        self.assertEqual(ListRemoteInput(refresh=True, wait_ready_ms=1000).wait_ready_ms, 1000)
