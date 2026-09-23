"""Target-owned execution admission, independent of transport capacity."""
from pal_shell_worker.protocol import RemoteError, TERMINAL


class ExecutionGate:
    def __init__(self, target):
        self.target = target
        self.operations = {}

    def restore(self, records):
        # Host tickets survive transport replacement. Never overwrite newer slot
        # evidence, or discard claims merely because a snapshot omitted them.
        for item in records:
            self.operations.setdefault(item['operation_id'], dict(item))

    def claim(self, operation_id, epoch, hold_output=False):
        if operation_id in self.operations:
            if self.operations[operation_id].get('epoch') != epoch:
                raise RemoteError('runtime_changed', 'Operation belongs to another Runtime', effect='unknown')
            return
        if self.operations:
            raise RemoteError('target_busy', f'Target {self.target} is busy: ' +
                              ', '.join(sorted({v['state'] for v in self.operations.values()})))
        self.operations[operation_id] = dict(operation_id=operation_id, epoch=epoch,
            state='submitting', session_id=0, output_id='', hold_output=hold_output)

    def failed(self, operation_id, effect):
        if effect == 'not_started':
            self.operations.pop(operation_id, None)
        elif operation_id in self.operations:
            self.operations[operation_id]['state'] = 'unknown'

    def cancel_prepared(self, operation_id):
        item = self.operations.get(operation_id)
        if item and item.get('unsigned'):
            self.operations.pop(operation_id, None)

    def result(self, operation_id, response, epoch):
        item = self.operations.get(operation_id)
        if item is None:
            return
        if not item.get('epoch'):
            item['epoch'] = epoch
        if response.get('error'):
            self.failed(operation_id, response['error'].get('effect', 'unknown'))
            return
        if response.get('state') == 'approval_required':
            item['state'] = 'awaiting_approval'
        result = response.get('result')
        if result:
            self.snapshot(result, epoch, operation_id)
            if item.get('control') and response.get('state') == 'complete':
                self.operations.pop(operation_id, None)

    def snapshot(self, result, epoch, operation_id=None):
        sid = result.get('session_id', 0)
        for oid, item in list(self.operations.items()):
            if item.get('epoch') != epoch or (item.get('control') and oid != operation_id):
                continue
            if oid != operation_id and (not sid or item.get('session_id') != sid or item['state'] == 'unknown'):
                continue
            item.update(session_id=sid, output_id=result.get('output_id', ''),
                        state='running' if result.get('status') not in TERMINAL | {'accepted'} else 'delivery')
            if item['state'] == 'delivery' and not item.get('hold_output'):
                self.operations.pop(oid, None)

    def release(self, output_id, epoch):
        for oid, item in list(self.operations.items()):
            if item['state'] != 'unknown' and output_id and item.get('output_id') == output_id and item.get('epoch') == epoch:
                self.operations.pop(oid, None)

    def status(self):
        return {'blocked': bool(self.operations),
                'reasons': sorted({v['state'] for v in self.operations.values()})}
