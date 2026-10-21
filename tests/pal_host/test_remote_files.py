"""Host delivery budgets and worker authority, including transport failures."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from uuid import uuid4

import pal.core
from pal.execution.contracts import CapabilityCall, ToolCallBudget
from pal.execution.session_state import FileDeliveryManifest
from pal.execution.tool_facade import ToolExecutionError, ToolRejectedError
from pal.shared.tool_protocol import new_tool_call
from pal_shell_contracts import Target, resolve_target, RemoteFailure
from pal_shell_native.runtime import NativeExecutionRuntime
from pal_shell_worker.worker import Worker, WorkerConfig
from pal_shell_worker.protocol import RemoteError
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class Port:
    closed = False
    drop = ''
    protocol = 1

    def __init__(self, worker, target):
        self.worker, self.target = worker, target

    async def resolve(self, target):
        return resolve_target([self.target], target)

    async def request(self, target, method, params, epoch=None, *, binding=''):
        route = resolve_target([self.target], target)
        if binding and binding != route['binding']:
            raise RemoteFailure('target_changed', 'Target changed')
        if epoch and epoch != self.worker.epoch:
            raise RemoteFailure('runtime_changed', 'Worker changed')
        if method == 'metadata':
            return {'runtime_epoch': self.worker.epoch, 'worker_id': self.target.worker_id,
                    'file_protocol_version': self.protocol}
        if method == self.drop:
            raise RemoteFailure('transport_lost', 'Injected lost reply', effect='unknown')
        try:
            return await self.worker.handle(method, params)
        except RemoteError as exc:
            raise RemoteFailure(exc.code, str(exc), effect=exc.effect, operation_id=exc.operation_id) from exc


class RemoteFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        key = Ed25519PrivateKey.generate()
        self.worker = Worker(WorkerConfig('surface-worker', 'client', key.public_key().public_bytes_raw().hex(), self.root/'worker.sock'))
        self.runtime = NativeExecutionRuntime(runtime_root=self.root / 'pal')
        self.port = Port(self.worker, Target(5, 'surface', 'surface-worker', 'client', '/key', '/worker.sock', shortcut='tablet'))
        self.runtime.shell_owner.remote_port = self.port
        self.files = self.runtime.shell_owner.files
        self.path = self.root/'file.txt'
        self.path.write_text('first\nsecond\nthird\n')

    async def asyncTearDown(self):
        self.port.drop = ''
        await self.files.close()
        await self.worker.close()
        await self.runtime.shutdown_async()
        self.temp.cleanup()

    async def invoke(self, action, **args):
        call = new_tool_call(name={'read': 'read_file', 'edit': 'edit_file', 'write': 'write_file'}[action],
            args={'target': 'surface', 'file_path': str(self.path), **args})
        result = await self.files.execute(self.runtime, action, CapabilityCall(call.name, call.args,
            {'execution_runtime': self.runtime, 'turn_id': 'turn', 'tool_call': call}))
        return call, result

    async def deliver(self, call, result):
        self.runtime.commit_tool_delivery(turn_id='turn', context_delivery=result.context_delivery, result_id=call.call_id)
        await self.runtime.acknowledge_tool_result_async(call.call_id, 'turn')

    async def test_ack_drop_requires_sync_and_retirement_cannot_resurrect(self):
        call, read = await self.invoke('read', limit=1)
        self.assertEqual(read.structured['target'], 5)
        self.assertIn('surface (5)', read.llm_text)
        self.port.drop = 'file_sync'
        await self.deliver(call, read)
        state = next(iter(self.files.scopes.values()))
        worker_scope = self.worker.files.registry.scopes[state['scope']]
        self.assertFalse(worker_scope.live)
        with self.assertRaises(RemoteFailure):
            await self.invoke('edit', edits=[{'old_string': 'first', 'new_string': 'updated'}])
        self.assertEqual(self.path.read_text(), 'first\nsecond\nthird\n')
        self.port.drop = ''
        _, edited = await self.invoke('edit', edits=[{'old_string': 'first', 'new_string': 'updated'}])
        self.assertEqual(edited.status, 'ok')
        self.runtime.retire_tool_results(turn_id='turn', result_ids=(call.call_id,))
        await self.files.acknowledge(call.call_id)
        with self.assertRaises(RuntimeError):
            self.files.commit(read.context_delivery, call.call_id)

    async def test_discard_before_ack_and_old_worker(self):
        call, read = await self.invoke('read')
        self.runtime.discard_uncommitted_tool_delivery(turn_id='turn', result_ref=call.call_id)
        with self.assertRaises(RuntimeError):
            self.files.commit(read.context_delivery, call.call_id)
        self.port.protocol = 0
        with self.assertRaises(ToolRejectedError) as rejected:
            await self.invoke('read')
        self.assertEqual(rejected.exception.error_code, 'file_protocol_unsupported')

    async def test_lost_write_reply_is_queried_without_replay(self):
        call, read = await self.invoke('read')
        await self.deliver(call, read)
        self.port.drop = 'query'
        with self.assertRaises(ToolExecutionError) as unknown:
            await self.invoke('edit', edits=[{'old_string': 'first', 'new_string': 'first first'}])
        self.assertEqual(unknown.exception.error_code, 'file_operation_unresolved')
        await asyncio.gather(*tuple(self.worker.tasks))
        self.assertEqual(self.path.read_text(), 'first first\nsecond\nthird\n')
        self.port.drop = ''
        with self.assertRaises(ToolExecutionError) as reconciled:
            await self.invoke('edit', edits=[{'old_string': 'first', 'new_string': 'first first', 'replace_all': True}])
        self.assertEqual(reconciled.exception.error_code, 'file_operation_reconciled')
        self.assertEqual(self.path.read_text(), 'first first\nsecond\nthird\n')

    async def test_budgeted_manifest_and_checkpoint_restore(self):
        from pal.execution.tool_facade import CompleteResult, EffectOutcome
        self.path.write_text('A' * 1500 + '\nsecond\nthird\n')
        call, read = await self.invoke('read')
        complete = CompleteResult(output=read.structured, effect=EffectOutcome.NONE,
            llm_text=read.llm_text, context_delivery=read.context_delivery)
        budgeted = self.runtime._budget_invocation_result(complete, call,
            budget=ToolCallBudget(max_output_chars=400), turn_id='turn')
        delivered = replace(read, context_delivery=budgeted.context_delivery)
        await self.deliver(call, delivered)
        snapshot = self.files.snapshot()
        self.files.restore(snapshot)
        with self.assertRaises(ToolRejectedError):
            await self.invoke('edit', edits=[{'old_string': 'A' * 1500, 'new_string': 'hidden'}])
        self.assertTrue(self.path.read_text().startswith('A' * 1500))

    async def test_history_reference_removal_retires_worker_authority(self):
        from pal.memory.turn_ir import L1TurnStore, L1TurnIR
        from pal.llm.ir import LLMMessageIR, MessageRole
        from pal.shared.tool_protocol import ToolResultIR
        call, read = await self.invoke('read')
        result = ToolResultIR(call_id=call.call_id, name='read_file', content=read.llm_text,
                             context_delivery=read.context_delivery)
        turn = L1TurnIR('history', messages=(LLMMessageIR(role=MessageRole.ASSISTANT, parts=(call,)),
                                           LLMMessageIR(role=MessageRole.TOOL, parts=(result,))))
        history = L1TurnStore((turn,))
        self.files.bind_history(history)
        await self.deliver(call, read)
        history.replace_all((L1TurnIR('compact', messages=(),
            metadata={'compact_retained_result_refs': [call.call_id]}),))
        self.assertTrue(next(iter(self.files.scopes.values()))['live'])
        history.clear()
        await self.files.acknowledge(call.call_id)
        state = next(iter(self.files.scopes.values()))
        self.assertFalse(self.worker.files.registry.scopes[state['scope']].live)
        self.assertFalse(self.worker.files.registry.scopes[state['scope']].staged)
        with self.assertRaises(ToolRejectedError):
            await self.invoke('edit', edits=[{'old_string': 'first', 'new_string': 'hidden'}])

    async def test_transient_context_with_same_turn_name_cannot_borrow_authority(self):
        call, read = await self.invoke('read')
        await self.deliver(call, read)
        child = self.runtime.fork_transient()
        args = {'target': 5, 'file_path': str(self.path), 'edits': [{'old_string': 'first', 'new_string': 'hidden'}]}
        child_call = new_tool_call(name='edit_file', args=args)
        with self.assertRaises(ToolRejectedError):
            await self.files.execute(child, 'edit', CapabilityCall(child_call.name, args,
                {'execution_runtime': child, 'turn_id': 'turn', 'tool_call': child_call}))
        self.files.retire_scope(self.files.lifetime_for(child, 'turn'))
        _, edited = await self.invoke('edit', edits=[{'old_string': 'first', 'new_string': 'allowed'}])
        self.assertEqual(edited.status, 'ok')

    async def test_worker_epoch_change_requires_a_fresh_read(self):
        call, read = await self.invoke('read')
        await self.deliver(call, read)
        self.worker.epoch = uuid4().hex
        with self.assertRaises(ToolRejectedError):
            await self.invoke('edit', edits=[{'old_string': 'first', 'new_string': 'hidden'}])
        call, read = await self.invoke('read')
        await self.deliver(call, read)
        _, edited = await self.invoke('edit', edits=[{'old_string': 'first', 'new_string': 'fresh'}])
        self.assertEqual(edited.status, 'ok')

    async def test_model_request_visibility_is_synchronized_before_mutation(self):
        from pal.memory import MemoryService
        call, read = await self.invoke('read')
        await self.deliver(call, read)
        memory = MemoryService()
        continuation = SimpleNamespace(turn_id='turn')
        self.runtime.prepare_model_context(memory, continuation, context_view=SimpleNamespace(turns={}))
        with self.assertRaises(ToolRejectedError):
            await self.invoke('edit', edits=[{'old_string': 'first', 'new_string': 'hidden'}])
        self.assertEqual(self.path.read_text(), 'first\nsecond\nthird\n')
        self.runtime.prepare_model_context(memory, continuation, context_view=SimpleNamespace(
            turns={'turn': SimpleNamespace(tool_results={call.call_id: 'message'})}))
        _, edited = await self.invoke('edit', edits=[{'old_string': 'first', 'new_string': 'visible'}])
        self.assertEqual(edited.status, 'ok')

    async def test_retirement_during_upload_prevents_dispatch(self):
        call, read = await self.invoke('read')
        await self.deliver(call, read)
        upload = self.files._upload
        async def retire_after_upload(state, value):
            ref = await upload(state, value)
            if value.get('action') == 'edit':
                self.files.retire(state['lifetime'], (call.call_id,))
            return ref
        self.files._upload = retire_after_upload
        with self.assertRaises(ToolRejectedError) as rejected:
            await self.invoke('edit', edits=[{'old_string': 'first', 'new_string': 'hidden'}])
        self.assertEqual(rejected.exception.error_code, 'file_context_changed')
        self.assertEqual(self.path.read_text(), 'first\nsecond\nthird\n')


class TargetTests(unittest.TestCase):
    def test_exact_alias_resolution_is_fail_closed(self):
        first = Target(5, 'surface', 'worker', 'client', '/key', '/socket', shortcut='surface')
        self.assertEqual(resolve_target([first], 'surface')['target'], 5)
        self.assertEqual(resolve_target([first], 5)['target'], 5)
        for value in (True, '5', ' Surface ', 'missing', -1, 0):
            with self.subTest(value=value), self.assertRaises(RemoteFailure):
                resolve_target([first], value)
        second = Target(6, 'other', 'worker2', 'client', '/key', '/socket', shortcut='surface')
        with self.assertRaises(RemoteFailure) as ambiguous:
            resolve_target([first, second], 'surface')
        self.assertEqual(ambiguous.exception.code, 'ambiguous_target')


if __name__ == '__main__':
    unittest.main()
