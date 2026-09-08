"""
Soroush Plus (سروش پلاس) platform adapter for Hermes Gateway.

Direct Bot API implementation (Telegram-compatible REST), NO third-party
SDK. Official docs: https://soroushplus.com/p/documents/bot-platform

  endpoint: https://api.splus.ir/bot<token>/<METHOD>
  getUpdates long-polling (like Bale/Telegram Bot API)

Setup
-----
1. Create bot via splus.ir/botfather (official Soroush bot maker)
2. Copy the token
3. Set SOROUSH_BOT_TOKEN in ~/.hermes/.env
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("gateway.platforms.soroush")

API_BASE = "https://api.splus.ir"
MAX_MESSAGE_LENGTH = 4000
POLL_TIMEOUT = 30
POLL_INTERVAL = 2.0


def _get_env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _api_url(token: str, method: str) -> str:
    return f"{API_BASE}/bot{token}/{method}"


def _guess_mime(path: str, fallback: str) -> str:
    """Guess a proper MIME type for a downloaded file."""
    import mimetypes

    guessed, _ = mimetypes.guess_type(path)
    if guessed:
        return guessed
    ext = os.path.splitext(path)[1].lower()
    if ext == ".ogg":
        return "audio/ogg"
    if ext == ".opus":
        return "audio/ogg"
    if ext in (".mp3", ".wav", ".m4a", ".aac", ".flac"):
        return "audio/mpeg" if ext == ".mp3" else "audio/ogg"
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext in (".mp4", ".mov", ".webm"):
        return "video/mp4" if ext == ".mp4" else f"video/{ext[1:]}"
    return fallback


def _looks_like_timeout(error: str) -> bool:
    err = (error or "").lower()
    return any(
        marker in err
        for marker in (
            "timeout", "timed out", "timedout", "request timed",
            "deadline exceeded", "asyncio.timeouterror", "socket.timeout",
        )
    )


# ---------------------------------------------------------------------------
# Requirement checks
# ---------------------------------------------------------------------------


def check_requirements() -> bool:
    return bool(_get_env("SOROUSH_BOT_TOKEN"))


def validate_config(config) -> bool:
    return bool(_get_env("SOROUSH_BOT_TOKEN"))


def is_connected(config) -> bool:
    return bool(_get_env("SOROUSH_BOT_TOKEN"))


def _env_enablement() -> Optional[dict]:
    token = _get_env("SOROUSH_BOT_TOKEN")
    if not token:
        return None
    extra = {"bot_token": token}
    home_channel = _get_env("SOROUSH_HOME_CHANNEL")
    if home_channel:
        return {"extra": extra, "home_channel": {"chat_id": home_channel}}
    return {"extra": extra}


def interactive_setup() -> None:
    import shlex

    env_path = os.path.expanduser("~/.hermes/.env")
    print("\n  📲 Soroush Plus Bot Setup (سروش پلاس)")
    print("  ─────────────────────────────────────")
    print("  1. Open https://splus.ir/botfather in Soroush app")
    print("  2. Create a bot and copy its token")
    print("  3. Paste the token here\n")

    token = input("  Bot Token: ").strip()
    chat_id = input("  Default chat ID for cron (empty = skip): ").strip()

    lines = []
    if token:
        lines.append(f"SOROUSH_BOT_TOKEN={shlex.quote(token)}")
    if chat_id:
        lines.append(f"SOROUSH_HOME_CHANNEL={shlex.quote(chat_id)}")

    if lines:
        with open(env_path, "a") as f:
            f.write("\n# Soroush Plus Bot\n")
            f.write("\n".join(lines) + "\n")
        print("\n  ✅ Saved. Restart: hermes gateway restart")


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

from gateway.config import Platform, PlatformConfig

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource


class SoroushAdapter(BasePlatformAdapter):
    """Soroush Plus bot adapter via official REST Bot API (no SDK)."""

    supports_code_blocks: bool = False
    supports_status_text: bool = False
    splits_long_messages: bool = False
    typed_command_prefix: str = "/"

    def __init__(self, config: PlatformConfig) -> None:
        super().__init__(config, Platform("soroush"))

        extra: dict = getattr(config, "extra", {}) or {}
        self._token: str = str(extra.get("bot_token") or _get_env("SOROUSH_BOT_TOKEN") or "")
        self._bot_id: str = ""
        self._bot_username: str = ""

        self._http: Any = None
        self._poll_task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._last_update_id: int = 0
        self._typing_sent: dict[str, float] = {}  # chat_id -> last sendChatAction time

        # Chat allowlist (comma-separated chat IDs). Empty = allow all chats.
        self._allowed_chats: set[str] = set()
        chats_env = str(extra.get("allowed_chats") or _get_env("SOROUSH_ALLOWED_CHATS") or "")
        for raw in chats_env.split(","):
            cid = raw.strip()
            if cid:
                self._allowed_chats.add(cid)

        # User allowlist (comma-separated user IDs). Empty = allow all users.
        _allow_all = str(
            extra.get("allow_all_users") or _get_env("SOROUSH_ALLOW_ALL_USERS") or ""
        ).strip().lower() in ("true", "1", "yes", "on")
        self._allowed_users: set[str] = set()
        if not _allow_all:
            users_env = str(extra.get("allowed_users") or _get_env("SOROUSH_ALLOWED_USERS") or "")
            for raw in users_env.split(","):
                uid = raw.strip()
                if uid:
                    self._allowed_users.add(uid)

        # require_mention gate (default: off). When enabled in groups,
        # messages without @mention are collected as context and prepended
        # to the next triggered message (like Telegram/Bale).
        _rm = extra.get("require_mention")
        if _rm is None:
            _rm = _get_env("SOROUSH_REQUIRE_MENTION")
        self._require_mention = str(_rm).strip().lower() in ("true", "1", "yes", "on")
        self._pending_context: dict[str, list[tuple[str, str]]] = {}

    # ------------------------------------------------------------------
    # connect / disconnect
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self._running:
            return True
        if not self._token:
            logger.error("[soroush] Missing SOROUSH_BOT_TOKEN")
            return False

        try:
            import aiohttp

            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30.0),
                trust_env=True,
            )
        except ImportError:
            logger.error("[soroush] aiohttp required: pip install aiohttp")
            return False

        # Verify token via getMe
        try:
            data = await self._api_get("getMe")
            self._bot_id = str(data.get("result", {}).get("id", ""))
            self._bot_username = data.get("result", {}).get("username", "")
            logger.info(
                "[soroush] Connected as @%s (id=%s)",
                self._bot_username, self._bot_id,
            )
        except Exception as exc:
            logger.error("[soroush] getMe failed: %s", exc)
            await self._http.close()
            self._http = None
            return False

        # Clear any existing webhook (polling won't work with webhook set)
        try:
            await self._api_get("deleteWebhook")
        except Exception:
            pass

        # Start polling loop
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("[soroush] Polling started")
        return True

    async def disconnect(self) -> None:
        self._running = False

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        if self._http:
            try:
                await self._http.close()
            except Exception:
                pass
            self._http = None

        logger.info("[soroush] Disconnected")

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Long-poll for updates from Soroush."""
        while self._running:
            try:
                updates = await self._get_updates()
                for upd in updates:
                    await self._handle_update(upd)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("[soroush] Poll error: %s", exc)
                await asyncio.sleep(POLL_INTERVAL)

    async def _get_updates(self) -> List[dict]:
        """Fetch updates via getUpdates with offset."""
        params = {"offset": self._last_update_id + 1, "timeout": POLL_TIMEOUT}
        data = await self._api_get("getUpdates", params=params)
        return data.get("result", []) or []

    async def _download_media(self, file_id: str) -> Optional[str]:
        """Download a file from Soroush servers via getFile + file URL."""
        import tempfile

        try:
            file_data = await self._api_get("getFile", {"file_id": file_id})
            if not file_data.get("ok"):
                logger.warning("[soroush] getFile failed: %s", file_data.get("description"))
                return None
            file_path = file_data["result"]["file_path"]
            file_url = f"{API_BASE}/file/bot{self._token}/{file_path}"

            ext = os.path.splitext(file_path)[1] or ".bin"
            fd, local_path = tempfile.mkstemp(suffix=ext.lower())
            os.close(fd)

            async with self._http.get(file_url) as resp:
                if resp.status != 200:
                    logger.warning("[soroush] Download failed: HTTP %s", resp.status)
                    os.unlink(local_path)
                    return None
                with open(local_path, "wb") as f:
                    f.write(await resp.read())

            logger.debug("[soroush] Downloaded media to %s", local_path)
            return local_path
        except Exception as exc:
            logger.warning("[soroush] Media download error: %s", exc)
            return None

    async def _handle_update(self, upd: dict) -> None:
        """Process a single update dict."""
        update_id = upd.get("update_id", 0)
        if update_id:
            self._last_update_id = max(self._last_update_id, update_id)

        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return

        chat = msg.get("chat", {})
        sender = msg.get("from", {})
        chat_id = str(chat.get("id", ""))
        chat_type = chat.get("type", "dm")

        user_id = str(sender.get("id", ""))
        user_name = sender.get("first_name") or sender.get("username") or user_id
        text = msg.get("text") or msg.get("caption") or ""

        msg_id = str(msg.get("message_id", ""))

        # Chat allowlist: drop messages from chats outside the allowed set
        if self._allowed_chats and chat_id not in self._allowed_chats:
            logger.info("[soroush] Ignoring message from non-allowed chat %s (user=%s)", chat_id, user_name)
            return

        # User allowlist: drop messages from users outside the allowed set
        if self._allowed_users and user_id not in self._allowed_users:
            logger.info("[soroush] Ignoring message from non-allowed user %s (%s)", user_id, user_name)
            return

        # Message type and media downloads
        mt = MessageType.TEXT
        media_urls: List[str] = []
        media_types: List[str] = []

        if msg.get("photo"):
            mt = MessageType.PHOTO
            photos = sorted(msg["photo"], key=lambda p: p.get("file_size", 0))
            if photos:
                file_id = photos[-1].get("file_id", "")
                if file_id:
                    local = await self._download_media(file_id)
                    if local:
                        media_urls.append(local)
                        media_types.append(_guess_mime(local, "image/jpeg"))
        elif msg.get("video"):
            mt = MessageType.VIDEO
            file_id = msg["video"].get("file_id", "")
            if file_id:
                local = await self._download_media(file_id)
                if local:
                    media_urls.append(local)
                    media_types.append(_guess_mime(local, "video/mp4"))
        elif msg.get("voice"):
            mt = MessageType.VOICE
            file_id = msg["voice"].get("file_id", "")
            if file_id:
                local = await self._download_media(file_id)
                if local:
                    media_urls.append(local)
                    media_types.append(
                        str(msg["voice"].get("mime_type") or "audio/ogg").lower()
                    )
        elif msg.get("audio"):
            _a_mime = str(msg["audio"].get("mime_type") or "").lower()
            _voice_like = ("ogg" in _a_mime) or ("opus" in _a_mime)
            mt = MessageType.VOICE if _voice_like else MessageType.AUDIO
            file_id = msg["audio"].get("file_id", "")
            if file_id:
                local = await self._download_media(file_id)
                if local:
                    media_urls.append(local)
                    media_types.append(_a_mime or ("audio/ogg" if _voice_like else "audio/mpeg"))
        elif msg.get("document"):
            mt = MessageType.DOCUMENT
            file_id = msg["document"].get("file_id", "")
            if file_id:
                local = await self._download_media(file_id)
                if local:
                    media_urls.append(local)
                    media_types.append(
                        str(msg["document"].get("mime_type") or "application/octet-stream").lower()
                    )
        elif msg.get("location"):
            mt = MessageType.LOCATION

        # require_mention gate: in groups, buffer non-mention messages as context
        channel_context = None
        if chat_type in ("group", "supergroup") and self._require_mention:
            bot_mention = f"@{self._bot_username}"
            reply_to = msg.get("reply_to_message", {})
            is_reply_to_bot = (
                str(reply_to.get("from", {}).get("id", "")) == self._bot_id
            )
            is_mention = bot_mention.lower() in text.lower()
            if not is_mention and not is_reply_to_bot:
                self._pending_context.setdefault(chat_id, []).append((user_name, text))
                if len(self._pending_context[chat_id]) > 50:
                    self._pending_context[chat_id] = self._pending_context[chat_id][-50:]
                return
            else:
                buf = self._pending_context.pop(chat_id, [])
                if buf:
                    lines = [f"[{un}] {tx}" for un, tx in buf]
                    channel_context = "[Earlier group messages]\n" + "\n".join(lines)

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat.get("first_name") or chat.get("title") or chat_id,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
            message_id=msg_id,
        )

        event = MessageEvent(
            text=text,
            message_type=mt,
            source=source,
            raw_message=msg,
            message_id=msg_id,
            timestamp=datetime.now(),
            media_urls=media_urls,
            media_types=media_types,
            channel_context=channel_context,
        )

        await self.handle_message(event)

    # ------------------------------------------------------------------
    # send
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._http:
            return SendResult(success=False, error="Not connected")

        payload: Dict[str, Any] = {"chat_id": chat_id, "text": content}
        if reply_to:
            payload["reply_to_message_id"] = reply_to

        try:
            data = await self._api_post("sendMessage", payload)
            if data.get("ok"):
                result = data.get("result", {})
                return SendResult(
                    success=True,
                    message_id=str(result.get("message_id", "")),
                )
            return SendResult(success=False, error=str(data.get("description", "unknown error")))
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    # ------------------------------------------------------------------
    # send_typing / send_image
    # ------------------------------------------------------------------

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Show \"typing...\" indicator via sendChatAction.

        Soroush rate-limits sendChatAction (429 Too Many Requests), so we
        throttle to at most one call per chat every SEND_CHAT_ACTION_MIN_INTERVAL.
        """
        if not self._http:
            return
        now = asyncio.get_event_loop().time()
        last = self._typing_sent.get(chat_id, 0.0)
        if now - last < 5.0:
            return
        self._typing_sent[chat_id] = now
        try:
            await self._api_post("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception:
            pass

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        if not self._http:
            return SendResult(success=False, error="Not connected")

        payload: Dict[str, Any] = {"chat_id": chat_id, "photo": image_url}
        if caption:
            payload["caption"] = caption
        if reply_to:
            payload["reply_to_message_id"] = reply_to

        # 1) Try direct URL — Soroush fetches the image server-side
        try:
            result = await self._api_post("sendPhoto", payload)
            if result.get("ok"):
                sent_msg_id = str(result.get("result", {}).get("message_id", ""))
                return SendResult(success=True, message_id=sent_msg_id)
        except Exception:
            pass

        # 2) Download and upload as multipart (for URLs Soroush can't reach)
        try:
            import tempfile

            async with self._http.get(image_url) as resp:
                if resp.status == 200:
                    img_data = await resp.read()
                    fd, tmp = tempfile.mkstemp(suffix=".jpg")
                    os.close(fd)
                    with open(tmp, "wb") as f:
                        f.write(img_data)

                    mp_data = {"chat_id": chat_id}
                    if caption:
                        mp_data["caption"] = caption
                    result = await self._api_post_multipart("sendPhoto", mp_data, {"photo": tmp})
                    os.unlink(tmp)
                    if result.get("ok"):
                        sent_msg_id = str(result.get("result", {}).get("message_id", ""))
                        return SendResult(success=True, message_id=sent_msg_id)
        except Exception:
            pass

        # 3) Fallback: send as text with URL
        text = caption or ""
        if image_url:
            text = f"{text}\n{image_url}".strip()
        return await self.send(chat_id, text)

    # ------------------------------------------------------------------
    # get_chat_info
    # ------------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        try:
            data = await self._api_post("getChat", {"chat_id": chat_id})
            result = data.get("result", {})
            return {
                "name": result.get("first_name") or result.get("title") or chat_id,
                "type": result.get("type", "dm"),
                "chat_id": str(result.get("id", chat_id)),
            }
        except Exception:
            return {"name": str(chat_id), "type": "dm", "chat_id": str(chat_id)}

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------

    @staticmethod
    def format_message(text: str) -> str:
        if not text:
            return text
        import re

        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"\*(.+?)\*", r"\1", text)
        text = re.sub(r"__(.+?)__", r"\1", text)
        text = re.sub(r"_(.+?)_", r"\1", text)
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"`{1,3}[^`]*`{1,3}", "", text)
        return text.strip()

    @staticmethod
    def truncate_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> str:
        if not text or len(text) <= max_length:
            return text
        return text[: max_length - 3] + "..."

    def build_source(self, **kwargs) -> SessionSource:
        return super().build_source(**{
            k: v or kwargs.get("chat_id", "")
            for k, v in kwargs.items()
        })

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    async def _api_get(self, method: str, params: Optional[dict] = None) -> dict:
        url = _api_url(self._token, method)
        if params is None:
            params = {}
        async with self._http.get(url, params=params) as resp:
            return await resp.json()

    async def _api_post(self, method: str, data: dict) -> dict:
        url = _api_url(self._token, method)
        async with self._http.post(url, json=data) as resp:
            return await resp.json()

    async def _api_post_multipart(
        self,
        method: str,
        data: dict,
        files: dict,
    ) -> dict:
        """POST with multipart/form-data for file uploads."""
        import aiohttp

        url = _api_url(self._token, method)
        form = aiohttp.FormData()
        for key, value in data.items():
            form.add_field(key, str(value))
        for field_name, file_path in files.items():
            form.add_field(
                field_name,
                open(file_path, "rb"),
                filename=os.path.basename(file_path),
            )
        async with self._http.post(url, data=form) as resp:
            return await resp.json()

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Upload and send a document/file to a Soroush chat."""
        if not self._http:
            return SendResult(success=False, error="Not connected")
        if not os.path.isfile(file_path):
            return SendResult(success=False, error=f"File not found: {file_path}")

        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        if reply_to:
            data["reply_to_message_id"] = reply_to
        files = {"document": file_path}

        try:
            result = await self._api_post_multipart("sendDocument", data, files)
            if result.get("ok"):
                sent_msg_id = str(result.get("result", {}).get("message_id", ""))
                return SendResult(success=True, message_id=sent_msg_id)
            return SendResult(success=False, error=str(result.get("description", "unknown error")))
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def send_photo(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Upload and send a photo/image to a Soroush chat."""
        if not self._http:
            return SendResult(success=False, error="Not connected")
        if not os.path.isfile(file_path):
            return SendResult(success=False, error=f"File not found: {file_path}")

        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        if reply_to:
            data["reply_to_message_id"] = reply_to
        files = {"photo": file_path}

        try:
            result = await self._api_post_multipart("sendPhoto", data, files)
            if result.get("ok"):
                sent_msg_id = str(result.get("result", {}).get("message_id", ""))
                return SendResult(success=True, message_id=sent_msg_id)
            return SendResult(success=False, error=str(result.get("description", "unknown error")))
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a LOCAL image file via sendPhoto (native upload)."""
        return await self.send_photo(
            chat_id=chat_id, file_path=image_path,
            caption=caption, reply_to=reply_to, metadata=metadata, **kwargs,
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Upload and send a video via sendVideo API."""
        if not self._http:
            return SendResult(success=False, error="Not connected")
        if not os.path.isfile(video_path):
            return SendResult(success=False, error=f"File not found: {video_path}")

        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        if reply_to:
            data["reply_to_message_id"] = reply_to
        files = {"video": video_path}

        try:
            result = await self._api_post_multipart("sendVideo", data, files)
            if result.get("ok"):
                sent_msg_id = str(result.get("result", {}).get("message_id", ""))
                return SendResult(success=True, message_id=sent_msg_id)
            return SendResult(success=False, error=str(result.get("description", "unknown error")))
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send audio as a native voice message via sendVoice API."""
        if not self._http:
            return SendResult(success=False, error="Not connected")
        if not os.path.isfile(audio_path):
            return SendResult(success=False, error=f"File not found: {audio_path}")

        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        if reply_to:
            data["reply_to_message_id"] = reply_to

        last_error = ""
        for attempt in range(3):
            if attempt > 0:
                if last_error and _looks_like_timeout(last_error):
                    logger.warning(
                        "[soroush] sendVoice attempt %d timed out — delivery state unknown, not retrying to avoid duplicates",
                        attempt + 1,
                    )
                    break
                await asyncio.sleep(1)
            try:
                result = await self._api_post_multipart("sendVoice", data, {"voice": audio_path})
                if result.get("ok"):
                    sent_msg_id = str(result.get("result", {}).get("message_id", ""))
                    return SendResult(success=True, message_id=sent_msg_id)
                last_error = str(result.get("description", "unknown error"))
            except Exception as exc:
                last_error = str(exc)

        logger.warning("[soroush] sendVoice failed after 3 attempts: %s", last_error)
        return SendResult(success=False, error=last_error)

    async def send_audio(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send audio file — same as send_voice for Soroush."""
        return await self.send_voice(
            chat_id=chat_id, audio_path=audio_path,
            caption=caption, reply_to=reply_to, metadata=metadata, **kwargs,
        )


# ---------------------------------------------------------------------------
# Standalone sender (for cron delivery without a live adapter)
# ---------------------------------------------------------------------------


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Send message via Soroush Bot API without a live adapter."""
    import aiohttp

    extra = getattr(pconfig, "extra", {}) or {}
    token = str(extra.get("bot_token") or os.getenv("SOROUSH_BOT_TOKEN", ""))
    if not token:
        return {"error": "Soroush standalone send: SOROUSH_BOT_TOKEN required"}

    url = _api_url(token, "sendMessage")
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30.0), trust_env=True
        ) as session:
            async with session.post(url, json={"chat_id": chat_id, "text": message}) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return {
                        "success": True,
                        "message_id": str(data.get("result", {}).get("message_id", "")),
                    }
                return {"error": data.get("description", "unknown error")}
    except Exception as exc:
        return {"error": f"Soroush send failed: {exc}"}


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    ctx.register_platform(
        name="soroush",
        label="Soroush Plus",
        adapter_factory=lambda cfg: SoroushAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["SOROUSH_BOT_TOKEN"],
        install_hint="",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="SOROUSH_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="SOROUSH_ALLOWED_USERS",
        allow_all_env="SOROUSH_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="📲",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via Soroush Plus (سروش پلاس), a Persian messaging platform. "
            "Soroush supports plain text only — do not use markdown formatting. "
            "Use simple text, emojis, and keep responses clear. "
            "Avoid backticks, asterisks for bold/italic, or code blocks. "
            "Respond in Persian (فارسی) when appropriate."
        ),
    )

    Platform("soroush")
