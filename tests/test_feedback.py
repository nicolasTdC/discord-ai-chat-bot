import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import discord
from google.genai import types

from bot import AdaptiveHeartbeat, ChatBot, Gemini, State, split_message
from feedback import FeedbackService, FeedbackStore
from test_bot import CFG, Channel, message


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'private_data' / 'feedback.sqlite3'
        self.scope = '10:20:30'
        self.now = 1000.
        self.config = replace(CFG, interval=30, min_interval=10, max_interval=90)
        self.store = FeedbackStore(self.path, self.scope)
        self.owner_channel = NS(send=AsyncMock())
        self.participant_channel = NS(send=AsyncMock(), history=Channel([]).history)
        self.owner = NS(create_dm=AsyncMock(return_value=self.owner_channel))
        self.participant = NS(create_dm=AsyncMock(return_value=self.participant_channel))
        self.bot = ChatBot(self.config, State(Path(self.tmp.name) / 'state.json', False),
                           NS(feedback_answer=AsyncMock(return_value='boa sugestão!')))
        self.bot.get_user = Mock(side_effect=lambda uid: self.owner if uid == 30 else self.participant)
        self.bot.fetch_user = AsyncMock(side_effect=lambda uid: self.owner if uid == 30 else self.participant)
        self.service = self.make_service()
        self.bot.feedback = self.service
        self.dm = Mock(spec=discord.DMChannel)
        self.dm.send = AsyncMock()

    def make_service(self):
        return FeedbackService(self.bot, self.store, lambda: AdaptiveHeartbeat(self.config),
                               split_message, clock=lambda: self.now)

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    def dm_message(self, mid=100, uid=40, text='could improve replies'):
        m = message(mid, uid=uid, text=text, guild=None)
        m.channel = self.dm
        return m

    async def receive(self, mid=100, uid=40, text='could improve replies'):
        await self.bot.on_message(self.dm_message(mid, uid, text))

    async def run_due(self):
        await self.service.tick()
        await asyncio.gather(*self.service.tasks.values())
        await self.service.flush_reports()

    def deadline(self, uid=40):
        return self.store.db.execute('SELECT deadline FROM conversations WHERE scope=? AND uid=?',
                                     (self.scope, uid)).fetchone()['deadline']

    async def test_delayed_combined_reply_and_immediate_verbatim_owner_report(self):
        await self.receive(text='next please be warmer')
        self.assertEqual(self.deadline(), 1030)
        self.dm.send.assert_not_awaited()
        self.participant_channel.send.assert_not_awaited()
        self.assertIn('next please be warmer', self.owner_channel.send.await_args.args[0])
        self.assertIn('user=40', self.owner_channel.send.await_args.args[0])
        self.now = 1005
        await self.receive(101, text='and shorter too')
        self.assertEqual(self.deadline(), 1030)
        await self.run_due()
        self.bot.gemini.feedback_answer.assert_not_awaited()
        self.now = 1030
        await self.run_due()
        call = self.bot.gemini.feedback_answer.await_args
        self.assertEqual(call.args[1], 40)
        self.assertEqual([x['text'] for x in call.args[0]], ['next please be warmer', 'and shorter too'])
        self.participant_channel.send.assert_awaited_once()
        self.assertEqual(self.participant_channel.send.await_args.args[0], 'boa sugestão!')
        report = self.owner_channel.send.await_args.args[0]
        self.assertIn('submissions=100,101', report)
        self.assertIn('boa sugestão!', report)
        self.assertIn('delivered', report)
        self.assertFalse(self.bot.state.enabled)  # pause does not block feedback
        self.assertIsNone(self.bot.state.pending_direction)

    async def test_authorization_and_owner_routing(self):
        await self.receive(uid=99)
        for overrides in [dict(guild=NS(id=10)), dict(author=NS(id=40, bot=True)),
                          dict(channel=Mock(spec=discord.GroupChannel))]:
            m = self.dm_message()
            for key, value in overrides.items():
                setattr(m, key, value)
            await self.bot.on_message(m)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)
        self.bot.owner_dm = AsyncMock()
        await self.receive(uid=30, text='status')
        self.bot.owner_dm.assert_awaited_once()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)

    async def test_attachment_only_requests_text_without_report(self):
        await self.receive(text=' ')
        self.assertIn('texto', self.dm.send.await_args.args[0])
        self.owner_channel.send.assert_not_awaited()

    async def test_duplicate_event_does_not_repeat_report_or_generation(self):
        await self.receive()
        await self.receive()
        self.assertEqual(self.owner_channel.send.await_count, 1)
        self.now = 1030
        await self.run_due()
        await self.receive()
        self.assertEqual(self.owner_channel.send.await_count, 2)
        self.bot.gemini.feedback_answer.assert_awaited_once()

    async def test_owner_failure_saved_and_retried_after_sixty_seconds(self):
        self.owner_channel.send.side_effect = RuntimeError('private payload')
        await self.receive()
        self.assertEqual(len(self.store.reports(1059)), 0)
        self.now = 1030
        await self.run_due()
        self.assertIn('pendente', self.participant_channel.send.await_args.args[0])
        self.owner_channel.send.side_effect = None
        self.now = 1060
        await self.service.flush_reports()
        self.assertTrue(self.store.pending_reports(40))  # response report failed at t=1030
        self.assertIn('Participant feedback', self.owner_channel.send.await_args.args[0])
        self.now = 1090
        await self.service.flush_reports()
        self.assertFalse(self.store.pending_reports(40))

    async def test_split_report_resumes_after_last_confirmed_chunk(self):
        calls = 0

        async def send(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('fail second chunk')

        self.owner_channel.send.side_effect = send
        await self.receive(text='😀' * 2500)
        self.assertEqual(self.store.reports(1060)[0]['next_chunk'], 1)
        self.owner_channel.send.side_effect = None
        self.now = 1060
        await self.service.flush_reports()
        self.assertFalse(self.store.pending_reports(40))
        self.assertEqual(self.owner_channel.send.await_count, 4)
        for call in self.owner_channel.send.await_args_list:
            self.assertLessEqual(len(call.args[0].encode('utf-16-le')) // 2, 2000)
            self.assertEqual(call.kwargs['allowed_mentions'].to_dict()['parse'], [])

    async def test_new_messages_during_generation_wait_for_next_batch(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def answer(*args):
            started.set()
            await release.wait()
            return 'reply'

        self.bot.gemini.feedback_answer.side_effect = answer
        await self.receive()
        self.now = 1030
        await self.service.tick()
        await started.wait()
        await self.receive(101, text='a second thought')
        self.assertGreater(self.deadline(), self.now)
        release.set()
        await asyncio.gather(*self.service.tasks.values())
        self.assertEqual(len(self.bot.gemini.feedback_answer.await_args.args[0]), 1)
        self.now = self.deadline()
        await self.run_due()
        self.assertEqual(self.bot.gemini.feedback_answer.await_count, 2)
        self.assertIn('a second thought', str(self.bot.gemini.feedback_answer.await_args.args[0]))

    async def test_independent_participants_and_public_pacing(self):
        self.bot.config = replace(self.config, allowed_ids=frozenset({40, 41}))
        await self.receive()
        self.now = 1005
        await self.receive(101, uid=41, text='other person')
        self.assertEqual(self.deadline(40), 1030)
        self.assertEqual(self.deadline(41), 1035)
        self.assertIsNone(self.bot.pacing.last_id)
        self.now = 1030
        await self.run_due()
        self.assertEqual(self.bot.gemini.feedback_answer.await_args.args[1], 40)
        self.assertNotIn('other person', str(self.bot.gemini.feedback_answer.await_args))

    async def test_slow_owner_delivery_does_not_block_reply_scheduler(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_owner_send(*args, **kwargs):
            entered.set()
            await release.wait()

        self.owner_channel.send.side_effect = slow_owner_send
        incoming = asyncio.create_task(self.receive())
        await entered.wait()
        self.now = 1030
        await self.service.tick()
        await asyncio.gather(*self.service.tasks.values())
        self.participant_channel.send.assert_awaited_once()
        self.assertFalse(incoming.done())
        release.set()
        await incoming

    async def test_submission_transaction_rolls_back_on_report_save_failure(self):
        with patch.object(self.store, 'enqueue', side_effect=OSError()):
            await self.receive()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 0)
        await self.receive()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 1)

    async def test_failed_save_no_generation_or_false_receipt(self):
        with patch.object(self.store, 'accept', side_effect=OSError('private content')):
            await self.receive()
        self.owner_channel.send.assert_not_awaited()
        self.assertIn('Não consegui salvar', self.dm.send.await_args.args[0])
        self.now = 1100
        await self.run_due()
        self.bot.gemini.feedback_answer.assert_not_awaited()

    async def test_generation_failure_and_no_reply_get_delayed_ack(self):
        self.bot.gemini.feedback_answer.side_effect = RuntimeError('private content')
        await self.receive()
        self.now = 1030
        await self.run_due()
        self.assertIn('obrigada', self.participant_channel.send.await_args.args[0])
        self.bot.gemini.feedback_answer.side_effect = None
        self.bot.gemini.feedback_answer.return_value = None
        await self.receive(101)
        self.now = self.deadline()
        await self.run_due()
        self.assertEqual(self.participant_channel.send.await_count, 2)

    async def test_failed_participant_send_not_retried_and_reported(self):
        self.participant_channel.send.side_effect = RuntimeError('private content')
        await self.receive()
        self.now = 1030
        await self.run_due()
        self.assertIn('failed; not resent', self.owner_channel.send.await_args.args[0])
        self.now = 1200
        await self.run_due()
        self.participant_channel.send.assert_awaited_once()

    async def test_prepare_failure_prevents_participant_send(self):
        await self.receive()
        self.now = 1030
        with patch.object(self.store, 'prepare', side_effect=OSError()):
            await self.run_due()
        self.participant_channel.send.assert_not_awaited()
        self.assertIn('processing failed', self.owner_channel.send.await_args.args[0])

    async def test_history_limit_and_dedupe_after_history_pruning(self):
        for i in range(30):
            await self.receive(100 + i, text=f'suggestion {i}')
        self.now = 1030
        await self.run_due()
        self.assertEqual(len(self.bot.gemini.feedback_answer.await_args.args[0]), 20)
        count = self.store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        self.assertEqual(count, 20)
        reports_before = self.owner_channel.send.await_count
        await self.receive(100)
        self.assertEqual(self.owner_channel.send.await_count, reports_before)

    async def test_restart_restores_future_deadlines_and_scopes(self):
        await self.receive()
        self.store.close()
        self.store = FeedbackStore(self.path, self.scope)
        self.service.store = self.store
        self.now = 1010
        self.store.recover(self.now, 30, self.service.response_report)
        self.assertEqual(self.deadline(), 1030)
        other = FeedbackStore(self.path, '99:88:77')
        self.assertEqual(other.due_users(2000), [])
        self.assertEqual(other.reports(2000), [])
        self.assertIsNone(other.claim(40))
        other.close()
        self.now = 1100
        self.store.recover(self.now, 30, self.service.response_report)
        self.assertEqual(self.deadline(), 1130)

    async def test_interrupted_prepared_reply_recovered_as_report_not_resend(self):
        await self.receive()
        batch = self.store.claim(40)
        self.store.prepare(batch, 'unsent reply')
        self.store.close()
        self.store = FeedbackStore(self.path, self.scope)
        self.service.store = self.store
        self.store.recover(1100, 30, self.service.response_report)
        self.now = 1130
        await self.run_due()
        self.participant_channel.send.assert_not_awaited()
        self.bot.gemini.feedback_answer.assert_not_awaited()
        self.assertIn('interrupted', self.owner_channel.send.await_args.args[0])
        self.assertIn('unsent reply', self.owner_channel.send.await_args.args[0])

    async def test_start_and_close_cancel_worker(self):
        self.bot.wait_until_ready = AsyncMock()
        self.bot.is_ready = Mock(return_value=True)
        self.service.start()
        await asyncio.sleep(0)
        self.assertIsNotNone(self.service.worker)
        await self.service.close()
        self.assertTrue(self.service.worker.done())
        # Tear down uses a fresh handle because this one was explicitly closed.
        self.store = FeedbackStore(self.path, self.scope)
        self.service = self.make_service()


    def setup_inbox(self, messages):
        self.inbox = Channel(messages)
        self.participant_channel.history = self.inbox.history
        self.bot.is_ready = Mock(return_value=True)

    async def test_catchup_imports_offline_dms_once_with_delayed_combined_reply(self):
        self.setup_inbox([self.dm_message(100, text='first offline suggestion'),
                          self.dm_message(101, text='second offline suggestion')])
        self.service.request_catchup()
        await self.service.catchup_tick()
        self.assertEqual(self.store.inbox_cursor(40), 101)
        self.assertEqual(self.deadline(), 1030)
        await self.service.flush_reports()
        self.assertEqual(self.owner_channel.send.await_count, 2)
        self.participant_channel.send.assert_not_awaited()
        self.service.request_catchup()
        await self.service.catchup_tick()
        await self.service.flush_reports()
        self.assertEqual(self.owner_channel.send.await_count, 2)
        self.now = 1030
        await self.run_due()
        self.assertEqual([m['text'] for m in self.bot.gemini.feedback_answer.await_args.args[0]],
                         ['first offline suggestion', 'second offline suggestion'])
        self.participant_channel.send.assert_awaited_once()

    async def test_catchup_ignores_bot_owner_and_nontext_messages(self):
        bot_message = self.dm_message(102, uid=50, text='bot response')
        bot_message.author.bot = True
        self.setup_inbox([self.dm_message(100), self.dm_message(101, text=''), bot_message,
                          self.dm_message(103, uid=30), self.dm_message(104, uid=99)])
        self.service.request_catchup()
        self.assertNotIn(30, self.service.catchup_pending)
        await self.service.catchup_tick()
        self.assertEqual(self.store.inbox_cursor(40), 104)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 1)
        self.dm.send.assert_not_awaited()
        self.bot.gemini.feedback_answer.assert_not_awaited()

    async def test_catchup_resumes_from_cursor_across_restart(self):
        self.setup_inbox([self.dm_message(100)])
        await self.service.catch_up(40)
        self.store.close()
        self.store = FeedbackStore(self.path, self.scope)
        self.service.store = self.store
        self.inbox.messages.append(self.dm_message(101, text='sent during downtime'))
        self.service.request_catchup()
        await self.service.catchup_tick()
        self.assertEqual(self.store.inbox_cursor(40), 101)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 2)
        await self.service.flush_reports()
        self.assertEqual(self.owner_channel.send.await_count, 2)

    async def test_catchup_migrates_from_latest_saved_feedback(self):
        await self.receive(101, text='already saved')
        self.setup_inbox([self.dm_message(100, text='old pre-feedback chat'),
                          self.dm_message(101, text='already saved'), self.dm_message(102, text='new')])
        self.service.request_catchup()
        await self.service.catchup_tick()
        await self.service.flush_reports()
        self.assertEqual(self.store.inbox_cursor(40), 102)
        self.assertEqual(self.owner_channel.send.await_count, 2)
        self.assertNotIn('old pre-feedback chat', str(self.owner_channel.send.await_args_list))

    async def test_catchup_failed_save_retries_without_skipping_message(self):
        self.setup_inbox([self.dm_message(100), self.dm_message(101), self.dm_message(102)])
        original = self.store.accept

        def fail_second(uid, mid, *args):
            if mid == 101:
                raise OSError('private text must not be logged')
            return original(uid, mid, *args)

        self.service.request_catchup()
        with patch.object(self.store, 'accept', side_effect=fail_second):
            await self.service.catchup_tick()
        self.assertEqual(self.store.inbox_cursor(40), 100)
        self.assertEqual(self.service.catchup_pending[40], 1060)
        self.now = 1030
        await self.service.tick()
        self.bot.gemini.feedback_answer.assert_not_awaited()
        self.now = 1060
        await self.service.catchup_tick()
        self.assertEqual(self.store.inbox_cursor(40), 102)
        self.assertNotIn(40, self.service.catchup_pending)
        self.assertGreater(self.deadline(), self.now)
        await self.service.flush_reports()
        self.assertEqual(self.owner_channel.send.await_count, 3)

    async def test_catchup_history_failure_does_not_advance_or_repeat_saved_reports(self):
        self.setup_inbox([self.dm_message(100), self.dm_message(101)])
        original = self.inbox.history

        async def broken_history(**kwargs):
            async for m in original(**kwargs):
                if kwargs.get('oldest_first') and m.id == 101:
                    raise OSError('history page failed')
                yield m

        self.participant_channel.history = broken_history
        self.service.request_catchup()
        await self.service.catchup_tick()
        self.assertEqual(self.store.inbox_cursor(40), 100)
        self.participant_channel.history = original
        self.now = 1060
        await self.service.catchup_tick()
        self.assertEqual(self.store.inbox_cursor(40), 101)
        await self.service.flush_reports()
        self.assertEqual(self.owner_channel.send.await_count, 2)

    async def test_live_arrival_during_catchup_waits_and_is_deduplicated(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.setup_inbox([self.dm_message(100), self.dm_message(101)])
        original = self.inbox.history

        async def slow_history(**kwargs):
            if kwargs.get('oldest_first'):
                entered.set()
                await release.wait()
            async for m in original(**kwargs):
                yield m

        self.participant_channel.history = slow_history
        self.service.request_catchup()
        catching_up = asyncio.create_task(self.service.catchup_tick())
        await entered.wait()
        live = asyncio.create_task(self.receive(101))
        await asyncio.sleep(0)
        self.assertFalse(live.done())
        self.now = 1040
        await self.service.tick()
        self.bot.gemini.feedback_answer.assert_not_awaited()
        release.set()
        await asyncio.gather(catching_up, live)
        self.assertEqual(self.store.inbox_cursor(40), 101)
        self.assertEqual(self.owner_channel.send.await_count, 2)
        self.assertGreater(self.deadline(), self.now)
        self.now = self.deadline()
        await self.run_due()
        self.bot.gemini.feedback_answer.assert_awaited_once()
        self.assertEqual(len(self.bot.gemini.feedback_answer.await_args.args[0]), 2)

    async def test_new_arrival_after_snapshot_is_saved_without_skipping_on_next_scan(self):
        self.setup_inbox([self.dm_message(100)])
        original = self.inbox.history

        async def growing_history(**kwargs):
            async for m in original(**kwargs):
                yield m
            if kwargs.get('limit') == 1:
                self.inbox.messages.append(self.dm_message(101))

        self.participant_channel.history = growing_history
        self.service.request_catchup()
        await self.service.catchup_tick()
        self.assertEqual(self.store.inbox_cursor(40), 100)
        await self.receive(101)
        self.service.request_catchup()
        await self.service.catchup_tick()
        await self.service.flush_reports()
        self.assertEqual(self.store.inbox_cursor(40), 101)
        self.assertEqual(self.owner_channel.send.await_count, 2)

    async def test_catchup_is_scoped_and_removed_participants_are_not_scanned(self):
        self.setup_inbox([self.dm_message(100)])
        self.store.advance_inbox(40, 100)
        other = FeedbackStore(self.path, 'another:channel:owner')
        self.assertEqual(other.inbox_cursor(40), 0)
        other.close()
        self.service.request_catchup()
        self.bot.config = replace(self.config, allowed_ids=frozenset({30}))
        await self.service.catchup_tick()
        self.assertNotIn(40, self.service.catchup_pending)
        self.participant.create_dm.assert_not_awaited()

    async def test_catchup_handles_more_than_one_history_page(self):
        self.setup_inbox([self.dm_message(i, text=f'offline message {i}') for i in range(100, 251)])
        self.service.request_catchup()
        await self.service.catchup_tick()
        self.assertEqual(self.store.inbox_cursor(40), 250)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 151)
        self.participant_channel.send.assert_not_awaited()

    async def test_failed_checkpoint_does_not_duplicate_reports_on_retry(self):
        self.setup_inbox([self.dm_message(100), self.dm_message(101)])
        self.store.advance_inbox(40, 0)
        self.service.request_catchup()
        with patch.object(self.store, 'advance_inbox', side_effect=OSError()):
            await self.service.catchup_tick()
        self.assertEqual(self.store.inbox_cursor(40), 0)
        self.now = 1060
        await self.service.catchup_tick()
        await self.service.flush_reports()
        self.assertEqual(self.store.inbox_cursor(40), 101)
        self.assertEqual(self.owner_channel.send.await_count, 2)

    async def test_catchup_handles_new_participant_and_history_fetch_failure(self):
        self.setup_inbox([self.dm_message(100)])
        self.bot.get_user.return_value = None
        self.bot.get_user.side_effect = None
        self.participant.create_dm.side_effect = RuntimeError('cannot open history')
        self.service.request_catchup()
        await self.service.catchup_tick()
        self.assertEqual(self.store.inbox_cursor(40), 0)
        self.assertIn(40, self.service.catchup_pending)
        self.participant.create_dm.side_effect = None
        self.now = 1060
        await self.service.catchup_tick()
        self.bot.fetch_user.assert_awaited_with(40)
        self.assertEqual(self.store.inbox_cursor(40), 100)

    async def test_already_scheduled_reply_yields_to_reconnect_catchup(self):
        await self.receive()
        self.service.request_catchup()
        await self.service.respond(40)
        self.bot.gemini.feedback_answer.assert_not_awaited()

    async def test_gateway_resume_requests_catchup_without_owner_replay(self):
        self.setup_inbox([])
        await self.bot.on_resumed()
        self.assertEqual(set(self.service.catchup_pending), {40})
        await self.service.catchup_tick()
        self.assertEqual(self.service.catchup_pending, {})
        self.bot.gemini.feedback_answer.assert_not_awaited()

    async def test_reconnect_during_scan_requests_another_pass(self):
        self.setup_inbox([self.dm_message(100)])
        original = self.inbox.history
        requested = False

        async def reconnect_history(**kwargs):
            nonlocal requested
            async for m in original(**kwargs):
                yield m
            if kwargs.get('oldest_first') and not requested:
                requested = True
                self.inbox.messages.append(self.dm_message(101))
                await self.bot.on_resumed()

        self.participant_channel.history = reconnect_history
        self.service.request_catchup()
        await self.service.catchup_tick()
        self.assertIn(40, self.service.catchup_pending)
        await self.service.catchup_tick()
        self.assertNotIn(40, self.service.catchup_pending)
        self.assertEqual(self.store.inbox_cursor(40), 101)



class FeedbackGeminiTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_feedback_prompt_preserves_contents_and_is_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / 'private.txt'
            content = 'custom feedback persona\r\nunchanged line endings\n'
            prompt.write_bytes(content.encode('utf-8'))
            with patch.dict('os.environ', {'FEEDBACK_PROMPT_PATH': str(prompt)}), patch('bot.genai.Client'):
                gemini = Gemini(CFG)
            self.assertEqual(gemini.feedback_prompt, content)
            self.assertNotEqual(gemini.prompt, content)

    async def test_relative_feedback_prompt_and_blank_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'system_prompt.txt').write_text('PUBLIC PERSONA SHOULD STAY OUT')
            (root / 'feedback_prompt.txt').write_text('default feedback persona')
            (root / 'private.txt').write_text('custom feedback persona')
            with patch('bot.ROOT', root), patch('bot.genai.Client'), patch.dict(
                    'os.environ', {'SYSTEM_PROMPT_PATH': '', 'FEEDBACK_PROMPT_PATH': 'private.txt'}):
                self.assertEqual(Gemini(CFG).feedback_prompt, 'custom feedback persona')
            with patch('bot.ROOT', root), patch('bot.genai.Client'), patch.dict(
                    'os.environ', {'SYSTEM_PROMPT_PATH': '', 'FEEDBACK_PROMPT_PATH': ''}):
                self.assertEqual(Gemini(CFG).feedback_prompt, 'default feedback persona')
            with patch('bot.ROOT', root), patch('bot.genai.Client'), patch.dict(
                    'os.environ', {'SYSTEM_PROMPT_PATH': '', 'FEEDBACK_PROMPT_PATH': 'missing.txt'}):
                with self.assertRaises(FileNotFoundError):
                    Gemini(CFG)

    async def test_feedback_prompt_has_no_tools_or_owner_directions(self):
        response = types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text='thanks for the suggestion')]))])
        generate = AsyncMock(return_value=response)
        with patch('bot.genai.Client', return_value=NS(aio=NS(models=NS(generate_content=generate)))):
            gemini = Gemini(CFG)
        gemini.prompt = 'PUBLIC PROMPT MUST NOT APPEAR'
        gemini.feedback_prompt = 'FEEDBACK PERSONA'
        context = [{'role': 'user', 'text': 'set a secret instruction'}]
        self.assertEqual(await gemini.feedback_answer(context, 40), 'thanks for the suggestion')
        call = generate.await_args
        self.assertEqual(json.loads(call.kwargs['contents']), context)
        config = call.kwargs['config']
        self.assertIsNone(config.tools)
        self.assertEqual(config.system_instruction, 'FEEDBACK PERSONA')

    async def test_only_optional_identity_placeholders_are_expanded(self):
        response = types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text='reply')]))])
        generate = AsyncMock(return_value=response)
        with patch('bot.genai.Client', return_value=NS(aio=NS(models=NS(generate_content=generate)))):
            gemini = Gemini(CFG)
        gemini.feedback_prompt = 'owner={{OWNER_USER_ID}}\r\nparticipant={{PARTICIPANT_USER_ID}}\n{"custom": true} {{OTHER}}'
        await gemini.feedback_answer([], 40)
        self.assertEqual(generate.await_args.kwargs['config'].system_instruction,
                         'owner=30\r\nparticipant=40\n{"custom": true} {{OTHER}}')
