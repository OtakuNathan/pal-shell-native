"""Bounded, non-waking observations; absence is not readiness."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import platform
import subprocess


def now():
    return datetime.now(timezone.utc).isoformat()


def command(argv):
    if os.name == 'nt':
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            return result.stdout.decode('utf-8', errors='replace').strip() if result.returncode == 0 and len(result.stdout) <= 65536 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    import selectors
    import time
    process = None
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
        raw = bytearray()
        deadline = time.monotonic() + 2
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    return None
                chunk = os.read(process.stdout.fileno(), min(8192, 65537-len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > 65536:
                    return None
        code = process.wait(timeout=max(.001, deadline-time.monotonic()))
        return raw.decode('utf-8', errors='replace').strip() if code == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()


def machine_identity():
    if platform.system() == 'Linux':
        try:
            return 'linux:' + Path('/etc/machine-id').read_text().strip()
        except OSError:
            return None
    if platform.system() == 'Darwin':
        data = command(['/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice']) or ''
        import re
        found = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', data)
        return 'macos:' + found[1] if found else None
    return None


def shell_info(path):
    if os.name == 'nt':
        version = command([path, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', '$PSVersionTable.PSVersion.ToString()'])
        return {'family': 'powershell', 'executable': path, 'version': version,
                'invocation': ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File'],
                'pty': False, 'verified': bool(version) and Path(path).name.lower() in {'powershell.exe', 'pwsh.exe'}, 'experimental': True}

    # Do not execute a login profile or trust $SHELL while discovering syntax.
    version = command([path, '--noprofile', '--norc', '-c', 'printf "%s" "$BASH_VERSION"'])
    return {'family': 'bash' if version else 'unknown', 'executable': path,
            'version': version, 'invocation': ['-lc'], 'pty': True, 'verified': bool(version)}


def probe(shell):
    system = platform.system()
    arch = platform.machine()
    logical = os.cpu_count()
    cpu, memory = {'model': platform.processor() or None, 'logical_cores': logical, 'physical_cores': None,
                   'available_cores': len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else logical,
                   'quota_cores': None}, {'total_bytes': None, 'available_bytes': None, 'limit_bytes': None}
    distribution = {}
    gpu = {'devices': [], 'state': 'unknown'}
    desktop = None
    required = []
    if system == 'Windows':
        import ctypes
        class MemoryStatus(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong),
                        ('total', ctypes.c_ulonglong), ('available', ctypes.c_ulonglong),
                        ('total_page', ctypes.c_ulonglong), ('available_page', ctypes.c_ulonglong),
                        ('total_virtual', ctypes.c_ulonglong), ('available_virtual', ctypes.c_ulonglong),
                        ('extended', ctypes.c_ulonglong)]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            memory.update(total_bytes=status.total, available_bytes=status.available)
        distribution = {'PRETTY_NAME': platform.platform(), 'VERSION_ID': platform.version()}
    if system == 'Linux':
        try:
            distribution = platform.freedesktop_os_release()
            data = Path('/proc/cpuinfo').read_text()
            cpu['model'] = next((line.split(':', 1)[1].strip() for line in data.splitlines()
                                 if line.startswith(('model name', 'Hardware'))), cpu['model'])
            info = {line.split(':')[0]: int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines()}
            memory.update(total_bytes=info.get('MemTotal'), available_bytes=info.get('MemAvailable'))
        except (OSError, ValueError):
            pass
        # Account for the worker's cgroup and all restricting parents, not just
        # the hierarchy root (a user service often has a narrower CPU/memory cap).
        root = Path('/sys/fs/cgroup')
        directories = [root]
        try:
            relative = next(line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
            current = root / relative.lstrip('/')
            if '..' not in current.parts and current.is_dir():
                directories = [current, *[x for x in current.parents if x == root or root in x.parents]]
        except (OSError, StopIteration):
            pass
        quotas, limits, available = [], [], []
        for directory in directories:
            try:
                quota, period = (directory/'cpu.max').read_text().split()
                if quota != 'max' and int(period) > 0:
                    quotas.append(int(quota)/int(period))
            except (OSError, ValueError):
                pass
            try:
                limit = (directory/'memory.max').read_text().strip()
                if limit != 'max':
                    limits.append(int(limit))
                    usage = int((directory/'memory.current').read_text().strip())
                    available.append(max(0, int(limit)-usage))
            except (OSError, ValueError):
                pass
        cpu['quota_cores'] = min(quotas) if quotas else None
        memory['limit_bytes'] = min(limits) if limits else None
        if available:
            memory['available_bytes'] = min([*available, *([memory['available_bytes']] if memory['available_bytes'] is not None else [])])
        data = command(['/usr/bin/nvidia-smi', '--query-gpu=name,memory.total,memory.free', '--format=csv,noheader,nounits'])
        if data:
            devices = []
            for line in data.splitlines():
                try:
                    name, total, free = line.rsplit(',', 2)
                    devices.append({'model': name.strip(), 'memory_type': 'dedicated',
                                    'total_bytes': int(total) * 1024**2, 'available_bytes': int(free) * 1024**2})
                except ValueError:
                    continue
            gpu = {'devices': devices, 'state': 'observed', 'acceleration': 'not_verified'}
    elif system == 'Darwin':
        cpu['model'] = command(['/usr/sbin/sysctl', '-n', 'machdep.cpu.brand_string'])
        for key, name in [('physical_cores', 'hw.physicalcpu'), ('logical_cores', 'hw.logicalcpu')]:
            value = command(['/usr/sbin/sysctl', '-n', name])
            cpu[key] = int(value) if value and value.isdigit() else None
        value = command(['/usr/sbin/sysctl', '-n', 'hw.memsize'])
        memory['total_bytes'] = int(value) if value and value.isdigit() else None
        # Rosetta worker architecture must not be advertised as hardware architecture.
        arm = command(['/usr/sbin/sysctl', '-n', 'hw.optional.arm64'])
        if arm == '1':
            arch = 'arm64'
        try:
            desktop = Path('/dev/console').stat().st_uid == os.getuid()
        except OSError:
            pass
        required = ['desktop_automation_not_verified'] if desktop else ['user_login_required']
    try:
        load = list(os.getloadavg())
    except (OSError, AttributeError):
        load = None
    return {'source': 'worker_probe', 'observed_at': now(), 'machine_identity': machine_identity(),
            'os': {'family': system.lower(), 'name': distribution.get('PRETTY_NAME', platform.system()),
                   'version': platform.mac_ver()[0] if system == 'Darwin' else distribution.get('VERSION_ID'),
                   'kernel': platform.release()},
            'machine_arch': arch, 'worker_arch': platform.machine(), 'cpu': cpu, 'memory': memory,
            'gpu': gpu, 'load_average': load, 'shell': shell,
            'shell_ready': shell['verified'], 'desktop_session_ready': desktop,
            'required_user_action': required}
