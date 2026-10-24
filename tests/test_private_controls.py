import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import discord
from google.genai import types

from bot import ChatBot, Gemini, State
from test_bot import CFG, Channel, message


class PrivateControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / 'state.json', True)
        self.state.save(True, 99)
        self.gemini = NS(answer=AsyncMock(return_value='natural response'))
        self.bot = ChatBot(CFG, self.state, self.gemini)
        self.bot._connection.user = NS(id=50)
        self.channel = Channel([message(100)])
        self.channel.fetch_message = AsyncMock(return_value=self.channel.messages[0])
        self.bot.channel = AsyncMock(return_value=self.channel)
        self.dm = Mock(spec=discord.DMChannel)
        self.dm.send = AsyncMock()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def command(self, text, **overrides):
        values = dict(content=text, guild=None, channel=self.dm, author=NS(id=30, bot=False))
        values.update(overrides)
        await self.bot.on_message(NS(**values))

    def reload(self):
        self.bot.state = self.state = State(self.state.path, True)

    async def test_only_owner_direct_messages_are_processed(self):
        for overrides in [dict(author=NS(id=40, bot=False)), dict(author=NS(id=30, bot=True)),
                          dict(guild=NS(id=10)), dict(channel=Mock(spec=discord.GroupChannel))]:
            await self.command('say secret direction', **overrides)
        self.gemini.answer.assert_not_awaited()
        self.dm.send.assert_not_awaited()
        await self.command('say secret direction')
        self.gemini.answer.assert_awaited_once()
        self.assertEqual(self.dm.send.await_count, 2)
        self.assertEqual(self.channel.send.await_args.args, ('natural response',))
        self.assertNotIn('secret direction', str(self.gemini.answer.await_args.args[0]))

    async def test_invalid_syntax_does_not_mutate_or_generate(self):
        for text in ['', 'hello', 'say', 'next ', 'set', 'clear', 'clear unknown',
                     'status extra', 'help extra', 'reply',
                     'reply https://discord.com/channels/10/99/100',
                     'reply https://evil.com/channels/10/20/100',
                     'reply https://discord.com/channels/10/20/0']:
            await self.command(text)
        self.gemini.answer.assert_not_awaited()
        self.assertIsNone(self.state.pending_direction)
        self.assertIsNone(self.state.standing_direction)
        self.channel.send.assert_not_awaited()

    async def test_saved_directions_replace_clear_and_survive_restart(self):
        await self.command('next first')
        await self.command('next second')
        await self.command('set standing')
        self.reload()
        self.assertEqual(self.state.pending_direction, 'second')
        self.assertEqual(self.state.standing_direction, 'standing')
        await self.command('status')
        self.assertIn('Next: second', self.dm.send.await_args.args[0])
        await self.command('clear next')
        self.assertIsNone(self.state.pending_direction)
        self.assertEqual(self.state.standing_direction, 'standing')
        await self.command('next another')
        await self.command('clear standing')
        self.assertIsNone(self.state.standing_direction)
        self.assertEqual(self.state.pending_direction, 'another')
        await self.command('set persistent')
        await self.command('clear all')
        self.reload()
        self.assertIsNone(self.state.pending_direction)
        self.assertIsNone(self.state.standing_direction)

    async def test_next_applies_once_and_standing_applies_to_each_response(self):
        await self.command('next one time')
        await self.command('set always')
        self.channel.messages.append(message(101))
        await self.bot.scan()
        calls = self.gemini.answer.await_args_list
        self.assertEqual(calls[0].kwargs['private_instructions']['response_direction'], 'one time')
        self.assertIsNone(calls[1].kwargs['private_instructions']['response_direction'])
        self.assertTrue(all(c.kwargs['private_instructions']['standing_direction'] == 'always' for c in calls))
        self.reload()
        self.assertIsNone(self.state.pending_direction)
        self.assertEqual(self.state.standing_direction, 'always')

    async def test_next_consumed_on_failure_and_no_reply(self):
        for result in [RuntimeError('failed'), None]:
            await self.command('next once')
            self.gemini.answer.side_effect = result if isinstance(result, Exception) else None
            self.gemini.answer.return_value = result
            self.channel.messages.append(message(self.channel.messages[-1].id + 1))
            await self.bot.scan()
            self.reload()
            self.assertIsNone(self.state.pending_direction)
        self.channel.send.assert_not_awaited()

    async def test_context_failure_preserves_next(self):
        await self.command('next retain')
        self.bot.context = AsyncMock(side_effect=OSError())
        await self.bot.scan()
        self.assertEqual(self.state.pending_direction, 'retain')
        self.gemini.answer.assert_not_awaited()

    async def test_manual_posts_while_paused_and_preserves_next(self):
        self.state.save(False, 99, pending_direction='later', standing_direction='standing')
        await self.command('say weekend plans')
        self.assertFalse(self.state.enabled)
        self.assertEqual(self.state.last_seen_message_id, 99)
        self.assertEqual(self.state.pending_direction, 'later')
        directions = self.gemini.answer.await_args.kwargs['private_instructions']
        self.assertEqual(directions['response_direction'], 'weekend plans')
        self.assertEqual(directions['standing_direction'], 'standing')
        self.assertIsNone(self.channel.send.await_args.kwargs['reference'])

    async def test_say_in_empty_channel(self):
        self.channel.messages = []
        await self.command('say start a topic')
        self.assertEqual(self.gemini.answer.await_args.args, ([],))
        self.channel.send.assert_awaited_once()

    async def test_old_target_uses_latest_context_and_explicit_target(self):
        self.channel.messages = [message(i, text=f"message {i}") for i in range(100, 130)]
        await self.command('reply https://discord.com/channels/10/20/100 Ask why')
        call = self.gemini.answer.await_args
        self.assertEqual(len(call.args[0]), 20)
        self.assertEqual(call.args[0][0]['text'], 'message 110')
        self.assertEqual(call.args[0][-1]['text'], 'message 129')
        self.assertEqual(call.kwargs['target']['id'], 100)
        self.assertEqual(call.kwargs['target']['author_id'], 30)
        self.assertEqual(call.kwargs['private_instructions']['response_direction'], 'Ask why')
        self.assertEqual(self.channel.send.await_args.kwargs['reference'].id, 100)
        self.assertEqual(self.state.last_seen_message_id, 99)
        self.reload()
        self.assertIn(100, self.state.manual_reply_ids)
        self.gemini.answer.reset_mock()
        await self.bot.scan()
        self.assertEqual(self.gemini.answer.await_count, CFG.max_responses)
        self.assertNotIn(100, self.state.manual_reply_ids)

    async def test_target_not_automatically_replied_to_after_manual_failure(self):
        self.gemini.answer.side_effect = RuntimeError('private error')
        await self.command('reply https://discord.com/channels/10/20/100')
        self.reload()
        self.gemini.answer.reset_mock()
        await self.bot.scan()
        self.gemini.answer.assert_not_awaited()
        self.assertNotIn(100, self.state.manual_reply_ids)

    async def test_successful_manual_target_skipped_but_other_messages_still_reply(self):
        await self.command('reply https://discord.com/channels/10/20/100')
        self.reload()
        self.gemini.answer.reset_mock()
        self.channel.messages.append(message(101, uid=40))
        await self.bot.scan()
        self.gemini.answer.assert_awaited_once()
        self.assertEqual(self.channel.send.await_args.kwargs['reference'].id, 101)

    async def test_failed_automatic_claim_preserves_pending_direction(self):
        await self.command('next retain')
        with patch.object(self.state, 'save', side_effect=OSError()):
            with self.assertRaises(OSError):
                await self.bot.scan()
        self.assertEqual(self.state.pending_direction, 'retain')
        self.assertEqual(self.state.last_seen_message_id, 99)
        self.gemini.answer.assert_not_awaited()

    async def test_invalid_or_missing_target_does_not_generate(self):
        for target in [message(100, uid=99), message(100, bot=True), message(100, text='😂'),
                       message(100, text=''), message(100, channel=99)]:
            self.channel.fetch_message.return_value = target
            await self.command('reply https://discord.com/channels/10/20/100')
        self.channel.fetch_message.side_effect = OSError('missing')
        await self.command('reply https://discord.com/channels/10/20/100')
        self.gemini.answer.assert_not_awaited()
        self.assertEqual(self.state.manual_reply_ids, set())

    async def test_failed_state_write_prevents_generation_and_changes(self):
        for command in ['say topic', 'reply https://discord.com/channels/10/20/100', 'next private']:
            with patch.object(self.state, 'save', side_effect=OSError('private data')):
                await self.command(command)
        self.gemini.answer.assert_not_awaited()
        self.assertIsNone(self.state.pending_direction)

    async def test_manual_failure_and_no_reply_report_only_in_dm(self):
        self.gemini.answer.return_value = None
        await self.command('say topic')
        self.assertIn('not to post', self.dm.send.await_args.args[0])
        self.gemini.answer.side_effect = RuntimeError('secret payload')
        with self.assertLogs('discord_ai_chat', level='ERROR') as captured:
            await self.command('say secret payload')
        self.assertNotIn('secret payload', str(captured.output))
        self.assertIn('no automatic retry', self.dm.send.await_args.args[0])
        self.channel.send.assert_not_awaited()

    async def test_commands_wait_for_active_reply_after_private_ack(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_answer(*args, **kwargs):
            entered.set()
            await release.wait()
            return 'reply'

        self.gemini.answer.side_effect = slow_answer
        scan = asyncio.create_task(self.bot.scan())
        await entered.wait()
        command = asyncio.create_task(self.command('say topic'))
        await asyncio.sleep(0)
        self.assertIn('queued', self.dm.send.await_args.args[0])
        self.assertEqual(self.gemini.answer.await_count, 1)
        release.set()
        await asyncio.gather(scan, command)
        self.assertEqual(self.gemini.answer.await_count, 2)

    async def test_multichunk_reply_references_only_first_chunk(self):
        self.gemini.answer.return_value = 'x' * 4001
        await self.command('reply https://discord.com/channels/10/20/100')
        calls = self.channel.send.await_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0].kwargs['reference'].id, 100)
        self.assertTrue(all(c.kwargs['reference'] is None for c in calls[1:]))
        self.assertTrue(all(not c.kwargs['mention_author'] for c in calls))


