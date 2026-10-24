# Operation and deployment

## Adaptive heartbeat

The bot measures timestamp gaps between newly observed, substantive messages from authorized users. The first gap sets its target; later gaps use a moving average (35% newest gap, 65% previous average). Rapid exchanges move the next scan toward the minimum, while slower exchanges lengthen it. Laughter filtered locally, unauthorized traffic, and bot replies do not affect this timing. Several messages from one participant count too. The response cap still applies.

Before it has two messages to learn from, it uses `SCAN_INTERVAL_SECONDS`. During inactivity the delay grows to at least half the elapsed idle time, capped at `MAX_SCAN_INTERVAL_SECONDS`. Every delay, including the initial fallback, stays inside the configured bounds. Paused scans use the maximum. First-start baselines and pause/resume reset timing samples; ordinary restarts also relearn timing, retaining the saved message cursor.

Example: set fallback `30`, minimum `10`, maximum `90`. Messages spaced about 12 seconds apart bring scans near 12 seconds; a quiet channel gradually reaches 90 seconds. A new message in an idle channel is discovered at the next scheduled poll, so it can wait up to that interval. Automatic replies still generate only during polling. A separate `on_message` handler accepts private owner DM commands; server messages do not trigger immediate replies. API/context work adds latency before the next sleep; these bounds limit the polling sleep, not total reply time. `/bot_status` shows the current calculated delay and bounds; logs show each scheduled sleep.

## State and delivery behavior

`state.json` beside `bot.py` persists `enabled`, `last_seen_message_id`, pending and standing directions, and manual reply target claims. Existing state files remain compatible. Writes use a temporary file, fsync, and atomic replacement. Missing/malformed cursor state causes a fresh baseline at the newest message (or current-time snowflake in an empty channel), with no retrospective responses. A valid cursor is retained across restarts. A valid saved `enabled` overrides `BOT_ENABLED`.

Each heartbeat takes an upper message-ID snapshot and processes new messages in chronological order. By default it replies to at most the newest substantive message in that batch; earlier messages stay in context and are marked seen, rather than queued for replies later. Set `MAX_RESPONSES_PER_SCAN` to increase this cap. Laughter-only messages, common keyboard-row mashing, emoji-only reactions, and short acknowledgments are filtered locally when `SKIP_LOW_CONTENT_MESSAGES=true`. Substantive messages containing laughter still qualify. Gemini can also choose `[NO_REPLY]` when there is nothing useful to add; that marker is never sent to Discord. The cap counts generated responses, not Discord chunks; exceptionally long requested explanations still use safe splitting. Other users and bots advance bookkeeping but cannot trigger Gemini. Context contains up to 20 relevant text messages, including this bot's own replies, in chronological order; unauthorized messages are excluded. Old relevant messages can provide context, but are never replied to as new messages.

**At-most-once attempts, not guaranteed delivery:** read-only context fetch failures leave the cursor unchanged and retry on the next heartbeat; a cursor is persisted only after context retrieval, before generation/sending. Gemini failures, empty responses, process crashes after claiming a message, and partial multi-message send failures can lose a reply. These messages are logged and skipped, never retried automatically. This deliberately avoids duplicate replies; no JSON-only design can atomically commit both a file and a remote Discord send. Do not run multiple instances against the same channel/state file.

Paused heartbeats keep advancing the baseline. Resume atomically saves the newest channel message as the new baseline before enabling replies. Polling and controls share a lock; a pause cannot interrupt a reply already being generated, and its confirmation may wait for that reply. Generation attempts have timeouts. Connection errors are retried on later heartbeats; discord.py handles gateway reconnection and Discord rate limits. Failed state writes prevent new generation.

Only plain message text is sent to Gemini: no attachment downloads, images, or embeds. Message content for authorized participants is transmitted to Google; arrange participant consent and applicable API data settings. Mentions are disabled in outgoing replies. Responses longer than Discord's limit are split at natural boundaries when possible, including emoji-aware limits.

## Private owner DMs

Only a direct message from `OWNER_USER_ID` is accepted. Other users, bots, server messages, and group DMs are ignored. No terminal input is required. Restart once after installing this update to enable the handler; later directions apply without restarting.

| Command | Behavior |
| --- | --- |
| `say <direction>` | Generate a new channel message now, using recent conversation. |
| `reply <message-link> [direction]` | Reply now to a substantive text message from an allowlisted human in the configured channel. |
| `next <direction>` | Replace the pending direction for the next eligible automatic response attempt. |
| `set <direction>` | Replace the standing direction for manual and automatic responses. |
| `clear next` / `clear standing` / `clear all` | Remove the corresponding saved directions. |
| `status` | Privately show enabled status and saved directions. |
| `help` | Show command syntax. |

