"""Verify a packaged local engine and the Hub's separately loaded RPC client."""
import os
from pathlib import Path
import subprocess
import sys


def verify(context):
    try:
        from pal.core.runtime import PalCore
        from pal.execution.runtime import ExecutionRuntime
        from pal.execution.session_state import FileDeliveryManifest
        from pal.execution.tool_facade import ToolGuidance
        from pal.memory.service import MemoryService
        from pal.shared.diagnostics import diagnostic_text, exception_diagnostic
        from pal.bunshin.verification_builder import SHELL_EVIDENCE_CAPABILITIES

        if not callable(diagnostic_text) or not callable(exception_diagnostic) or not SHELL_EVIDENCE_CAPABILITIES:
            raise RuntimeError("Pal lacks diagnostics or Bunshin shell verification")
        if not callable(PalCore.start_background_turn):
            raise RuntimeError("Pal lacks background-turn admission")
        if "search_enum_fields" not in ToolGuidance.model_fields:
            raise RuntimeError("Pal lacks enum-field tool discovery support")
        if "remote" not in FileDeliveryManifest.__dataclass_fields__ or not callable(ExecutionRuntime.unbind_result_history):
            raise RuntimeError("Pal lacks file delivery or history retirement support")
        if not callable(ExecutionRuntime.prepare_model_context) or not callable(MemoryService.l1_context_view):
            raise RuntimeError("Pal lacks indexed request visibility or observation delivery")
    except (ImportError, AttributeError, RuntimeError) as exc:
        return {"ok": False, "detail": f"Install the matching Pal file-delivery protocol v1 revision: {exc}"}

    root = Path(context["package_dir"]) / "host" if context is not None else Path(__file__).resolve().parent
    if context is not None and not (root / "backend").is_dir():
        return {"ok": False, "detail": "remote package is missing its bundled engine and Hub dependencies"}
    program = """
from pathlib import Path
import sys
from pal_shell_contracts.package_layout import hub_dependency_root
from pal_shell_native.local_engine import LocalEngine
root = Path(sys.argv[1])
engine = LocalEngine(package_root=root)
try:
    dependency = hub_dependency_root(root)
    if dependency is not None:
        sys.path.insert(0, str(dependency))
    import _pal_shell_rpc
    from pal_shell_worker import PROTOCOL_VERSION
    from pal_shell_worker.files import FILE_PROTOCOL_VERSION
    if (PROTOCOL_VERSION != 3 or FILE_PROTOCOL_VERSION != 1 or not callable(_pal_shell_rpc.channel_request)
            or (dependency is not None and not Path(_pal_shell_rpc.__file__).resolve().is_relative_to(dependency))):
        raise RuntimeError('wrong bundled Hub dependencies')
finally:
    engine.close()
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) if context is not None else os.pathsep.join(
        filter(None, (str(root), env.get("PYTHONPATH")))
    )
    try:
        result = subprocess.run([sys.executable, "-c", program, str(root)], env=env,
                                capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": f"Could not check bundled shell dependencies: {exc}"}
    if result.returncode:
        return {"ok": False, "detail": result.stderr[-1600:]}
    return {"ok": True, "local_protocol": 1, "remote_protocol": 3}
