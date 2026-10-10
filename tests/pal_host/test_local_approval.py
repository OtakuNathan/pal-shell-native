"""Local privilege admission and cancellation without invoking system sudo."""
import asyncio
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from pal_shell_native.adapter import ShellRuntime, ShellRejected
import _pal_shell_runtime as native
from pal_shell_native.router import ShellRouter
from pal_shell_contracts import RemoteFailure


@unittest.skipUnless(sys.platform.startswith('linux'), 'Local Linux sudo')
class LocalApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = SimpleNamespace(closed=False, core=None, sessions={}, remote_port=None,
                                     approvals=SimpleNamespace(request=AsyncMock()))
        self.router = ShellRouter(owner=self.owner)
        self.result = {'session_id': 0, 'output_id': 1, 'status': 'exited', 'returncode': 0}

    async def asyncTearDown(self):
        await self.router.close()

    async def test_exact_local_command_and_directory_require_approval_before_launch(self):
        command = 'printf "quoted ; sudo && text"; pacman -S --noconfirm cmake'
        with patch.object(ShellRuntime, 'run', AsyncMock(return_value=self.result)) as launch:
            async def approve(turn, target, args, grant):
                launch.assert_not_called()
                self.assertEqual((turn, target), ('turn', 0))
                self.assertEqual(args, {'action': 'sudo', 'cmd': command,
                                        'cwd': str(Path.home())})
                self.assertEqual(grant['runtime_epoch'], self.router.epoch)
            self.owner.approvals.request.side_effect = approve
            result = await self.router.run(command, sudo=True, cwd='~', turn_id='turn')
            self.assertEqual(result['target'], 0)
            launcher = launch.call_args.args[0]
            self.assertEqual(shlex.split(launcher),
                ['exec', '/usr/bin/sudo', '-n', '--', '/bin/sh', '-c', command])
            self.assertEqual(launch.call_args.kwargs['cwd'], str(Path.home()))
            self.assertEqual(launch.call_args.kwargs['shell'], '/bin/sh')
            self.assertTrue(launch.call_args.kwargs['_authorized'])
            self.assertFalse(self.router._foreground)

    async def test_rejection_expiry_and_unavailable_route_never_launch(self):
        with patch.object(ShellRuntime, 'run', AsyncMock(return_value=self.result)) as launch:
            for code in ('approval_rejected', 'approval_expired', 'approval_unavailable'):
                self.owner.approvals.request.side_effect = RemoteFailure(code, code)
                with self.subTest(code=code), self.assertRaises(RemoteFailure):
                    await self.router.run('id', sudo=True, turn_id='turn')
            launch.assert_not_called()
            self.assertFalse(self.router._foreground)

    async def test_expiration_during_card_close_never_launches(self):
        async def approve(turn, target, args, grant):
            grant['expires_at'] = 0
        self.owner.approvals.request.side_effect = approve
        with patch.object(ShellRuntime, 'run', AsyncMock(return_value=self.result)) as launch:
            with self.assertRaises(RemoteFailure) as caught:
                await self.router.run('id', sudo=True, turn_id='turn')
            self.assertEqual(caught.exception.code, 'approval_expired')
            launch.assert_not_called()

    async def test_privileged_pty_rejected_before_approval(self):
        with self.assertRaisesRegex(ShellRejected, 'privilege_pty_unsupported'):
            await self.router.run('id', sudo=True, tty=True)
        self.owner.approvals.request.assert_not_called()

    async def test_turn_interruption_and_close_cancel_waiting_approval(self):
        for stop in ('turn', 'close'):
            opened = asyncio.Event()
            async def wait(*args):
                opened.set()
                await asyncio.Event().wait()
            self.owner.approvals.request.side_effect = wait
            with patch.object(ShellRuntime, 'run', AsyncMock(return_value=self.result)) as launch:
                task = asyncio.create_task(self.router.run('id', sudo=True, turn_id='turn'))
                await asyncio.wait_for(opened.wait(), 1)
                if stop == 'turn':
                    await self.router.interrupt_turn('turn')
                else:
                    await self.router.close()
                with self.assertRaises(asyncio.CancelledError): await task
                launch.assert_not_called()
                self.assertFalse(self.router._foreground)

    async def test_ordinary_command_keeps_normal_native_entry(self):
        with patch.object(ShellRuntime, 'run', AsyncMock(return_value=self.result)) as launch:
            await self.router.run('echo sudo', turn_id='turn')
            self.owner.approvals.request.assert_not_called()
            self.assertNotIn('_authorized', launch.call_args.kwargs)

    async def test_old_native_binary_is_rejected_before_creating_runtime(self):
        with patch.object(native, 'PRIVILEGE_GATE_VERSION', 0), \
             patch.object(native, 'Runtime') as runtime:
            with self.assertRaisesRegex(ShellRejected, 'native_incompatible'):
                ShellRuntime()
            runtime.assert_not_called()

