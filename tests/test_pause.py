import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import discord

from bot import AdaptiveHeartbeat, ChatBot, Config, State, split_message
from feedback import FeedbackService, FeedbackStore
from test_bot import CFG, Channel, message


class PauseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = State(self.root / 'state.json', True)
        self.state.save(True, 99)
        self.model = NS(answer=AsyncMock(return_value='public reply'),
                        feedback_answer=AsyncMock(return_value='private reply'))
        self.bot = ChatBot(replace(CFG, interval=30), self.state, self.model)
        self.bot._connection.user = NS(id=50)
        self.channel = Channel([message(100)])
        self.bot.channel = AsyncMock(return_value=self.channel)
        self.dm = Mock(spec=discord.DMChannel)
        self.dm.send = AsyncMock()
        self.service = None
        self.message_id = 100
        self.bot.http.bulk_upsert_global_commands = AsyncMock()
        self.bot.http.bulk_upsert_guild_commands = AsyncMock()

    async def asyncTearDown(self):
        if self.bot.feedback is not None:
            await self.bot.feedback.close()
            self.bot.feedback = None
        if self.bot.worker:
            self.bot.worker.cancel()
            await asyncio.gather(self.bot.worker, return_exceptions=True)
        self.tmp.cleanup()

    async def command(self, text, uid=30):
        self.message_id += 1
        await self.bot.on_message(NS(id=self.message_id, content=text,
            author=NS(id=uid, bot=False, display_name=str(uid)),
            created_at=discord.utils.snowflake_time(self.message_id), guild=None, channel=self.dm))

    def feedback(self):
        self.now = 1000
        self.service = FeedbackService(
            self.bot, FeedbackStore(self.root / 'feedback.sqlite3', '10:20:30'),
            lambda: AdaptiveHeartbeat(self.bot.config), split_message, clock=lambda: self.now)
        self.bot.feedback = self.service
        self.owner_dm = NS(send=AsyncMock())
        self.participant_dm = NS(send=AsyncMock())
        self.bot.get_user = Mock(side_effect=lambda uid: NS(create_dm=AsyncMock(
            return_value=self.owner_dm if uid == 30 else self.participant_dm)))
        return self.service

    async def test_startup_forces_saved_running_state_to_pause_before_workers(self):
        self.state.save(True, 99, pending_direction='pending', standing_direction='standing')
        self.bot.wait_until_ready = AsyncMock(side_effect=asyncio.CancelledError)
        with patch('bot.ROOT', self.root):
            await self.bot.setup_hook()
        self.assertFalse(self.state.enabled)
        self.assertFalse(self.bot.automatic_replies_enabled)
        persisted = State(self.state.path, True)
        self.assertFalse(persisted.enabled)
        self.assertEqual(persisted.last_seen_message_id, 99)
        self.assertEqual(persisted.pending_direction, 'pending')
        self.assertEqual(persisted.standing_direction, 'standing')
        await self.bot.scan()
        self.model.answer.assert_not_awaited()
        self.bot.http.bulk_upsert_global_commands.assert_not_awaited()

    async def test_startup_state_write_failure_aborts_without_workers(self):
        with patch.object(self.state, 'save', side_effect=OSError()):
            with self.assertRaises(OSError):
                await self.bot.setup_hook()
        self.assertFalse(self.bot.automatic_replies_enabled)
        self.assertIsNone(self.bot.worker)
        self.assertIsNone(self.bot.feedback)

    async def test_bot_enabled_environment_is_ignored(self):
        env = dict(DISCORD_BOT_TOKEN='test', DISCORD_GUILD_ID='10', DISCORD_CHANNEL_ID='20',
                   ALLOWED_USER_IDS='30,40', OWNER_USER_ID='30', GEMINI_API_KEY='test')
        for value in ['true', 'false', 'obsolete']:
            with patch.dict('os.environ', dict(env, BOT_ENABLED=value), clear=True), patch('bot.load_dotenv'):
                self.assertFalse(Config.from_env().enabled)

    async def test_owner_dm_commands_and_idempotency(self):
        await self.command('pause', uid=99)
        self.assertTrue(self.bot.automatic_replies_enabled)
        await self.command('pause extra')
        self.assertTrue(self.bot.automatic_replies_enabled)
        await self.command('pause')
        self.assertFalse(self.bot.automatic_replies_enabled)
        await self.command('pause')
        self.assertIn('Already paused', self.dm.send.await_args.args[0])
        await self.command('status')
        self.assertIn('mode=paused', self.dm.send.await_args.args[0])
        await self.command('resume')
        self.assertTrue(self.bot.automatic_replies_enabled)
        deadline = self.bot.automatic_ready_at
        await self.command('resume')
        self.assertEqual(self.bot.automatic_ready_at, deadline)
        self.assertIn('Already running', self.dm.send.await_args.args[0])
        await self.command('help')
        self.assertIn('pause', self.dm.send.await_args.args[0])
        self.assertFalse(hasattr(self.bot, 'tree'))

    async def test_pause_finishes_current_public_reply_but_not_next_in_scan(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def answer(*args, **kwargs):
            entered.set()
            await release.wait()
            return 'current reply'

        self.model.answer.side_effect = answer
        self.channel.messages.append(message(101))
        scan = asyncio.create_task(self.bot.scan())
        await entered.wait()
        pause = asyncio.create_task(self.command('pause'))
        await asyncio.sleep(0)
        self.assertTrue(self.bot.pause_requested)
        self.assertFalse(pause.done())
        self.dm.send.assert_not_awaited()
        release.set()
        await asyncio.gather(scan, pause)
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.state.last_seen_message_id, 100)
        self.assertIn('Paused.', self.dm.send.await_args.args[0])
        await self.command('resume')
        await self.bot.scan()
        self.model.answer.assert_awaited_once()
        self.bot.automatic_ready_at = 0
        await self.bot.scan()
        self.assertEqual(self.state.last_seen_message_id, 101)
        self.assertEqual(self.model.answer.await_count, 2)

    async def test_pause_during_context_read_does_not_start_generation(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def context(*args):
            entered.set()
            await release.wait()
            return []

        self.bot.context = context
        scan = asyncio.create_task(self.bot.scan())
        await entered.wait()
        pause = asyncio.create_task(self.command('pause'))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(scan, pause)
        self.model.answer.assert_not_awaited()
        self.assertEqual(self.state.last_seen_message_id, 99)

    async def test_public_backlog_cap_and_first_launch_baseline(self):
        self.state.save(False, None)
        self.bot.pause_requested = True
        await self.bot.scan()
        self.assertEqual(self.state.last_seen_message_id, 100)
        self.channel.messages.extend([message(101), message(102)])
        await self.bot.scan()
        self.assertEqual(self.state.last_seen_message_id, 100)
        self.bot.config = replace(self.bot.config, max_responses=1)
        await self.command('resume')
        self.bot.automatic_ready_at = 0
        await self.bot.scan()
        self.model.answer.assert_awaited_once()
        self.assertEqual(self.channel.send.await_args.kwargs['reference'].id, 102)

    async def test_pause_and_resume_failed_writes_stay_fail_closed(self):
        with patch.object(self.state, 'save', side_effect=OSError()):
            await self.command('pause')
        self.assertFalse(self.bot.automatic_replies_enabled)
        self.assertTrue(self.state.enabled)
        await self.command('pause')  # retry persists the pause despite the closed gate
        self.assertFalse(self.state.enabled)
        with patch.object(self.state, 'save', side_effect=OSError()):
            await self.command('resume')
        self.assertFalse(self.bot.automatic_replies_enabled)
        self.bot.automatic_ready_at = 0
        await self.bot.scan()
        self.model.answer.assert_not_awaited()

    async def test_manual_posts_work_while_paused(self):
        await self.command('pause')
        self.channel.fetch_message = AsyncMock(return_value=self.channel.messages[0])
        await self.command('say bring up weekend plans')
        await self.command('reply https://discord.com/channels/10/20/100 ask why')
        self.assertEqual(self.channel.send.await_count, 2)
        self.assertFalse(self.bot.automatic_replies_enabled)

    async def test_feedback_collects_and_reports_but_waits_for_resume_delay(self):
        service = self.feedback()
        await self.command('pause')
        await self.command('resume', uid=40)  # participant text remains feedback
        self.assertFalse(self.bot.automatic_replies_enabled)
        self.owner_dm.send.assert_awaited_once()
        self.now = 1100
        await service.tick()
        await service.respond(40)
        self.model.feedback_answer.assert_not_awaited()
        self.participant_dm.send.assert_not_awaited()
        await self.command('resume')
        await service.tick()
        self.model.feedback_answer.assert_not_awaited()
        self.now = 1130
        await service.tick()
        await asyncio.gather(*service.tasks.values())
        self.participant_dm.send.assert_awaited_once()
        self.assertIn('resume', str(self.model.feedback_answer.await_args))

    async def test_paused_feedback_suppresses_nontext_and_failed_save_notices(self):
        service = self.feedback()
        await self.command('pause')
        self.dm.send.reset_mock()
        await self.command('', uid=40)
        self.dm.send.assert_not_awaited()
        with patch.object(service.store, 'accept', side_effect=OSError()):
            await self.command('a suggestion', uid=40)
        self.dm.send.assert_not_awaited()
        self.model.feedback_answer.assert_not_awaited()

    async def test_pause_waits_for_current_feedback_response(self):
        service = self.feedback()
        await self.command('some feedback', uid=40)
        entered, release = asyncio.Event(), asyncio.Event()

        async def answer(*args):
            entered.set()
            await release.wait()
            return 'current feedback reply'

        self.model.feedback_answer.side_effect = answer
        self.now = 1030
        await service.tick()
        await entered.wait()
        pause = asyncio.create_task(self.command('pause'))
        await asyncio.sleep(0)
        self.assertFalse(pause.done())
        release.set()
        await pause
        self.participant_dm.send.assert_awaited_once()
        self.assertIn('Paused.', self.dm.send.await_args.args[0])
        await self.command('new feedback', uid=40)
        self.now = 1200
        await service.tick()
        self.model.feedback_answer.assert_awaited_once()

    async def test_legacy_command_cleanup_only_clears_configured_guild_and_global(self):
        await self.bot.clear_legacy_commands()
        self.bot.http.bulk_upsert_global_commands.assert_awaited_once_with(self.bot.application_id, [])
        self.bot.http.bulk_upsert_guild_commands.assert_awaited_once_with(
            self.bot.application_id, 10, [])
        await self.bot.clear_legacy_commands()
        self.bot.http.bulk_upsert_global_commands.assert_awaited_once()
        self.bot.http.bulk_upsert_guild_commands.assert_awaited_once()

    async def test_command_cleanup_retries_on_reconnect_without_changing_mode(self):
        self.bot.http.bulk_upsert_guild_commands.side_effect = RuntimeError('failure')
        await self.bot.on_ready()
        self.assertTrue(self.bot.automatic_replies_enabled)
        self.assertEqual(self.bot.commands_cleared, {'global'})
        await self.command('pause')
        self.bot.http.bulk_upsert_guild_commands.side_effect = None
        await self.bot.on_resumed()
        self.assertFalse(self.bot.automatic_replies_enabled)
        self.assertEqual(self.bot.commands_cleared, {'global', 'guild'})
        self.bot.http.bulk_upsert_global_commands.assert_awaited_once()
        self.assertEqual(self.bot.http.bulk_upsert_guild_commands.await_count, 2)

    async def test_heartbeat_timeout_uses_pacing_and_resume_resets_the_wait(self):
        delays = []

        async def wait_for(awaitable, timeout):
            awaitable.close()
            delays.append(timeout)
            if len(delays) == 1:
                await self.bot.set_mode(True)
                return True
            raise asyncio.TimeoutError()

        await self.bot.set_mode(False)
        with patch('bot.time.monotonic', return_value=1000), patch('bot.asyncio.wait_for', side_effect=wait_for):
            await self.bot.wait_for_scan()
        self.assertEqual(delays, [self.bot.config.max_interval, 30])
