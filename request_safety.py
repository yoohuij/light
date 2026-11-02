import asyncio
import hashlib
import json
import logging
import time
from contextlib import closing

import discord
from discord.webhook.async_ import AsyncWebhookAdapter, async_context

LOG = logging.getLogger(__name__)


class RequestSafety:
    def __init__(self, bot, database):
        self.bot = bot
        self.database = database
        self.started = False
        self.startup_task = None
        self.close_task = None
        self.tripped = False
        self.pacing_lock = asyncio.Lock()
        self.next_write = 0.0
        with closing(database()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS request_safety (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute("DELETE FROM request_safety WHERE key='blocked_until'")
        self.original_request = bot.http.request
        bot.http.request = self.request
        adapter = AsyncWebhookAdapter()
        self.original_webhook_request = adapter.request
        adapter.request = self.webhook_request
        async_context.set(adapter)

    def get(self, key):
        with closing(self.database()) as db:
            row = db.execute('SELECT value FROM request_safety WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def put(self, key, value):
        with closing(self.database()) as db, db:
            db.execute('INSERT OR REPLACE INTO request_safety VALUES (?,?)', (key, str(value)))

    async def wait_to_start(self):
        return

    async def request(self, route, **kwargs):
        return await self.dispatch(self.original_request, route, **kwargs)

    async def webhook_request(self, route, *args, **kwargs):
        return await self.dispatch(self.original_webhook_request, route, *args, **kwargs)

    async def dispatch(self, original, route, *args, **kwargs):
        if self.tripped:
            raise RuntimeError('Discord 요청 제한 보호 중: 추가 전송을 중단했습니다.')
        reason = kwargs.get('reason') or ''
        if reason.startswith(('역할 카테고리', '하위 역할 없음', '예외 인원 또는', '역할 변경에 따른', '누적 후원', '후원 순위 변경', '후원 상위 3위')):
            async with self.pacing_lock:
                await asyncio.sleep(max(0, self.next_write - time.monotonic()))
                self.next_write = time.monotonic() + 1.0
        if self.tripped:
            raise RuntimeError('Discord 요청 제한 보호 중: 추가 전송을 중단했습니다.')
        try:
            return await original(route, *args, **kwargs)
        except discord.HTTPException as exc:
            if exc.status != 429:
                raise
            self.tripped = True
            LOG.error('Discord HTTP 429: 추가 요청 중단 및 봇 연결 종료. 호스팅 제한 상태를 확인하고 반복 재시작하지 마세요.')
            if self.close_task is None:
                self.close_task = asyncio.create_task(self.bot.close())
            raise discord.HTTPException(exc.response, '요청 제한으로 봇 연결을 종료합니다. 호스팅 제한 상태를 확인해 주세요.') from None

    async def sync_commands(self):
        payload = [command.to_dict(self.bot.tree) for command in self.bot.tree.get_commands()]
        fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        key = f'commands:{self.bot.application_id}'
        if self.get(key) == fingerprint:
            LOG.info('명령어 구성 동일: 재등록 생략')
            return
        await self.bot.tree.sync()
        self.put(key, fingerprint)

    def schedule_startup(self, callback):
        if self.started:
            return
        self.started = True
        key = f'startup:{self.bot.application_id}'
        previous = float(self.get(key) or 0)
        delay = max(60, previous + 900 - time.time())
        self.put(key, time.time())
        self.startup_task = asyncio.create_task(self.run_startup(callback, delay))

    async def run_startup(self, callback, delay):
        await asyncio.sleep(delay)
        if self.bot.is_closed() or self.tripped:
            return
        try:
            await callback()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception('시작 자동 갱신 중단: 연결·권한·요청 제한을 확인하세요.')


def install(bot, database):
    return RequestSafety(bot, database)
