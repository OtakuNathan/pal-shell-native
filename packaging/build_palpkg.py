"""Assemble remote's host payload from repaired release wheels."""
import argparse
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import tomllib
from zipfile import ZipFile


def stage_project(source, destination):
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "backend"))
    manifest = destination / "package.toml"
    text = manifest.read_text()
    manifest.write_text(text.replace('host_files = [', 'host_files = ["backend", ', 1))
    (destination / "backend").mkdir()


def wheel_platform(tags):
    for suffix, key in (("aarch64", "linux-aarch64"), ("x86_64", "linux-x86_64")):
        if tags.startswith(("manylinux", "linux")) and tags.endswith("_" + suffix):
            return key
    for suffix in ("arm64", "x86_64"):
        if tags.startswith("macosx") and tags.endswith("_" + suffix):
            return "darwin-" + suffix
    raise ValueError(f"unsupported Pal platform wheel tag: {tags}")


def stage_wheel(wheel, destination, version):
    distribution, wheel_version, interpreter, abi, tags = wheel.stem.split("-")
    match = re.fullmatch(r"cp(311|312|313)", interpreter)
    if distribution != "pal_shell_native" or wheel_version != version or not match or abi != interpreter:
        raise ValueError(f"wrong shell plugin wheel: {wheel.name}")
    platform = wheel_platform(tags)
    platform_root = destination / "backend" / platform
    hub = platform_root / f"cpython-{match[1]}"
    hub.mkdir(parents=True)
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        rpc = [name for name in names if "/" not in name and name.startswith("_pal_shell_rpc.") and name.endswith(".so")]
        if len(rpc) != 1 or not any(name.startswith("pal_shell_worker/") for name in names):
            raise ValueError(f"wheel lacks its Hub dependencies: {wheel.name}")
        executable = platform_root / "pal-shell-local-worker"
        engine_bytes = archive.read("libexec/pal-shell-local-worker")
        # Prefer the release's CPython 3.13 build for the Python-free executable.
        if interpreter == "cp313" or not executable.exists():
            executable.write_bytes(engine_bytes)
        for name in names:
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("wheel member escapes its staging directory")
            if name.endswith("/"):
                continue
            if relative.parts[0] == "pal_shell_native_licenses":
                path = destination / "backend" / name
            elif (name in rpc or relative.parts[0] == "pal_shell_worker"
                  or relative.parts[0].endswith((".libs", ".dylibs"))):
                path = hub / name
            else:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(name))
    return platform, interpreter


def main():
    from pal.packages.archive import build

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheels", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--complete", action="store_true", help="require every release platform and Python ABI")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "pal_plugin"
    version = tomllib.loads((source / "package.toml").read_text())["version"]
    with tempfile.TemporaryDirectory(prefix="pal-remote-package-") as directory:
        staged = Path(directory) / "pal_plugin"
        stage_project(source, staged)
        available = {stage_wheel(wheel, staged, version) for wheel in sorted(args.wheels)}
        if args.complete:
            required = {(host, abi) for host in ("linux-aarch64", "linux-x86_64", "darwin-arm64", "darwin-x86_64")
                        for abi in ("cp311", "cp312", "cp313")}
            if available != required:
                raise ValueError(f"release is missing wheel builds: {sorted(required - available)}")
        print(build(staged, args.output))


if __name__ == "__main__":
    main()
