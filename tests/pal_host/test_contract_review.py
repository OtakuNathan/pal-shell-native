"""Model-visible failure recovery and management outcomes."""
from __future__ import annotations

import errno
import asyncio
import json
from pathlib import Path
import shlex
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from jsonschema import Draft202012Validator

from pal.core import PalCore
from pal.core.main_context import MainContext
from pal.execution import register_with_core
from pal.execution.contracts import ToolCallBudget
from pal.execution.tool_facade import EffectOutcome, FailedResult, RetryDirective
from pal.shared import IntrospectionCall
from pal.shared.tool_protocol import new_tool_call
from pal_shell_native.capabilities import NativeExecutionProvider, NativeSessionInput
from pal_shell_native.runtime import NativeExecutionRuntime, native_action
from pal_shell_native.output_contract import output_result
from pal_shell_native.remote_contract import RemoteFailure
from pal_shell_remote.slot import RemoteSlot
from pal_shell_contracts import Target
from pal_shell_worker.protocol import RemoteError


class ContractReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = NativeExecutionRuntime()
        self.core = PalCore(context=MainContext(execution_runtime=self.runtime))
        register_with_core(self.core.context)
        self.core.publish_module_capabilities('execution')

    async def asyncTearDown(self):
        await self.runtime.shutdown_async()
        self.core.close()

    async def tool(self, alias, args):
        return await self.runtime.execute_tool_async(new_tool_call(name=alias, args=args), turn_id='test')

    async def test_nonzero_exit_is_explained_in_description_and_model_result(self):
        description = await self.tool('read_tool', {'name': 'run_shell'})
        self.assertIn('kind=complete means a shell result was returned', description.llm_text)
        self.assertIn('rg/grep exit 1 means no matches', description.llm_text)
        result = await self.tool('run_shell', {'cmd': 'printf command-output; printf cause >&2; exit 7'})
        metadata = json.loads(result.llm_text.rsplit('Tool result metadata: ', 1)[1])
        self.assertEqual(result.structured['returncode'], 7)
        self.assertIn('cause', result.llm_text)
        self.assertEqual(metadata['effect'], 'applied')
        self.assertIn('rg/grep exit 1 means no matches', metadata['recovery'])
        self.assertIn('do not repeat them automatically', metadata['recovery'])

    async def test_paged_nonzero_exit_keeps_recovery_and_full_output(self):
        result = await self.runtime.execute_tool_async(new_tool_call(name='run_shell', args={
            'cmd': "printf '%06000d' 1; printf specific-stderr-tail >&2; exit 7"}),
            turn_id='test', budget=ToolCallBudget(max_output_chars=500, preview_chars=100))
        self.assertTrue(result.snapshot_refs)
        self.assertIn('"returncode":7', result.llm_text)
        self.assertIn('rg/grep exit 1 means no matches', result.llm_text)
        snapshots = '\n'.join(Path(ref.path).read_text() for ref in result.snapshot_refs)
        self.assertIn('specific-stderr-tail', snapshots)

    async def test_launch_failure_has_no_applied_effect_and_retains_cause(self):
        with tempfile.TemporaryDirectory() as root:
            result = await self.tool('run_shell', {'cmd': 'true', 'cwd': str(Path(root) / 'absent')})
        metadata = json.loads(result.llm_text.rsplit('Tool result metadata: ', 1)[1])
        self.assertEqual(result.structured['status'], 'failed')
        self.assertIn('shell spawn', result.llm_text)
        self.assertEqual(metadata['effect'], 'not_started')
        self.assertIn('command did not start', metadata['recovery'])

    async def test_terminal_failures_explain_status_without_erasing_effects(self):
        for args, status, hint in (
            ({'cmd': 'sleep 60', 'timeout_ms': 20, 'wait_ms': 5000}, 'timed_out', 'execution deadline'),
            ({'cmd': 'kill -TERM $$'}, 'exited', 'reported signal'),
        ):
            with self.subTest(status=status):
                result = await self.tool('run_shell', args)
                self.assertEqual(result.structured['status'], status)
                self.assertEqual(result.invocation_result.effect, EffectOutcome.APPLIED)
                self.assertIn(hint, result.llm_text)
                await asyncio.gather(*tuple(self.runtime.shell_owner.observations.acking.values()))
        record = self.runtime.registry_generation.record_for_alias('run_shell')
        for status, hint in (('cancelled', 'does not undo'), ('failed', 'execution failed')):
            with self.subTest(status=status):
                result = self.runtime.deliver_invocation_result(record, new_tool_call(name='run_shell', args={}),
                    output_result({'status': status, 'returncode': -15, 'error': 'specific backend cause',
                                   'stdout': 'partial output'}), budget=None, turn_id='test')
                text = self.runtime._render_invocation_for_llm(result)
                self.assertEqual(result.effect, EffectOutcome.APPLIED)
                self.assertIn('specific backend cause', text)
                self.assertIn('partial output', text)
                self.assertIn(hint, text)

    async def test_output_save_failure_preserves_full_cause_chain_for_model(self):
        try:
            raise PermissionError('underlying output-store cause')
        except PermissionError as cause:
            error = ValueError('save failed ' + 'x' * 5000 + ' diagnostic-tail')
            error.__cause__ = cause
        with patch('pal_shell_native.snapshot_delivery.materialize_delivery', side_effect=error):
            result = await self.tool('run_shell', {'cmd': 'printf retained'})
        self.assertIn('underlying output-store cause', result.llm_text)
        self.assertIn('diagnostic-tail', result.llm_text)
        action = result.invocation_result.affordances[0]
        recovered = await self.tool(action.tool, dict(action.arguments))
        self.assertIn('retained', recovered.llm_text)

    async def test_unconfirmed_execution_reports_reconciliation_failure_too(self):
        owner = self.runtime.shell_owner
        unknown = RemoteFailure('transport_lost', 'submission confirmation lost', effect='unknown', operation_id='op')
        with patch.object(owner.shell, 'run', side_effect=unknown), patch.object(
                owner.shell, 'reconcile', side_effect=ValueError('specific journal query failure')):
            result = await self.tool('run_shell', {'cmd': 'true'})
        self.assertFalse(result.ok)
        self.assertEqual(result.invocation_result.effect, EffectOutcome.UNKNOWN)
        self.assertIn('submission confirmation lost', result.llm_text)
        self.assertIn('specific journal query failure', result.llm_text)
        self.assertIn('Do not repeat it automatically', result.llm_text)

    async def test_busy_recovery_keeps_status_entry_for_tracked_sessions(self):
        started = await self.tool('run_shell', {'cmd': 'sleep 60', 'wait_ms': 0})
        self.assertTrue(started.ok, started.llm_text)
        rejected = await self.tool('run_shell', {'cmd': 'true'})
        self.assertFalse(rejected.ok)
        actions = rejected.invocation_result.affordances
        self.assertTrue(any(a.arguments.get('name') == 'inspect_shell_status' for a in actions), rejected.llm_text)
        self.assertIn('last observed', rejected.llm_text)
        stopped = await self.tool('call_tool', {'name': 'manage_shell_session', 'args': {'session_id': started.structured['session_id'], 'action': 'terminate'}})
        self.assertTrue(stopped.ok, stopped.llm_text)

    async def test_failed_normalization_preserves_cause_and_reexports_without_reexecution(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / 'executions'
            original = self.runtime._normalize_invocation_result
            def fail(record, call, raw, **kwargs):
                if native_action(record):
                    return FailedResult(error_code='output_validation_failed', error='number requires integer',
                        llm_text='Validation error: number requires integer', effect=EffectOutcome.APPLIED,
                        retry=RetryDirective.RECONCILE_FIRST)
                return original(record, call, raw, **kwargs)
            with patch.object(self.runtime, '_normalize_invocation_result', side_effect=fail):
                result = await self.tool('run_shell', {'cmd': f'printf once >> {shlex.quote(str(marker))}; printf retained-evidence'})
            self.assertFalse(result.ok)
            self.assertIn('number requires integer', result.llm_text)
            self.assertIn('Do not rerun', result.llm_text)
            self.assertIn('output_ref', result.llm_text)
            actions = result.invocation_result.affordances
            action = next(a for a in actions if a.arguments.get('name') == 'read_shell_session')
            recovered = await self.tool(action.tool, dict(action.arguments))
            self.assertTrue(recovered.ok, recovered.llm_text)
            self.assertIn('retained-evidence', recovered.llm_text)
            self.assertEqual(marker.read_text(), 'once')

    async def test_start_and_shutdown_do_not_claim_completed_readiness(self):
        provider = NativeExecutionProvider(runtime=self.runtime)
        port = SimpleNamespace(call=AsyncMock(return_value={'returncode': 7, 'status': 'start_action_completed', 'target': 1}))
        shell = SimpleNamespace(_port=lambda: port, privileged=AsyncMock(return_value={'status': 'unknown'}))
        execution = SimpleNamespace(shell_owner=SimpleNamespace(shell=shell))
        call = IntrospectionCall(name='op_exec_remote_start', args={'target': 1, 'action': 'wake'}, meta={'execution_runtime': execution})
        result = await provider.remote_start_async(call)
        self.assertNotEqual(result.status, 'ok')
        self.assertEqual(result.structured['readiness'], 'unverified')
        self.assertEqual(result.affordances[0].arguments['args'], {'target': 1, 'refresh': True})
        result = await provider.remote_power_async(call)
        self.assertNotEqual(result.status, 'ok')
        self.assertFalse(result.structured['shutdown_confirmed'])
        shell.privileged.return_value = {'status': 'accepted'}
        result = await provider.remote_power_async(call)
        self.assertEqual(result.status, 'ok')
        self.assertFalse(result.structured['shutdown_confirmed'])


class SessionContractReviewTests(unittest.TestCase):
    def test_action_matrix_and_output_reference_rules(self):
        accepted = [
            {'session_id': 1, 'action': 'read', 'wait_ms': 0},
            {'session_id': 1, 'action': 'write', 'text': ''},
            {'session_id': 1, 'action': 'resize', 'rows': 30, 'columns': 80},
            {'session_id': 1, 'action': 'watch', 'wait_ms': 1, 'extend_by_ms': 0},
            {'session_id': 1, 'action': 'extend', 'extend_by_ms': 1},
            {'output_ref': 'saved', 'action': 'read'}, {'output_ref': 'saved', 'action': 'release'},
        ]
        schema = NativeSessionInput.model_json_schema()
        for args in accepted:
            self.assertTrue(Draft202012Validator(schema).is_valid(args), args)
            self.assertIsNotNone(NativeSessionInput.model_validate(args))
        rejected = [
            {'session_id': 1, 'action': 'write', 'text': '', 'wait_ms': 0},
            {'session_id': 1, 'action': 'read', 'extend_by_ms': 1},
            {'session_id': 1, 'action': 'resize', 'rows': 30},
            {'session_id': 1, 'action': 'watch', 'wait_ms': 0},
            {'session_id': 1, 'action': 'extend', 'extend_by_ms': 0},
            {'session_id': 1, 'output_ref': 'saved'}, {'output_ref': 'saved', 'wait_ms': 0}, {},
        ]
        for args in rejected:
            self.assertFalse(Draft202012Validator(schema).is_valid(args), args)
            with self.subTest(args=args), self.assertRaises(ValueError):
                NativeSessionInput.model_validate(args)


class RemoteDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def config(self, **kwargs):
        return Target(target=1, name='test', socket_path='/missing/socket', client_id='client', client_key='/missing/key', worker_id='worker', **kwargs)

    async def test_start_stderr_is_bounded_and_unknown_readiness_is_explicit(self):
        import sys
        slot = RemoteSlot(self.config(start_actions={'wake': [sys.executable, '-c', "import sys; sys.stderr.write('x'*20000+'specific-start-failure'); sys.exit(7)"]}))
        try:
            result = await slot.start('wake')
            self.assertEqual(result['returncode'], 7)
            self.assertEqual(result['readiness'], 'unverified')
            self.assertIn('specific-start-failure', result['stderr'])
            self.assertLess(len(result['stderr']), 2100)
        finally:
            await slot.close()

    async def test_connection_setup_retains_os_failure_reason(self):
        slot = RemoteSlot(self.config())
        try:
            with patch.object(slot, '_connect', side_effect=PermissionError(errno.EACCES, 'socket permission denied')):
                with self.assertRaises(RemoteError) as caught:
                    await slot.request('metadata', {})
            self.assertIn('socket permission denied', str(caught.exception))
        finally:
            await slot.close()

    async def test_start_does_not_wait_for_daemon_inheriting_stderr(self):
        import asyncio
        import os
        import signal
        import sys
        with tempfile.TemporaryDirectory() as root:
            pid_file = Path(root) / 'child.pid'
            script = (
                "import os,sys,time; from pathlib import Path; child=os.fork(); "
                f"Path({str(pid_file)!r}).write_text(str(child)) if child else None; "
                "sys.stderr.write('wake completed\\n') if child else None; "
                "sys.exit(0) if child else time.sleep(60)"
            )
            slot = RemoteSlot(self.config(start_actions={'wake': [sys.executable, '-c', script]}))
            try:
                result = await asyncio.wait_for(slot.start('wake'), 2)
                self.assertEqual(result['returncode'], 0)
                self.assertIn('wake completed', result['stderr'])
                self.assertEqual(result['readiness'], 'unverified')
            finally:
                if pid_file.exists():
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                await slot.close()

    async def test_ssh_failure_and_disconnect_do_not_wait_for_inherited_stderr(self):
        import asyncio
        import os
        import signal
        import sys
        original_spawn = asyncio.create_subprocess_exec
        with tempfile.TemporaryDirectory() as root:
            pid_file = Path(root) / 'ssh-child.pid'
            script = (
                "import os,sys,time; from pathlib import Path; child=os.fork(); "
                f"Path({str(pid_file)!r}).write_text(str(child)) if child else None; "
                "sys.stderr.write('specific SSH authentication failure\\n') if child else None; "
                "sys.exit(7) if child else time.sleep(60)"
            )
            async def fake_ssh(*args, **kwargs):
                return await original_spawn(sys.executable, '-c', script, **kwargs)
            slot = RemoteSlot(self.config(ssh_host='example.test', ssh_identity='/identity', known_hosts='/hosts'))
            try:
                with patch('asyncio.create_subprocess_exec', side_effect=fake_ssh):
                    with self.assertRaises(RemoteError) as caught:
                        await asyncio.wait_for(slot._connect(), 2)
                self.assertIn('specific SSH authentication failure', str(caught.exception))
                await asyncio.wait_for(slot.close(), 2)
                self.assertIsNone(slot.tunnel)
                self.assertIsNone(slot.tunnel_diagnostics)
            finally:
                if pid_file.exists():
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                await slot.close()

    async def test_live_ssh_disconnect_reaps_parent_without_waiting_for_child_stderr(self):
        import asyncio
        import os
        import signal
        import sys
        with tempfile.TemporaryDirectory() as root:
            pid_file = Path(root) / 'ssh-child.pid'
            script = (
                "import os,time; from pathlib import Path; child=os.fork(); "
                f"Path({str(pid_file)!r}).write_text(str(child)) if child else None; "
                "time.sleep(60)"
            )
            slot = RemoteSlot(self.config())
            slot.tunnel, slot.tunnel_stderr_transport, slot.tunnel_diagnostics = await slot._spawn_with_stderr(
                [sys.executable, '-c', script], slot.tunnel_stderr)
            parent = slot.tunnel
            try:
                async with asyncio.timeout(2):
                    while not pid_file.exists():
                        await asyncio.sleep(.01)
                await asyncio.wait_for(slot.close(), 2)
                self.assertIsNotNone(parent.returncode)
                self.assertIsNone(slot.tunnel)
                self.assertIsNone(slot.tunnel_diagnostics)
            finally:
                if pid_file.exists():
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                await slot.close()
