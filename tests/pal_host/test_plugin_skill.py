"""The setup manual follows the optional plugin's declared-skill lifecycle."""
from pathlib import Path
import re
import tempfile
import tomllib
from types import SimpleNamespace
import unittest

from pal.skill.models import SkillModel
from pal.core import PalCore
from pal.core.main_context import MainContext
from pal.execution import register_with_core
from pal.foundation import PalV2Database
from pal.plugins.capabilities import register_with_core as register_plugins
from pal.skill import SkillService, SkillSearchTool, register_with_core as register_skill
from pal_shell_native.guidance import RUN_GUIDANCE, REMOTE_RUN_GUIDANCE, RESIDENT_SESSION_GUIDANCE
from pal_shell_native.plugin import NativePlugin
from pal_shell_native.remote_setup_manual import PAL_REMOTE_SETUP_MANUAL
from pal_shell_native.runtime import NativeExecutionRuntime


class NativeSkillTests(unittest.TestCase):
    def test_setup_references_resolve_against_the_host_tool_registry(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = PalV2Database(Path(temporary) / 'skill.sqlite3')
            database.initialize([SkillModel])
            runtime = NativeExecutionRuntime(runtime_root=Path(temporary))
            core = PalCore(context=MainContext(execution_runtime=runtime))
            try:
                service = SkillService()
                register_with_core(core.context)
                register_skill(core.context, service)
                register_plugins(core.context, SimpleNamespace())
                for module in ('execution', 'skill', 'plugins'):
                    core.publish_module_capabilities(module)
                core.context.register_module(NativePlugin(temporary).start(None))
                core.publish_module_capabilities('remote')
                published = service.inject_skill('pal.remote.setup')
                references = set(published.capability_refs)
                references.update(re.findall(r'`([a-z]+(?:_[a-z]+)+)(?:\(|`)', published.manual_text))
                for guidance in (RUN_GUIDANCE, REMOTE_RUN_GUIDANCE, RESIDENT_SESSION_GUIDANCE):
                    references.update(hint.name for hint in guidance.next_tool_hints)
                for alias in sorted(references):
                    with self.subTest(alias=alias):
                        self.assertIsNotNone(runtime.registry_generation.record_for_alias(alias), alias)
            finally:
                runtime.shutdown()
                core.close()
                database.close()

    def test_runtime_and_documented_artifacts_match_package_identity(self):
        root = Path(__file__).resolve().parents[2]
        package = tomllib.loads((root / 'pal_plugin/package.toml').read_text())
        manifest = tomllib.loads((root / 'pal_plugin/plugin.toml').read_text())
        project = tomllib.loads((root / 'pyproject.toml').read_text())['project']
        self.assertEqual(NativePlugin.plugin_id, package['id'])
        for version in (NativePlugin.version, manifest['version'], project['version']):
            self.assertEqual(version, package['version'])
        for text in (PAL_REMOTE_SETUP_MANUAL, (root / 'README.md').read_text(),
                     (root / 'docs/remote-shell.md').read_text()):
            versions = set(re.findall(r'plugin-remote-(\d+\.\d+\.\d+)\.palpkg', text))
            self.assertEqual(versions, {package['version']})

    def test_package_verification_works_in_a_fresh_host_import(self):
        import subprocess
        import sys
        hook = Path(__file__).resolve().parents[2] / 'pal_plugin/hooks.py'
        script = "import runpy, sys; result = runpy.run_path(sys.argv[1])['verify'](None); assert result['ok'], result"
        subprocess.run([sys.executable, '-c', script, str(hook)], check=True,
                       capture_output=True, text=True, timeout=30)

    def test_plugin_rejects_host_without_observation_hooks(self):
        from unittest.mock import patch
        from pal.execution.runtime import ExecutionRuntime
        from pal_shell_native.plugin import NativeExtension
        with patch.object(ExecutionRuntime, 'prepare_model_context', None):
            with self.assertRaisesRegex(RuntimeError, 'matching observation delivery'):
                NativeExtension('/tmp').build_runtime(None)

    def test_package_verification_rejects_missing_host_dependencies(self):
        import runpy
        import sys
        from unittest.mock import patch
        verify = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'pal_plugin/hooks.py'))['verify']
        for module in ('pal.shared.diagnostics', 'pal.bunshin.verification_builder'):
            with self.subTest(module=module), patch.dict(sys.modules, {module: None}):
                result = verify(None)
                self.assertFalse(result['ok'])
                self.assertIn('618f70f4d18b1972aae0580fd7955e8076479d0d', result['detail'])
                self.assertIn(module, result['detail'])

    def test_install_and_activation_reject_host_without_indexed_visibility(self):
        import runpy
        from unittest.mock import patch
        from pal.memory.service import MemoryService
        from pal_shell_native.plugin import NativeExtension
        verify = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'pal_plugin' / 'hooks.py'))['verify']
        with patch.object(MemoryService, 'l1_context_view', None):
            self.assertFalse(verify(None)['ok'])
            with self.assertRaisesRegex(RuntimeError, 'matching observation delivery'):
                NativeExtension('/tmp').build_runtime(None)

    def test_setup_skill_is_published_and_withdrawn_with_plugin(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = PalV2Database(Path(temporary) / 'skill.sqlite3')
            database.initialize([SkillModel])
            core = PalCore()
            try:
                service = SkillService()
                register_with_core(core.context)
                register_skill(core.context, service)
                core.publish_module_capabilities('skill')
                self.assertIsNone(service.inject_skill('pal.remote.setup'))
                handle = NativePlugin(temporary).start(None)
                core.context.register_module(handle)
                core.publish_module_capabilities('remote')
                for query in ('远端接入', '安装remote端', 'remote worker', '云主机升级',
                              'Upgrade and deploy pal-shell-native pal-shell-worker to a Linux x86_64 SSH server using the existing systemd user service'):
                    result = SkillSearchTool(service=service).invoke({'query': query, 'top_k': 3})
                    self.assertEqual(result.structured['hits'][0]['skill_id'], 'pal.remote.setup')
                self.assertIn(f'plugin-remote-{NativePlugin.version}.palpkg', service.inject_skill('pal.remote.setup').manual_text)
                core.withdraw_module_capabilities('remote')
                self.assertIsNone(service.inject_skill('pal.remote.setup'))
            finally:
                core.context.execution_runtime.shutdown()
                core.close()
                database.close()
