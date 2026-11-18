"""Stage the runtime payload from the shell artifacts available to host tests."""
from pathlib import Path
import shutil

from pal.packages.archive import build
from pal_shell_contracts.package_layout import packaged_engine, packaged_hub
from pal_shell_native.local_engine import _engine_source


def build_test_package(source, root, *, hub_dependencies=True):
    staged = root / 'plugin-source'
    shutil.copytree(source, staged, ignore=shutil.ignore_patterns('__pycache__', 'backend'))
    executable = packaged_engine(staged)
    executable.parent.mkdir(parents=True)
    shutil.copyfile(_engine_source(None), executable)
    if hub_dependencies:
        import _pal_shell_rpc
        import pal_shell_worker

        hub = packaged_hub(staged)
        hub.mkdir()
        rpc = Path(_pal_shell_rpc.__file__)
        shutil.copyfile(rpc, hub / rpc.name)
        shutil.copytree(Path(pal_shell_worker.__file__).parent, hub / 'pal_shell_worker',
                        ignore=shutil.ignore_patterns('__pycache__'))
    manifest = staged / 'package.toml'
    manifest.write_text(manifest.read_text().replace('host_files = [', 'host_files = ["backend", ', 1))
    return build(staged, root / 'dist')
