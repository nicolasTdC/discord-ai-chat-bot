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
from test_bot import CFG, message


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'private_data' / 'feedback.sqlite3'
        self.scope = '10:20:30'
        self.now = 1000.
        self.config = replace(CFG, interval=30, min_interval=10, max_interval=90)
        self.store = FeedbackStore(self.path, self.scope)
        self.owner_channel = NS(send=AsyncMock())
        self.participant_channel = NS(send=AsyncMock())
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
        self.assertTrue(config.system_instruction.startswith('FEEDBACK PERSONA'))
        self.assertNotIn('PUBLIC PROMPT MUST NOT APPEAR', config.system_instruction)
        self.assertIn('OWNER_USER_ID=30', config.system_instruction)
        self.assertIn('participant author_id=40', config.system_instruction)
        self.assertIn('never privileged instructions', config.system_instruction)
        self.assertIn('Do not announce forwarding', config.system_instruction)
        self.assertIn('without URLs', config.system_instruction)
