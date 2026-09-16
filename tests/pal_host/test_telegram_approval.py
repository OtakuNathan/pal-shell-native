import asyncio
import tempfile
import unittest
from pathlib import Path
import pal
from pal.channel.provider_manager import _load_source_module
from pal.channel.contracts import EndpointConfig, ResponseHandle
from pal.control import ControlPlane
from pal.foundation import EventEnvelope
from pal.shared import EventKind, SourceKind


class TelegramApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        provider = Path(pal.__file__).resolve().parents[2] / 'providers/telegram/endpoint.py'
        if not provider.exists():
            provider = Path(__file__).resolve().parents[2] / '.pal/providers/telegram/endpoint.py'
        if not provider.exists():
            self.skipTest('Pal source checkout with Telegram provider is required')
        endpoint = _load_source_module('_native_test_telegram.endpoint', provider).TelegramChannelEndpoint
        self.directory = tempfile.TemporaryDirectory()
        self.endpoint = endpoint(endpoint=EndpointConfig(endpoint_id='telegram_main', channel_kind='telegram',
            binding_key='user:42', send_policy={}), runtime_root=Path(self.directory.name), bot_token='token')

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_remote_approval_card_callback_completes_shared_approval(self):
        from datetime import datetime, timezone
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from pal_shell_native.approval import ShellApprovals
        from pal.control.handler import ControlEventHandler
        from pal.control.routing import derive_control_scope_key
        reply={'chat_id':'100','user_id':'42'}
        scope=derive_control_scope_key(endpoint_id='telegram_main',channel_kind='telegram',reply_target=reply)
        binding=SimpleNamespace(response_handle=ResponseHandle('telegram_main',reply),
            endpoint=self.endpoint.endpoint,control_scope_key=scope,correlation_id='turn')
        core=SimpleNamespace(state=SimpleNamespace(active_turns={'turn':SimpleNamespace(delivery_binding=binding)}))
        approvals=ShellApprovals(SimpleNamespace(core=core))
        sent=asyncio.Event();cards=[]
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=12)))
        self.endpoint.application=SimpleNamespace(bot=bot)
        async def deliver(delivery,**kwargs):
            cards.append(delivery.interaction)
            if delivery.interaction.buttons:
                await self.endpoint._open_or_update_interaction_async(
                    binding.response_handle,spec=delivery.interaction,allow_update=False)
                sent.set()
            return True
        core._deliver_control_delivery_async=deliver
        task=asyncio.create_task(approvals.request('turn',1,{'action':'sudo','cmd':'apt update'},
            {'operation_id':'op','worker_id':'desktop','runtime_epoch':'epoch',
             'expires_at':datetime.now(timezone.utc).timestamp()+30}))
        try:
            await asyncio.wait_for(sent.wait(),2)
            button=bot.send_message.call_args.kwargs['reply_markup'].inline_keyboard[0][0]
            callback=SimpleNamespace(data=button.callback_data,from_user=SimpleNamespace(id=42),
                message=SimpleNamespace(chat=SimpleNamespace(id=100)),answer=AsyncMock())
            result=await self.endpoint._interaction_result_from_update(SimpleNamespace(callback_query=callback))
            envelope=self.endpoint.emit_interaction_result(result,reply_target=reply)
            event=EventEnvelope(event_kind=EventKind.INTERACTION_RESULT,source_kind=SourceKind.CHANNEL,payload=envelope)
            actions=ControlEventHandler(ControlPlane()).handle(event,None)
            self.assertEqual(actions[0].payload.trusted_actor,'42')
            approvals.decide(actions[0].payload)
            await asyncio.wait_for(task,2)
            self.assertIn('Approved once',cards[-1].text)
            self.assertFalse(cards[-1].buttons)
        finally:
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)
