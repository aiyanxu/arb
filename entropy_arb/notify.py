"""Telegram notifications — an explicit one-shot send API.

This module only provides the *capability* to push a message to a Telegram
chat; nothing calls it automatically. Where and when a notification fires
(a fill, a halt, a daily summary, …) is decided by the code that calls
`notify.send(text)`.

Credentials come from the env layer like every other secret:
    TELEGRAM_BOT_TOKEN   from @BotFather
    TELEGRAM_CHAT_ID     your chat/group id (message @userinfobot or add the
                         bot to a group and read getUpdates)

Delivery model: send() only enqueues (never blocks, never raises — a
notification problem must not take down trading); a daemon thread posts one
message per ~1.1s tick (Telegram allows ~30 messages/min per chat) via the
Bot API (https://core.telegram.org/bots/api#sendmessage) and drops with a
log line if the queue overflows. No credentials in the environment -> every
call is a silent no-op.

Telegram 通知能力：本模块只提供 send() 发送能力，何时调用由调用方决定——
不自动转发任何日志。凭据来自 .env（TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID），
未配置时 send() 静默跳过。发送在守护线程中按 Telegram 频率限制节流执行，
绝不阻塞交易、绝不抛异常。

Usage:
    from entropy_arb import notify
    notify.send("base +2 -> 0, hedge -1.5 -> 0 — both legs flat")
    notify.drain()             # optional: wait for pending sends at exit
"""
from __future__ import annotations

import html
import logging
import os
import queue
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("telegram")

API_URL = "https://api.telegram.org"
# quieter than the API limit (~30 messages/min per chat): one message per
# tick keeps a burst from eating the budget and getting 429s
SEND_GAP_SEC = 1.1
MAX_MESSAGE = 3800          # Telegram's hard cap is 4096 UTF-16 chars
MAX_QUEUE = 200


def esc(s: str) -> str:
    """Escape message text so venue/error strings can't inject markup if a
    caller later switches the send to HTML parse mode."""
    return html.escape(s, quote=False)


class _Sender:
    """Queue + daemon worker thread; one instance per process."""

    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self._q: queue.Queue = queue.Queue(maxsize=MAX_QUEUE)
        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name="telegram-notify")
        self._thread.start()

    @property
    def empty(self) -> bool:
        return self._q.empty()

    def submit(self, text: str) -> None:
        """Queue one message. Never raises; on overflow drop the oldest."""
        try:
            self._q.put_nowait(text)
        except queue.Full:
            try:
                self._q.get_nowait()
                self._q.put_nowait(text)
            except Exception:
                pass

    def _worker(self) -> None:
        last_send = 0.0
        while True:
            try:
                text = self._q.get()
                wait = last_send + SEND_GAP_SEC - time.time()
                if wait > 0:
                    time.sleep(wait)
                last_send = time.time()
                self._post(text)
            except Exception:
                # a broken worker must not take the process down with it
                time.sleep(1.0)

    def _post(self, text: str) -> None:
        if len(text) > MAX_MESSAGE:
            text = text[:MAX_MESSAGE - 1] + "…"
        body = urllib.parse.urlencode({
            "chat_id": self.chat_id, "text": text,
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"{API_URL}/bot{self.token}/sendMessage", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
        except Exception as e:
            log.warning("telegram send failed: %r", e)


_instance: _Sender | None = None


def _get_sender() -> _Sender | None:
    """The process-wide sender, or None without credentials in the env."""
    global _instance
    if _instance is not None:
        return _instance
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None
    try:
        _instance = _Sender(token, chat_id)
        return _instance
    except Exception as e:
        log.warning("telegram notifications disabled: %r", e)
        return None


def send(text: str) -> None:
    """Send one notification message. Never raises, never blocks long.

    No credentials configured -> silent no-op. The caller decides when to
    call this (fills, halts, summaries — wherever it wants).
    """
    try:
        s = _get_sender()
        if s is None:
            return
        s.submit(text)
    except Exception:
        pass


def drain(timeout: float = 6.0) -> None:
    """Best-effort wait until queued messages have been posted (call at
    process shutdown so a final notification is not lost)."""
    try:
        if _instance is not None:
            deadline = time.time() + timeout
            while time.time() < deadline and not _instance._q.empty():
                time.sleep(0.1)
    except Exception:
        pass
