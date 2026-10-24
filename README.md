# Discord AI Chat Bot

A configurable Discord chat bot built with Python, discord.py, and Google's Gemini SDK. It follows conversations in one server channel and responds to an allowlist of participants using a customizable system prompt.

## Features

- General chat example with no fixed character or debate scenario.
- Google Search grounding with a cautious fallback when search is unavailable.
- Adaptive polling, conversation context, and configurable reply limits.
- Owner-only `/bot_pause`, `/bot_resume`, and `/bot_status` commands.
- Private owner DM commands to post now, reply to a specific message, or save conversation directions.
- Delayed participant feedback DMs, private conversation history, and full owner reports.
- Persistent message cursor, disabled outgoing mentions, and offline tests.

## Quick start

Requires Python 3.12 and a Discord bot token plus a Gemini API key.

```sh
git clone https://github.com/nicolasTdC/discord-ai-chat-bot.git
cd discord-ai-chat-bot
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1` and copy configuration with `Copy-Item .env.example .env`.

Fill in the tokens and numeric Discord IDs in `.env`, enable Message Content Intent for the bot, and invite it with View Channels, Send Messages, and Read Message History permissions. See [setup and configuration](docs/setup.md) for the complete instructions and environment variables.

```sh
python bot.py
```

## Customize the prompt

The included [system_prompt.txt](system_prompt.txt) is a general chat example. To use a personal prompt, save it outside the repository or as ignored `system_prompt.local.txt`, then set `SYSTEM_PROMPT_PATH` in your private `.env`:

```dotenv
SYSTEM_PROMPT_PATH=system_prompt.local.txt
```

Relative paths resolve from the repository root; absolute paths work too. The file is read as UTF-8 without altering its contents. Restart the bot after changing the prompt. Keep personal prompts and credentials out of commits.

## Private instructions while running

DM the bot from the account configured as `OWNER_USER_ID`. Type `help` to see commands.

```text
say Bring up weekend plans naturally
reply https://discord.com/channels/GUILD_ID/CHANNEL_ID/MESSAGE_ID Ask why they disagree
next Ask a follow-up about their last point
set Keep responses short and informal
status
clear next
clear standing
clear all
```

Replace the example reply URL with **Copy Message Link** from a message in the configured channel. `say` and `reply` act immediately after any current response finishes, even while automatic replies are paused. `next` guides one automatic response attempt; `set` stays active until replaced or cleared. Both survive restarts.

Instructions and confirmations stay in your DMs, outside the channel conversation. Directions are sent to Gemini and saved directions are stored in local `state.json`. Only generated conversational text is posted in the channel. See [operation details](docs/operations.md#private-owner-dms) for delivery and failure behavior.

## Participant feedback DMs

Allowlisted participants can DM the bot normally to discuss recommendations. The owner continues to use the owner-command interface. Participant messages are saved and forwarded privately to `OWNER_USER_ID`; the bot gives one natural response after a delay, combining messages sent before its timer fires. Feedback cannot execute owner commands or change bot behavior.

Each participant has independent adaptive pacing using the same scan interval/bounds as public replies. Feedback conversations work while public replies are paused, and histories and pending work survive restarts in ignored `private_data/feedback.sqlite3`. Owner reports include the full exchange; undelivered reports retry every 60 seconds. No first-message notice or immediate routine receipt is sent.

### Customize feedback behavior

Feedback uses its own [feedback_prompt.txt](feedback_prompt.txt), independently from the public `SYSTEM_PROMPT_PATH`. Copy it to ignored `feedback_prompt.local.txt` or a file under `private_data`, then set this in your private environment file:

```dotenv
FEEDBACK_PROMPT_PATH=private_data/feedback_prompt.txt
```

Use this prompt to define the private persona, tone, and approach to recommendations. Relative paths resolve from the repository root; absolute paths and `~` work too. UTF-8 contents and line endings are preserved. Blank/unset uses the included default. Restart after editing the prompt or changing the path; an invalid path fails startup. Feedback uses neither public channel history nor the owner's saved directions. Feedback text is sent to Gemini without web search. Keep personal prompts out of commits.

## Documentation

- [Setup and configuration](docs/setup.md)
- [Operation, delivery behavior, deployment, and tests](docs/operations.md)

Public replies are restricted to allowlisted participants in the configured channel; allowlisted participants can also send private feedback DMs. Their message text is sent to Google's API. Run one instance per channel/state file. The bot skips existing messages on a fresh start and uses at-most-once response attempts; a failed generation or send may lose a reply.

## Tests

```sh
python -m unittest discover -s tests -v
```

Tests run offline without Discord or Gemini credentials.
