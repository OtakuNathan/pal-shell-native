"""Independent session actions preserve delivery, control and role boundaries."""
from __future__ import annotations

import asyncio
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from pal.bunshin.scoped_execution import BunshinScopedExecutionRuntime
from pal.core import PalCore
from pal.core.main_context import MainContext
from pal.execution import register_with_core
from pal.shared.tool_protocol import new_tool_call
from pal_shell_native.capabilities import SESSION_ACTIONS, SESSION_CAPABILITIES
from pal_shell_native.runtime import NativeExecutionRuntime


class SessionActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = NativeExecutionRuntime()
        self.core = PalCore(context=MainContext(execution_runtime=self.runtime))
        register_with_core(self.core.context)
        self.core.publish_module_capabilities('execution')

    async def asyncTearDown(self):
        await self.runtime.shutdown_async()
        self.core.close()

    async def tool(self, alias, args, runtime=None):
        runtime = runtime or self.runtime
        call = new_tool_call(name=alias, args=args) if alias == 'run_shell' else new_tool_call(
            name='call_tool', args={'name': alias, 'args': args})
        result = await runtime.execute_tool_async(call, turn_id='actions')
        await runtime.acknowledge_tool_result_async(call.call_id, 'actions')
        return result

    async def test_each_action_exports_one_narrow_strict_contract(self):
        required = {'write': {'text': '\n'}, 'resize': {'rows': 24, 'columns': 80},
                    'watch': {'wait_ms': 100}, 'extend': {'extend_by_ms': 100}}
        for action in SESSION_ACTIONS:
            alias = f'{action}_shell_session'
            record = self.runtime.registry_generation.record_for_alias(alias)
            self.assertEqual(record.canonical_path, f'op_exec_session_{action}')
            self.assertIn(alias, self.runtime.registry_generation.indirect_aliases)
            self.assertEqual(record.execution.retry_policy.value, 'reconcile_first')
            args = {'session_id': 1, **required.get(action, {})}
            schema = Draft202012Validator(record.input_schema)
            self.assertTrue(schema.is_valid(args), (alias, args))
            self.assertIsNotNone(record.input_model.model_validate(args))
            for invalid in ({**args, 'action': action}, {**args, 'irrelevant_control': 1}, {}):
                self.assertFalse(schema.is_valid(invalid), (alias, invalid))
                with self.assertRaises(ValueError):
                    record.input_model.model_validate(invalid)
            if action in {'read', 'release'}:
                self.assertTrue(schema.is_valid({'output_ref': 'retained'}))
                self.assertFalse(schema.is_valid({'output_ref': 'retained', 'session_id': 1}))
                if action == 'read':
                    self.assertFalse(schema.is_valid({'output_ref': 'retained', 'wait_ms': 0}))
        write = self.runtime.registry_generation.record_for_alias('write_shell_session')
        with self.assertRaises(ValueError):
            write.input_model.model_validate({'session_id': 1, 'text': '界' * 22000})

    async def test_pty_control_and_terminal_read_use_original_session(self):
        started = await self.tool('run_shell', {'cmd': 'read value; printf "REPLY:%s" "$value"', 'tty': True, 'wait_ms': 0})
        self.assertTrue(started.ok, started.llm_text)
        sid = started.structured['session_id']
        for alias, args in [('resize_shell_session', {'rows': 30, 'columns': 100}),
                            ('unwatch_shell_session', {}),
                            ('watch_shell_session', {'wait_ms': 1000}),
                            ('write_shell_session', {'text': 'hello\n'})]:
            result = await self.tool(alias, {'session_id': sid, **args})
            self.assertTrue(result.ok, result.llm_text)
        result = await self.tool('read_shell_session', {'session_id': sid, 'wait_ms': 1000})
        self.assertTrue(result.ok, result.llm_text)
        self.assertIn('REPLY:hello', result.llm_text)
        self.assertEqual(result.structured['status'], 'exited')
        # Delivery may already retire a terminal session; never resurrect it.
        await asyncio.gather(*tuple(self.runtime.shell_owner.observations.acking.values()))
        self.assertFalse(self.runtime.shell_owner.completion_blocked)

    async def test_deadline_extension_and_cancellation_are_separate_controls(self):
        result = await self.tool('run_shell', {'cmd': 'sleep 60', 'wait_ms': 0, 'timeout_ms': 5000})
        sid = result.structured['session_id']
        extended = await self.tool('extend_shell_session', {'session_id': sid, 'extend_by_ms': 1000})
        self.assertTrue(extended.ok, extended.llm_text)
        stopped = await self.tool('terminate_shell_session', {'session_id': sid})
        self.assertTrue(stopped.ok, stopped.llm_text)
        read = await self.tool('read_shell_session', {'session_id': sid, 'wait_ms': 1000})
        self.assertTrue(read.ok, read.llm_text)
        self.assertNotEqual(read.structured['status'], 'running')

    async def test_output_ref_recovery_does_not_reexecute_and_release_discards(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / 'executions'
            with patch('pal_shell_native.snapshot_delivery.materialize_delivery', side_effect=OSError('disk unavailable')):
                failed = await self.tool('run_shell', {'cmd': f'printf once >> {shlex.quote(str(marker))}; printf original-output'})
            refs = [p for p in self.runtime.shell_owner.pending if self.runtime.shell_owner.pending[p].failure]
            self.assertEqual(len(refs), 1, failed.llm_text)
            recovered = await self.tool('read_shell_session', {'output_ref': refs[0]})
            self.assertTrue(recovered.ok, recovered.llm_text)
            self.assertIn('original-output', recovered.llm_text)
            self.assertEqual(marker.read_text(), 'once')
            with patch('pal_shell_native.snapshot_delivery.materialize_delivery', side_effect=OSError('disk unavailable')):
                await self.tool('run_shell', {'cmd': 'printf discarded-output'})
            ref = next(p for p in self.runtime.shell_owner.pending if self.runtime.shell_owner.pending[p].failure)
            released = await self.tool('release_shell_session', {'output_ref': ref})
            self.assertTrue(released.ok, released.llm_text)
            self.assertNotIn(ref, self.runtime.shell_owner.pending)

    async def test_role_allowlist_and_other_owner_cannot_gain_session_controls(self):
        without_shell = BunshinScopedExecutionRuntime(self.runtime, ['op_file_write'], {})
        with_shell = BunshinScopedExecutionRuntime(self.runtime, ['op_exec_shell'], {})
        for action in SESSION_ACTIONS:
            alias = f'{action}_shell_session'
            self.assertIsNone(without_shell.registry_generation.record_for_alias(alias))
            self.assertIsNotNone(with_shell.registry_generation.record_for_alias(alias))
        result = await self.tool('run_shell', {'cmd': 'sleep 60', 'wait_ms': 0})
        other = NativeExecutionRuntime()
        core = PalCore(context=MainContext(execution_runtime=other))
        register_with_core(core.context)
        core.publish_module_capabilities('execution')
        try:
            denied = await self.tool('terminate_shell_session', {'session_id': result.structured['session_id']}, other)
            self.assertFalse(denied.ok)
            self.assertIn('invalid_session', denied.llm_text)
            self.assertEqual(set(self.runtime.role_capabilities(['op_exec_shell'])) & SESSION_CAPABILITIES, SESSION_CAPABILITIES)
        finally:
            await other.shutdown_async()
            core.close()
