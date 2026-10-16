"""Slot queue acceptance through real RPC, without SSH or privileged effects."""
import asyncio
from pathlib import Path
import tempfile
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pal_shell_remote.admission import RequestAdmission
from pal_shell_remote.hub import RemoteHub
from pal_shell_remote.slot import Target
from pal_shell_worker.protocol import RemoteError
from pal_shell_worker.worker import Worker, WorkerConfig


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiters_are_admitted_in_fifo_order(self):
        gate = RequestAdmission(capacity=1)
        await gate.acquire()
        order = []
        async def wait(number):
            async with gate.permit():
                order.append(number)
                await asyncio.sleep(0)
        waiters = [asyncio.create_task(wait(number)) for number in range(8)]
        await asyncio.sleep(0)
        self.assertEqual(order, [])
        gate.release()
        await asyncio.gather(*waiters)
        self.assertEqual(order, list(range(8)))
        self.assertEqual(gate.active, 0)

    async def test_cancel_after_grant_returns_permit(self):
        gate = RequestAdmission(capacity=1)
        await gate.acquire()
        waiter = asyncio.create_task(gate.acquire())
        await asyncio.sleep(0)
        gate.release()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(gate.active, 0)
        self.assertFalse(gate.waiters)
        await gate.acquire()
        gate.release()


class SlotQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        key = Ed25519PrivateKey.generate()
        identity = root/'key'
        identity.write_text(key.private_bytes_raw().hex())
        identity.chmod(0o600)
        self.worker = await Worker(WorkerConfig('worker','pal',
            key.public_key().public_bytes_raw().hex(),root/'worker.sock')).start()
        self.hub = RemoteHub([Target(1,'test','worker','pal',str(identity),str(root/'worker.sock'))])
        self.slot = self.hub.slots[1]
        self.tasks = []
        self.started = []
        self.releases = [asyncio.Event() for _ in range(32)]
        original = self.worker.handle
        async def handle(method, args):
            if method == 'hold':
                self.started.append(args['number'])
                await self.releases[args['number']].wait()
                return {'number':args['number']}
            return await original(method, args)
        self.worker.handle = handle

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.hub.close()
        await self.worker.close()
        self.directory.cleanup()

    def task(self, method, args):
        task = asyncio.create_task(self.slot.request(method, args))
        self.tasks.append(task)
        return task

    async def saturated(self):
        for number in range(32):
            self.task('hold', {'number':number})
        async with asyncio.timeout(3):
            while len(self.started) != 32:
                await asyncio.sleep(.001)

    async def queued(self, count=1):
        async with asyncio.timeout(3):
            while len(self.slot.admission.waiters) != count:
                await asyncio.sleep(.001)

    async def test_pending_submit_receipt_is_not_a_permanent_submitting_claim(self):
        response = await self.slot.request('submit', {'operation_id': 'pending', 'cmd': 'sleep 30',
                                                    'wait_ms': 0, '_hold_output': True})
        self.assertEqual(response['state'], 'pending')
        self.assertEqual(self.slot.execution.operations['pending']['state'], 'pending')
        result = await self.slot.request('query', {'operation_id': 'pending', 'wait_ms': 5000})
        self.assertEqual(self.slot.execution.operations['pending']['session_id'], result['result']['session_id'])
        self.assertEqual(self.slot.execution.operations['pending']['state'], 'running')

    async def test_failed_duplicate_submit_cannot_erase_original_delivery(self):
        counter = Path(self.directory.name) / 'duplicate-count'
        args = {'operation_id': 'original', 'cmd': f'echo once >> "{counter}"; printf retained',
                'wait_ms': 5000, '_hold_output': True}
        await self.slot.request('submit', args)
        await self.slot.request('query', {'operation_id': 'original', 'wait_ms': 5000})
        record = dict(self.slot.execution.operations['original'])
        original = self.slot._request
        async def reject(method, params, epoch=None):
            if method == 'submit':
                raise RemoteError('transport_capacity', 'Duplicate request was not sent')
            return await original(method, params, epoch)
        self.slot._request = reject
        with self.assertRaises(RemoteError):
            await self.slot.request('submit', args)
        self.assertEqual(self.slot.execution.operations.get('original'), record)
        self.assertEqual(counter.read_text(), 'once\n')
        await self.slot.request('release', {'output_id': record['output_id']}, self.slot.epoch)

    async def test_first_connection_can_associate_event_before_submit_receipt_returns(self):
        original = self.slot._request
        entered, proceed = asyncio.Event(), asyncio.Event()
        async def hold_receipt(method, params, epoch=None):
            result = await original(method, params, epoch)
            if method == 'submit':
                entered.set()
                await proceed.wait()
            return result
        self.slot._request = hold_receipt
        submit = self.task('submit', {'operation_id': 'first', 'cmd': 'printf done',
                                     'wait_ms': 0, '_hold_output': True})
        try:
            await asyncio.wait_for(entered.wait(), 3)
            async with asyncio.timeout(5):
                while not self.worker.events:
                    await asyncio.sleep(.001)
            events = await self.slot.request('events', {'after': 0, 'wait_ms': 0})
            self.assertEqual(len(events['events']), 1)
            self.assertEqual(self.slot.execution.operations['first']['state'], 'delivery')
            output_id = self.slot.execution.operations['first']['output_id']
            await self.slot.request('release', {'output_id': output_id}, self.slot.epoch)
        finally:
            proceed.set()
            await asyncio.wait_for(submit, 3)
        self.assertNotIn('first', self.slot.execution.operations)

    async def test_concurrent_duplicate_cannot_dispatch_behind_a_rejected_first_attempt(self):
        original = self.slot._request
        entered, proceed = asyncio.Event(), asyncio.Event()
        async def reject_first(method, params, epoch=None):
            if method == 'submit':
                entered.set()
                await proceed.wait()
                raise RemoteError('connection_unavailable', 'First attempt was not sent')
            return await original(method, params, epoch)
        self.slot._request = reject_first
        args = {'operation_id': 'same', 'cmd': 'printf once', 'wait_ms': 0}
        first = self.task('submit', args)
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with self.assertRaises(RemoteError) as caught:
                await asyncio.wait_for(self.slot.request('submit', args), .5)
            self.assertEqual(caught.exception.code, 'target_busy')
            self.assertEqual(caught.exception.effect, 'unknown')
        finally:
            proceed.set()
            await asyncio.gather(first, return_exceptions=True)
        self.assertFalse(self.worker.operations)
        self.assertFalse(self.slot.execution.operations)
        self.assertFalse(self.slot.operation_requests)

    async def test_colliding_control_id_cannot_overwrite_another_epochs_evidence(self):
        self.slot.execution.restore([dict(operation_id='old-control', epoch='old-epoch',
            state='unknown', session_id=123, output_id='old-output', control=True)])
        record = dict(self.slot.execution.operations['old-control'])
        await self.slot.request('metadata', {})
        with self.assertRaises(RemoteError):
            await self.slot.request('session', {'operation_id': 'old-control',
                'session_id': 123, 'action': 'terminate'})
        self.assertEqual(self.slot.execution.operations.get('old-control'), record)

    async def test_query_of_new_worker_approval_cannot_retire_old_epoch_operation(self):
        await self.slot.request('metadata', {})
        self.slot.execution.restore([dict(operation_id='old', epoch='old-epoch',
            state='unknown', session_id=0, output_id='')])
        record = dict(self.slot.execution.operations['old'])
        async def prepared(method, params, epoch=None):
            return {'state': 'approval_required', 'result': None, 'error': None}
        self.slot._request = prepared
        await self.slot.request('query', {'operation_id': 'old'})
        self.assertEqual(self.slot.execution.operations.get('old'), record)

    async def test_repeated_prepare_cannot_make_executed_operation_unsigned(self):
        await self.slot.request('metadata', {})
        self.slot.execution.restore([dict(operation_id='signed', epoch=self.slot.epoch,
            state='delivery', session_id=0, output_id='retained', hold_output=True, unsigned=False)])
        async def prepared(method, params, epoch=None):
            return {'state': 'complete', 'result': {'session_id': 0, 'status': 'exited',
                'output_id': 'retained'}, 'error': None}
        self.slot._request = prepared
        await self.slot.request('prepare_privileged', {'operation_id': 'signed'})
        self.slot.execution.cancel_prepared('signed')
        self.assertIn('signed', self.slot.execution.operations)
        self.assertFalse(self.slot.execution.operations['signed']['unsigned'])

    @unittest.skipUnless(__import__('sys').platform.startswith('linux'), 'Linux signed management fixture')
    async def test_real_signed_commit_and_late_approval_keep_uncertain_effect_owned(self):
        from dataclasses import replace
        from unittest.mock import AsyncMock, patch
        self.worker.config = replace(self.worker.config, management_helper='/usr/bin/true',
                                     management_actions=('shell_command',))
        proceed = asyncio.Event()
        async def uncertain_execution(worker, operation):
            await proceed.wait()
            worker._finish(operation, error=RemoteError(
                'execution_unknown', 'Injected uncertain effect', effect='unknown'))
        original = self.slot._request
        async def lose_commit(method, params, epoch=None):
            result = await original(method, params, epoch)
            if method == 'commit_privileged':
                raise RemoteError('transport_lost', 'Lost signed commit receipt', effect='unknown')
            return result
        with patch('pal_shell_worker.privilege.management_status', AsyncMock(
                return_value={'ok': True, 'allowed_actions': ['shell_command']})), patch(
                'pal_shell_worker.privilege.execute_privileged', uncertain_execution):
            try:
                prepared = await self.slot.request('prepare_privileged', {
                    'operation_id': 'signed', 'action': 'sudo', 'target': 1,
                    'cmd': 'printf must-not-execute', 'wait_ms': 0, '_hold_output': True})
                self.slot._request = lose_commit
                reply = await self.hub.call('approve', {'target': 1, 'action': 'sudo',
                                                       'approval': prepared['approval']})
                self.assertEqual(reply['error']['effect'], 'unknown')
                async def delayed_approval(method, params, epoch=None):
                    return prepared if method == 'query' else await original(method, params, epoch)
                self.slot._request = delayed_approval
                await self.slot.request('query', {'operation_id': 'signed'})
                self.slot.execution.cancel_prepared('signed')
                record = self.slot.execution.operations['signed']
                self.assertFalse(record['unsigned'])
                self.assertEqual(record['state'], 'unknown')
                self.slot._request = original
                proceed.set()
                await self.slot.request('query', {'operation_id': 'signed', 'wait_ms': 5000})
                self.assertEqual(self.slot.execution.operations['signed']['state'], 'unknown')
                self.assertFalse(self.worker.native_outputs)
            finally:
                proceed.set()

    async def test_failed_query_cannot_apply_its_not_started_effect_to_original_submit(self):
        await self.slot.request('submit', {'operation_id': 'pending', 'cmd': 'sleep 30', 'wait_ms': 0})
        original = self.slot._request
        async def fail_query(method, params, epoch=None):
            if method == 'query':
                raise RemoteError('connection_unavailable', 'Query never reached worker', effect='not_started')
            return await original(method, params, epoch)
        self.slot._request = fail_query
        with self.assertRaises(RemoteError):
            await self.slot.request('query', {'operation_id': 'pending', 'wait_ms': 5000})
        self.assertEqual(self.slot.execution.operations['pending']['state'], 'unknown')
        self.assertIn('pending', self.worker.operations)
        self.slot._request = original
        result = await self.slot.request('query', {'operation_id': 'pending', 'wait_ms': 5000})
        self.assertEqual(self.slot.execution.operations['pending']['state'], 'running')
        self.assertEqual(self.slot.execution.operations['pending']['session_id'], result['result']['session_id'])

    async def test_cancelled_query_preserves_original_operation_as_unknown(self):
        await self.slot.request('submit', {'operation_id': 'pending', 'cmd': 'sleep 30', 'wait_ms': 0})
        original = self.slot._request
        entered = asyncio.Event()
        async def hold_query(method, params, epoch=None):
            if method == 'query':
                entered.set()
                await asyncio.Event().wait()
            return await original(method, params, epoch)
        self.slot._request = hold_query
        query = self.task('query', {'operation_id': 'pending', 'wait_ms': 5000})
        await asyncio.wait_for(entered.wait(), 3)
        query.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await query
        self.assertEqual(self.slot.execution.operations['pending']['state'], 'unknown')
        self.assertIn('pending', self.worker.operations)

    async def test_completion_event_binds_lost_submit_and_output_release_retires_it(self):
        counter = Path(self.directory.name) / 'executions'
        original = self.slot._request
        async def lose_submit(method, params, epoch=None):
            result = await original(method, params, epoch)
            if method == 'submit':
                raise RemoteError('transport_lost', 'Lost the submit receipt', effect='unknown')
            return result
        self.slot._request = lose_submit
        with self.assertRaises(RemoteError):
            await self.slot.request('submit', {'operation_id': 'lost',
                'cmd': f'echo once >> "{counter}"; sleep .1; printf completed', 'wait_ms': 0, '_hold_output': True})
        self.assertEqual(self.slot.execution.operations['lost']['session_id'], 0)
        self.assertEqual(self.slot.execution.operations['lost']['state'], 'unknown')
        cursor = 0
        async with asyncio.timeout(5):
            while self.slot.execution.operations['lost']['state'] != 'delivery':
                response = await self.slot.request('events', {'after': cursor, 'wait_ms': 1000})
                cursor = max([cursor, *(e['cursor'] for e in response['events'])])
        record = self.slot.execution.operations['lost']
        self.assertGreater(record['session_id'], 0)
        self.assertTrue(record['output_id'])
        self.assertEqual(counter.read_text(), 'once\n')
        await self.slot.request('release', {'output_id': record['output_id']}, self.slot.epoch)
        self.assertNotIn('lost', self.slot.execution.operations)
        self.assertEqual(counter.read_text(), 'once\n')

    async def test_unknown_session_control_does_not_block_another_session(self):
        sessions = []
        for oid in ('first', 'second'):
            await self.slot.request('submit', {'operation_id': oid, 'cmd': 'sleep 30', 'wait_ms': 0})
            result = await self.slot.request('query', {'operation_id': oid, 'wait_ms': 5000})
            self.assertIsNone(result['error'], result)
            sessions.append(result['result']['session_id'])
        self.slot.execution.restore([dict(operation_id='uncertain-control', epoch=self.slot.epoch,
            state='unknown', session_id=sessions[0], output_id='', control=True),
            dict(operation_id='old-control', epoch='previous-runtime', state='unknown',
                session_id=sessions[1], output_id='', control=True)])
        with self.assertRaises(RemoteError) as error:
            await self.slot.request('session', {'operation_id': 'same-session',
                'session_id': sessions[0], 'action': 'unwatch'})
        self.assertEqual(error.exception.code, 'target_busy')
        self.assertNotIn('same-session', self.worker.operations)
        await self.slot.request('session', {'operation_id': 'other-session',
            'session_id': sessions[1], 'action': 'unwatch'})
        result = await self.slot.request('query', {'operation_id': 'other-session', 'wait_ms': 5000})
        self.assertIsNone(result['error'], result)
        self.assertEqual(result['result']['status'], 'running')
        self.assertFalse(result['result']['watching'])
        self.assertEqual(self.slot.execution.operations['uncertain-control']['state'], 'unknown')
        self.assertEqual(self.slot.execution.operations['old-control']['state'], 'unknown')

    async def test_inflight_session_control_excludes_only_its_own_session(self):
        sessions = []
        for oid in ('first', 'second'):
            await self.slot.request('submit', {'operation_id': oid, 'cmd': 'sleep 30', 'wait_ms': 0})
            result = await self.slot.request('query', {'operation_id': oid, 'wait_ms': 5000})
            self.assertIsNone(result['error'], result)
            sessions.append(result['result']['session_id'])
        entered, proceed = asyncio.Event(), asyncio.Event()
        original = self.slot._request
        async def hold(method, params, epoch=None):
            if params.get('operation_id') == 'held-control':
                entered.set()
                await proceed.wait()
            return await original(method, params, epoch)
        self.slot._request = hold
        task = self.task('session', {'operation_id': 'held-control',
            'session_id': sessions[0], 'action': 'unwatch'})
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with self.assertRaises(RemoteError) as error:
                await self.slot.request('session', {'operation_id': 'conflicting-control',
                    'session_id': sessions[0], 'action': 'unwatch'}, epoch=self.slot.epoch)
            self.assertEqual(error.exception.code, 'target_busy')
            self.assertNotIn('conflicting-control', self.worker.operations)
            await self.slot.request('session', {'operation_id': 'independent-control',
                'session_id': sessions[1], 'action': 'unwatch'})
            result = await self.slot.request('query', {'operation_id': 'independent-control', 'wait_ms': 5000})
            self.assertIsNone(result['error'], result)
            self.assertFalse(result['result']['watching'])
        finally:
            proceed.set()
            await asyncio.wait_for(task, 3)
        self.assertFalse(self.slot.session_locks)

    async def test_pending_control_keeps_its_session_exclusive_until_reconciled(self):
        await self.slot.request('submit', {'operation_id': 'shell', 'cmd': 'sleep 30', 'wait_ms': 0})
        result = await self.slot.request('query', {'operation_id': 'shell', 'wait_ms': 5000})
        sid = result['result']['session_id']
        proceed = asyncio.Event()
        original = self.worker._execute
        async def hold(op):
            if op.operation_id == 'held-control':
                await proceed.wait()
            await original(op)
        self.worker._execute = hold
        try:
            response = await self.slot.request('session', {'operation_id': 'held-control',
                'session_id': sid, 'action': 'unwatch'})
            self.assertEqual(response['state'], 'pending')
            self.assertFalse(self.slot.session_locks)
            with self.assertRaises(RemoteError) as error:
                await self.slot.request('session', {'operation_id': 'conflicting-control',
                    'session_id': sid, 'action': 'unwatch'})
            self.assertEqual(error.exception.code, 'target_busy')
            self.assertNotIn('conflicting-control', self.worker.operations)
            await self.slot.request('session', {'operation_id': 'read-control',
                'session_id': sid, 'action': 'read', 'wait_ms': 0})
        finally:
            proceed.set()
        result = await self.slot.request('query', {'operation_id': 'held-control', 'wait_ms': 5000})
        self.assertIsNone(result['error'], result)
        self.assertNotIn('held-control', self.slot.execution.operations)
        await self.slot.request('session', {'operation_id': 'next-control',
            'session_id': sid, 'action': 'unwatch'})
        result = await self.slot.request('query', {'operation_id': 'next-control', 'wait_ms': 5000})
        self.assertIsNone(result['error'], result)

    async def test_33rd_request_waits_and_executes_once_after_capacity_returns(self):
        await self.saturated()
        queued = self.task('submit', {'operation_id':'queued','cmd':'printf queued','wait_ms':0})
        await self.queued()
        self.assertNotIn('queued', self.worker.operations)
        self.assertFalse(queued.done())
        self.releases[0].set()
        await asyncio.wait_for(queued, 3)
        self.assertIn('queued', self.worker.operations)
        self.assertEqual(len(self.started), 32)

    async def test_cancelled_waiter_is_never_sent(self):
        await self.saturated()
        queued = self.task('submit', {'operation_id':'cancelled','cmd':'printf cancelled','wait_ms':0})
        await self.queued()
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        self.releases[0].set()
        self.assertEqual((await self.slot.request('metadata', {}))['worker_id'], 'worker')
        self.assertNotIn('cancelled', self.worker.operations)
        self.assertFalse(self.slot.execution.operations)

    async def test_timeout_and_queue_limit_are_known_not_started(self):
        await self.saturated()
        self.slot.admission.timeout = .1
        self.slot.admission.queue_limit = 1
        queued = self.task('submit', {'operation_id':'expired','cmd':'printf expired','wait_ms':0})
        await self.queued()
        reply = await self.hub.call('request', {'target':1,'method':'metadata',
            'params':{}})
        self.assertEqual(reply['error']['code'], 'transport_capacity')
        self.assertEqual(reply['error']['effect'], 'not_started')
        with self.assertRaises(RemoteError) as error:
            await queued
        self.assertEqual(error.exception.code, 'transport_queue_timeout')
        self.assertEqual(error.exception.effect, 'not_started')
        self.assertFalse(self.worker.operations)

    async def test_detach_wakes_queued_and_inflight_calls(self):
        await self.saturated()
        queued = self.task('submit', {'operation_id':'detached','cmd':'printf detached','wait_ms':0})
        await self.queued()
        await asyncio.wait_for(self.slot.close(), 3)
        with self.assertRaises(RemoteError) as error:
            await queued
        self.assertEqual(error.exception.effect, 'not_started')
        self.assertEqual(self.slot.admission.active, 0)
        self.assertNotIn('detached', self.worker.operations)
        self.assertFalse(self.worker.closed)


if __name__ == '__main__':
    unittest.main()
