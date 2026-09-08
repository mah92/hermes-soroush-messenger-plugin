"""
Soroush Plus (سروش پلاس) platform adapter for Hermes Gateway.

Connects via PySPlusthon — a Python library for the official Soroush Plus
Bot API (https://api.splus.ir/bot<TOKEN>/...).

This is the BOT mode (like Telegram/Bale BotFather):
  - Get a bot token from Soroush Plus (via their bot/developer channel)
  - Set SOROUSH_BOT_TOKEN in ~/.hermes/.env
  - The adapter long-polls getUpdates and delivers messages to Hermes.

NOTE: Soroush has no public self-serve BotFather like Telegram/Bale yet —
tokens are issued through Soroush's official bot/developer channels
(see https://splus.ir/PySPlusthonDevelopers or Soroush's business portal).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("gateway.platforms.soroush")

MAX_MESSAGE_LENGTH = 4000
POLL_INTERVAL = 2.0


def _get_env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


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
    print("  1. Open Soroush Plus and go to the official bot channel")
    print("     (e.g. https://splus.ir/PySPlusthonDevelopers)")
    print("  2. Request a bot token (Soroush issues tokens officially)")
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
    """Soroush Plus bot adapter via PySPlusthon (Bot API)."""

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

        # Allowlists
        self._allowed_chats: set[str] = set()
        chats_env = str(extra.get("allowed_chats") or _get_env("SOROUSH_ALLOWED_CHATS") or "")
        for raw in chats_env.split(","):
            cid = raw.strip()
            if cid:
                self._allowed_chats.add(cid)

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

        # Runtime state
        self._client: Any = None
        self._poll_task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._last_update_id: int = 0

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
            from PySPlusthon import Client
        except ImportError:
            logger.error("[soroush] PySPlusthon required: pip install PySPlusthon")
            return False

        try:
            self._client = Client(self._token)
        except Exception as exc:
            logger.error("[soroush] Client init failed: %s", exc)
            return False

        try:
            await self._client.initialize()
            await self._client.connect()
            me = await self._client.get_me()
            if me:
                self._bot_id = str(getattr(me, "id", "") or "")
                self._bot_username = str(getattr(me, "username", "") or "")
                logger.info(
                    "[soroush] Connected as @%s (id=%s)",
                    self._bot_username, self._bot_id,
                )
            elif self._client.user:
                self._bot_id = str(getattr(self._client.user, "id", "") or "")
                self._bot_username = str(getattr(self._client.user, "username", "") or "")
                logger.info(
                    "[soroush] Connected as @%s (id=%s) [cached]",
                    self._bot_username, self._bot_id,
                )
        except Exception as exc:
            logger.error("[soroush] getMe/init failed: %s", exc)
            await self._safe_shutdown()
            return False

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
        await self._safe_shutdown()
        logger.info("[soroush] Disconnected")

    async def _safe_shutdown(self) -> None:
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            try:
                await self._client.shutdown()
            except Exception:
                pass
            self._client = None

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                updates = await self._client.get_updates(
                    offset=self._last_update_id + 1,
                )
                for upd in updates:
                    await self._handle_update(upd)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("[soroush] Poll error: %s", exc)
                await asyncio.sleep(POLL_INTERVAL)

    async def _download_media(self, file_id: str) -> Optional[str]:
        """Download a file from Soroush servers to a local temp path."""
        import tempfile

        try:
            file_data = await self._client.get_file(file_id)
            if not file_data:
                return None
            # PySPlusthon download returns bytes
            data = await self._client.download(file_id)
            if not data:
                return None
            ext = os.path.splitext(str(getattr(file_data, "file_path", "") or ""))[1] or ".bin"
            fd, local_path = tempfile.mkstemp(suffix=ext.lower())
            os.close(fd)
            with open(local_path, "wb") as f:
                f.write(data)
            logger.debug("[soroush] Downloaded media to %s", local_path)
            return local_path
        except Exception as exc:
            logger.warning("[soroush] Media download error: %s", exc)
            return None

    async def _handle_update(self, upd: Any) -> None:
        """Process a single update object from PySPlusthon."""
        # NOTE: PySPlusthon's Update object stores its id as `.id`, NOT
        # `.update_id` (Telegram-style). Reading .update_id always returns 0,
        # which keeps our offset stuck and getUpdates re-delivers the same
        # messages forever -> busy-mode loop + status-message spam.
        update_id = int(getattr(upd, "id", 0) or 0)
        if update_id:
            self._last_update_id = max(self._last_update_id, update_id)

        msg = getattr(upd, "message", None)
        if not msg:
            return

        chat = getattr(msg, "chat", None)
        # PySPlusthon Message exposes the sender as `author` (a User object),
        # NOT `from_user` (Telegram-style). Use author first, then fallbacks.
        sender = getattr(msg, "author", None) or getattr(msg, "sender_chat", None)
        chat_id = str(getattr(chat, "id", "") or "")
        # Chat.type is a ChatType enum; normalize to a plain string.
        _raw_chat_type = getattr(chat, "type", None)
        if _raw_chat_type is not None and not isinstance(_raw_chat_type, str):
            _raw_chat_type = _raw_chat_type.value if hasattr(_raw_chat_type, "value") else str(_raw_chat_type)
        chat_type = str(_raw_chat_type or "dm")

        user_id = str(getattr(sender, "id", "") or "") if sender else ""
        user_name = (
            (getattr(sender, "first_name", None) or getattr(sender, "username", None) or user_id)
            if sender else user_id
        )
        text = getattr(msg, "text", None) or getattr(msg, "caption", None) or ""
        msg_id = str(getattr(msg, "id", "") or getattr(msg, "message_id", "") or "")

        # CRITICAL: never echo our own bot's messages. Soroush's getUpdates
        # returns the bot's own outgoing sends too; without this filter the
        # bot replies to itself in an infinite loop (observed: "Dropping
        # busy-mode follow-up ... pending queue at cap (32)").
        if user_id and user_id == self._bot_id:
            logger.debug("[soroush] Skipping own bot message id=%s (loop guard)", msg_id)
            return

        # Ignore /start pings entirely: they create an active session and
        # the gateway treats them as platform pings, which triggers status
        # message spam on Soroush.
        if text.strip().lower().startswith("/start"):
            logger.debug("[soroush] Ignoring /start from %s", user_id)
            return

        # Allowlists
        if self._allowed_chats and chat_id not in self._allowed_chats:
            logger.info("[soroush] Ignoring chat %s (not allowed)", chat_id)
            return
        if self._allowed_users and user_id not in self._allowed_users:
            logger.info("[soroush] Ignoring user %s (not allowed)", user_id)
            return

        # Message type + media
        mt = MessageType.TEXT
        media_urls: List[str] = []
        media_types: List[str] = []

        def _file_id(obj: Any) -> str:
            return str(getattr(obj, "file_id", "") or "")

        try:
            if getattr(msg, "photo", None):
                photos = getattr(msg, "photo", None)
                if photos:
                    largest = photos[-1]
                    fid = _file_id(largest)
                    if fid:
                        local = await self._download_media(fid)
                        if local:
                            mt = MessageType.PHOTO
                            media_urls.append(local)
                            media_types.append("image/jpeg")
            elif getattr(msg, "voice", None):
                fid = _file_id(getattr(msg, "voice", None))
                if fid:
                    local = await self._download_media(fid)
                    if local:
                        mt = MessageType.VOICE
                        media_urls.append(local)
                        media_types.append("audio/ogg")
            elif getattr(msg, "audio", None):
                fid = _file_id(getattr(msg, "audio", None))
                if fid:
                    local = await self._download_media(fid)
                    if local:
                        mt = MessageType.AUDIO
                        media_urls.append(local)
                        media_types.append("audio/mpeg")
            elif getattr(msg, "video", None):
                fid = _file_id(getattr(msg, "video", None))
                if fid:
                    local = await self._download_media(fid)
                    if local:
                        mt = MessageType.VIDEO
                        media_urls.append(local)
                        media_types.append("video/mp4")
            elif getattr(msg, "document", None):
                fid = _file_id(getattr(msg, "document", None))
                if fid:
                    local = await self._download_media(fid)
                    if local:
                        mt = MessageType.DOCUMENT
                        media_urls.append(local)
                        media_types.append("application/octet-stream")
        except Exception as exc:
            logger.warning("[soroush] Media handling error: %s", exc)

        source = self.build_source(
            chat_id=chat_id,
            chat_name=str(getattr(chat, "title", None) or getattr(chat, "first_name", None) or chat_id),
            chat_type=chat_type,
            user_id=user_id,
            user_name=str(user_name),
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
            channel_context=None,
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
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            msg = await self._client.send_message(
                chat_id,
                content,
                reply_to_message_id=int(reply_to) if reply_to and reply_to.isdigit() else None,
            )
            return SendResult(
                success=True,
                message_id=str(getattr(msg, "id", "") or getattr(msg, "message_id", "") or ""),
            )
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Show \"typing...\" indicator (like Bale's sendChatAction).

        Attempts Soroush's sendChatAction via raw HTTP; silently ignores
        failure (some Soroush bot endpoints may not implement it).
        """
        if not self._client:
            return
        try:
            await self._client.execute_http("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception as exc:
            logger.debug("[soroush] sendChatAction failed for %s: %s", chat_id, exc)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            msg = await self._client.send_photo(
                chat_id,
                image_url,
                caption=caption,
                reply_to_message_id=int(reply_to) if reply_to and reply_to.isdigit() else None,
            )
            return SendResult(
                success=True,
                message_id=str(getattr(msg, "id", "") or getattr(msg, "message_id", "") or ""),
            )
        except Exception as exc:
            # Fallback: send as text with URL
            text = (caption or "") + f"\n{image_url}" if (caption and image_url) else (image_url or caption or "")
            return await self.send(chat_id, text)

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
        if not self._client:
            return SendResult(success=False, error="Not connected")
        if not os.path.isfile(file_path):
            return SendResult(success=False, error=f"File not found: {file_path}")

        try:
            msg = await self._client.send_document(
                chat_id,
                file_path,
                caption=caption,
                reply_to_message_id=int(reply_to) if reply_to and reply_to.isdigit() else None,
            )
            return SendResult(
                success=True,
                message_id=str(getattr(msg, "id", "") or getattr(msg, "message_id", "") or ""),
            )
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
        return await self.send_image(
            chat_id=chat_id, image_url=file_path, caption=caption,
            reply_to=reply_to, metadata=metadata, **kwargs,
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self.send_image(
            chat_id=chat_id, image_url=image_path, caption=caption,
            reply_to=reply_to, metadata=metadata, **kwargs,
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
        if not self._client:
            return SendResult(success=False, error="Not connected")
        if not os.path.isfile(video_path):
            return SendResult(success=False, error=f"File not found: {video_path}")

        try:
            msg = await self._client.send_video(
                chat_id,
                video_path,
                caption=caption,
                reply_to_message_id=int(reply_to) if reply_to and reply_to.isdigit() else None,
            )
            return SendResult(
                success=True,
                message_id=str(getattr(msg, "id", "") or getattr(msg, "message_id", "") or ""),
            )
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
        if not self._client:
            return SendResult(success=False, error="Not connected")
        if not os.path.isfile(audio_path):
            return SendResult(success=False, error=f"File not found: {audio_path}")

        try:
            msg = await self._client.send_voice(
                chat_id,
                audio_path,
                caption=caption,
                reply_to_message_id=int(reply_to) if reply_to and reply_to.isdigit() else None,
            )
            return SendResult(
                success=True,
                message_id=str(getattr(msg, "id", "") or getattr(msg, "message_id", "") or ""),
            )
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    async def send_audio(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        return await self.send_voice(
            chat_id=chat_id, audio_path=audio_path, caption=caption,
            reply_to=reply_to, metadata=metadata, **kwargs,
        )

    # ------------------------------------------------------------------
    # get_chat_info / formatting
    # ------------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        try:
            chat = await self._client.get_chat(chat_id)
            return {
                "name": (
                    getattr(chat, "title", None)
                    or getattr(chat, "first_name", None)
                    or str(chat_id)
                ),
                "type": getattr(chat, "type", "dm"),
                "chat_id": str(getattr(chat, "id", chat_id)),
            }
        except Exception:
            return {"name": str(chat_id), "type": "dm", "chat_id": str(chat_id)}

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


# ---------------------------------------------------------------------------
# Standalone sender (for cron delivery without a live gateway)
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
    """Send a message via PySPlusthon without a live adapter."""
    extra = getattr(pconfig, "extra", {}) or {}
    token = str(extra.get("bot_token") or os.getenv("SOROUSH_BOT_TOKEN", ""))
    if not token:
        return {"error": "Soroush standalone: SOROUSH_BOT_TOKEN required"}

    try:
        from PySPlusthon import Client

        client = Client(token)
        await client.initialize()
        await client.connect()
        try:
            if media_files:
                sent = []
                for f in media_files:
                    if os.path.isfile(f):
                        msg = await client.send_document(chat_id, f, caption=message or None)
                        sent.append(str(getattr(msg, "id", "") or getattr(msg, "message_id", "")))
                return {"success": True, "message_id": ",".join(sent)}
            msg = await client.send_message(chat_id, message)
            return {"success": True, "message_id": str(getattr(msg, "id", "") or getattr(msg, "message_id", ""))}
        finally:
            await client.disconnect()
            await client.shutdown()
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
        install_hint="pip install PySPlusthon",
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
