"""Native target routing against real independent Runtime/RPC endpoints."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
try:
    from pal_shell_worker.worker import Worker, WorkerConfig
    from pal_shell_remote.hub import RemoteHub
    from pal_shell_remote.slot import Target
except ModuleNotFoundError as exc:
    if exc.name not in {"pal_shell_worker", "_pal_shell_rpc", "pal_shell_remote"}:
        raise
    raise unittest.SkipTest("Remote integration requires the matching pal-shell-native worker build") from exc
from pal_shell_worker.protocol import RemoteError
from pal_shell_native.remote_contract import RemoteFailure
from pal.core import PalCore
from pal_shell_native.runtime import NativeExecutionRuntime
from pal.core.main_context import MainContext
from pal.execution import register_with_core
from pal.shared.tool_protocol import new_tool_call


class DirectPort:
    def __init__(self, hub):
        self.hub = hub
        self.drop_submit = False
        self.drop_release = False

    async def request(self, target, method, params, epoch=None):
        result = await self.call('request', {'target': target, 'method': method, 'params': params, 'runtime_epoch': epoch})
        if method == 'submit' and self.drop_submit:
            self.drop_submit = False
            raise RemoteFailure('transport_lost', 'Injected lost confirmation', effect='unknown')
        if method == 'release' and self.drop_release:
            self.drop_release = False
            raise RemoteFailure('transport_lost', 'Injected lost release confirmation', effect='unknown')
        return result

    async def call(self, method, params):
        reply = await self.hub.call(method, params)
        if 'error' in reply:
            error = reply['error']
            raise RemoteFailure(error['code'], error['message'], effect=error['effect'])
        return reply['result']

    async def list(self, refresh=False, *, target=None):
        return (await self.call('list', {'refresh': refresh, 'target': target}))['targets']


class RemoteRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        key = Ed25519PrivateKey.generate()
        keypath = self.path/'identity'
        keypath.write_text(key.private_bytes_raw().hex())
        keypath.chmod(0o600)
        self.worker = await Worker(WorkerConfig('worker', 'pal', key.public_key().public_bytes_raw().hex(), self.path/'worker.sock')).start()
        self.target = Target(1, 'test', 'worker', 'pal', str(keypath), str(self.path/'worker.sock'))
        self.hub = RemoteHub([self.target])
        self.port = DirectPort(self.hub)
        self.runtime = NativeExecutionRuntime()
        self.core = PalCore(context=MainContext(execution_runtime=self.runtime))
        register_with_core(self.core.context)
        self.core.publish_module_capabilities('execution')
        self.owner = self.runtime.shell_owner
        self.owner.attach_remote(self.port)

    async def asyncTearDown(self):
        await self.runtime.shutdown_async()
        self.core.close()
        await self.hub.close()
        await self.worker.close()
        self.directory.cleanup()

    async def tool(self, tool_name, **args):
        return await self.runtime.execute_tool_async(new_tool_call(name=tool_name, args=args), turn_id='origin')




    async def test_malformed_metadata_never_creates_unknown_execution(self):
        original = self.port.request
        async def malformed(target, method, params, epoch=None):
            if method == 'metadata':
                return {}
            return await original(target, method, params, epoch)
        self.port.request = malformed
        result = await self.tool('run_shell', target=1, cmd='printf must-not-run')
        self.assertFalse(result.ok)
        self.assertIn('not_started', result.llm_text)
        self.assertFalse(self.owner.shell.operations)
        self.assertFalse(self.worker.operations)
        self.assertFalse(self.hub.slots[1].execution.status()['blocked'])

    async def test_unknown_claim_survives_hub_and_worker_replacement_only_blocks_its_target(self):
        slot = self.hub.slots[1]
        original = slot._request
        async def lose(method, params, epoch=None):
            if method == 'submit':
                raise RemoteError('transport_lost', 'unconfirmed submission', effect='unknown')
            return await original(method, params, epoch)
        slot._request = lose
        with self.assertRaises(RemoteFailure):
            await self.owner.shell.run('printf uncertain', target=1, turn_id='origin')
        self.assertEqual(slot.execution.status()['reasons'], ['unknown'])
        config = self.worker.config
        await self.worker.close()
        self.worker = await Worker(config).start()
        self.owner.detach_remote(self.port)
        await self.hub.close()
        self.hub = RemoteHub([self.target])
        self.port = DirectPort(self.hub)
        self.owner.attach_remote(self.port)
        # No status/list call is needed to reinstate the safety claim.
        result = await self.tool('run_shell', target=1, cmd='printf must-not-replay')
        self.assertFalse(result.ok)
        self.assertIn('target_busy', result.llm_text)
        self.assertFalse(self.worker.operations)
        local = await self.tool('run_shell', cmd='printf independent')
        self.assertTrue(local.ok, local.llm_text)

    async def test_slow_output_from_one_target_does_not_block_other_target(self):
        from dataclasses import replace
        from pal_shell_remote.slot import RemoteSlot
        second = await Worker(WorkerConfig('worker2', 'pal', self.worker.config.client_public_key,
                                           self.path/'worker2.sock')).start()
        pending = None
        release = asyncio.Event()
        try:
            self.hub.slots[2] = RemoteSlot(replace(self.target, target=2, worker_id='worker2',
                socket_path=str(self.path/'worker2.sock'), shortcut=''), self.hub.executor)
            shell = self.owner.shell
            raw1 = await shell.run('printf first', target=1, load_output=False)
            raw2 = await shell.run('printf second', target=2, load_output=False)
            entered = asyncio.Event()
            original = self.port.request
            async def slow(target, method, params, epoch=None):
                if target == 1 and method == 'output':
                    entered.set()
                    await release.wait()
                return await original(target, method, params, epoch)
            self.port.request = slow
            pending = asyncio.create_task(shell.materialize(raw1))
            await asyncio.wait_for(entered.wait(), 3)
            loaded = await asyncio.wait_for(shell.materialize(raw2), 3)
            self.assertEqual(loaded['stdout'], 'second')
            release.set()
            await pending
            await shell.release_output(raw1)
            await shell.release_output(raw2)
        finally:
            release.set()
            if pending:
                await asyncio.gather(pending, return_exceptions=True)
            await second.close()

    async def test_cwd_home_expands_at_execution_endpoint(self):
        import os
        from unittest.mock import patch
        for target in (0, 1):
            home = self.path / f'home-{target}'
            (home / 'workspace').mkdir(parents=True)
            with patch.dict(os.environ, {'HOME': str(home)}):
                result = await self.tool('run_shell', target=target, cwd='~/workspace', cmd='pwd')
            self.assertTrue(result.ok, result.llm_text)
            self.assertIn(str(home / 'workspace'), result.llm_text)

    async def test_remote_running_target_does_not_block_local_or_other_slot(self):
        from dataclasses import replace
        from pal_shell_remote.slot import RemoteSlot
        second = await Worker(WorkerConfig('worker2', 'pal', self.worker.config.client_public_key,
                                           self.path/'worker2.sock')).start()
        try:
            self.hub.slots[2] = RemoteSlot(replace(self.target, target=2, worker_id='worker2',
                socket_path=str(self.path/'worker2.sock'), shortcut=''), self.hub.executor)
            started = await self.tool('run_shell', target=1, cmd='sleep 30', wait_ms=0)
            self.assertTrue(started.ok, started.llm_text)
            busy = await self.tool('run_shell', target=1, cmd='printf should-not-run')
            self.assertFalse(busy.ok)
            self.assertIn('target_busy', busy.llm_text)
            for target in (0, 2):
                result = await self.tool('run_shell', target=target, cmd='printf independent')
                self.assertTrue(result.ok, result.llm_text)
            stopped = await self.tool('call_tool', name='shell_session', args={'session_id': started.structured['session_id'], 'action': 'terminate'})
            self.assertTrue(stopped.ok, stopped.llm_text)
        finally:
            await second.close()

    async def test_idle_worker_restart_reconnects_without_replacing_hub(self):
        first = await self.tool('run_shell', target=1, cmd='printf before')
        self.assertTrue(first.ok, first.llm_text)
        old_epoch = self.worker.epoch
        config = self.worker.config
        await self.worker.close()
        self.worker = await Worker(config).start()
        result = await self.tool('run_shell', target=1, cmd='printf after')
        self.assertTrue(result.ok, result.llm_text)
        self.assertNotEqual(old_epoch, self.hub.slots[1].epoch)
        self.assertIs(self.owner.remote_port, self.port)

    async def test_output_download_resumes_validated_prefix_after_failure(self):
        shell = self.owner.shell
        raw = await shell.run('head -c 700000 /dev/zero', target=1,
                              wait_ms=1000, load_output=False, turn_id='origin')
        original = self.port.request
        offsets = []
        fail = True
        async def interrupted(target, method, params, epoch=None):
            nonlocal fail
            if method == 'output' and params['stream'] == 'stdout':
                offsets.append(params['offset'])
                if params['offset'] > 0 and fail:
                    fail = False
                    raise RemoteFailure('transport_lost', 'injected download interruption')
            return await original(target, method, params, epoch)
        self.port.request = interrupted
        with self.assertRaises(RemoteFailure):
            await shell.materialize(raw)
        loaded = await shell.materialize(raw)
        self.assertEqual(loaded['stdout_bytes'], bytes(700000))
        self.assertEqual(offsets.count(0), 1)
        self.assertEqual(offsets[:3], [0, 262144, 262144])
        previous = list(offsets)
        self.assertEqual((await shell.materialize(raw))['stdout_bytes'], bytes(700000))
        self.assertEqual(offsets, previous)
        await shell.release_output(raw)
        self.assertFalse(shell.cache_files)
        self.assertFalse(shell.cache_sizes)

    async def test_old_worker_observation_never_falls_back_to_journalled_read(self):
        response = await self.tool('run_shell', target=1, cmd='sleep 30', wait_ms=0)
        self.assertTrue(response.ok, response.llm_text)
        sid = response.structured['session_id']
        original = self.port.request
        methods = []
        async def old_metadata(target, method, params, epoch=None):
            methods.append(method)
            result = await original(target, method, params, epoch)
            if method == 'metadata':
                result.pop('observation_methods', None)
            return result
        self.port.request = old_metadata
        operations = dict(self.worker.operations)
        for _ in range(20):
            self.assertIsNone(await self.owner.shell.observe(sid))
        self.assertEqual(methods.count('metadata'), 1)
        # The independent event poller may run while observe awaits metadata.
        self.assertLessEqual(set(methods), {'metadata', 'events'})
        self.assertEqual(self.worker.operations, operations)

    async def test_privileged_precommit_failure_does_not_leave_write_busy(self):
        original = self.port.request
        for stage in ('metadata', 'prepare_privileged', 'malformed_metadata'):
            async def fail(target, method, params, epoch=None):
                if stage == 'malformed_metadata' and method == 'metadata':
                    return {}
                if method == stage:
                    raise RemoteFailure('transport_lost', 'lost before commit', effect='unknown')
                return await original(target, method, params, epoch)
            self.port.request = fail
            response = await self.tool('run_shell', target=1, sudo=True, cmd='apt update')
            self.assertFalse(response.ok)
            self.assertFalse(self.owner.shell.operations)
            self.assertFalse(self.owner.shell.operation_context)
            self.assertTrue((await self.tool('run_shell', cmd='printf local')).ok)
        self.port.request = original

    async def test_privileged_commit_loss_is_retained(self):
        from unittest.mock import AsyncMock
        original = self.port.request
        async def prepared(target, method, params, epoch=None):
            if method == 'prepare_privileged':
                return {'approval': {'operation_id': params['operation_id']}}
            return await original(target, method, params, epoch)
        self.port.request = prepared
        self.owner.approvals.request = AsyncMock()
        original_call = self.port.call
        async def lose(method, params):
            if method == 'approve':
                raise RemoteFailure('transport_lost', 'lost commit', effect='unknown')
            return await original_call(method, params)
        self.port.call = lose
        response = await self.tool('run_shell', target=1, sudo=True, cmd='apt update')
        self.assertFalse(response.ok)
        self.assertEqual(len(self.owner.shell.operations), 1)
        self.assertTrue(next(iter(self.owner.shell.operations.values())).epoch)

    async def test_approval_failure_returns_to_model_without_commit_or_write_busy(self):
        from unittest.mock import AsyncMock
        original = self.port.request
        async def prepared(target, method, params, epoch=None):
            if method == 'prepare_privileged':
                return {'approval': {'operation_id': params['operation_id']}}
            return await original(target, method, params, epoch)
        self.port.request = prepared
        self.port.call = AsyncMock(wraps=self.port.call)
        for code, message in [('approval_rejected', 'Approval rejected by user'),
                              ('approval_unavailable', 'Approval could not be completed')]:
            with self.subTest(code=code):
                self.owner.approvals.request = AsyncMock(side_effect=RemoteFailure(code, message))
                response = await self.tool('run_shell', target=1, sudo=True, cmd='apt update')
                self.assertFalse(response.ok)
                self.assertIn(message, response.text)
                self.assertFalse(self.owner.shell.operations)
                self.assertFalse(self.owner.shell.operation_context)
                self.assertFalse(any(call.args[0] == 'approve' for call in self.port.call.call_args_list))
                self.assertTrue((await self.tool('run_shell', cmd='printf local')).ok)

    async def test_target_output_and_local_default(self):
        result = await self.tool('run_shell', cmd='printf remote', target=1)
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.structured['target'], 1)
        self.assertIn('remote', result.text)
        await asyncio.gather(*tuple(self.runtime.shell_owner.observations.acking.values()))
        self.assertFalse(self.worker.outputs)
        self.assertFalse(self.owner.shell.remote_work)
        self.owner.detach_remote(self.port)
        local = await self.tool('run_shell', cmd='printf local')
        self.assertTrue(local.ok, local.text)
        self.assertEqual(local.structured['target'], 0)
        remote = await self.tool('run_shell', cmd='false', target=1)
        self.assertFalse(remote.ok)

    async def test_palpkg_install_discover_reload_and_execute(self):
        import json
        import sys
        from pal.packages.archive import build
        from pal.packages.service import PackageService
        from pal.plugins import PluginHost
        from pal.plugins.models import PluginBundleModel
        from pal.foundation.persistence import PalV2Database
        from pal.shared import RuntimeStatus
        import pal_shell_remote
        saved_modules = {name: module for name, module in sys.modules.items() if name.startswith(('pal_shell_native', 'pal_shell_remote'))}
        source = Path(pal_shell_remote.__file__).resolve().parent.parent
        artifact = build(source, self.path/'dist')
        result = PackageService(self.path).install(artifact)
        self.assertEqual(result['status'], 'ready')
        self.owner.detach_remote(self.port)
        await self.runtime.shutdown_async()
        self.core.close()
        from pal.execution.runtime import ExecutionRuntime
        self.core = PalCore(context=MainContext(execution_runtime=ExecutionRuntime(runtime_root=self.path)))
        register_with_core(self.core.context)
        self.core.publish_module_capabilities('execution')
        self.runtime = self.core.context.execution_runtime
        (self.path/'config').mkdir()
        c = self.target
        (self.path/'config/remote.toml').write_text('[[targets]]\n' + '\n'.join(
            f'{key} = {json.dumps(value)}' for key, value in {
                'target': c.target, 'name': c.name, 'worker_id': c.worker_id,
                'client_id': c.client_id, 'client_key': c.client_key,
                'socket_path': c.socket_path}.items()))
        database = PalV2Database(self.path/'plugins.sqlite3')
        database.initialize([PluginBundleModel])
        host = PluginHost(self.core.context, self.path)
        installed = self.path/'plugins/community/remote'
        try:
            host.bootstrap()
            host.publish_management_capabilities()
            self.assertIn('remote', host.generations)
            plugin = host.generations['remote'].instance
            module = sys.modules[type(plugin).__module__]
            self.assertTrue(Path(module.__file__).is_relative_to(installed))
            self.runtime = self.core.context.execution_runtime
            reply = await self.tool('run_shell', cmd='printf installed-palpkg', target=1)
            self.assertTrue(reply.ok, reply.text)
            self.assertIn('installed-palpkg', reply.text)
            await asyncio.gather(*tuple(self.runtime.shell_owner.observations.acking.values()))
            reloaded = await self.tool('call_tool', name='plugin_attach', args={'name': 'remote'})
            self.assertTrue(reloaded.ok, reloaded.text)
            self.assertIsNot(host.generations['remote'].instance, plugin)
            self.assertIsNotNone(self.worker.server)
            background = await self.tool('run_shell', cmd='sleep .05; printf idle', wait_ms=0)
            self.assertTrue(background.ok, background.text)
            self.assertEqual(host.detach('remote')['status'], RuntimeStatus.ERROR)
            self.assertIn('wait_ms', self.runtime.registry_generation.record_for_alias('run_shell').input_schema['properties'])
            completed = await self.tool('call_tool', name='shell_session', args={'session_id': background.structured['session_id'], 'wait_ms': 5000})
            self.assertTrue(completed.ok, completed.text)
            await asyncio.gather(*tuple(self.runtime.shell_owner.observations.acking.values()))
            disabled = await self.tool('call_tool', name='plugin_disable', args={'name': 'remote'})
            self.assertTrue(disabled.ok, disabled.text)
            self.assertNotIn('wait_ms', self.runtime.registry_generation.record_for_alias('run_shell').input_schema['properties'])
            self.assertNotIn('shell_session', self.runtime.registry_generation.indirect_aliases)
            host.rescan()
            self.assertFalse(host.third_party_repository.get('remote').enabled)
            local = await self.tool('run_shell', cmd='printf local')
            self.assertTrue(local.ok, local.text)
        finally:
            host.shutdown()
            database.close()
            if str(installed) in sys.path:
                sys.path.remove(str(installed))
            for name in list(sys.modules):
                if name.startswith(('pal_shell_native', 'pal_shell_remote')):
                    sys.modules.pop(name)
            sys.modules.update(saved_modules)

    async def test_shutdown_approval_preserves_pending_and_failed_outcomes(self):
        from unittest.mock import AsyncMock, patch
        slot = self.hub.slots[1]
        slot.epoch = self.worker.epoch
        grant = {'client_id': self.target.client_id, 'worker_id': self.target.worker_id,
                 'target': 1, 'runtime_epoch': slot.epoch, 'operation_id': uuid4().hex}
        pending = {'state': 'pending', 'result': None}
        failed = {'state': 'complete', 'result': None, 'error': {'code': 'target_busy'}}
        for outcome in (pending, failed):
            with patch.object(slot, 'request', AsyncMock(side_effect=[pending, outcome])):
                reply = await self.hub.call('approve', {'target': 1, 'action': 'shutdown', 'approval': grant})
            self.assertEqual(reply, {'result': outcome})
            self.assertFalse(slot.expected_offline)

    async def test_lost_submission_is_reconciled_internally_without_replay(self):
        count = self.path/'count'
        self.port.drop_submit = True
        result = await self.tool('run_shell', cmd=f'echo one >> {count}; printf recovered', target=1)
        self.assertTrue(result.ok, result.text)
        self.assertIn('recovered', result.text)
        self.assertNotIn('operation_id', result.text)
        self.assertNotIn('shell_reconcile', result.text)
        self.assertEqual(count.read_text(), 'one\n')

    async def test_local_output_capacity_failure_is_honest_and_never_reexecutes(self):
        from unittest.mock import patch
        count = self.path / 'capacity-count'
        call = new_tool_call(name='run_shell', args={'cmd': f'echo one >> {count}; printf retained', 'target': 1})
        with patch('pal_shell_native.runtime.REMOTE_PENDING_BYTES', 1):
            result = await self.runtime.execute_tool_async(call, turn_id='origin')
        self.assertTrue(result.ok)
        self.assertEqual(result.structured['status'], 'exited')
        self.assertEqual(result.structured['returncode'], 0)
        self.assertIn('output_error', result.structured)
        self.assertIn(call.call_id, self.owner.pending)
        self.assertEqual(count.read_text(), 'one\n')
        self.assertNotIn('shell_recover_output', result.text)
        await asyncio.sleep(0)
        self.assertTrue(self.owner.pending)
        self.assertTrue(self.worker.outputs)
        recovered = await self.runtime.execute_tool_async(new_tool_call(name='call_tool', args={
            'name': 'shell_session', 'args': {'output_ref': call.call_id}}), turn_id='origin')
        self.assertIn('retained', recovered.text)
        self.assertEqual(count.read_text(), 'one\n')
        await asyncio.gather(*tuple(self.owner.observations.acking.values()))
        self.assertFalse(self.owner.pending)
        self.assertFalse(self.worker.outputs)

    async def test_detach_preserves_pty_and_reconnect(self):
        result = await self.tool('run_shell', cmd='read -r x; printf "got:%s" "$x"', target=1, tty=True, wait_ms=0)
        self.assertTrue(result.ok, result.text)
        sid = result.structured['session_id']
        self.assertGreaterEqual(sid, 1 << 48)
        self.owner.detach_remote(self.port)
        await self.hub.close()
        unavailable = await self.tool('call_tool', name='shell_session', args={'session_id': sid})
        self.assertFalse(unavailable.ok)
        self.hub = RemoteHub([self.target])
        self.port = DirectPort(self.hub)
        self.owner.attach_remote(self.port)
        write = await self.tool('call_tool', name='shell_session', args={'session_id': sid, 'action': 'write', 'text': 'hello\n'})
        self.assertTrue(write.ok, write.text)
        read = await self.tool('call_tool', name='shell_session', args={'session_id': sid, 'wait_ms': 5000})
        self.assertTrue(read.ok, read.text)
        self.assertIn('got:hello', read.text)
        await asyncio.gather(*tuple(self.runtime.shell_owner.observations.acking.values()))
        self.assertFalse(self.worker.outputs)

    async def test_targeted_list_refreshes_only_selected_target_and_summary_is_compact(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        self.hub.slots[2] = SimpleNamespace(describe=AsyncMock(side_effect=AssertionError("unselected target probed")))
        try:
            summary = await self.tool('call_tool', name='list_remote', args={'target': 1, 'refresh': True})
            self.assertTrue(summary.ok, summary.text)
            self.assertEqual(summary.structured['view'], 'summary')
            self.assertEqual(len(summary.structured['targets']), 1)
            row = summary.structured['targets'][0]
            self.assertEqual(row['target'], 1)
            self.assertTrue(row['reachable'])
            self.assertNotIn('limits', row['dynamic'])
            self.assertNotIn('memory', row['dynamic'])
            self.assertIn('observed_at', row['dynamic'])
            detail = await self.tool('call_tool', name='list_remote', args={'target': 1, 'view': 'detail'})
            self.assertIn('limits', detail.structured['targets'][0]['dynamic'])
            self.assertLess(len(summary.text), len(detail.text))
            local = await self.tool('call_tool', name='list_remote', args={'target': 0, 'refresh': True})
            self.assertEqual([item['target'] for item in local.structured['targets']], [0])
            unknown = await self.tool('call_tool', name='list_remote', args={'target': 999, 'refresh': True})
            self.assertFalse(unknown.ok)
            self.assertIn('invalid_target', str(unknown))
            self.hub.slots[2].describe.assert_not_awaited()
        finally:
            del self.hub.slots[2]

    async def test_summary_preserves_unknown_offline_observations(self):
        from pal_shell_native.capabilities import _target_summary
        row = _target_summary({'target': 1, 'name': 'offline', 'reachable': False,
                               'probe_error': 'ssh_unavailable', 'dynamic': None,
                               'start_actions': ['wake'], 'execution': {'blocked': False}})
        self.assertIsNone(row['dynamic'])
        self.assertFalse(row['reachable'])
        self.assertEqual(row['probe_error'], 'ssh_unavailable')
        self.assertEqual(row['start_actions'], ['wake'])

    async def test_metadata_and_runtime_restart_fence(self):
        data = await self.tool('call_tool', name='list_remote', args={'refresh': True, 'view': 'detail'})
        self.assertTrue(data.ok, data.text)
        self.assertEqual(data.structured['targets'][1]['dynamic']['shell']['family'], 'bash')
        result = await self.tool('run_shell', cmd='sleep 30', target=1, wait_ms=0)
        sid = result.structured['session_id']
        old_config = self.worker.config
        await self.worker.close()
        self.worker = await Worker(old_config).start()
        await self.hub.slots[1]._disconnect()
        result = await self.tool('call_tool', name='shell_session', args={'session_id': sid})
        self.assertFalse(result.ok)
        self.assertIn(sid, self.owner.shell.tickets)

    async def test_unsupported_management_has_no_execution_effect(self):
        data = await self.tool('call_tool', name='list_remote', args={'refresh': True, 'view': 'detail'})
        management = data.structured['targets'][1]['management']
        self.assertFalse(management['start']['supported'])
        self.assertFalse(management['shutdown']['supported'])
        for name, action in [('remote_start', 'wake'), ('remote_power', 'shutdown')]:
            result = await self.tool('call_tool', name=name, args={'target': 1, 'action': action})
            self.assertFalse(result.ok, result.text)
        self.assertFalse(self.worker.operations)
        self.assertFalse(self.worker.draining)
        self.assertFalse(self.owner.shell.remote_work)

    async def test_plugin_owns_one_real_sidecar_and_detaches_without_killing_worker(self):
        import json
        from types import SimpleNamespace
        from pal_shell_remote.plugin import RemoteHubClient
        self.owner.detach_remote(self.port)
        (self.path/'config').mkdir()
        config = self.path/'config'/'remote.toml'
        c = self.target
        config.write_text('[[targets]]\n' + '\n'.join(f'{key} = {json.dumps(value)}' for key, value in {
            'target': c.target, 'name': c.name, 'worker_id': c.worker_id, 'client_id': c.client_id,
            'client_key': c.client_key, 'socket_path': c.socket_path}.items()))
        plugin = RemoteHubClient(self.path, self.owner)
        try:
            plugin.start()
            result = await self.tool('run_shell', cmd='read x; printf "done:%s" "$x"', target=1, tty=True, wait_ms=0)
            self.assertTrue(result.ok, result.text)
            sid = result.structured['session_id']
            process = plugin.process
            plugin.close()

            self.assertEqual(process.returncode, 0)
            self.core.context.module_registry.unregister('remote')
            self.core.context.port_registry.pop('remote:remote', None)
            self.assertTrue(self.worker.outputs)
            self.assertIn(sid, self.owner.shell.tickets)
            plugin.start()
            result = await self.tool('call_tool', name='shell_session', args={'session_id': sid, 'action': 'write', 'text': 'yes\n'})
            self.assertTrue(result.ok, result.text)
            result = await self.tool('call_tool', name='shell_session', args={'session_id': sid, 'wait_ms': 5000})
            self.assertTrue(result.ok, result.text)
            self.assertIn('done:yes', result.text)
        finally:
            plugin.close()

    async def test_default_plugin_without_targets_keeps_local_available(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from pal_shell_remote.plugin import RemoteHubClient
        self.owner.detach_remote(self.port)
        plugin = RemoteHubClient(self.path, self.owner)
        try:
            with patch('pal_shell_remote.plugin.subprocess.Popen', side_effect=AssertionError('Empty hub must not spawn')):
                plugin.start()
            self.assertIsNone(plugin.process)
            listed = await self.tool('call_tool', name='list_remote', args={'refresh': True})
            self.assertTrue(listed.ok, listed.text)
            self.assertEqual([item['target'] for item in listed.structured['targets']], [0])
            local = await self.tool('run_shell', cmd='printf local-ready')
            self.assertTrue(local.ok, local.text)
            remote = await self.tool('run_shell', cmd='exit 1', target=1)
            self.assertFalse(remote.ok)
            self.assertFalse(self.owner.shell.remote_work)
        finally:
            plugin.close()

    async def test_real_ssh_forwarding_disconnect_and_host_authentication(self):
        import getpass
        import shutil
        import socket
        from dataclasses import replace
        from pal_shell_remote.slot import RemoteSlot
        sshd = shutil.which('sshd')
        if not sshd or not shutil.which('ssh-keygen'):
            self.skipTest('Isolated SSH E2E requires sshd and ssh-keygen')
        for name in ('host', 'ssh-identity'):
            process = await asyncio.create_subprocess_exec('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(self.path/name))
            self.assertEqual(await process.wait(), 0)
        host_key = (self.path/'host.pub').read_text().split()
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        known_hosts = self.path/'known_hosts'
        known_hosts.write_text(f'[127.0.0.1]:{port} {host_key[0]} {host_key[1]}\n')
        authdir = tempfile.TemporaryDirectory(prefix='.pal-ssh-test-', dir=Path.home())
        self.addCleanup(authdir.cleanup)
        authorized = Path(authdir.name)/'authorized_keys'
        authorized.write_text((self.path/'ssh-identity.pub').read_text())
        authorized.chmod(0o600)
        config = self.path/'sshd.conf'
        config.write_text(f"Port {port}\nListenAddress 127.0.0.1\nHostKey {self.path/'host'}\n"
            f"PidFile {self.path/'sshd.pid'}\nAuthorizedKeysFile {authorized}\n"
            "UsePAM no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
            "StrictModes yes\nAllowStreamLocalForwarding yes\n")
        with (self.path/'sshd.log').open('wb') as log:
            daemon = await asyncio.create_subprocess_exec(sshd, '-D', '-e', '-f', str(config), stdout=log, stderr=log)
        slot = RemoteSlot(replace(self.target, ssh_host=getpass.getuser()+'@127.0.0.1', ssh_port=port,
            ssh_identity=str(self.path/'ssh-identity'), known_hosts=str(known_hosts)))
        try:
            await asyncio.sleep(.15)
            if daemon.returncode is not None:
                self.skipTest('Isolated sshd unavailable: '+(self.path/'sshd.log').read_text()[:300])
            try:
                metadata = await slot.request('metadata', {})
            except RemoteError as exc:
                self.fail(str(exc) + ': ' + (self.path/'sshd.log').read_text()[-1500:])
            self.assertEqual(metadata['runtime_epoch'], self.worker.epoch)
            oid = uuid4().hex
            await slot.request('submit', {'operation_id': oid, 'cmd': 'sleep .1; printf ssh-survived', 'wait_ms': 1000})
            await slot._disconnect()
            result = await slot.request('query', {'operation_id': oid, 'wait_ms': 5000}, self.worker.epoch)
            self.assertEqual(result['result']['status'], 'exited')
            await slot._disconnect()
            # Valid public key, wrong server identity: no permissive enrollment.
            other = (self.path/'ssh-identity.pub').read_text().split()
            known_hosts.write_text(f'[127.0.0.1]:{port} {other[0]} {other[1]}\n')
            with self.assertRaises(RemoteError):
                await slot.request('metadata', {})
        finally:
            await slot.close()
            if daemon.returncode is None:
                daemon.terminate()
                await daemon.wait()

    async def test_completion_uses_original_channel_and_retries_only_ack(self):
        import test_production as production
        from pal_shell_native.events import attach_completion_source
        from pal.memory import MemoryService, register_with_core as register_memory
        from pal.core.turns import LLMPreflightEffect, LLMRequestEffect, MailboxReplyEffect, EffectResult
        from pal.llm import LLMPreflightAdvice, generation_result_from_values
        self.memory = MemoryService()
        register_memory(self.core.context, self.memory)
        attach_completion_source(self.core, self.runtime)
        self.core.main_loop.bind_async_loop()
        replies, model_calls = [], []
        original = self.core._execute_turn_effect_async
        async def model(continuation, effect):
            if isinstance(effect, LLMPreflightEffect):
                return EffectResult(status='ok', payload=LLMPreflightAdvice(status='ready'))
            if isinstance(effect, LLMRequestEffect):
                model_calls.append(effect.assembly_context.event.payload)
                return EffectResult(status='ok', payload=generation_result_from_values(text='completed'))
            if isinstance(effect, MailboxReplyEffect):
                replies.append(continuation.delivery_binding)
                return EffectResult(status='queued', text=effect.text)
            return await original(continuation, effect)
        self.core._execute_turn_effect_async = model
        call = new_tool_call(name='run_shell', args={'cmd': 'sleep .1; printf from-remote', 'target': 1, 'wait_ms': 0})
        origin = production.ProductionTests.origin(self, call)
        result = await self.runtime.execute_tool_async(call, turn_id='origin')
        self.assertTrue(result.ok, result.text)
        sid = result.structured['session_id']
        await production.ProductionTests.commit_origin(self, origin, call, result)
        async with asyncio.timeout(5):
            while not self.owner.shell.remote_completions:
                await asyncio.sleep(.02)
        self.port.drop_release = True
        await production.ProductionTests.pump(self)
        self.assertEqual(replies, [origin.delivery_binding])
        self.assertEqual(len(model_calls), 1)
        self.assertIn('from-remote', model_calls[0].text)
        self.assertFalse(self.owner.observations.acking)
        await production.ProductionTests.pump(self)
        self.assertFalse(self.owner.events.failures)
        self.assertEqual(len(model_calls), 1)
        self.assertFalse(self.owner.shell.remote_work)

    async def test_configured_projection_cannot_change_target(self):
        from dataclasses import replace
        import json
        from dataclasses import asdict
        (self.path/'config').mkdir()
        config = asdict(replace(self.target, shortcut='cloud'))
        config.pop('static')
        config.pop('start_actions')
        (self.path/'config/remote.toml').write_text('[[targets]]\n' + '\n'.join(
            f'{key} = {json.dumps(value)}' for key, value in config.items()))
        await self.runtime.shutdown_async()
        self.core.close()
        self.runtime = NativeExecutionRuntime(runtime_root=self.path)
        self.core = PalCore(context=MainContext(execution_runtime=self.runtime))
        register_with_core(self.core.context)
        self.core.publish_module_capabilities('execution')
        self.owner = self.runtime.shell_owner
        self.owner.attach_remote(self.port)
        result = await self.tool('run_shell_cloud', cmd='printf desktop')
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.structured['target'], 1)
        invalid = await self.tool('run_shell_cloud', cmd='printf bypass', target=0)
        self.assertFalse(invalid.ok)

    async def test_reset_does_not_drop_remote_ticket(self):
        result = await self.tool('run_shell', cmd='sleep 30', target=1, wait_ms=0)
        self.assertTrue(result.ok)
        with self.assertRaises(Exception):
            await self.owner.reset()
        self.assertTrue(self.owner.shell.remote_work)

    async def test_explicit_remote_release_recovers_lost_reply_without_model_protocol(self):
        started = await self.tool('run_shell', target=1, cmd='sleep .05; printf retained', wait_ms=0)
        self.assertTrue(started.ok, started.text)
        sid = started.structured['session_id']
        async with asyncio.timeout(5):
            while not self.owner.shell.remote_completions:
                await asyncio.sleep(.02)
        self.port.drop_release = True
        released = await self.tool('call_tool', name='shell_session', args={'session_id':sid, 'action':'release'})
        self.assertTrue(released.ok, released.text)
        self.assertEqual(released.structured['status'], 'released')
        self.assertNotIn(sid, self.owner.sessions)
        await asyncio.gather(*tuple(self.runtime.shell_owner.observations.acking.values()))
        self.assertFalse(self.worker.outputs)
