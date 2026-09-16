"""The setup manual follows the optional plugin's declared-skill lifecycle."""
import asyncio
from pathlib import Path
import tempfile
import unittest

from pal.behavior import BehaviorAffordanceModel, BehaviorSkillModel, BehaviorService, BehaviorRepository
from pal.behavior.contracts import BehaviorAdviceRequest
from pal.core import PalCore
from pal.execution import register_with_core
from pal.foundation import PalV2Database
from pal.skill import SkillService, SkillSearchTool, register_with_core as register_skill
from pal_shell_native.plugin import NativePlugin


class NativeSkillTests(unittest.TestCase):
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
            database.initialize([BehaviorAffordanceModel, BehaviorSkillModel])
            core = PalCore()
            try:
                service = SkillService()
                register_with_core(core.context)
                register_skill(core.context, service)
                behavior = BehaviorService(repository=BehaviorRepository(skill_repository=service.repository))
                core.publish_module_capabilities('skill')
                self.assertIsNone(service.inject_skill('pal.remote.setup'))
                handle = NativePlugin(temporary).start(None)
                core.context.register_module(handle)
                core.publish_module_capabilities('remote')
                behavior.register_declared_module(handle)
                for query in ('远端接入', '安装remote端', 'remote worker'):
                    result = SkillSearchTool(service=service).invoke({'query': query, 'top_k': 3})
                    self.assertEqual(result.structured['hits'][0]['skill_id'], 'pal.remote.setup')
                    advice = asyncio.run(behavior.advise_async(BehaviorAdviceRequest(scenario=query, top_k=5)))
                    self.assertTrue(any('pal.remote.setup' in item.skill_refs for item in advice.candidates))
                self.assertIn('plugin-remote-0.4.0.palpkg', service.inject_skill('pal.remote.setup').manual_text)
                core.withdraw_module_capabilities('remote')
                self.assertIsNone(service.inject_skill('pal.remote.setup'))
            finally:
                core.context.execution_runtime.shutdown()
                core.close()
                database.close()
