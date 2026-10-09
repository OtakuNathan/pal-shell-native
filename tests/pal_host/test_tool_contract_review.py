"""Host-only diagnostics: real local commands, no native executor or remote network."""
import asyncio
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def slot(monkeypatch):
    # Fail loudly if these tests accidentally attempt native transport work.
    if '_pal_shell_rpc' not in sys.modules:
        sentinel = ModuleType('_pal_shell_rpc')
        def unexpected(name):
            raise AssertionError('native backend must not run: ' + name)
        sentinel.__getattr__ = unexpected
        monkeypatch.setitem(sys.modules, '_pal_shell_rpc', sentinel)
    from pal_shell_remote.slot import RemoteSlot
    config = SimpleNamespace(target=1, name='audit', shortcut='', static={}, start_actions={})
    return RemoteSlot(config, executor=object())


@pytest.mark.parametrize('message,expected_offline,needs_start', [
    ('Permission denied (publickey)', False, None),
    ('Network is unreachable', False, None),
    ('Worker socket does not exist', False, None),
    ('Connection refused after requested shutdown', True, True),
])
def test_probe_failure_preserves_cause_without_guessing_start(slot, message, expected_offline, needs_start):
    from pal_shell_worker.protocol import RemoteError
    slot.config.start_actions = {'wake': ['/bin/true']}
    slot.expected_offline = expected_offline
    async def fail(*args):
        try:
            raise OSError(message)
        except OSError as exc:
            raise RemoteError('connection_unavailable', 'SSH connection failed') from exc
    slot.request = fail
    result = asyncio.run(slot.describe(refresh=True))
    assert result['reachable'] is False
    assert result['needs_start'] is needs_start
    assert message in result['probe_error']
    assert message in result['probe_details']['error']
    assert result['probed_at']


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_start_retains_complete_stream_and_parent_exit(slot, stream):
    source = f'import sys; sys.{stream}.write("ROOT\\n" + "x" * 100000 + "\\nTAIL"); sys.exit(7)'
    slot.config.start_actions = {'wake': [sys.executable, '-c', source]}
    result = asyncio.run(slot.start('wake'))
    assert result['returncode'] == 7 and result['status'] == 'start_action_failed'
    full = Path(result[stream + '_path']).read_text()
    assert full.startswith('ROOT\n') and full.endswith('\nTAIL')
    assert len(result[stream]) < 2100


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX fork inheritance')
def test_start_does_not_wait_for_descendant_output_handles(slot):
    source = 'import os,time; pid=os.fork(); time.sleep(2) if pid == 0 else None'
    slot.config.start_actions = {'wake': [sys.executable, '-c', source]}
    async def run():
        return await asyncio.wait_for(slot.start('wake'), 1)
    assert asyncio.run(run())['returncode'] == 0


def test_native_registry_preserves_discovery_and_shortcut_vocabulary(monkeypatch, tmp_path):
    sentinel = ModuleType('_pal_shell_runtime')
    def unexpected(name):
        raise AssertionError('native backend must not run: ' + name)
    sentinel.__getattr__ = unexpected
    monkeypatch.setitem(sys.modules, '_pal_shell_runtime', sentinel)
    from pal_shell_native.capabilities import build_provider
    from pal.execution.capability_compiler import compile_provider_subtree
    from pal.execution.runtime import ExecutionRuntime
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'remote.toml').write_text('''[[targets]]
target = 1
name = "Pi"
worker_id = "worker"
client_id = "client"
client_key = "/unused/key"
socket_path = "/unused/socket"
shortcut = "pi"
''')
    runtime = ExecutionRuntime()
    runtime.runtime_root = tmp_path
    try:
        provider = build_provider(runtime)
        tree = compile_provider_subtree(provider, module_id='execution', lifecycle_scope='runtime', detachable=False)
        runtime.mount_subtree(SimpleNamespace(mounted_subtree=tree))
        for action in ('read', 'write', 'resize', 'terminate', 'release', 'watch', 'extend', 'unwatch'):
            hits = runtime._search_generation(runtime.registry_generation, {'query': f'{action} shell sessions'})['hits']
            assert f'{action}_shell_session' in [hit['alias'] for hit in hits]
        for query, expected in (
            ('run commands', 'run_shell'), ('read shell sessions', 'read_shell_session'),
            ('list remote targets', 'list_remote'), ('start remote targets', 'start_remote_target'),
            ('shutdown remote targets', 'shutdown_remote_target'), ('run pi commands', 'run_shell_pi'),
        ):
            hits = runtime._search_generation(runtime.registry_generation, {'query': query})['hits']
            assert expected in [hit['alias'] for hit in hits], (query, hits)
    finally:
        runtime.shutdown()


def test_start_timeout_retains_both_complete_outputs(slot, tmp_path, monkeypatch):
    import pal_shell_remote.slot as module
    from pal_shell_worker.protocol import RemoteError
    monkeypatch.setattr(module.tempfile, 'mkdtemp', lambda **kwargs: str(tmp_path))
    slot.config.start_actions = {'wake': [sys.executable, '-c',
        'import sys,time; print("stdout cause",flush=True); print("stderr cause",file=sys.stderr,flush=True); time.sleep(60)']}
    with pytest.raises(RemoteError, match='timed out') as error:
        asyncio.run(slot.start('wake'))
    assert str(tmp_path / 'stdout.log') in str(error.value)
    assert str(tmp_path / 'stderr.log') in str(error.value)
    assert (tmp_path / 'stdout.log').read_text() == 'stdout cause\n'
    assert (tmp_path / 'stderr.log').read_text() == 'stderr cause\n'