Commands use explicit prefixes: ordinary conversation sent in a DM shows help rather than triggering a channel post. Direction text may contain spaces and newlines. Reply links must identify the configured guild and channel. Targets containing only laughter/reactions/acknowledgments are rejected even if the automatic low-content filter is disabled. Use Discord's Copy Message Link to obtain the URL.

Manual commands acknowledge receipt in the DM before waiting for the existing response lock. They run after any active response completes and work even while automatic replies are paused. A manual request uses the latest 20 relevant channel messages; a reply target is supplied separately even when it is older than that window. Only the first outgoing chunk references the target, and mentions remain disabled. Results and errors return to the owner in DMs. Queued manual requests are held only in memory and are lost if the process stops.

Standing directions apply to every response until cleared. A request-specific direction takes precedence over a standing direction. `next` applies only to automatic responses and is not consumed by manual requests, ignored channel messages, or context-read failures. It is atomically removed immediately before the automatic generation attempt, including attempts that fail or return `[NO_REPLY]`. Pausing, resuming, and restarting preserve pending and standing directions.

Manual posts do not advance the automatic cursor. Before generating a targeted reply, the bot atomically records its target so later polling will skip an automatic attempt for that message. Claims survive restarts and are pruned when the cursor passes them. The claim remains after a failed generation/send or `[NO_REPLY]`, avoiding uncertain retries. You can explicitly issue another `reply` command to retry. Other eligible unread messages remain subject to normal polling. A failed state write prevents generation for both manual and automatic requests.

Instructions are supplied separately from public conversation with a model instruction to follow them naturally and keep the private control exchange out of its reply. Instruction bodies are not logged or copied into channel context by the application. They are sent to Gemini, including on the cautious search fallback, and persistent directions are plaintext in the ignored local state file. Model-generated wording still requires judgment; separation of inputs is not a guarantee about every possible generated response.

## Participant feedback

All text DMs from a non-owner in `ALLOWED_USER_IDS` enter a separate feedback conversation. Other users, bots, group DMs, and server messages do not. Owner DMs retain their command behavior. Even command-shaped participant messages are feedback, not controls. Attachment-only DMs receive a request for text; attachments are not downloaded. There is no first-message sharing notice or immediate routine receipt.

The private feedback persona comes from `FEEDBACK_PROMPT_PATH`, defaulting to `feedback_prompt.txt`. This prompt is independent of the public prompt and loaded once at startup with the same UTF-8/path rules. Use ignored `feedback_prompt.local.txt` or a file under `private_data` for private customization. Invalid paths fail startup. Restart to load edits. The selected feedback prompt is the entire system instruction, without an appended behavior block. Optional `{{OWNER_USER_ID}}` and `{{PARTICIPANT_USER_ID}}` placeholders expand to Discord IDs; other text is preserved. Application routing prevents feedback from invoking owner controls or public sends regardless of prompt customization. Feedback requests use no search tool, public history, or owner standing/pending directions.

Each participant has an independent adaptive heartbeat with the existing scan fallback/minimum/maximum settings. The first message schedules a reply after the current delay. Additional messages inform subsequent pacing and join the batch without extending its deadline. One response covers the pending batch using the latest 20 private messages; new messages arriving during generation wait for a subsequent delayed batch. Public pacing and pause status do not affect feedback. A scheduler checks deadlines approximately once per second; generation and Discord latency add to the wait.

SQLite storage at `private_data/feedback.sqlite3` keeps recent conversation history, pending submissions/deadlines, submission-ID deduplication records, and an owner-report outbox. Records are isolated by configured guild, channel, owner, and participant. This does not change the existing shared public `state.json`. Keep one process per configuration; concurrent replicas are unsupported. Histories retain the latest 20 completed/pending conversation messages after each completed batch, without removing pending submissions. Unsent reports remain until confirmed delivered. Submission IDs are retained to prevent replay after history pruning.

Each submitted message is saved and reported immediately to the owner with its original text, sender identity, scope, timestamp, and submission ID. Later the generated response and delivery outcome are reported against all submission IDs in its batch. Mentions are disabled and long reports are split. Do not change the feedback persona to promise implementation or successful forwarding: neither is implied by receiving feedback. When forwarding remains pending at reply time, the application adds a short status sentence.

