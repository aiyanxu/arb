"""Telegram notifications — a logging.Handler, so the engine needs no changes.

Every log line the engine already emits (fills, hedges, rate limits, venue
outages, the critical HALT) flows through the root logger; a handler attached
there forwards the configured level and up to a Telegram chat via the Bot API
(https://core.telegram.org/bots/api#sendmessage). Credentials come from the
env layer like every other secret:

    TELEGRAM_BOT_TOKEN   from @BotFather
    TELEGRAM_CHAT_ID     your chat/group id (message @userinfobot or add the
                         bot to a group and read getUpdates)

Delivery model: emit() only enqueues (never blocks, never raises — a
notification problem must not take down trading); a daemon thread drains the
queue, batches lines that arrive in a burst into one message, spaces sends
~1.1s apart (Telegram allows ~30 messages/min per chat), and drops with a
log line if the queue overflows. HTML is used for <b>/bold session markers;
message text is escaped so venue/error strings can never inject markup.

Telegram 通知：以 logging.Handler 形式接入根 logger，引擎无需任何改动。
日志按配置级别转发到 Telegram Bot API；emit() 绝不阻塞、绝不抛异常，由
后台线程合并突发日志、按 Telegram 频率限制节流发送。凭据同样来自 .env。
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

API_URL = "https://api.telegram.org"
# quieter than the API limit (~30/min per chat): one message per tick keeps
# a burst from eating the budget and getting 429s
SEND_GAP_SEC = 1.1
MAX_MESSAGE = 3800          # Telegram's hard cap is 4096 UTF-16 chars
MAX_QUEUE = 200

_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
           "WARNING": logging.WARNING, "ERROR": logging.ERROR,
           "CRITICAL": logging.CRITICAL}


def _esc(s: str) -> str:
    return html.escape(s, quote=False)


class TelegramHandler(logging.Handler):
    """Forward log records to a Telegram chat; fire-and-forget by design.

    Heartbeat: [status] lines arrive every status_interval_sec; forwarding
    them all would be spam, so only one heartbeat per `heartbeat_sec` window
    is sent (a fresh one breaks the silence — silence itself then means the
    process is gone).
    """

    def __init__(self, token: str, chat_id: str, level: str = "WARNING",
                 heartbeat_sec: float = 0.0,
                 session_prefix: str = "") -> None:
        super().__init__(level=_LEVELS.get(level.upper(), logging.WARNING))
        self.token = token
        self.chat_id = chat_id
        self.heartbeat_sec = max(float(heartbeat_sec), 0.0)
        self.prefix = session_prefix
        self._q: queue.Queue = queue.Queue(maxsize=MAX_QUEUE)
        self._hb_until = 0.0          # suppress [status] until this ts
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name="telegram-notify")
        self._thread.start()

    # ------------------------------------------------------------- emit path

    def emit(self, record: logging.LogRecord) -> None:
        """Queue one record. Must never raise or block the caller's thread."""
        line = None
        try:
            if record.name == "telegram":
                return  # never loop on our own send failures
            line = self.format(record)
            if record.name == "status" and "[status]" in line:
                now = time.time()
                if now < self._hb_until:
                    return          # within the heartbeat window: drop
                self._hb_until = now + self.heartbeat_sec
            self._q.put_nowait(line)
        except queue.Full:
            try:
                self._q.get_nowait()   # drop oldest, keep the newest flowing
                self._q.put_nowait(line)
            except Exception:
                pass
        except Exception:
            pass

    # ------------------------------------------------------------- worker

    def _worker(self) -> None:
        batch: list[str] = []
        last_send = 0.0
        while True:
            try:
                # batch window: collect whatever is already queued, then a
                # short grace period for stragglers (a burst becomes one msg)
                line = self._q.get(timeout=0.25)
                batch.append(line)
                while len(batch) < 20:
                    try:
                        batch.append(self._q.get_nowait())
                    except queue.Empty:
                        break
                if len(batch) < 20:
                    time.sleep(0.2)
                    while len(batch) < 20:
                        try:
                            batch.append(self._q.get_nowait())
                        except queue.Empty:
                            break
                text = self.prefix + "\n".join(_esc(b) for b in batch)
                batch = []
                wait = last_send + SEND_GAP_SEC - time.time()
                if wait > 0:
                    time.sleep(wait)
                last_send = time.time()
                self._send(text)
            except Exception:
                # a broken worker must not take the process down with it
                batch = []
                time.sleep(1.0)

    def _send(self, text: str) -> None:
        if len(text) > MAX_MESSAGE:
            text = text[:MAX_MESSAGE - 1] + "…"
        body = urllib.parse.urlencode({
            "chat_id": self.chat_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"{API_URL}/bot{self.token}/sendMessage", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
        except Exception as e:
            # report via logging (never via telegram — emit drops them)
            logging.getLogger("telegram").warning(
                "telegram send failed: %r", e)

    # ------------------------------------------------------------- shutdown

    def flush_pending(self, timeout: float = 6.0) -> None:
        """Best-effort drain so a shutdown summary actually arrives."""
        self._stopped.wait(timeout)


# ------------------------------------------------------------------- wiring

def from_env(min_level: str, heartbeat_sec: float, prefix: str):
    """Build a TelegramHandler from TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID, or
    return None when the credentials are absent/incomplete (silently off)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None
    return TelegramHandler(token, chat_id, level=min_level,
                           heartbeat_sec=heartbeat_sec, session_prefix=prefix)


def attach(cfg) -> object | None:
    """Create the handler for a Config and attach it to the root logger.

    Returns the handler (caller may flush_pending() at shutdown) or None
    when not configured. Never raises: a bad telegram setup must not block
    trading. Overrides: telegram.enabled=false disables; explicit
    bot_token/chat_id in the config section win over the env defaults.
    """
    try:
        sec = getattr(cfg, "telegram", None)
        if sec is None or not sec.get("enabled"):
            return None
        token = sec.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = sec.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID", "")
        token, chat_id = token.strip(), chat_id.strip()
        if not token or not chat_id:
            logging.getLogger("telegram").warning(
                "telegram.enabled but token/chat_id missing — check .env "
                "(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
            return None
        h = TelegramHandler(token, chat_id,
                            level=str(sec.get("min_level", "WARNING")),
                            heartbeat_sec=float(sec.get("heartbeat_sec", 0.0)),
                            session_prefix=sec.get("session_prefix", ""))
        logging.getLogger().addHandler(h)
        return h
    except Exception as e:
        logging.getLogger("telegram").warning("telegram disabled: %r", e)
        return None
