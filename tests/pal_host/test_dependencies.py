"""Import boundaries for the local plugin and optional remote transport."""
import ast
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2] / 'pal_plugin'


class DependencyTests(unittest.TestCase):
    def test_plugin_import_graph_is_acyclic_and_contracts_are_independent(self):
        modules = {}
        for package in ('pal_shell_native', 'pal_shell_remote', 'pal_shell_contracts'):
            for path in (ROOT / package).rglob('*.py'):
                name = '.'.join(path.relative_to(ROOT).with_suffix('').parts)
                modules[name.removesuffix('.__init__')] = path
        graph = {name: set() for name in modules}
        for name, path in modules.items():
            package = name if path.name == '__init__.py' else name.rpartition('.')[0]
            for node in ast.walk(ast.parse(path.read_text())):
                targets = []
                if isinstance(node, ast.Import):
                    targets = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = importlib.util.resolve_name('.' * node.level + (node.module or ''), package) if node.level else node.module
                    targets = [base, *(base + '.' + alias.name for alias in node.names)]
                for target in targets:
                    if name.startswith('pal_shell_contracts'):
                        self.assertIn(target.split('.')[0], sys.stdlib_module_names, (name, target))
                    if name.startswith('pal_shell_remote'):
                        self.assertFalse(target.startswith('pal_shell_native'), (name, target))
                    if name == 'pal_shell_native.output_contract':
                        self.assertFalse(target in {
                            'pal_shell_native.runtime', 'pal_shell_native.capabilities',
                            'pal_shell_native.observation_owner', 'pal_shell_native.events',
                        }, target)
                    # Importing a child also initializes its package.
                    parts = target.split('.')
                    graph[name].update('.'.join(parts[:i]) for i in range(1, len(parts) + 1)
                                       if '.'.join(parts[:i]) in modules and '.'.join(parts[:i]) != name)
        visited = set()
        active = []

        def visit(name):
            self.assertNotIn(name, active, ' -> '.join([*active, name]))
            if name in visited:
                return
            active.append(name)
            for target in sorted(graph[name]):
                visit(target)
            active.pop()
            visited.add(name)

        for name in sorted(graph):
            visit(name)

    def test_provider_configuration_needs_no_worker_or_rpc(self):
        script = r'''
import sys, tempfile
from pathlib import Path
from importlib.abc import MetaPathFinder
class NoRPC(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'pal_shell_worker', '_pal_shell_rpc'}:
            raise AssertionError(fullname)
sys.meta_path.insert(0, NoRPC())
from pal.core import PalCore
from pal_shell_native.runtime import NativeExecutionRuntime
from pal_shell_native.capabilities import build_provider, NativeExecutionProvider
from pal_shell_contracts import Target
assert isinstance(build_provider(NativeExecutionRuntime()), NativeExecutionProvider)
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    runtime = NativeExecutionRuntime(runtime_root=root)
    build_provider(runtime)  # missing configuration
    (root / 'config').mkdir()
    config = root / 'config/remote.toml'
    config.write_text('targets = []')
    build_provider(runtime)
    valid = """[[targets]]
target = 1
name = "test"
worker_id = "worker"
client_id = "client"
client_key = "/identity"
socket_path = "/worker.sock"
shortcut = "build"
"""
    config.write_text(valid)
    provider = build_provider(runtime)
    assert callable(provider.shortcut_build)
    assert callable(provider.shortcut_build_async)
    for invalid, message in [
        (valid.replace('target = 1', 'target = 0'), 'Remote targets must be positive integers'),
        (valid.replace('shortcut = "build"', 'shortcut = "bad shortcut"'), 'Shortcut must be'),
        (valid + valid, 'Duplicate remote target'),
        (valid + valid.replace('target = 1', 'target = 2'), 'Target shortcuts must be unique'),
    ]:
        config.write_text(invalid)
        try:
            build_provider(runtime)
        except ValueError as exc:
            assert message in str(exc), str(exc)
        else:
            raise AssertionError('invalid configuration accepted')
assert not any(name.startswith(('pal_shell_worker', '_pal_shell_rpc', 'pal_shell_remote.slot')) for name in sys.modules)
'''
        env = dict(os.environ)
        env['PYTHONPATH'] = os.pathsep.join([str(ROOT), env.get('PYTHONPATH', '')])
        result = subprocess.run([sys.executable, '-c', script], env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_compatibility_exports_preserve_identity(self):
        from pal_shell_contracts import Target, RemoteFailure, RemotePort
        from pal_shell_native import remote_contract, runtime, output_contract
        from pal_shell_remote import slot
        self.assertIs(slot.Target, Target)
        self.assertIs(remote_contract.RemoteFailure, RemoteFailure)
        self.assertIs(remote_contract.RemotePort, RemotePort)
        self.assertIs(runtime.PendingOutput, output_contract.PendingOutput)
        self.assertIs(runtime.output_result, output_contract.output_result)

    def test_output_projection_and_pending_defaults(self):
        from pal_shell_native.output_contract import PendingOutput, output_result
        from pal.shared.result_snapshot import ResultSnapshotRef
        snapshot = ResultSnapshotRef('snapshot', '/output', 'digest', 5)
        raw = {'session_id': 7, 'status': 'running', 'returncode': 9, 'stdout': 'hello',
               'runtime_epoch': 'private', 'cursor': 12, '_snapshot_text': 'snapshot footer',
               '_output_snapshots': [snapshot]}
        result = output_result(raw)
        self.assertEqual(result.structured, {
            'session_id': 7, 'status': 'running', 'returncode': None, 'stdout': 'hello'})
        self.assertTrue(result.llm_text.endswith('\nsnapshot footer'))
        self.assertEqual(result.effect_receipt.outcome, 'applied')
        self.assertEqual(result.snapshot_refs, (snapshot,))
        self.assertEqual(output_result({**raw, 'status': 'exited'}).structured['returncode'], 9)
        pending = PendingOutput(raw, 'turn')
        self.assertIs(pending.result, raw)
        self.assertIsNone(pending.raw)
        self.assertFalse(pending.prepared or pending.delivered)
        self.assertTrue(pending.covers_output)
