"""Online package switching must not count its own waiter as native work."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from pal.core import PalCore
from pal.core.main_context import MainContext
from pal.core.turns import TurnContinuation
from pal.execution import register_with_core
from pal.execution.runtime import ExecutionRuntime
from pal.foundation.persistence import PalV2Database
from pal.packages.archive import build
from pal.packages.jobs import PackageJobs
from pal.packages.service import PackageService
from pal.plugins import PluginHost
from pal.plugins.models import PluginBundleModel
from pal.shared.tool_protocol import new_tool_call


class PackageLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.saved_modules = {name: module for name, module in sys.modules.items()
                              if name.startswith(('pal_shell_native', 'pal_shell_remote', 'pal_shell_contracts'))}
        source = Path(__file__).resolve().parents[2] / 'pal_plugin'
        self.artifact = build(source, self.root / 'dist')
        # The dependency hook is covered separately. Keep publication fast so
        # the real package worker switches while the tool is still waiting.
        self.hooks = patch.object(PackageService, '_hook', return_value={'ok': True})
        self.hooks.start()
        self.addCleanup(self.hooks.stop)
        PackageService(self.root).install(self.artifact)
        self.database = PalV2Database(self.root / 'plugins.sqlite3')
        self.database.initialize([PluginBundleModel])
        self.core = PalCore(context=MainContext(execution_runtime=ExecutionRuntime(runtime_root=self.root)))
        self.core.context.port_registry['core:core'] = self.core
        register_with_core(self.core.context)
        self.core.publish_module_capabilities('execution')
        self.host = PluginHost(self.core.context, self.root)
        self.host.bootstrap()
        self.host.publish_management_capabilities()
        self.runtime = self.core.context.execution_runtime
        self.assertIs(self.runtime.shell_owner.core, self.core)
        self.installed = self.root / 'plugins/community/remote'
        self.assertIn('remote', self.host.generations)
        self.core.state.active_turns['origin'] = TurnContinuation(
            turn_id='origin', program=iter(()), correlation_id='origin')

    async def asyncTearDown(self):
        await self.runtime.prepare_shutdown_async()
        self.host.shutdown()
        self.assertFalse(self.host.shutdown_errors)
        await self.runtime.shutdown_async()
        self.core.close()
        self.database.close()
        if str(self.installed) in sys.path:
            sys.path.remove(str(self.installed))
        for name in list(sys.modules):
            if name.startswith(('pal_shell_native', 'pal_shell_remote', 'pal_shell_contracts')):
                sys.modules.pop(name)
        sys.modules.update(self.saved_modules)
        self.directory.cleanup()

    async def tool(self, name, args):
        return await self.runtime.execute_tool_async(
            new_tool_call(name=name, args=args), turn_id='origin')

    async def install(self):
        return await self.tool('call_tool', {
            'name': 'install_package', 'args': {'path': str(self.artifact), 'wait_ms': 5000}})

    def package_record(self):
        return json.loads((self.root / 'packages/records/plugin-remote.json').read_text())

    async def test_online_self_update_prepare_and_uninstall_finish_while_tool_waits(self):
        owner = self.runtime.shell_owner
        self.assertFalse(owner.has_work)
        self.assertIsNone(owner._shell)
        for name, args in (
            ('install_package', {'path': str(self.artifact)}),
            ('prepare_package', {'name': 'remote'}),
        ):
            with self.subTest(tool=name):
                previous = self.host.generations['remote']
                owner = self.runtime.shell_owner
                counts = []
                check_idle = owner.check_idle

                def checked():
                    counts.append(owner.active_calls)
                    check_idle()

                with patch.object(owner, 'check_idle', side_effect=checked):
                    result = await self.tool('call_tool', {
                        'name': name, 'args': {**args, 'wait_ms': 5000}})
                self.assertTrue(result.ok, result.text)
                self.assertEqual(self.package_record()['status'], 'ready', result.text)
                self.assertEqual(self.package_record()['activation'], 'attached')
                self.assertIsNot(self.host.generations['remote'], previous)
                self.assertTrue(counts)
                self.assertEqual(set(counts), {0})
                self.assertIn('origin', self.core.state.active_turns)
                self.assertTrue(owner.closed)
                self.assertFalse(self.runtime.shell_owner.closed)

        result = await self.tool('call_tool', {
            'name': 'uninstall_plugin', 'args': {'name': 'remote', 'wait_ms': 5000}})
        self.assertTrue(result.ok, result.text)
        self.assertNotIn('remote', self.host.generations)
        self.assertFalse(self.installed.exists())
        self.assertEqual(self.runtime.execution_diagnostics()['backend'], 'python')
        self.assertIn('origin', self.core.state.active_turns)

    async def test_package_status_wait_allows_background_self_update(self):
        previous = self.host.generations['remote']
        entered, release = threading.Event(), threading.Event()
        publish = PackageService._publish
        status = PackageJobs.status

        def held_publish(service, *args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('package status never released publication')
            return publish(service, *args, **kwargs)

        def waiting_status(jobs, *args, **kwargs):
            release.set()
            return status(jobs, *args, **kwargs)

        try:
            with patch.object(PackageService, '_publish', new=held_publish):
                queued = await self.tool('call_tool', {
                    'name': 'install_package', 'args': {'path': str(self.artifact), 'wait_ms': 0}})
                self.assertTrue(queued.ok, queued.text)
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                job_id = next((self.root / 'packages/jobs').glob('*.json')).stem
                with patch.object(PackageJobs, 'status', new=waiting_status):
                    result = await self.tool('call_tool', {
                        'name': 'inspect_package_status', 'args': {'job_id': job_id, 'wait_ms': 5000}})
                self.assertTrue(result.ok, result.text)
                self.assertEqual(self.package_record()['status'], 'ready', result.text)
                self.assertIsNot(self.host.generations['remote'], previous)
        finally:
            release.set()

    async def test_undelivered_output_still_blocks_online_self_update(self):
        previous = self.host.generations['remote']
        owner = self.runtime.shell_owner
        result = await self.tool('run_shell', {'cmd': 'printf retained'})
        self.assertTrue(result.ok, result.text)
        self.assertEqual(owner.active_calls, 0)
        self.assertTrue(owner.pending)
        pending = dict(owner.pending)
        diagnostic = self.runtime.execution_diagnostics()
        self.assertEqual(diagnostic['pending_outputs'], len(pending))
        self.assertTrue(diagnostic['has_work'])
        result = await self.install()
        self.assertFalse(result.ok, result.text)
        self.assertIn('execution_busy', result.text)
        self.assertIs(self.host.generations['remote'], previous)
        self.assertEqual(owner.pending, pending)
        self.assertFalse(owner.closed)

    async def test_live_shell_session_still_blocks_online_self_update(self):
        previous = self.host.generations['remote']
        owner = self.runtime.shell_owner
        started = await self.tool('run_shell', {'cmd': 'sleep 60', 'wait_ms': 0})
        self.assertTrue(started.ok, started.text)
        sid = started.structured['session_id']
        await self.runtime.acknowledge_tool_result_async(started.call_id, 'origin')
        before = (self.installed / 'pal_shell_native/runtime.py').read_bytes()
        result = await self.install()
        self.assertFalse(result.ok, result.text)
        self.assertIn('execution_busy', result.text)
        self.assertEqual(self.package_record()['stage'], 'publish')
        self.assertEqual(self.package_record()['status'], 'failed')
        self.assertIs(self.host.generations['remote'], previous)
        self.assertIn(sid, owner.sessions)
        self.assertFalse(owner.closed)
        self.assertEqual(owner.active_calls, 0)
        self.assertEqual((self.installed / 'pal_shell_native/runtime.py').read_bytes(), before)

    async def test_foreground_shell_call_still_blocks_online_self_update(self):
        previous = self.host.generations['remote']
        owner = self.runtime.shell_owner
        task = asyncio.create_task(self.tool('run_shell', {'cmd': 'sleep 60'}))
        try:
            async def started():
                while owner._shell is None or not owner._shell._foreground:
                    await asyncio.sleep(0)
            await asyncio.wait_for(started(), 5)
            self.assertEqual(owner.active_calls, 1)
            diagnostic = self.runtime.execution_diagnostics()
            self.assertEqual(diagnostic['active_calls'], 1)
            self.assertEqual(diagnostic['pending_outputs'], 0)
            self.assertTrue(diagnostic['has_work'])
            result = await self.install()
            self.assertFalse(result.ok, result.text)
            self.assertIn('execution_busy', result.text)
            self.assertIs(self.host.generations['remote'], previous)
            self.assertEqual(owner.active_calls, 1)
            self.assertFalse(task.done())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(owner.active_calls, 0)