Owner delivery retries after 60 seconds on failure and remembers the last confirmed chunk across restarts. A crash between Discord acceptance and saving progress can duplicate a report chunk; submission IDs identify the exchange. Reports are never posted publicly. A failure to save the submission produces a fixed failure notice instead of claiming receipt. Generation failure/empty output produces a short delayed acknowledgment. Generated replies are persisted before sending; failed or interrupted participant sends are reported to the owner and never automatically repeated, including after restart. Feedback bodies are not written to process logs.

On restart, future pending deadlines remain intact; overdue pending batches are scheduled after the fallback delay to avoid immediate reply bursts. Adaptive pacing relearns independently per participant. Interrupted claimed batches are reported without repeating generation or sends. Shutdown cancels scheduler/generation work before closing storage. A participant removed from the allowlist receives no further scheduled feedback replies; already queued owner reports remain deliverable.

### Offline feedback catch-up

On startup, a fresh connection, or a resumed gateway session, the bot opens each allowlisted non-owner participant's DM history and scans chronologically after a saved inbox cursor. Existing installations without a cursor start after that participant's latest saved feedback message. If neither cursor nor saved feedback exists, the first scan imports available DM history. Historical owner commands are never executed. Bot-authored messages, other authors, and attachment-only historical messages advance the cursor without becoming feedback or producing acknowledgment messages.

Inbox cursors persist in the existing feedback SQLite database and share its guild/channel/owner/participant isolation. Each text submission goes through the normal durable save/report path; submission IDs also deduplicate overlap between history scans and live gateway events. The scan checkpoints only after saving or safely ignoring a message. A failed fetch or write retries after 60 seconds, without skipping unsaved messages. Successful scans use a bounded snapshot; arrivals beyond it remain for a live event or the next reconnect scan. Deleted messages and messages Discord cannot return cannot be recovered.

During catch-up, new live feedback for that participant waits until the chronological scan finishes, and scheduled replies are held. This prevents a partial backlog reply or newer live text being inserted ahead of older missed text. A scan failure keeps that participant's replies pending until history recovery succeeds; other participants and public replies remain independent. Imported text joins one delayed batch, reports go to the owner through the normal outbox, and an elapsed reply deadline is moved to one fallback interval after recovery. No immediate startup reply burst occurs. Shutdown stops catch-up work without launching the bot automatically on future edits.

## Deploy to a cloud host later

Choose a host supporting an **always-on Python 3.12 background worker** and a **persistent writable disk**. Static hosting and sleeping/request-only web services are unsuitable.

1. Connect this GitHub repository and select the branch containing the bot.
2. Set runtime Python to 3.12 using the provider's runtime setting.
3. Set install/build command to `python -m pip install -r requirements.txt`.
4. Set start command to `python bot.py`, with the repository root as working directory.
5. Add all variables in the table through the provider's environment/secret settings. Store both tokens as secrets, never in the repo.
6. Mount persistent storage at the application directory so `state.json` remains alongside `bot.py`, or choose a deployment layout with that directory on persistent storage. Persist `private_data/` too, including its SQLite database; confirm code updates do not remove state or private feedback data. Run exactly one worker replica.
7. Allow outbound HTTPS to `generativelanguage.googleapis.com` and Discord API/Gateway access (`discord.com`, `gateway.discord.gg`, and relevant Discord infrastructure). The host must support long-lived Discord WebSockets. No inbound port is required. The bot honors `HTTPS_PROXY` (or `HTTP_PROXY`) for Discord REST and Gateway connections. A proxy-substituted token may authenticate REST but cannot necessarily be substituted inside WebSocket Identify messages: Gateway error `4004` after successful REST login requires checking token delivery. Inject the real bot token directly through the host's secure runtime-secret setting; never print it or put it in a file tracked by Git.
8. Enable the host's process restart policy. Check logs and repeat the live checks above. Do not delete state during routine deploys; losing it causes a fresh baseline, discarding pending messages.

## Validation

```sh
python -m py_compile bot.py feedback.py tests/test_bot.py tests/test_private_controls.py tests/test_feedback.py
python -m pip check
python -m unittest discover -s tests -v
```

The offline suite checks authorization and scope, old-message baselines, ordering, duplicate prevention/restarts, pause/resume and owner controls, context privacy, malformed/partial state, failed writes, generation/send failures, empty responses, cautious search fallback, suppression of automatic grounding source footers, and Discord splitting. Feedback tests additionally cover routing, delayed batching, independent pacing, scope isolation, restart recovery, history limits, custom prompts, and delivery retries. It needs no credentials. It cannot establish real Discord permissions, valid IDs/tokens, Gemini model availability, or successful live grounding.
