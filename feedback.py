"""Durable, scoped participant feedback; independent of public chat state."""
import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger('discord_ai_chat.feedback')


class FeedbackStore:
    def __init__(self, path, scope):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.scope = scope
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS conversations (
                scope TEXT, uid INTEGER, deadline REAL, PRIMARY KEY(scope, uid));
            CREATE TABLE IF NOT EXISTS messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT, uid INTEGER,
                mid TEXT, role TEXT, text TEXT, status TEXT,
                UNIQUE(scope, mid));
            CREATE TABLE IF NOT EXISTS batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT, uid INTEGER,
                ids TEXT, cutoff INTEGER, text TEXT, status TEXT);
            CREATE TABLE IF NOT EXISTS receipts (
                scope TEXT, mid TEXT, PRIMARY KEY(scope, mid));
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT, uid INTEGER, chunks TEXT,
                next_chunk INTEGER DEFAULT 0, retry_at REAL DEFAULT 0);
        ''')

    def enqueue(self, chunks, uid):
        self.db.execute('INSERT INTO reports(scope,uid,chunks) VALUES (?,?,?)',
                        (self.scope, uid, json.dumps(chunks, ensure_ascii=False)))

    def accept(self, uid, mid, text, deadline, chunks):
        with self.db:
            receipt = self.db.execute('INSERT OR IGNORE INTO receipts VALUES (?,?)',
                                      (self.scope, str(mid)))
            if not receipt.rowcount:
                return False
            cursor = self.db.execute('''INSERT OR IGNORE INTO messages
                (scope,uid,mid,role,text,status) VALUES (?,?,?,'user',?,'pending')''',
                (self.scope, uid, str(mid), text))
            if not cursor.rowcount:
                return False
            self.db.execute('''INSERT INTO conversations VALUES (?,?,?)
                ON CONFLICT(scope,uid) DO UPDATE SET deadline=COALESCE(deadline,excluded.deadline)''',
                (self.scope, uid, deadline))
            self.enqueue(chunks, uid)
        return True

    def due_users(self, now):
        return [r['uid'] for r in self.db.execute(
            'SELECT uid FROM conversations WHERE scope=? AND deadline<=?', (self.scope, now))]

    def claim(self, uid):
        with self.db:
            rows = list(self.db.execute('''SELECT seq,mid FROM messages
                WHERE scope=? AND uid=? AND status='pending' ORDER BY seq''', (self.scope, uid)))
            if not rows:
                return None
            cutoff = rows[-1]['seq']
            ids = [r['mid'] for r in rows]
            cursor = self.db.execute('''INSERT INTO batches(scope,uid,ids,cutoff,status)
                VALUES (?,?,?,?,'generating')''', (self.scope, uid, json.dumps(ids), cutoff))
            self.db.execute("UPDATE messages SET status='claimed' WHERE scope=? AND uid=? AND status='pending'",
                            (self.scope, uid))
            self.db.execute('UPDATE conversations SET deadline=NULL WHERE scope=? AND uid=?',
                            (self.scope, uid))
            context = list(self.db.execute('''SELECT role,text FROM messages
                WHERE scope=? AND uid=? AND seq<=? AND (role='user' OR status='done')
                ORDER BY seq DESC LIMIT 20''', (self.scope, uid, cutoff)))
            return dict(id=cursor.lastrowid, uid=uid, ids=ids, cutoff=cutoff,
                        context=[dict(r) for r in reversed(context)])

    def prepare(self, batch, text):
        with self.db:
            self.db.execute("UPDATE batches SET text=?,status='prepared' WHERE id=? AND scope=?",
                            (text, batch['id'], self.scope))
            self.db.execute('''INSERT INTO messages(scope,uid,mid,role,text,status)
                VALUES (?,?,?,'assistant',?,'prepared')''',
                (self.scope, batch['uid'], f"reply:{batch['id']}", text))

    def finish(self, batch, outcome, chunks):
        with self.db:
            self.db.execute("UPDATE batches SET status='done' WHERE id=? AND scope=?",
                            (batch['id'], self.scope))
            self.db.execute("UPDATE messages SET status='done' WHERE scope=? AND uid=? AND seq<=? AND role='user'",
                            (self.scope, batch['uid'], batch['cutoff']))
            self.db.execute('UPDATE messages SET status=? WHERE scope=? AND mid=?',
                            ('done' if outcome == 'delivered' else 'failed', self.scope,
                             f"reply:{batch['id']}"))
            self.enqueue(chunks, batch['uid'])
            # Pending work is never trimmed; undelivered report bodies are independent.
            self.db.execute('''DELETE FROM messages WHERE scope=? AND uid=?
                AND status IN ('done','failed') AND seq NOT IN
                (SELECT seq FROM messages WHERE scope=? AND uid=? ORDER BY seq DESC LIMIT 20)''',
                (self.scope, batch['uid'], self.scope, batch['uid']))
            self.db.execute('DELETE FROM batches WHERE id=? AND scope=?', (batch['id'], self.scope))

    def recover(self, now, fallback, report):
        for row in list(self.db.execute("SELECT * FROM batches WHERE scope=? AND status!='done'", (self.scope,))):
            batch = dict(row)
            batch['ids'] = json.loads(batch['ids'])
            self.finish(batch, 'interrupted', report(batch, batch['text'], 'interrupted; not resent'))
        with self.db:
            self.db.execute('''UPDATE conversations SET deadline=?
                WHERE scope=? AND deadline IS NOT NULL AND deadline<=?''',
                (now + fallback, self.scope, now))

    def pending_reports(self, uid):
        return self.db.execute('SELECT 1 FROM reports WHERE scope=? AND uid=? LIMIT 1',
                               (self.scope, uid)).fetchone() is not None

    def reports(self, now):
        return list(self.db.execute('SELECT * FROM reports WHERE scope=? AND retry_at<=? ORDER BY id',
                                    (self.scope, now)))

    def progress(self, report_id, chunk_index):
        with self.db:
            self.db.execute('UPDATE reports SET next_chunk=? WHERE scope=? AND id=?',
                            (chunk_index, self.scope, report_id))

    def retry(self, report_id, now):
        with self.db:
            self.db.execute('UPDATE reports SET retry_at=? WHERE scope=? AND id=?',
                            (now + 60, self.scope, report_id))

    def remove_report(self, report_id):
        with self.db:
            self.db.execute('DELETE FROM reports WHERE scope=? AND id=?', (self.scope, report_id))

    def close(self):
        self.db.close()


class FeedbackService:
    def __init__(self, bot, store, pacing_factory, split_message, clock=time.time):
        self.bot, self.store = bot, store
        self.pacing_factory, self.split_message, self.clock = pacing_factory, split_message, clock
        self.pacing = {}
        self.tasks = {}
        self.report_lock = asyncio.Lock()
        self.worker = None
        self.report_worker = None
        self.stopping = False

    def response_report(self, batch, text, outcome):
        header = (f"Feedback response | user={batch['uid']} | scope={self.store.scope}\n"
                  f"submissions={','.join(batch['ids'])}\nDelivery: {outcome}\n")
        return list(self.split_message(header + (text or '(no generated reply)')))

    def start(self):
        fallback = self.pacing_factory().fallback
        self.store.recover(self.clock(), fallback, self.response_report)
        self.worker = asyncio.create_task(self.run())
        self.report_worker = asyncio.create_task(self.run_reports())

    async def receive(self, message):
        if self.stopping:
            return
        if not message.content.strip():
            await self.bot.dm_send(message.channel, 'Me manda a sugestão por texto?')
            return
        uid = message.author.id
        pacing = self.pacing.setdefault(uid, self.pacing_factory())
        pacing.observe(message.id, message.created_at.timestamp())
        header = (f'Participant feedback | user={uid} | name={message.author.display_name}\n'
                  f'scope={self.store.scope} | submission={message.id}\n'
                  f'time={message.created_at.isoformat()}\n\n')
        try:
            self.store.accept(uid, message.id, message.content,
                              self.clock() + pacing.delay(),
                              list(self.split_message(header + message.content)))
        except Exception as error:
            log.error('Feedback save failed user=%s (%s)', uid, type(error).__name__)
            await self.bot.dm_send(message.channel, 'Não consegui salvar sua sugestão agora. Pode tentar de novo mais tarde?')
            return
        # Owner reports bypass conversational pacing and have their own durable retry queue.
        try:
            await self.flush_reports()
        except Exception as error:
            log.warning('Saved feedback report awaits scheduler (%s)', type(error).__name__)

    async def flush_reports(self):
        async with self.report_lock:
            reports = self.store.reports(self.clock())
            if not reports:
                return
            try:
                owner = self.bot.get_user(self.bot.config.owner_id)
                if owner is None:
                    owner = await self.bot.fetch_user(self.bot.config.owner_id)
                channel = await owner.create_dm()
            except Exception as error:
                for row in reports:
                    self.store.retry(row['id'], self.clock())
                log.warning('Owner feedback DM unavailable (%s)', type(error).__name__)
                return
            for row in reports:
                try:
                    chunks = json.loads(row['chunks'])
                    for index in range(row['next_chunk'], len(chunks)):
                        await self.bot.dm_send(channel, chunks[index])
                        self.store.progress(row['id'], index + 1)
                    self.store.remove_report(row['id'])
                except Exception as error:
                    self.store.retry(row['id'], self.clock())
                    log.warning('Feedback report pending id=%s (%s)', row['id'], type(error).__name__)

    async def respond(self, uid):
        batch = None
        text = None
        try:
            batch = self.store.claim(uid)
            if batch is None:
                return
            try:
                text = await self.bot.gemini.feedback_answer(batch['context'], uid)
                if not text:
                    text = 'Entendi a sugestão, obrigada por me contar!'
            except Exception as error:
                log.warning('Feedback generation failed user=%s (%s)', uid, type(error).__name__)
                text = 'Entendi a sugestão, obrigada por me contar!'
            if self.store.pending_reports(uid):
                text += '\n\nA sugestão ficou salva; o encaminhamento ainda está pendente.'
            self.store.prepare(batch, text)
            outcome = 'delivered'
            try:
                user = self.bot.get_user(uid)
                if user is None:
                    user = await self.bot.fetch_user(uid)
                await self.bot.dm_send(await user.create_dm(), text)
            except Exception as error:
                outcome = 'failed; not resent'
                log.warning('Feedback reply failed user=%s (%s)', uid, type(error).__name__)
            self.store.finish(batch, outcome, self.response_report(batch, text, outcome))
        except asyncio.CancelledError:
            # Leave the claim durable; restart recovery reports it without another send.
            raise
        except Exception as error:
            log.error('Feedback processing failed user=%s (%s)', uid, type(error).__name__)
            if batch is not None:
                try:
                    self.store.finish(batch, 'failed', self.response_report(batch, text, 'processing failed; not resent'))
                except Exception:
                    log.error('Feedback failure could not be saved user=%s', uid)

    async def tick(self):
        for uid in self.store.due_users(self.clock()):
            if uid not in self.bot.config.allowed_ids or uid == self.bot.config.owner_id:
                continue
            task = self.tasks.get(uid)
            if task is None or task.done():
                self.tasks[uid] = asyncio.create_task(self.respond(uid))

    async def run(self):
        await self.bot.wait_until_ready()
        while not self.stopping:
            try:
                if self.bot.is_ready():
                    await self.tick()
            except Exception as error:
                log.error('Feedback scheduler failed (%s)', type(error).__name__)
            await asyncio.sleep(1)

    async def run_reports(self):
        await self.bot.wait_until_ready()
        while not self.stopping:
            try:
                if self.bot.is_ready():
                    await self.flush_reports()
            except Exception as error:
                log.error('Feedback report worker failed (%s)', type(error).__name__)
            await asyncio.sleep(1)

    async def close(self):
        self.stopping = True
        tasks = list(self.tasks.values()) + [task for task in (self.worker, self.report_worker) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.store.close()
