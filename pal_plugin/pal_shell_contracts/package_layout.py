"""Platform and interpreter paths in the remote plugin payload."""
import platform
import sys
import sysconfig


def platform_key():
    return f"{sys.platform}-{platform.machine().lower()}"


def packaged_engine(package_root):
    return package_root / "backend" / platform_key() / "pal-shell-local-worker"


def packaged_hub(package_root):
    return package_root / "backend" / platform_key() / sys.implementation.cache_tag


def hub_dependency_root(package_root):
    if not (package_root / "backend").exists():
        if (package_root / ".pal-package.json").is_file():
            raise RuntimeError("remote package is missing its bundled Hub dependencies")
        return None
    root = packaged_hub(package_root)
    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    if not suffix or not (root / ("_pal_shell_rpc" + suffix)).is_file():
        raise RuntimeError("remote package has no Hub RPC build for this interpreter")
    return root