class PrivateStateTests(unittest.TestCase):
    def test_legacy_and_malformed_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text(json.dumps(dict(enabled=False, last_seen_message_id=100,
                                           pending_direction=[], standing_direction=42,
                                           manual_reply_ids=[101, False, -1, '102'])))
            state = State(path, True)
            self.assertFalse(state.enabled)
            self.assertIsNone(state.pending_direction)
            self.assertIsNone(state.standing_direction)
            self.assertEqual(state.manual_reply_ids, {101})
            state.save(True, 101, pending_direction='next')
            self.assertEqual(state.manual_reply_ids, set())
            with patch('pathlib.Path.replace', side_effect=OSError()):
                with self.assertRaises(OSError):
                    state.save(True, 102, pending_direction=None)
            self.assertEqual(state.pending_direction, 'next')
            self.assertEqual(state.last_seen_message_id, 101)


class PrivateGeminiTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_control_and_target_survive_search_fallback(self):
        response = types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text='natural response')]))])
        generate = AsyncMock(side_effect=[RuntimeError(), response])
        with patch('bot.genai.Client', return_value=NS(aio=NS(models=NS(generate_content=generate)))):
            gemini = Gemini(CFG)
        context = [{'author': 'participant', 'text': 'public message'}]
        self.assertEqual(await gemini.answer(context, private_instructions={'response_direction': 'private'},
                                             target={'id': 100, 'text': 'target'}), 'natural response')
        for call in generate.await_args_list:
            self.assertEqual(json.loads(call.kwargs['contents']), context)
            instruction = call.kwargs['config'].system_instruction
            self.assertTrue(instruction.startswith(gemini.prompt))
            self.assertIn('private', instruction)
            self.assertIn('response_target', instruction)
            self.assertIn('Never mention', instruction)
