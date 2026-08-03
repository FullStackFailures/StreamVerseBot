"""
╔══════════════════════════════════════════════════════════╗
║       Telegram Media Indexer Bot  —  Production v6.5      ║
╠══════════════════════════════════════════════════════════╣
║  Monitors admin posts in a Telegram supergroup and        ║
║  builds an organised index of media files in a closed     ║
║  forum topic (ALL_ADDED_SHOWS).                            ║
╠══════════════════════════════════════════════════════════╣
║  v6.5 → Backup-group forward detection (NEW):              ║
║                                                            ║
║  • MiddleMan (GROUP_CHAT_ID) remains the single source of  ║
║    truth exactly as before — nothing about it changed.     ║
║  • Backup (BACKUP_GROUP_CHAT_ID) is the group members       ║
║    actually join. The bot NEVER posts movies there on its   ║
║    own. Instead, the admin manually forwards a MiddleMan    ║
║    post into Backup.                                        ║
║  • When the bot sees a FORWARDED message land inside        ║
║    Backup (from an admin), it automatically creates a NEW   ║
║    Search-index post inside BACKUP_ALL_ADDED_SHOWS whose    ║
║    button links to that forwarded message's OWN message id  ║
║    inside Backup — never back to MiddleMan.                 ║
║  • This runs as an independent handler (separate PTB        ║
║    handler group) so the existing MiddleMan admin-indexing   ║
║    handler (`handle_message`) is completely unaffected.      ║
╠══════════════════════════════════════════════════════════╣
║  v6.4 → MongoDB Atlas migration:                          ║
║                                                            ║
║  • The link/bundle store (previously SQLite) is now       ║
║    backed by MongoDB Atlas via `pymongo`. Two collections  ║
║    are used in the configured database:                    ║
║      - "downloads"  → one document per uploaded file       ║
║      - "bundles"    → one document per quality/season/     ║
║                        sendall bundle (child_tokens is a    ║
║                        native array, no more JSON strings)  ║
║  • Required .env keys: MONGODB_URI, MONGODB_DB_NAME         ║
║  • init_link_store() connects + pings the cluster at        ║
║    startup and raises immediately if the connection fails,  ║
║    instead of failing silently on first upload.             ║
║  • All read/write helper functions keep their EXACT same    ║
║    names and signatures (_store_download, _get_download,    ║
║    _increment_download_count, _store_bundle, _get_bundle,   ║
║    _increment_bundle_count) so nothing else in the bot      ║
║    (wizard, redemption, delivery) needed to change.          ║
╠══════════════════════════════════════════════════════════╣
║  Required .env keys:                                     ║
║    BOT_TOKEN         — Telegram bot token                ║
║    GROUP_CHAT_ID     — Supergroup chat ID (negative int) ║
║    ALL_ADDED_SHOWS   — Topic ID for "SEARCH ALL ADDED    ║
║                        SHOWS" (currently id 3)           ║
║    SEARCH_SHOWS_HERE — Topic ID new-member welcome links ║
║                        into (currently id 1, the built-  ║
║                        in "# General" topic)             ║
║    ADMIN_IDS         — Comma-separated admin user IDs    ║
║    MONGODB_URI       — MongoDB Atlas connection string   ║
║    MONGODB_DB_NAME   — MongoDB database name             ║
║    BACKUP_GROUP_CHAT_ID — Backup supergroup chat ID       ║
║    BACKUP_ALL_ADDED_SHOWS — Backup group's Search topic id║
║                                                          ║
║  Optional — dead MiddleMan mirror groups (up to 8,       ║
║  numbered 1-8, for disaster recovery if the PRIMARY      ║
║  MiddleMan itself gets banned):                          ║
║    MIDDLEMAN_MIRROR_<N>_CHAT_ID          — mirror's chat ║
║    MIDDLEMAN_MIRROR_<N>_TOPIC_MAP        — same syntax   ║
║                        as TOPIC_MAP, mirror's OWN topic  ║
║                        ids (Telegram assigns these       ║
║                        independently per group)          ║
║    MIDDLEMAN_MIRROR_<N>_ALL_ADDED_SHOWS  — mirror's OWN  ║
║                        "Search All Added Shows" topic id ║
║                        (dedicated field, NOT part of the ║
║                        TOPIC_MAP string — same pattern   ║
║                        as ALL_ADDED_SHOWS /              ║
║                        BACKUP_ALL_ADDED_SHOWS above)     ║
║                                                          ║
║  Optional .env keys (with defaults):                     ║
║    IGNORED_TOPICS    — Comma-separated forum topic IDs   ║
║                        whose messages are never indexed  ║
║                        or forwarded into ALL_ADDED_SHOWS. ║
║                        Currently: 1 (# General), 1796    ║
║                        (GENERAL CHAT (GC)). Add more IDs  ║
║                        here any time you create a new     ║
║                        topic to exclude - no code change  ║
║                        needed.                             ║
║    HOW_TO_LINK       — URL for the ZIP guide button      ║
║    PORT              — Flask health server port (10000)  ║
║    DEBOUNCE_SEC      — Batch window in seconds (3.0)     ║
║    FLUSH_INTERVAL    — Flush loop tick in seconds (1.0)  ║
║    SEEN_TTL_SEC      — Seen-cache TTL seconds (21600)    ║
║    MAX_BUTTONS       — Max inline buttons per post (20)  ║
║    MAX_BUTTON_LEN    — Max button label characters (56)  ║
╚══════════════════════════════════════════════════════════╝
"""

# ──────────────────────────────────────────────────────────
#  IMPORTS
# ──────────────────────────────────────────────────────────
import asyncio
import logging
import os
import re
import secrets
from pymongo import MongoClient
from pymongo.collection import Collection
import threading
import time
import json
from datetime import timedelta
from dataclasses import dataclass, field
from functools import wraps
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from flask import Flask, jsonify
from html import escape as html_escape
import httpx

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from welcome_templates import (
    WELCOME_TEMPLATE_HTML,
    WELCOME_BUTTON_LABEL,
    DELETE_WARNING_TEMPLATE_HTML,
)


# ──────────────────────────────────────────────────────────
#  LOGGING  +  TOKEN-MASKING FILTER
#
#  httpx logs the full request URL which includes the bot
#  token as a path segment. The filter below replaces it
#  with "***BOT_TOKEN***" before any handler sees it.
# ──────────────────────────────────────────────────────────
_TOKEN_MASK = "***BOT_TOKEN***"


class _TokenRedactFilter(logging.Filter):
    """Redact the bot token from every log record and formatted line."""

    _token: str = ""

    @classmethod
    def configure(cls, token: str) -> None:
        cls._token = token or ""

    @classmethod
    def redact(cls, value):
        if not cls._token or not isinstance(value, str):
            return value
        return value.replace(cls._token, _TOKEN_MASK)

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        # Redact before formatting
        record.msg = self.redact(record.msg)

        if isinstance(record.args, dict):
            record.args = {
                k: self.redact(v) for k, v in record.args.items()
            }
        elif record.args:
            record.args = tuple(self.redact(a) for a in record.args)

        return True


class _RedactingFormatter(logging.Formatter):
    """Final safety net: redact the token from the rendered log line."""

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        tok = _TokenRedactFilter._token
        return rendered.replace(tok, _TOKEN_MASK) if tok else rendered


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

log = logging.getLogger("MediaIndexer")

_redact = _TokenRedactFilter()

# Attach the filter + redacting formatter to the ROOT handlers
_root = logging.getLogger()
for handler in _root.handlers:
    handler.addFilter(_redact)
    handler.setFormatter(
        _RedactingFormatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

# Reduce noisy libraries that commonly emit token-bearing URLs
for _ln in (
    "httpx",
    "httpcore",
    "urllib3",
    "telegram",
    "telegram.ext",
    "telegram.request",
    "pymongo",
):
    logging.getLogger(_ln).addFilter(_redact)
    logging.getLogger(_ln).setLevel(logging.WARNING)


# ──────────────────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────────────────
load_dotenv()


def _env(name: str, default: Optional[str] = None) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        if default is not None:
            return default
        raise RuntimeError(f"❌  Required env var missing: {name}")
    return val


def _env_int(name: str, default: Optional[int] = None) -> int:
    return int(_env(name, None if default is None else str(default)))


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _env_frozenset(name: str, required: bool = True) -> frozenset:
    if required:
        raw = _env(name)
    else:
        raw = os.getenv(name, "").strip()
    return frozenset(
        int(x.strip()) for x in raw.split(",") if x.strip().isdigit()
    )


def _parse_topic_map(raw: str) -> Dict[str, int]:
    """
    Parse a topic map from ENV entries like `Movies (Hindi):1234|Movies (English):1235`.

    Keys are kept in their ORIGINAL display casing (e.g. "MOVIES (HINDI)") so
    button labels look right. Use `_build_topic_lookup()` below to get a
    normalised dict for free-text (typed) topic-name matching.
    """
    mapping: Dict[str, int] = {}
    raw = (raw or "").strip()
    if not raw:
        return mapping

    for chunk in re.split(r"[|;\n]+", raw):
        part = chunk.strip()
        if not part or ":" not in part:
            continue
        name, id_text = part.rsplit(":", 1)
        name = re.sub(r"\s+", " ", name).strip()
        try:
            topic_id = int(id_text.strip())
        except ValueError:
            continue
        if name:
            mapping[name] = topic_id
    return mapping


# Strips everything except letters/digits after casefolding, so topic-name
# matching is both case-insensitive AND punctuation/spacing-insensitive:
#   "MOVIES (HINDI)"  ->  "movieshindi"
#   "movies (hindi)"  ->  "movieshindi"
#   "Movies Hindi"     -> "movieshindi"
#   "movies   hindi"   -> "movieshindi"
# All four normalise to the same key and therefore match each other.
_TOPIC_KEY_STRIP_RE = re.compile(r"[^a-z0-9]+")


def _normalize_topic_key(text: str) -> str:
    return _TOPIC_KEY_STRIP_RE.sub("", (text or "").casefold())


def _build_topic_lookup(topic_map: Dict[str, int]) -> Dict[str, int]:
    """Build the normalised-key -> topic_id lookup used by `_resolve_topic_name`."""
    lookup: Dict[str, int] = {}
    for name, topic_id in topic_map.items():
        norm = _normalize_topic_key(name)
        if norm:
            lookup[norm] = topic_id
    return lookup


@dataclass(frozen=True)
class Config:
    # ── Required ──────────────────────────────────────────
    BOT_TOKEN:         str       = field(default_factory=lambda: _env("BOT_TOKEN"))
    GROUP_CHAT_ID:     int       = field(default_factory=lambda: _env_int("GROUP_CHAT_ID"))
    ALL_ADDED_SHOWS:   int       = field(default_factory=lambda: _env_int("ALL_ADDED_SHOWS"))
    SEARCH_SHOWS_HERE: int       = field(default_factory=lambda: _env_int("SEARCH_SHOWS_HERE"))
    BACKUP_GROUP_CHAT_ID: int    = field(default_factory=lambda: _env_int("BACKUP_GROUP_CHAT_ID"))
    BACKUP_ALL_ADDED_SHOWS: int  = field(default_factory=lambda: _env_int("BACKUP_ALL_ADDED_SHOWS"))
    ADMIN_IDS:         frozenset = field(default_factory=lambda: _env_frozenset("ADMIN_IDS"))

    # ── Optional per-topic remap for the Backup group (v6.7) ───────────
    # Only needed if the Backup group's topic IDs DON'T line up 1:1 with
    # the MiddleMan (GROUP_CHAT_ID) topic IDs stored on each movie doc.
    # Same "Name:id|Name:id" syntax as TOPIC_MAP, keyed by the SAME
    # topic names used in TOPIC_MAP. If left blank (default), Publish
    # simply reuses the MiddleMan topic_id as-is inside the Backup
    # group — zero extra config needed when both groups mirror each
    # other's topic layout (the current setup).
    BACKUP_TOPIC_MAP: str = field(default_factory=lambda: _env("BACKUP_TOPIC_MAP", ""))


    # ── Link delivery / redemption ────────────────────────
    BOT_USERNAME:      str       = field(default_factory=lambda: _env("BOT_USERNAME", ""))
    MONGODB_URI:       str       = field(default_factory=lambda: _env("MONGODB_URI"))
    MONGODB_DB_NAME:   str       = field(default_factory=lambda: _env("MONGODB_DB_NAME", "StreamVerseOG"))
    GPLINKS_API_KEY:   str       = field(default_factory=lambda: _env("GPLINKS_API_KEY", ""))
    GPLINKS_API_URL:   str       = field(default_factory=lambda: _env("GPLINKS_API_URL", "https://api.gplinks.com/api"))
    TOPIC_MAP:         str       = field(default_factory=lambda: _env("TOPIC_MAP", ""))

    # ── Ignored topics ────────────────────────────────────
    # Comma-separated forum topic (message_thread_id) IDs. Any admin
    # message posted inside one of these topics is completely ignored
    # by the indexer — it will never be batched/forwarded into
    # ALL_ADDED_SHOWS ("SEARCH ALL ADDED SHOWS", topic id 3).
    #
    # Add/remove topic IDs here any time you create a new topic that
    # should be excluded — no code changes needed.
    #
    # Currently configured to ignore:
    #   • id 1    → the built-in "# General" topic
    #   • id 1796 → "GENERAL CHAT (GC)"
    IGNORED_TOPICS: frozenset = field(
        default_factory=lambda: _env_frozenset("IGNORED_TOPICS", required=False)
    )

    # ── Optional ──────────────────────────────────────────
    HOW_TO_LINK: str = field(default_factory=lambda: _env(
        "HOW_TO_LINK", "https://t.me/c/3935135937/9/481"
    ))
    PORT: int = field(default_factory=lambda: _env_int("PORT", 10000))

    # ── File-delivery self-destruct + group footer (v6.3) ──────────────
    # After a member redeems a file/bundle via /start or /send, the bot
    # sends a warning telling them to forward the file(s) to their own
    # Saved Messages because the delivered message(s) will be auto-deleted
    # after DELETE_DELAY_SEC seconds. GROUP_NAME / GROUP_JOIN_LINK are
    # appended as the last line of that warning so members can find their
    # way back to the group. If GROUP_JOIN_LINK is left blank, that footer
    # line is simply omitted.
    GROUP_NAME: str = field(default_factory=lambda: _env("GROUP_NAME", "StreamVerseOG"))
    GROUP_JOIN_LINK: str = field(default_factory=lambda: _env("GROUP_JOIN_LINK", ""))

    GROUP_NAME_2: str = field(default_factory=lambda: _env("GROUP_NAME_2", "StreamVerseOG Backup"))
    GROUP_JOIN_LINK_2: str = field(default_factory=lambda: _env("GROUP_JOIN_LINK_2", ""))


    INSTAGRAM_LABEL: str = field(default_factory=lambda: _env("INSTAGRAM_LABEL", "Instagram"))
    INSTAGRAM_LINK: str = field(default_factory=lambda: _env("INSTAGRAM_LINK", ""))

    DELETE_DELAY_SEC: int = field(default_factory=lambda: _env_int("DELETE_DELAY_SEC", 120))

    # ── Tunable ───────────────────────────────────────────
    DEBOUNCE_SEC:   float = field(default_factory=lambda: _env_float("DEBOUNCE_SEC", 3.0))
    FLUSH_INTERVAL: float = field(default_factory=lambda: _env_float("FLUSH_INTERVAL", 1.0))
    SEEN_TTL_SEC:   int   = field(default_factory=lambda: _env_int("SEEN_TTL_SEC", 21_600))
    MAX_BUTTONS:    int   = field(default_factory=lambda: _env_int("MAX_BUTTONS", 20))
    MAX_BUTTON_LEN: int   = field(default_factory=lambda: _env_int("MAX_BUTTON_LEN", 56))


CFG = Config()

_TOPIC_MAP = _parse_topic_map(CFG.TOPIC_MAP)
_TOPIC_LOOKUP = _build_topic_lookup(_TOPIC_MAP)

# ── Backup-group topic remap (v6.7, Publish feature) ────────────────
# _MIDDLEMAN_ID_TO_NAME:    MiddleMan topic_id -> topic name (reverse of TOPIC_MAP)
# _BACKUP_TOPIC_MAP:        topic name (raw)        -> Backup-group topic_id
# _BACKUP_TOPIC_MAP_NORM:   normalized topic name    -> Backup-group topic_id
#   (normalized the same case/punctuation-insensitive way as TOPIC_MAP's own
#   lookup, so "MOVIES (HINDI)" in TOPIC_MAP still matches "Movies - Hindi"
#   typed in BACKUP_TOPIC_MAP instead of silently missing and falling back
#   to the wrong topic_id.)
_MIDDLEMAN_ID_TO_NAME: Dict[int, str] = {v: k for k, v in _TOPIC_MAP.items()}
_BACKUP_TOPIC_MAP = _parse_topic_map(CFG.BACKUP_TOPIC_MAP)
_BACKUP_TOPIC_MAP_NORM: Dict[str, int] = _build_topic_lookup(_BACKUP_TOPIC_MAP)


def _backup_topic_id(middleman_topic_id: Optional[int]) -> Optional[int]:
    """
    Resolve which topic a Publish should land in inside the Backup group.

    If BACKUP_TOPIC_MAP is configured, remap by topic NAME (looked up via
    the MiddleMan TOPIC_MAP, matched case/punctuation-insensitively).
    Otherwise — the default, zero-config case — simply reuse the same
    topic_id in the Backup group as-is (only safe when both groups happen
    to share identical topic ids, which is NOT true for a freshly created
    replacement group).
    """
    if middleman_topic_id is None:
        return None
    if _BACKUP_TOPIC_MAP:
        name = _MIDDLEMAN_ID_TO_NAME.get(middleman_topic_id)
        norm = _normalize_topic_key(name) if name else ""
        if norm and norm in _BACKUP_TOPIC_MAP_NORM:
            return _BACKUP_TOPIC_MAP_NORM[norm]
        log.warning(
            f"[BACKUP_TOPIC_MAP] no match for MiddleMan topic_id={middleman_topic_id} "
            f"(name={name!r}) — falling back to reusing the same topic_id "
            f"in the Backup group, which is almost certainly WRONG for a "
            f"freshly created group. Add this topic to BACKUP_TOPIC_MAP."
        )
    return middleman_topic_id


# ── Dead-clone MiddleMan mirror groups (v6.8, disaster recovery) ────
#
#  If the PRIMARY MiddleMan (GROUP_CHAT_ID) itself ever gets banned,
#  admin needs a second, totally-private, never-shared "dead" clone
#  group that already contains every post (with a working "🚀 Publish"
#  button) so publishing to the CURRENT Backup group doesn't depend on
#  the primary MiddleMan surviving at all.
#
#  Configure up to 8 mirrors via numbered env vars (1-indexed, gaps OK):
#
#    MIDDLEMAN_MIRROR_1_CHAT_ID=-100xxxxxxxxxx
#    MIDDLEMAN_MIRROR_1_TOPIC_MAP=OTT SHOWS:11|MOVIES (ENGLISH):13|...
#    MIDDLEMAN_MIRROR_1_ALL_ADDED_SHOWS=7
#    MIDDLEMAN_MIRROR_2_CHAT_ID=-100yyyyyyyyyy
#    MIDDLEMAN_MIRROR_2_TOPIC_MAP=OTT SHOWS:21|MOVIES (ENGLISH):23|...
#    MIDDLEMAN_MIRROR_2_ALL_ADDED_SHOWS=17
#
#  TOPIC_MAP uses the SAME topic names as the primary's own TOPIC_MAP
#  (Telegram assigns each group's own topic ids independently — they
#  will NOT match the primary's ids, so this per-mirror map is required
#  for posts to land in the "same" named topic there).
#
#  ALL_ADDED_SHOWS is its OWN dedicated field per mirror (same pattern
#  as the primary's ALL_ADDED_SHOWS and the Backup group's
#  BACKUP_ALL_ADDED_SHOWS) — that's where auto-indexed posts (which
#  have no separate named topic, just the "SEARCH ALL ADDED SHOWS"
#  index) get mirrored. It is NOT read out of TOPIC_MAP.
#
#  Every post made in the primary MiddleMan — /makepost wizard AND
#  automatically-indexed uploads — is best-effort mirrored (recreated,
#  never forwarded) into every configured mirror, carrying the EXACT
#  SAME "🚀 Publish" button (same movie_id, same callback_data) as the
#  primary copy. Since Publish only ever looks the movie up by its
#  MongoDB _id — never by which chat/message the button lives in —
#  pressing Publish from ANY mirror behaves identically to pressing it
#  on the primary. Mirrors are otherwise inert: no search index, no
#  cross-post, nothing browses them — they only exist so a working
#  Publish button survives a primary-MiddleMan ban.
# ──────────────────────────────────────────────────────────
def _load_middleman_mirrors() -> List[dict]:
    mirrors: List[dict] = []
    for i in range(1, 9):
        chat_id_raw = _env(f"MIDDLEMAN_MIRROR_{i}_CHAT_ID", "")
        if not chat_id_raw:
            continue
        try:
            chat_id = int(chat_id_raw)
        except ValueError:
            log.warning(f"[MIRROR] MIDDLEMAN_MIRROR_{i}_CHAT_ID={chat_id_raw!r} is not a valid integer — skipping this mirror.")
            continue

        topic_map_raw = _env(f"MIDDLEMAN_MIRROR_{i}_TOPIC_MAP", "")
        topic_map = _parse_topic_map(topic_map_raw)

        all_added_shows: Optional[int] = None
        all_added_raw = _env(f"MIDDLEMAN_MIRROR_{i}_ALL_ADDED_SHOWS", "")
        if all_added_raw:
            try:
                all_added_shows = int(all_added_raw)
            except ValueError:
                log.warning(
                    f"[MIRROR] MIDDLEMAN_MIRROR_{i}_ALL_ADDED_SHOWS={all_added_raw!r} "
                    f"is not a valid integer — ignoring it for this mirror."
                )

        mirror = {
            "index": i,
            "chat_id": chat_id,
            "topic_map_norm": _build_topic_lookup(topic_map),
            "all_added_shows": all_added_shows,
        }

        # Startup-time validation: catch a missing ALL_ADDED_SHOWS config
        # immediately (loudly, once) instead of it silently misrouting
        # every single auto-indexed post to General chat forever.
        if all_added_shows is None and _normalize_topic_key("ALL_ADDED_SHOWS") not in mirror["topic_map_norm"]:
            log.warning(
                f"[MIRROR] MIDDLEMAN_MIRROR_{i}_ALL_ADDED_SHOWS is not set (and "
                f"TOPIC_MAP has no ALL_ADDED_SHOWS entry either) — auto-indexed "
                f"posts mirrored into chat_id={chat_id} will land in General "
                f"chat instead of a proper 'Search All Added Shows' topic. Set "
                f"MIDDLEMAN_MIRROR_{i}_ALL_ADDED_SHOWS=<topic_id> to fix this."
            )

        mirrors.append(mirror)
    return mirrors


_MIDDLEMAN_MIRRORS: List[dict] = _load_middleman_mirrors()
if _MIDDLEMAN_MIRRORS:
    log.info(f"[MIRROR] {len(_MIDDLEMAN_MIRRORS)} dead MiddleMan mirror group(s) configured: {[m['chat_id'] for m in _MIDDLEMAN_MIRRORS]}")


def _resolve_mirror_topic(mirror: dict, topic_name: str) -> Optional[int]:
    """Resolve the target topic_id for one mirror group.

    "ALL_ADDED_SHOWS" (the logical name used for auto-indexed posts,
    which have no separate named topic) is resolved via the mirror's
    OWN dedicated `all_added_shows` field FIRST — that's the correct,
    intended path, matching how the primary/Backup groups are configured
    (ALL_ADDED_SHOWS / BACKUP_ALL_ADDED_SHOWS are their own env vars,
    not entries inside a pipe-separated topic map).

    Named-topic posts are resolved via topic_map_norm as before. If a
    named topic isn't found there, it falls back to this mirror's
    ALL_ADDED_SHOWS thread rather than losing the post — and finally to
    General chat as an absolute last resort. Every fallback is logged
    so a missing mapping is never silent.
    """
    norm = _normalize_topic_key(topic_name) if topic_name else ""
    all_added_norm = _normalize_topic_key("ALL_ADDED_SHOWS")

    if norm == all_added_norm and mirror.get("all_added_shows") is not None:
        return mirror["all_added_shows"]

    topic_map_norm = mirror.get("topic_map_norm") or {}
    if norm and norm in topic_map_norm:
        return topic_map_norm[norm]

    if mirror.get("all_added_shows") is not None:
        log.warning(
            f"[MIRROR] mirror chat_id={mirror['chat_id']} has no topic mapped "
            f"for {topic_name!r} — using its ALL_ADDED_SHOWS fallback thread."
        )
        return mirror["all_added_shows"]

    if all_added_norm in topic_map_norm:
        log.warning(
            f"[MIRROR] mirror chat_id={mirror['chat_id']} has no topic mapped "
            f"for {topic_name!r} — using its ALL_ADDED_SHOWS fallback thread."
        )
        return topic_map_norm[all_added_norm]

    log.warning(
        f"[MIRROR] mirror chat_id={mirror['chat_id']} has NO topic mapping for "
        f"{topic_name!r} and no ALL_ADDED_SHOWS fallback configured — "
        f"posting to General chat instead."
    )
    return None


async def _mirror_post_to_middlemen(
    bot: Bot,
    *,
    poster_file_id: Optional[str],
    text: str,
    buttons: List[List[InlineKeyboardButton]],
    topic_name: str,
) -> Dict[int, Message]:
    """Best-effort: recreate this exact MiddleMan post (poster + text +
    buttons, INCLUDING the same "🚀 Publish" button) inside every
    configured dead mirror group. Never raises — a mirror failing must
    never block or break the primary MiddleMan post.

    Returns {mirror_chat_id: sent_message} for every mirror the post was
    successfully sent to, so a caller (e.g. `_mirror_crosspost_stub`) can
    link BACK to that specific message from a stub post elsewhere in the
    SAME mirror group.
    """
    sent_by_chat: Dict[int, Message] = {}
    if not _MIDDLEMAN_MIRRORS:
        return sent_by_chat
    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
    for mirror in _MIDDLEMAN_MIRRORS:
        target_topic_id = _resolve_mirror_topic(mirror, topic_name)
        try:
            if poster_file_id:
                sent = await safe_send(
                    bot,
                    method="send_photo",
                    chat_id=mirror["chat_id"],
                    message_thread_id=target_topic_id,
                    photo=poster_file_id,
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML,
                )
            else:
                sent = await safe_send(
                    bot,
                    chat_id=mirror["chat_id"],
                    message_thread_id=target_topic_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML,
                )
            if sent:
                sent_by_chat[mirror["chat_id"]] = sent
        except TelegramError as exc:
            log.warning(f"[MIRROR] failed to mirror post into chat_id={mirror['chat_id']}: {exc}")
    return sent_by_chat


async def _mirror_crosspost_stub(
    bot: Bot,
    *,
    mirror_messages: Dict[int, Message],
    poster_file_id: Optional[str],
    text: str,
    title: str,
    specific_topic_name: str,
) -> None:
    """v6.11: after `_mirror_post_to_middlemen` puts the FULL post (poster +
    all quality/season buttons + Publish) into a mirror's specific named
    topic, post a lightweight STUB — poster + a SINGLE "Open Post" button
    linking back to that exact message inside the SAME mirror group's
    specific topic — into that mirror's own ALL_ADDED_SHOWS topic instead.

    This mirrors exactly what the primary MiddleMan already does via
    `_crosspost_to_all_added_shows`: ALL_ADDED_SHOWS is a search index that
    points AT the real post, never a second copy of every download/Publish
    button. Only mirrors that actually received the specific-topic post
    (present in `mirror_messages`) get a stub — never raises, a failure
    here must never block anything else.
    """
    if not _MIDDLEMAN_MIRRORS or not mirror_messages:
        return
    button_label = _button_label(title) if title else "Open Post"
    for mirror in _MIDDLEMAN_MIRRORS:
        chat_id = mirror["chat_id"]
        sent_msg = mirror_messages.get(chat_id)
        if not sent_msg:
            continue

        specific_topic_id = _resolve_mirror_topic(mirror, specific_topic_name)
        all_added_topic_id = _resolve_mirror_topic(mirror, "ALL_ADDED_SHOWS")
        if specific_topic_id:
            link = make_topic_msg_link(chat_id, specific_topic_id, sent_msg.message_id)
        else:
            link = make_msg_link(chat_id, sent_msg.message_id)

        stub_buttons = InlineKeyboardMarkup([[InlineKeyboardButton(button_label, url=link)]])
        try:
            if poster_file_id:
                await safe_send(
                    bot,
                    method="send_photo",
                    chat_id=chat_id,
                    message_thread_id=all_added_topic_id,
                    photo=poster_file_id,
                    caption=text,
                    reply_markup=stub_buttons,
                    parse_mode=ParseMode.HTML,
                )
            else:
                await safe_send(
                    bot,
                    chat_id=chat_id,
                    message_thread_id=all_added_topic_id,
                    text=text,
                    reply_markup=stub_buttons,
                    parse_mode=ParseMode.HTML,
                )
        except TelegramError as exc:
            log.warning(f"[MIRROR] failed to post crosspost stub into chat_id={chat_id}: {exc}")

# ── Link-delivery globals ───────────────────────────────
_BOT_USERNAME: str = CFG.BOT_USERNAME.strip()
_DB_LOCK = threading.Lock()

# ▶ Activate token masking now that BOT_TOKEN is known
_TokenRedactFilter.configure(CFG.BOT_TOKEN)


# ──────────────────────────────────────────────────────────
#  STATS
# ──────────────────────────────────────────────────────────
_START_TIME = time.time()


class _Stats:
    """Thread-safe counter bag."""

    def __init__(self):
        self._lock         = threading.Lock()
        self.indexed       = 0
        self.zip_hints     = 0
        self.errors        = 0
        self.skipped_dupes = 0
        self.welcomes_sent = 0
        self.auto_deleted  = 0
        self.backup_indexed = 0

    def inc(self, key: str, n: int = 1):
        with self._lock:
            setattr(self, key, getattr(self, key) + n)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "indexed":       self.indexed,
                "zip_hints":     self.zip_hints,
                "errors":        self.errors,
                "skipped_dupes": self.skipped_dupes,
                "welcomes_sent": self.welcomes_sent,
                "auto_deleted":  self.auto_deleted,
                "backup_indexed": self.backup_indexed,
                "uptime_sec":    int(time.time() - _START_TIME),
            }


stats = _Stats()


# ──────────────────────────────────────────────────────────
#  FLASK HEALTH SERVER
# ──────────────────────────────────────────────────────────
web_app = Flask(__name__)


@web_app.route("/")
def _health():
    return jsonify({"status": "ok", **stats.snapshot()})


@web_app.route("/ping")
def _ping():
    return "pong", 200


def _run_flask():
    web_app.run(host="0.0.0.0", port=CFG.PORT, use_reloader=False, threaded=True)


# ──────────────────────────────────────────────────────────
#  TTL CACHE
# ──────────────────────────────────────────────────────────
class TTLCache:
    """Thread-safe string set where every entry expires after `ttl` seconds."""

    def __init__(self, ttl: int):
        self._ttl  = ttl
        self._data: Dict[str, float] = {}
        self._lock = threading.Lock()

    def has(self, key: str) -> bool:
        with self._lock:
            ts = self._data.get(key)
            if ts is None:
                return False
            if time.time() - ts > self._ttl:
                del self._data[key]
                return False
            return True

    def set(self, key: str):
        with self._lock:
            self._data[key] = time.time()

    def delete(self, key: str):
        with self._lock:
            self._data.pop(key, None)

    def clear(self):
        with self._lock:
            self._data.clear()

    def keys(self) -> List[str]:
        """Return a snapshot of all current (non-expired) keys."""
        cutoff = time.time() - self._ttl
        with self._lock:
            return [k for k, ts in self._data.items() if ts >= cutoff]

    def purge_expired(self) -> int:
        cutoff = time.time() - self._ttl
        with self._lock:
            stale = [k for k, ts in self._data.items() if ts < cutoff]
            for k in stale:
                del self._data[k]
        return len(stale)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


seen_cache = TTLCache(ttl=CFG.SEEN_TTL_SEC)   # prevent re-indexing same title
zip_cache  = TTLCache(ttl=CFG.SEEN_TTL_SEC)   # track if zip hint already shown


# ──────────────────────────────────────────────────────────
#  LINK STORE (MongoDB / Atlas)
#
#  Collections in the configured database:
#    "downloads" → one document per uploaded file
#      { token, file_id, file_type, file_name, caption,
#        deep_link, short_url, source_chat_id,
#        source_message_id, created_at, downloads_count }
#    "bundles"   → one document per quality/season/sendall bundle
#      { token, label, kind, child_tokens: [...], deep_link,
#        short_url, source_chat_id, source_message_id,
#        created_at, downloads_count }
#    "movies"    → one document per /makepost movie/series post
#      (see "movies collection" section further below, added in v6.6
#      for the "🚀 Publish" Backup-deploy feature).
#
#  child_tokens is stored as a native Mongo array (no more manual
#  json.dumps/json.loads round-tripping like the old SQLite store).
# ──────────────────────────────────────────────────────────
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from bson import ObjectId
from bson.errors import InvalidId

_mongo_client: MongoClient | None = None
_mongo_db: Database | None = None
_downloads_col: Collection | None = None
_bundles_col: Collection | None = None
_movies_col: Collection | None = None
_publish_records_col: Collection | None = None

def _downloads_collection() -> Collection:
    if _downloads_col is None:
        raise RuntimeError("MongoDB downloads collection is not initialized.")
    return _downloads_col


def _bundles_collection() -> Collection:
    if _bundles_col is None:
        raise RuntimeError("MongoDB bundles collection is not initialized.")
    return _bundles_col


def _movies_collection() -> Collection:
    if _movies_col is None:
        raise RuntimeError("MongoDB movies collection is not initialized.")
    return _movies_col


def _publish_records_collection() -> Collection:
    if _publish_records_col is None:
        raise RuntimeError("MongoDB publish_records collection is not initialized.")
    return _publish_records_col


def init_link_store() -> None:
    """
    Connect to MongoDB Atlas (CFG.MONGODB_URI / CFG.MONGODB_DB_NAME) and
    ensure the required indexes exist. Called once from on_startup().
    """
    global _mongo_client, _mongo_db, _downloads_col, _bundles_col, _movies_col, _publish_records_col

    client = MongoClient(CFG.MONGODB_URI, serverSelectionTimeoutMS=10_000)
    client.admin.command("ping")

    db = client[CFG.MONGODB_DB_NAME]
    downloads_col = db["downloads"]
    bundles_col = db["bundles"]
    movies_col = db["movies"]
    publish_records_col = db["publish_records"]

    downloads_col.create_index("token", unique=True)
    bundles_col.create_index("token", unique=True)
    publish_records_col.create_index("movie_id", unique=True)

    _mongo_client = client
    _mongo_db = db
    _downloads_col = downloads_col
    _bundles_col = bundles_col
    _movies_col = movies_col
    _publish_records_col = publish_records_col

    log.info(
        f"[MONGO] Connected — db={CFG.MONGODB_DB_NAME!r} "
        f"(downloads={downloads_col.estimated_document_count()}, "
        f"bundles={bundles_col.estimated_document_count()}, "
        f"movies={movies_col.estimated_document_count()}, "
        f"publish_records={publish_records_col.estimated_document_count()})"
    )


def _store_download(record: dict) -> None:
    col = _downloads_collection()

    document = {
        "token": record["token"],
        "file_id": record["file_id"],
        "file_type": record["file_type"],
        "file_name": record.get("file_name", ""),
        "caption": record.get("caption", ""),
        "deep_link": record["deep_link"],
        "short_url": record.get("short_url", ""),
        "source_chat_id": record.get("source_chat_id"),
        "source_message_id": record.get("source_message_id"),
        "created_at": record.get("created_at", time.time()),
        "downloads_count": record.get("downloads_count", 0),
    }

    with _DB_LOCK:
        existing = col.find_one({"token": document["token"]})

        if existing:
            same_record = (
                existing.get("file_id") == document["file_id"]
                and existing.get("file_type") == document["file_type"]
                and existing.get("deep_link") == document["deep_link"]
            )

            if not same_record:
                log.error(
                    "[TOKEN COLLISION] Token %s already belongs to another file. Refusing overwrite.",
                    record["token"],
                )
                return

        col.replace_one(
            {"token": document["token"]},
            document,
            upsert=True,
        )


def _get_download(token: str) -> Optional[dict]:
    col = _downloads_collection()
    with _DB_LOCK:
        return col.find_one({"token": token}, {"_id": 0})


def _increment_download_count(token: str) -> None:
    col = _downloads_collection()
    with _DB_LOCK:
        col.update_one({"token": token}, {"$inc": {"downloads_count": 1}})


def _store_bundle(record: dict) -> None:
    col = _bundles_collection()

    document = {
        "token": record["token"],
        "label": record.get("label", ""),
        "kind": record.get("kind", "bundle"),
        "child_tokens": record.get("child_tokens", []),
        "deep_link": record["deep_link"],
        "short_url": record.get("short_url", ""),
        "source_chat_id": record.get("source_chat_id"),
        "source_message_id": record.get("source_message_id"),
        "created_at": record.get("created_at", time.time()),
        "downloads_count": record.get("downloads_count", 0),
    }

    with _DB_LOCK:
        existing = col.find_one({"token": document["token"]})

        if existing:
            same_record = (
                existing.get("label") == document["label"]
                and existing.get("kind") == document["kind"]
                and existing.get("child_tokens") == document["child_tokens"]
            )

            if not same_record:
                log.error(
                    "[TOKEN COLLISION] Bundle token %s already belongs to another bundle. Refusing overwrite.",
                    document["token"],
                )
                return

        col.replace_one(
            {"token": document["token"]},
            document,
            upsert=True,
        )


def _get_bundle(token: str) -> Optional[dict]:
    col = _bundles_collection()
    with _DB_LOCK:
        doc = col.find_one({"token": token}, {"_id": 0})
        if not doc:
            return None
        if not isinstance(doc.get("child_tokens"), list):
            doc["child_tokens"] = []
        return doc


def _increment_bundle_count(token: str) -> None:
    col = _bundles_collection()
    with _DB_LOCK:
        col.update_one({"token": token}, {"$inc": {"downloads_count": 1}})


# ──────────────────────────────────────────────────────────
#  MOVIES COLLECTION  (v6.6 — "🚀 Publish" Backup deploy)
#
#  Exactly ONE document per /makepost movie/series post. Captures
#  everything needed to recreate the EXACT same Telegram post (text,
#  poster, buttons) inside a Backup group later, without re-uploading
#  or duplicating any data:
#
#    { title, mode, poster_file_id, text, topic_id, topic_name,
#      buttons: [{label, url}, ...], created_at,
#      source_chat_id, source_message_id }
#
#  Button URLs are the SAME permanent GPLinks/bot deep-links already
#  used on the MiddleMan post — they are not tied to any message id,
#  so reposting them verbatim inside Backup works unchanged.
# ──────────────────────────────────────────────────────────
def _store_movie(document: dict) -> str:
    """Insert a new movie document and return its Mongo _id as a string."""
    col = _movies_collection()
    with _DB_LOCK:
        result = col.insert_one(document)
    return str(result.inserted_id)


def _get_movie(movie_id: str) -> Optional[dict]:
    col = _movies_collection()
    try:
        oid = ObjectId(movie_id)
    except (InvalidId, TypeError):
        return None
    with _DB_LOCK:
        doc = col.find_one({"_id": oid})
    if doc:
        doc["_id"] = str(doc["_id"])
    return doc


def _update_movie_source_message(movie_id: str, message_id: int) -> None:
    """Best-effort: record the MiddleMan message id the movie was posted as."""
    col = _movies_collection()
    try:
        oid = ObjectId(movie_id)
    except (InvalidId, TypeError):
        return
    with _DB_LOCK:
        col.update_one({"_id": oid}, {"$set": {"source_message_id": message_id}})


# ──────────────────────────────────────────────────────────
#  PUBLISH RECORDS  (v6.7 — "🚀 Publish" MiddleMan → Backup)
#
#  ONE lightweight document per movie, upserted on every (re-)publish.
#  Movie data itself is NEVER duplicated — this only tracks WHERE the
#  latest Backup copy lives so it can be found/replaced later:
#
#    { movie_id, middleman_message_id, public_group_id,
#      public_topic_id, public_message_id, publish_timestamp }
# ──────────────────────────────────────────────────────────
def _store_publish_record(record: dict) -> None:
    col = _publish_records_collection()
    document = {
        "movie_id":              record["movie_id"],
        "middleman_message_id":  record.get("middleman_message_id"),
        "public_group_id":       record.get("public_group_id"),
        "public_topic_id":       record.get("public_topic_id"),
        "public_message_id":     record.get("public_message_id"),
        "publish_timestamp":     record.get("publish_timestamp", time.time()),
    }
    with _DB_LOCK:
        col.replace_one({"movie_id": document["movie_id"]}, document, upsert=True)


def _get_publish_record(movie_id: str) -> Optional[dict]:
    col = _publish_records_collection()
    with _DB_LOCK:
        return col.find_one({"movie_id": movie_id}, {"_id": 0})


def _store_index_movie(
    *,
    title: str,
    text: str,
    buttons: List[List[InlineKeyboardButton]],
    topic_id: Optional[int] = None,
) -> str:
    """
    v6.7: persist a lightweight "movie" document for AUTO-indexed posts
    (the automatic indexing pipeline in `process_batch`) too — not just
    /makepost wizard posts — so the same admin-only "🚀 Publish" button
    can recreate them inside the disposable public Backup group later.

    topic_id=None means this post already lives directly inside
    ALL_ADDED_SHOWS (it IS the search-index post, there's no separate
    per-topic post to also index) — see `handle_deploy_callback`.
    """
    movie_buttons = [
        {"label": btn.text, "url": btn.url}
        for row in buttons for btn in row
        if getattr(btn, "url", None)
    ]
    movie_doc = {
        "title": title,
        "mode": "auto",
        "poster_file_id": None,
        "text": text,
        "topic_id": topic_id,
        "topic_name": _topic_name_by_id(topic_id) if topic_id else "",
        "buttons": movie_buttons,
        "created_at": time.time(),
        "source_chat_id": CFG.GROUP_CHAT_ID,
        "source_message_id": None,
    }
    return _store_movie(movie_doc)


# ──────────────────────────────────────────────────────────
#  PENDING BATCH
# ──────────────────────────────────────────────────────────
@dataclass
class Batch:
    msgs:            List  = field(default_factory=list)
    last_ts:         float = field(default_factory=time.time)
    thread_ids:      set   = field(default_factory=set)
    media_group_ids: set   = field(default_factory=set)

    def touch(self):
        self.last_ts = time.time()

    def is_ready(self, debounce: float) -> bool:
        return time.time() - self.last_ts >= debounce


# Global state — only accessed inside _pending_lock (async context)
_pending: Dict[str, Batch] = {}
_pending_lock: asyncio.Lock                    # initialised in on_startup()
_mgid_map: Dict[str, str]  = {}               # media_group_id → movie_key


# ──────────────────────────────────────────────────────────
#  PRIVATE FILE / LINK HELPERS
# ──────────────────────────────────────────────────────────
def _supported_private_payload(msg) -> Optional[dict]:
    """Extract a sendable Telegram file payload from a private message."""
    if getattr(msg, "document", None):
        doc = msg.document
        return {
            "file_type": "document",
            "file_id": doc.file_id,
            "file_name": getattr(doc, "file_name", "") or extract_text(msg),
            "caption": getattr(msg, "caption", "") or "",
        }
    if getattr(msg, "video", None):
        vid = msg.video
        return {
            "file_type": "video",
            "file_id": vid.file_id,
            "file_name": getattr(vid, "file_name", "") or extract_text(msg),
            "caption": getattr(msg, "caption", "") or "",
        }
    if getattr(msg, "audio", None):
        aud = msg.audio
        return {
            "file_type": "audio",
            "file_id": aud.file_id,
            "file_name": getattr(aud, "file_name", "") or extract_text(msg),
            "caption": getattr(msg, "caption", "") or "",
        }
    if getattr(msg, "animation", None):
        ani = msg.animation
        return {
            "file_type": "animation",
            "file_id": ani.file_id,
            "file_name": getattr(ani, "file_name", "") or extract_text(msg),
            "caption": getattr(msg, "caption", "") or "",
        }
    if getattr(msg, "voice", None):
        voice = msg.voice
        return {
            "file_type": "voice",
            "file_id": voice.file_id,
            "file_name": extract_text(msg) or "voice",
            "caption": getattr(msg, "caption", "") or "",
        }
    if getattr(msg, "video_note", None):
        note = msg.video_note
        return {
            "file_type": "video_note",
            "file_id": note.file_id,
            "file_name": extract_text(msg) or "video_note",
            "caption": getattr(msg, "caption", "") or "",
        }
    if getattr(msg, "photo", None):
        photo = msg.photo[-1]
        return {
            "file_type": "photo",
            "file_id": photo.file_id,
            "file_name": extract_text(msg) or "photo",
            "caption": getattr(msg, "caption", "") or "",
        }
    return None


def _bot_username() -> str:
    return _BOT_USERNAME.strip()


def make_bot_start_link(token: str) -> str:
    username = _bot_username()
    if not username:
        raise RuntimeError("BOT_USERNAME is not set and could not be resolved at startup.")
    return f"https://t.me/{username}?start={token}"


async def _shorten_with_gplinks(long_url: str, alias: Optional[str] = None) -> str:
    """Return a GPLinks short URL, or the original URL on failure."""
    if not CFG.GPLINKS_API_KEY.strip():
        return long_url

    endpoints = [CFG.GPLINKS_API_URL.strip()]
    alt = None
    if ".com" in endpoints[0]:
        alt = endpoints[0].replace(".com", ".in")
    elif ".in" in endpoints[0]:
        alt = endpoints[0].replace(".in", ".com")
    if alt and alt not in endpoints:
        endpoints.append(alt)

    params = {
        "api": CFG.GPLINKS_API_KEY.strip(),
        "url": long_url,
    }
    if alias:
        params["alias"] = alias

    errors: List[str] = []
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for endpoint in endpoints:
            try:
                resp = await client.get(endpoint, params=params)
                body = resp.text.strip()
                if not body:
                    errors.append(f"{endpoint}: empty response")
                    continue

                try:
                    data = resp.json()
                except Exception:
                    data = None

                if isinstance(data, dict):
                    if data.get("status") == "success" and data.get("shortenedUrl"):
                        return str(data["shortenedUrl"]).strip().strip('"')
                    msg = data.get("message") or data.get("error") or body
                    errors.append(f"{endpoint}: {msg}")
                    continue

                if body.lower().startswith("http"):
                    return body
                errors.append(f"{endpoint}: unexpected response")
            except Exception as exc:
                errors.append(f"{endpoint}: {exc}")

    log.warning("GPLinks shortening failed: %s", " | ".join(errors))
    return long_url


def _trim_caption(text: str, limit: int = 900) -> str:
    text = (text or "").strip()
    return text[:limit]


async def _deliver_file(bot: Bot, chat_id: int, record: dict) -> Optional[Message]:
    file_type = record["file_type"]
    file_id = record["file_id"]
    caption = _trim_caption(record.get("caption") or record.get("file_name") or "")

    if file_type == "document":
        return await bot.send_document(chat_id=chat_id, document=file_id, caption=caption or None)
    if file_type == "video":
        return await bot.send_video(chat_id=chat_id, video=file_id, caption=caption or None)
    if file_type == "audio":
        return await bot.send_audio(chat_id=chat_id, audio=file_id, caption=caption or None)
    if file_type == "animation":
        return await bot.send_animation(chat_id=chat_id, animation=file_id, caption=caption or None)
    if file_type == "voice":
        return await bot.send_voice(chat_id=chat_id, voice=file_id, caption=caption or None)
    if file_type == "video_note":
        return await bot.send_video_note(chat_id=chat_id, video_note=file_id)
    if file_type == "photo":
        return await bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption or None)
    return None


async def _deliver_token(bot: Bot, chat_id: int, token: str, *, _visited: Optional[set] = None) -> List[Message]:
    """
    Deliver a download token or bundle token recursively.
    Returns the list of Message objects the bot actually sent (files AND any
    bundle "header" messages), so callers can schedule their auto-deletion.
    """
    if _visited is None:
        _visited = set()
    if token in _visited:
        return []
    _visited.add(token)

    record = _get_download(token)
    if record:
        sent = await _deliver_file(bot, chat_id, record)
        if sent:
            _increment_download_count(token)
            return [sent]
        return []

    bundle = _get_bundle(token)
    if not bundle:
        return []

    label = (bundle.get("label") or "").strip()
    kind = (bundle.get("kind") or "bundle").strip()
    child_tokens = bundle.get("child_tokens") or []
    delivered: List[Message] = []

    header = None
    if label:
        heading = label if kind == "bundle" else f"{label}"
        header = f"📦  <b>{html_escape(heading)}</b>"
    elif kind:
        header = f"📦  <b>{html_escape(kind.upper())}</b>"

    if header:
        header_msg = await bot.send_message(chat_id=chat_id, text=header, parse_mode=ParseMode.HTML)
        if header_msg:
            delivered.append(header_msg)

    for child in child_tokens:
        delivered.extend(await _deliver_token(bot, chat_id, str(child), _visited=_visited))

    _increment_bundle_count(token)
    return delivered


# ──────────────────────────────────────────────────────────
#  DELIVERY SELF-DESTRUCT NOTICE  +  AUTO-DELETE  (v6.3)
#
#  Every time a member redeems a file or bundle via /start / /send, the
#  bot follows up with a short warning (tells them to forward everything
#  to Saved Messages) and schedules the delivered message(s) for deletion
#  after CFG.DELETE_DELAY_SEC seconds. The warning message itself is
#  intentionally left alone — only the actual file messages (and any
#  bundle header messages) are removed.
# ──────────────────────────────────────────────────────────
def _delete_delay_minutes_label() -> str:
    """Render CFG.DELETE_DELAY_SEC as a friendly '2' / '1.5' minutes label."""
    minutes = CFG.DELETE_DELAY_SEC / 60
    if minutes == int(minutes):
        return str(int(minutes))
    return f"{minutes:.1f}"


def _build_delivery_notice_text(user) -> str:
    """
    Build the HTML text of the post-delivery warning message.
    """
    name = "there"
    if user is not None:
        name = html_escape(user.full_name or user.username or str(user.id))

    return DELETE_WARNING_TEMPLATE_HTML.format(
        name=name,
        minutes=_delete_delay_minutes_label(),
        group_name=html_escape(CFG.GROUP_NAME.strip() or "our group"),
        group_link=html_escape(CFG.GROUP_JOIN_LINK.strip(), quote=True),

        group_name_2=html_escape(CFG.GROUP_NAME_2.strip() or "our group"),
        group_link_2=html_escape(CFG.GROUP_JOIN_LINK_2.strip(), quote=True),

        instagram_label=html_escape(CFG.INSTAGRAM_LABEL.strip() or "Instagram"),
        instagram_link=html_escape(CFG.INSTAGRAM_LINK.strip(), quote=True),
    )


async def _schedule_delete(bot: Bot, chat_id: int, message_id: int, delay: float) -> None:
    """Wait `delay` seconds, then delete a single message. Never raises."""
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        stats.inc("auto_deleted")
        log.debug(f"[AUTO-DELETE] removed message {message_id} in chat {chat_id}")
    except asyncio.CancelledError:
        raise
    except TelegramError as exc:
        # Common and harmless: the member already deleted/forwarded it,
        # blocked the bot, or the message is simply too old to delete.
        log.debug(f"[AUTO-DELETE] could not delete message {message_id} in chat {chat_id}: {exc}")
    except Exception as exc:
        log.warning(f"[AUTO-DELETE] unexpected error deleting {message_id} in chat {chat_id}: {exc}")


async def _send_delivery_notice(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user,
    delivered_messages: List[Message],
) -> None:
    """Send the self-destruct warning and schedule auto-deletion of every
    message that was just delivered (files + any bundle header lines)."""
    text = _build_delivery_notice_text(user)
    await safe_send(
        context.application.bot,
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

    delay = max(1, CFG.DELETE_DELAY_SEC)
    for m in delivered_messages:
        if m is None:
            continue
        asyncio.create_task(
            _schedule_delete(context.application.bot, chat_id, m.message_id, delay),
            name=f"autodelete-{chat_id}-{m.message_id}",
        )


# ──────────────────────────────────────────────────────────
#  TEXT EXTRACTION
# ──────────────────────────────────────────────────────────
def extract_text(msg) -> str:
    """Return the most meaningful text we can pull from a message."""
    for attr in ("text", "caption"):
        val = getattr(msg, attr, None)
        if val and val.strip():
            return val.strip()
    doc = getattr(msg, "document", None)
    if doc:
        name = getattr(doc, "file_name", None)
        if name and name.strip():
            return name.strip()
    return ""


# ──────────────────────────────────────────────────────────
#  NORMALISER  +  TITLE UTILITIES
# ──────────────────────────────────────────────────────────

_QUALITY_RE = re.compile(
    r"\b("
    r"4k|2160p|1080p|720p|480p|360p|240p|uhd|fhd|hd\b|sd\b|"
    r"bluray|blu.?ray|bdrip|brrip|web.?dl|webrip|hdrip|"
    r"hdtv|dvdrip|dvdscr|dvd|hdcam|cam\b|ts\b|tc\b|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|xvid|divx|"
    r"aac|ac3|mp3|dts|truehd|flac|eac3|dd5\.?1|"
    r"10bit|12bit|8bit|hdr10\+?|sdr\b|dolby|atmos|dv\b|hdr\b|"
    r"extended|theatrical|unrated|directors?.?cut|remastered|restored|"
    r"dubbed|dual.?audio|multi.?audio|multi\b|subbed|esubs?|"
    r"hindi|english|tamil|telugu|kannada|malayalam|"
    r"bengali|punjabi|marathi|gujarati|urdu|"
    r"proper|repack|internal|nf\b|amzn\b|ott\b"
    r")\b",
    re.I,
)

_EPISODE_RE = re.compile(
    r"\b("
    r"s\d{1,2}e\d{1,3}|s\d{1,2}[-–]s?\d{1,2}|"
    r"e\d{1,3}|ep\s*\d+|episode\s*\d+|"
    r"season\s*\d+|part\s*\d+|vol\.?\s*\d+"
    r")\b",
    re.I,
)

_FILE_EXT_RE = re.compile(
    r"\.(mkv|mp4|avi|mov|wmv|flv|m4v|ts|"
    r"zip|rar|7z|tar|gz|001|002|003|004|005)$",
    re.I,
)

_URL_RE = re.compile(r'https?://[^\s<>\"]+', re.I)


def _strip_urls(text: str) -> str:
    return _URL_RE.sub(" ", text or "").strip()


def _extract_primary_url(text: str) -> str:
    m = _URL_RE.search(text or "")
    if not m:
        return ""
    return m.group(0).rstrip(').,]}>\'"')

# Matches full archive suffixes: .zip.001, .rar.003, .part01.rar, .part002.zip, bare .001
_ARCHIVE_SUFFIX_RE = re.compile(
    r"("
    r"\.(zip|rar|7z|tar)(\.?\d{3,})?$"
    r"|\.part\d+\.(rar|zip|7z)$"
    r"|\.\d{3,}$"
    r")",
    re.I,
)

_QUALITY_TAG_RE = re.compile(
    r"\b("
    r"4k|2160p|1080p|720p|480p|360p|uhd|"
    r"x265|x264|h\.?265|h\.?264|hevc|avc|"
    r"bluray|blu.?ray|web.?dl|webrip|hdrip|hdtv|dvdrip|"
    r"hdr10\+?|hdr\b|sdr\b|10bit|"
    r"hindi|english|tamil|telugu|kannada|malayalam|bengali|punjabi|marathi|"
    r"dubbed|dual.?audio|multi\b"
    r")\b",
    re.I,
)

_SERIES_RE = re.compile(
    r"\b("
    r"s\d{1,2}e\d{1,3}|s\d{1,2}[-–]s?\d{1,2}|"
    r"season|episode|ep\d+|mini.?series|complete.?series"
    r")\b",
    re.I,
)

_DOCU_RE = re.compile(r"\b(documentary|docuseries|docu)\b", re.I)

# Year: 1950–2030 range is safe for media titles
_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-2]\d)\b")

# Quality → sort rank (lower number = higher quality)
_QUALITY_RANK: List[Tuple[str, int]] = [
    (r"4k|2160p|uhd",    0),
    (r"1080p|fhd",       1),
    (r"720p",            2),
    (r"480p",            3),
    (r"360p",            4),
    (r"240p|sd\b",       5),
]


def _title_from_archive(filename: str) -> str:
    """
    Strip archive suffixes from a filename to expose the clean title fragment.

        "Movie.2026.1080p.BluRay.x265.zip.001"  →  "Movie.2026.1080p.BluRay.x265"
        "Show.S01.Complete.part01.rar"           →  "Show.S01.Complete"
        "Film.2025.1080p.rar"                    →  "Film.2025.1080p"
    """
    return _ARCHIVE_SUFFIX_RE.sub("", filename.strip()).strip()


def movie_key(text: str) -> str:
    """
    Normalise a title string into a stable deduplication key.

    Archive suffixes are stripped FIRST so that:
        "Movie.2026.zip.001"  and  "Movie 2026 1080p"
    both produce the same key → "movie 2026".
    """
    t = _strip_urls(text).lower()
    t = _ARCHIVE_SUFFIX_RE.sub("", t)
    t = _EPISODE_RE.sub(" ", t)
    t = _QUALITY_RE.sub(" ", t)
    t = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", t)
    t = re.sub(r"[._\-|/\\+@#]+", " ", t)
    t = re.sub(r"\b(19|20)\d{2}\b", " ", t)
    t = re.sub(r"\b(zip|rar|7z|tar)\b", " ", t)
    t = re.sub(r"\b\d{3}\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    tokens = [tok for tok in t.split() if len(tok) > 1]
    return " ".join(tokens)


_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "but", "or", "for", "nor",
    "on", "at", "to", "by", "in", "of", "up", "as", "vs",
})


def clean_title_for_display(text: str) -> Tuple[str, str]:
    """
    Return (display_title, year) after stripping quality tags and year.

        "Spider-Man No Way Home 2021 1080p BluRay x265 Hindi"
        → ("Spider-Man No Way Home", "2021")
    """
    text = _strip_urls(text)
    ym = _YEAR_RE.search(text)
    year = ym.group(1) if ym else ""

    t = _QUALITY_RE.sub(" ", text)
    t = re.sub(r"\b(19|20)\d{2}\b", " ", t)
    t = re.sub(r"\b(zip|rar|7z|tar)\b", " ", t, flags=re.I)
    t = re.sub(r"\b\d{3}\b", " ", t)
    t = re.sub(r"[._\-|/\\+@#]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    words = t.split()
    if not words:
        return "Download Link", year

    result = []
    for i, w in enumerate(words):
        low = w.lower()
        if i == 0 or low not in _STOP_WORDS:
            result.append(w.upper() if len(w) <= 2 and low not in _STOP_WORDS else w.capitalize())
        else:
            result.append(low)

    return " ".join(result), year


def is_filename(text: str) -> bool:
    """Return True if the text ends with a known media/archive extension."""
    return bool(_FILE_EXT_RE.search(text.strip()))


def is_file_only_message(msg) -> bool:
    """
    Return True when the message is a bare file with no meaningful caption.
    These are not used as index link anchors (but are still kept in batches
    so zip-detection and file counts work).
    """
    for attr in ("text", "caption"):
        val = getattr(msg, attr, None)
        if val and val.strip() and not is_filename(val.strip()):
            return False
    return bool(getattr(msg, "document", None))


def extract_quality_tags(text: str) -> str:
    """
    Pull quality/codec/language tokens from a title string.
    Returns a compact label, e.g. "1080p · x265 · Hindi".
    """
    tags = _QUALITY_TAG_RE.findall(text)
    if not tags:
        return ""
    seen, unique = set(), []
    for t in tags:
        tl = t.lower()
        if tl not in seen:
            seen.add(tl)
            unique.append(t.upper() if len(t) <= 4 else t.capitalize())
    return " · ".join(unique)


def _determine_icon(title: str) -> str:
    """Return an appropriate content-type icon."""
    if _DOCU_RE.search(title):
        return "🎥"
    if _SERIES_RE.search(title):
        return "📺"
    return "🎬"


def _btn_quality_rank(label: str) -> int:
    """Return sort rank for a quality button label (lower = higher quality)."""
    low = label.lower()
    for pattern, rank in _QUALITY_RANK:
        if re.search(pattern, low):
            return rank
    return 50


# ──────────────────────────────────────────────────────────
#  ZIP / SPLIT-ARCHIVE DETECTION
# ──────────────────────────────────────────────────────────
_SPLIT_FNAME_RE = re.compile(r"\.(zip|rar)\.\d{3,}$", re.I)
_PARTRAR_RE     = re.compile(r"\.part\d+\.(rar|zip|7z)$", re.I)
_BARE_PART_RE   = re.compile(r"^0+[1-9]\d?$")
_ARCHIVE_EXT_RE = re.compile(r"\.(zip|rar|7z)\b", re.I)


def _msg_is_split_part(msg) -> bool:
    """Return True if the message is one part of a split archive."""
    text  = extract_text(msg).lower().strip()
    doc   = getattr(msg, "document", None)
    fname = (getattr(doc, "file_name", "") or "").lower().strip()

    for t in (text, fname):
        if not t:
            continue
        if _SPLIT_FNAME_RE.search(t):
            return True
        if _PARTRAR_RE.search(t):
            return True
        if _BARE_PART_RE.fullmatch(t):
            return True
        if _ARCHIVE_EXT_RE.search(t) and re.search(r"\b\d{3}\b", t):
            return True
    return False


def batch_has_split_parts(msgs: list) -> bool:
    return any(_msg_is_split_part(m) for m in msgs)


# ──────────────────────────────────────────────────────────
#  LINK BUILDER
# ──────────────────────────────────────────────────────────
def make_msg_link(chat_id: int, message_id: int) -> str:
    cid = str(chat_id)
    cid = cid[4:] if cid.startswith("-100") else cid.lstrip("-")
    return f"https://t.me/c/{cid}/{message_id}"


def make_topic_link(chat_id: int, thread_id: int) -> str:
    """Deep-link straight into a specific forum topic."""
    cid = str(chat_id)
    cid = cid[4:] if cid.startswith("-100") else cid.lstrip("-")
    return f"https://t.me/c/{cid}/{thread_id}"


def make_topic_msg_link(chat_id: int, thread_id: int, message_id: int) -> str:
    """
    Deep-link straight to a SPECIFIC message inside a forum topic.

    Telegram's link format for a message that lives inside a forum topic is
    `https://t.me/c/<chat>/<thread_id>/<message_id>` (three segments) — as
    opposed to `make_msg_link()` above, which only has two segments and is
    correct for non-topic messages but does NOT reliably jump to the right
    message when the target is inside a specific topic.
    """
    cid = str(chat_id)
    cid = cid[4:] if cid.startswith("-100") else cid.lstrip("-")
    return f"https://t.me/c/{cid}/{thread_id}/{message_id}"


# ──────────────────────────────────────────────────────────
#  CLOSED TOPIC HELPER
#
#  When ALL_ADDED_SHOWS is a closed forum topic, a regular
#  send_message call raises  telegram.error.BadRequest:
#  "Topic_closed".
#
#  Fix: the bot (as an admin with "Manage Topics" permission)
#  briefly reopens the topic, posts the message, then closes
#  it again.  The whole sequence is transparent to members —
#  the topic appears closed before and after every post.
#
#  ⚠️  One-time Telegram setup required:
#      1. Open the group → Manage Group → Administrators
#      2. Add the bot as admin
#      3. Enable the "Manage Topics" permission
#      Without this, _send_to_closed_topic will fail and log
#      a clear error message telling you exactly what to fix.
# ──────────────────────────────────────────────────────────
async def _send_to_closed_topic(bot: Bot, method: str, **kwargs) -> Optional[Message]:
    """
    Temporarily reopen a closed forum topic, send a message (or photo, if
    method="send_photo"), then close it again.

    Receives the full kwargs dict (same one passed to safe_send) so there
    is no risk of passing chat_id / message_thread_id twice.

    ⚠️  Prerequisites (configure in Telegram):
        1. Promote the bot to group administrator.
        2. Grant it the "Manage Topics" (can_manage_topics) permission.
    """
    chat_id   = kwargs.get("chat_id")
    thread_id = kwargs.get("message_thread_id")

    if not chat_id or not thread_id:
        log.error("[TOPIC-CLOSED] Missing chat_id or message_thread_id — cannot reopen.")
        stats.inc("errors")
        return None

    # Step 1 — Reopen
    try:
        await bot.reopen_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
        await asyncio.sleep(0.5)   # brief settle window
        log.debug(f"[TOPIC] Reopened topic {thread_id} in {chat_id}")
    except TelegramError as exc:
        log.error(
            f"[TOPIC-CLOSED] ❌ Cannot reopen topic {thread_id} (chat {chat_id}).\n"
            f"  ➜  Group → Manage Group → Admins → Bot → enable 'Manage Topics'.\n"
            f"  ➜  Telegram error: {exc}"
        )
        stats.inc("errors")
        return None

    # Step 2 — Send (full kwargs already has chat_id + message_thread_id)
    sent: Optional[Message] = None
    try:
        send_fn = getattr(bot, method)
        sent = await send_fn(**kwargs)
        log.debug(f"[TOPIC] Message sent to previously-closed topic {thread_id}")
    except TelegramError as exc:
        log.error(f"[TOPIC-CLOSED] {method} failed after reopen: {exc}")
        stats.inc("errors")

    # Step 3 — Re-close (always attempt, even if send failed)
    try:
        await bot.close_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
        log.debug(f"[TOPIC] Re-closed topic {thread_id} in {chat_id}")
    except TelegramError as exc:
        log.warning(f"[TOPIC-CLOSED] Could not re-close topic {thread_id}: {exc}")

    return sent


# ──────────────────────────────────────────────────────────
#  SAFE SEND  (auto-retry + closed-topic support)
# ──────────────────────────────────────────────────────────
async def safe_send(bot: Bot, *, method: str = "send_message", **kwargs) -> Optional[Message]:
    """
    Send a Telegram message (or photo, when method="send_photo") with up to
    4 retries on rate limits.

    Closed-topic handling:
        If the target thread raises Topic_closed, calls _send_to_closed_topic
        which reopens → sends → closes automatically.
        Requires the bot to be a group admin with 'Manage Topics' permission.
    """
    chat_id   = kwargs.get("chat_id")
    thread_id = kwargs.get("message_thread_id")
    send_fn   = getattr(bot, method)

    for attempt in range(4):
        try:
            return await send_fn(**kwargs)

        except RetryAfter as e:
            wait = (
                int(e.retry_after.total_seconds()) + 1
                if isinstance(e.retry_after, timedelta)
                else int(e.retry_after) + 1
            )
            log.warning(f"Rate-limited — sleeping {wait}s (attempt {attempt + 1}/4)")
            await asyncio.sleep(wait)

        except BadRequest as e:
            err_str = str(e)

            # ── Closed topic: reopen → send → close ───────────────────
            if "Topic_closed" in err_str and chat_id and thread_id:
                log.info(
                    f"[TOPIC] Topic {thread_id} is closed — "
                    f"attempting reopen → send → close"
                )
                # Pass the FULL kwargs — _send_to_closed_topic extracts
                # chat_id and thread_id from it internally, so no duplication.
                return await _send_to_closed_topic(bot, method, **kwargs)

            log.error(f"BadRequest: {e}")
            stats.inc("errors")
            return None

        except TelegramError as e:
            log.error(f"TelegramError (attempt {attempt + 1}/4): {e}")
            if attempt < 3:
                await asyncio.sleep(2 ** attempt)
            else:
                stats.inc("errors")
                return None

    stats.inc("errors")
    return None


# ──────────────────────────────────────────────────────────
#  BUTTON LABEL HELPERS
# ──────────────────────────────────────────────────────────
def _button_label(text: str, max_len: int = CFG.MAX_BUTTON_LEN) -> str:
    label = re.sub(r"\s+", " ", text).strip()
    label = _FILE_EXT_RE.sub("", label).strip()
    label = re.sub(r"^[.\-_|\s]+", "", label)
    label = re.sub(r"[.\-_|\s]+$", "", label)
    return label[:max_len] if label and len(label) >= 2 else "📥 Download"


def _quality_button_label(text: str, max_len: int = CFG.MAX_BUTTON_LEN) -> str:
    """Extract quality/codec portion for use as a button label."""
    tags = extract_quality_tags(text)
    if tags:
        return tags[:max_len]
    return _button_label(text, max_len)


# ──────────────────────────────────────────────────────────
#  POST TEXT BUILDER
# ──────────────────────────────────────────────────────────
_SEP = "─" * 22


def _build_post_text(
    raw_title: str,
    text_msgs: list,
    batch: "Batch",
    icon: Optional[str] = None,
) -> str:
    """
    Build the formatted HTML body for a normal (non-zip-only) index post.

    Layout:
        {icon}  <b>Clean Title</b>  <code>YEAR</code>
        🎞  <i>1080p · x265 · Hindi</i>
        📌  <code>Thread #42</code>
        ──────────────────────
        👇  Tap a quality button to download
    """
    display_title, year = clean_title_for_display(raw_title)
    year_badge  = f"  <code>{year}</code>" if year else ""
    chosen_icon = icon or _determine_icon(raw_title)

    all_tags: List[str] = []
    seen_tags: set = set()
    for m in text_msgs:
        for tag in extract_quality_tags(extract_text(m)).split(" · "):
            if tag and tag.lower() not in seen_tags:
                seen_tags.add(tag.lower())
                all_tags.append(tag)

    quality_line = f"\n🎞  <i>{' · '.join(all_tags)}</i>" if all_tags else ""

    thread_note = ""
    if batch.thread_ids:
        tid = next(iter(batch.thread_ids))
        thread_note = f"\n📌  <code>Thread #{tid}</code>"

    return (
        f"{chosen_icon}  <b>{display_title[:200]}</b>{year_badge}"
        f"{quality_line}"
        f"{thread_note}\n"
        f"{_SEP}\n"
        f"👇  <i>Tap a quality button to download</i>"
    )


# ──────────────────────────────────────────────────────────
#  PROCESS BATCH  →  POST TO ALL_ADDED_SHOWS
#
#  Posts go to ALL_ADDED_SHOWS which may be a closed topic.
#  safe_send handles the reopen/send/close flow automatically.
# ──────────────────────────────────────────────────────────
async def process_batch(tg_app: Application, batch: Batch) -> None:
    """
    Given a completed batch of related messages, build and post ONE index entry.

    KEY BEHAVIOURS (v6):
    1. File-only messages (no caption) are SKIPPED as link anchors
       but still used for zip-detection and part counting.
    2. ZIP-ONLY BATCHES (all messages are split-archive parts, no
       text message) get their own index post extracted from the
       archive filename.
    3. Multiple quality variants are merged into ONE post with
       buttons sorted HIGHEST quality first (4K → 1080p → 720p …).
    4. The ZIP guide button lives inside the main post.
    5. Closed topics are handled transparently via safe_send.
    """
    msgs = batch.msgs
    if not msgs:
        return

    text_msgs = [m for m in msgs if not is_file_only_message(m)]
    has_zip   = batch_has_split_parts(msgs)

    # ══════════════════════════════════════════════════════
    #  BRANCH A: ZIP-ONLY BATCH
    # ══════════════════════════════════════════════════════
    if not text_msgs:
        if not has_zip:
            log.debug("[SKIP] All messages are file-only (non-archive), nothing to index")
            return

        archive_msgs = [m for m in msgs if _msg_is_split_part(m)]
        if not archive_msgs:
            return

        best_fname = ""
        for m in archive_msgs:
            doc   = getattr(m, "document", None)
            fname = (getattr(doc, "file_name", "") or extract_text(m)).strip()
            if len(fname) > len(best_fname):
                best_fname = fname

        if not best_fname:
            return

        raw_title = _title_from_archive(best_fname)
        key       = movie_key(raw_title)

        if not key:
            log.debug(f"[SKIP] empty key from archive: {best_fname!r}")
            return

        if seen_cache.has(key):
            log.debug(f"[DUPE] {key!r}")
            stats.inc("skipped_dupes")
            return
        seen_cache.set(key)

        display_title, year  = clean_title_for_display(raw_title)
        year_badge           = f"  <code>{year}</code>" if year else ""
        quality_tags         = extract_quality_tags(raw_title)
        quality_line         = f"\n🎞  <i>{quality_tags}</i>" if quality_tags else ""
        part_count           = len(archive_msgs)

        thread_note = ""
        if batch.thread_ids:
            tid = next(iter(batch.thread_ids))
            thread_note = f"\n📌  <code>Thread #{tid}</code>"

        first_msg  = archive_msgs[0]
        link       = make_msg_link(first_msg.chat.id, first_msg.message_id)
        part_label = f"📥  Download ({part_count} part{'s' if part_count > 1 else ''})"

        buttons: List[List[InlineKeyboardButton]] = [
            [InlineKeyboardButton(part_label, url=link)]
        ]

        if not zip_cache.has(key):
            zip_cache.set(key)
            buttons.append([InlineKeyboardButton(
                "📂 (ZIP.001, ZIP.002..) Download Guide", url=CFG.HOW_TO_LINK
            )])
            stats.inc("zip_hints")

        post_text = (
            f"📦  <b>{display_title[:200]}</b>{year_badge}"
            f"{quality_line}"
            f"\n🗂  <code>{part_count} archive part{'s' if part_count > 1 else ''}</code>"
            f"{thread_note}\n"
            f"{_SEP}\n"
            f"👇  <i>Download all parts via the button below</i>"
        )

        # v6.7: MiddleMan/Backup separation — store a lightweight movie
        # doc so this post can be Published to the Backup group later.
        movie_id = _store_index_movie(title=display_title, text=post_text, buttons=buttons)
        buttons.append([InlineKeyboardButton("🚀 Publish", callback_data=f"deploy:{movie_id}")])

        # v6.8: best-effort mirror this exact post (same Publish button,
        # same movie_id) into every dead MiddleMan clone group, so a
        # primary-MiddleMan ban doesn't lose the ability to Publish.
        await _mirror_post_to_middlemen(
            tg_app.bot, poster_file_id=None, text=post_text,
            buttons=buttons, topic_name="ALL_ADDED_SHOWS",
        )

        sent = await safe_send(
            tg_app.bot,
            chat_id=CFG.GROUP_CHAT_ID,
            message_thread_id=CFG.ALL_ADDED_SHOWS,
            text=post_text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML,
        )
        if sent:
            stats.inc("indexed")
            _update_movie_source_message(movie_id, sent.message_id)
            log.info(
                f"[ZIP-INDEX] {display_title!r} | {part_count} parts | "
                f"threads={batch.thread_ids}"
            )
        return

    # ══════════════════════════════════════════════════════
    #  BRANCH B: NORMAL BATCH (has ≥1 text / captioned message)
    # ══════════════════════════════════════════════════════
    candidates = [
        extract_text(m) for m in text_msgs
        if extract_text(m) and not is_filename(extract_text(m))
    ]
    base_title = (
        max(candidates, key=len) if candidates
        else extract_text(text_msgs[0]) or "Unknown Title"
    )
    key = movie_key(base_title)

    if not key:
        log.debug(f"[SKIP] empty key for: {base_title!r}")
        return

    if seen_cache.has(key):
        log.debug(f"[DUPE] {key!r}")
        stats.inc("skipped_dupes")
        return
    seen_cache.set(key)

    log.info(
        f"[INDEX] {base_title!r} | {len(text_msgs)} text msg(s) | "
        f"{len(msgs) - len(text_msgs)} file-only skipped | "
        f"threads={batch.thread_ids} | albums={batch.media_group_ids}"
    )

    # ── Build inline buttons (one per quality variant, sorted) ────────
    raw_buttons: List[Tuple[int, InlineKeyboardButton]] = []
    seen_labels: set = set()

    for m in text_msgs:
        if len(raw_buttons) >= CFG.MAX_BUTTONS - 1:
            break
        raw = extract_text(m)
        if not raw:
            continue

        label     = "📥  Download" if len(text_msgs) == 1 else _quality_button_label(raw)
        label_key = label.lower()
        if label_key in seen_labels:
            continue
        seen_labels.add(label_key)

        link = _extract_primary_url(raw) or make_msg_link(m.chat.id, m.message_id)
        rank = _btn_quality_rank(label)
        raw_buttons.append((rank, InlineKeyboardButton(label, url=link)))

    raw_buttons.sort(key=lambda x: x[0])
    buttons: List[List[InlineKeyboardButton]] = [[btn] for _, btn in raw_buttons]

    if not buttons:
        m    = text_msgs[0]
        link = make_msg_link(m.chat.id, m.message_id)
        buttons.append([InlineKeyboardButton("📥  Download", url=link)])

    # ── ZIP guide button (embedded in the main post) ───────────────────
    if has_zip and not zip_cache.has(key):
        zip_cache.set(key)
        buttons.append([InlineKeyboardButton(
            "📂 (ZIP.001, ZIP.002..) Download Guide", url=CFG.HOW_TO_LINK
        )])
        stats.inc("zip_hints")
        log.debug(f"[ZIP] appended guide button for {key!r}")

    # ── Format and send ────────────────────────────────────────────────
    post_text = _build_post_text(base_title, text_msgs, batch)

    # v6.7: MiddleMan/Backup separation — store a lightweight movie doc
    # so this post can be Published to the Backup group later.
    movie_id = _store_index_movie(title=base_title, text=post_text, buttons=buttons)
    buttons.append([InlineKeyboardButton("🚀 Publish", callback_data=f"deploy:{movie_id}")])

    # v6.8: best-effort mirror this exact post into every dead MiddleMan
    # clone group — see `_mirror_post_to_middlemen` for why this makes
    # a primary-MiddleMan ban survivable.
    await _mirror_post_to_middlemen(
        tg_app.bot, poster_file_id=None, text=post_text,
        buttons=buttons, topic_name="ALL_ADDED_SHOWS",
    )

    sent = await safe_send(
        tg_app.bot,
        chat_id=CFG.GROUP_CHAT_ID,
        message_thread_id=CFG.ALL_ADDED_SHOWS,
        text=post_text,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )
    if sent:
        stats.inc("indexed")
        _update_movie_source_message(movie_id, sent.message_id)


# ──────────────────────────────────────────────────────────
#  WELCOME + JOIN REDIRECT  (topic-to-topic)
# ──────────────────────────────────────────────────────────
async def handle_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    On a non-admin user joining the group, post a welcome message INSIDE
    the SEARCH_SHOWS_HERE topic with a deep-link button to that topic.

    Telegram's Bot API cannot force a user's client to switch topics.
    The closest real equivalent is posting the welcome directly into the
    target topic and attaching a deep-link button — one tap takes the
    user straight there.
    """
    msg  = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    if chat.id != CFG.GROUP_CHAT_ID:
        return

    cmu = update.chat_member
    if not cmu:
        return

    old_status = getattr(cmu, "old_chat_member", None)
    new_status = getattr(cmu, "new_chat_member", None)

    old_status_val = getattr(old_status, "status", None)
    new_status_val = getattr(new_status, "status", None)

    # A "join" is any transition INTO an active membership state
    # (member / administrator / creator) FROM a non-member state
    # (left / kicked / restricted-not-a-member / unset). Checking
    # the *new* status this way (instead of only checking the old
    # status was "left") avoids false triggers when someone is
    # promoted/demoted or briefly restricted, and also correctly
    # ignores the case where a user is banned right after joining.
    joined = (
        new_status_val in ("member", "administrator", "creator")
        and old_status_val != new_status_val
    )

    if not joined:
        return

    user = getattr(cmu, "from_user", None) or getattr(cmu, "user", None)
    if not user or user.is_bot:
        return

    # Admins already have full access — no need to welcome them.
    if user.id in CFG.ADMIN_IDS:
        return

    # IMPORTANT: the display name is untrusted user input and is being
    # interpolated into an HTML-parsed message. Without escaping it,
    # a name containing characters like `<`, `>` or `&` (fully legal in
    # a Telegram display name) breaks Telegram's HTML entity parser and
    # safe_send silently swallows the resulting BadRequest — so the
    # welcome message simply never appears for that user, with only a
    # log line to show for it. Escaping fixes that "not connected" bug.
    raw_name   = user.full_name or user.username or str(user.id)
    name       = html_escape(raw_name)
    text       = WELCOME_TEMPLATE_HTML.format(name=name)
    topic_link = make_topic_link(CFG.GROUP_CHAT_ID, CFG.SEARCH_SHOWS_HERE)

    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton(WELCOME_BUTTON_LABEL, url=topic_link)]
    ])

    sent = await safe_send(
        context.application.bot,
        chat_id=CFG.GROUP_CHAT_ID,
        message_thread_id=CFG.SEARCH_SHOWS_HERE,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=buttons,
    )
    if sent:
        stats.inc("welcomes_sent")
        log.info(f"[WELCOME] sent for user_id={user.id} name={raw_name!r}")
    else:
        log.warning(
            f"[WELCOME] failed to send for user_id={user.id} name={raw_name!r} "
            f"— check that SEARCH_SHOWS_HERE={CFG.SEARCH_SHOWS_HERE} is a valid, "
            f"open (or bot-manageable) topic and the bot is a group admin."
        )


# ──────────────────────────────────────────────────────────
#  POST BUILDER WIZARD
# ──────────────────────────────────────────────────────────
@dataclass
class PostAsset:
    token: str
    short_url: str
    deep_link: str
    label: str
    kind: str = "quality"


@dataclass
class PostDraft:
    user_id: int
    chat_id: int
    step: str = "mode"
    mode: str = ""
    title: str = ""
    languages: List[str] = field(default_factory=list)
    qualities: List[str] = field(default_factory=list)

    # ── Poster image (v6.4) ─────────────────────────────────────────────
    # Asked right after Movie/Series mode is chosen, before the title.
    # Stored as a raw Telegram file_id so the final post can be sent via
    # send_photo(photo=poster_file_id, caption=...) instead of send_message.
    # Only "photo" type is accepted (see `_handle_wizard_poster`) — a
    # send_photo call requires a file_id that was itself uploaded as a
    # photo; reusing a "document" file_id there would fail.
    poster_file_id: Optional[str] = None

    # ── Series-only: multi-season support (v6.2) ───────────────────────
    # `seasons` holds normalised labels like ["S01", "S02", "S03"].
    # `current_season_index` walks through `seasons` the same way
    # `current_quality_index` walks through `qualities`, nested INSIDE
    # each season (all qualities of S01, then all qualities of S02, …).
    seasons: List[str] = field(default_factory=list)
    current_season_index: int = 0

    topic_name: str = ""
    topic_id: Optional[int] = None
    current_quality_index: int = 0
    current_quality_files: List[str] = field(default_factory=list)

    # Movie mode: quality -> uploaded-file asset (one file per quality).
    quality_assets: Dict[str, PostAsset] = field(default_factory=dict)

    # Series mode: season -> { quality -> episode-bundle asset }.
    season_quality_assets: Dict[str, Dict[str, PostAsset]] = field(default_factory=dict)
    # Series mode: season -> combined "whole season, all qualities" asset.
    season_assets: Dict[str, PostAsset] = field(default_factory=dict)

    file_tokens_by_quality: Dict[str, List[str]] = field(default_factory=dict)
    send_all_asset: Optional[PostAsset] = None
    edit_field: str = ""

    def current_quality(self) -> Optional[str]:
        if 0 <= self.current_quality_index < len(self.qualities):
            return self.qualities[self.current_quality_index]
        return None

    def current_season(self) -> Optional[str]:
        if 0 <= self.current_season_index < len(self.seasons):
            return self.seasons[self.current_season_index]
        return None


_POST_DRAFTS: Dict[int, PostDraft] = {}


def _draft_for(user_id: int, chat_id: int) -> PostDraft:
    draft = _POST_DRAFTS.get(user_id)
    if draft is None:
        draft = PostDraft(user_id=user_id, chat_id=chat_id)
        _POST_DRAFTS[user_id] = draft
    return draft


def _get_draft(user_id: int) -> Optional[PostDraft]:
    return _POST_DRAFTS.get(user_id)


def _clear_draft(user_id: int) -> None:
    _POST_DRAFTS.pop(user_id, None)


def _clean_split_csv(raw: str) -> List[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


# ── Season label normalisation (v6.2) ───────────────────────────────────
# Accepts "S01", "s1", "1", "01" and normalises all of them to "S01" so
# admins can type whichever is fastest. Anything that doesn't look like a
# plain season number (e.g. a custom arc name) is kept as typed, just with
# whitespace collapsed — so "Final Arc" stays "Final Arc".
_SEASON_NUM_RE = re.compile(r"^s?0*(\d{1,3})$", re.I)


def _normalize_season_label(raw: str) -> str:
    raw = (raw or "").strip()
    m = _SEASON_NUM_RE.match(raw)
    if m:
        return f"S{int(m.group(1)):02d}"
    return re.sub(r"\s+", " ", raw).strip().upper()


def _parse_seasons(raw: str) -> List[str]:
    """Parse 'S01, S02, S03' / '1,2,3' / 'S01' into a de-duplicated season list."""
    seasons: List[str] = []
    seen: set = set()
    for part in _clean_split_csv(raw):
        label = _normalize_season_label(part)
        if label and label not in seen:
            seen.add(label)
            seasons.append(label)
    return seasons


def _resolve_topic_name(name: str) -> Optional[int]:
    """
    Resolve a free-typed topic name to its topic id, ignoring case,
    punctuation, and spacing differences — "MOVIES (HINDI)", "movies hindi",
    and "Movies   Hindi" all resolve the same TOPIC_MAP entry.
    """
    if not name:
        return None
    key = _normalize_topic_key(name)
    if not key:
        return None
    return _TOPIC_LOOKUP.get(key)


def _topic_buttons() -> InlineKeyboardMarkup:
    buttons: List[List[InlineKeyboardButton]] = []
    if _TOPIC_MAP:
        for name, topic_id in _TOPIC_MAP.items():
            buttons.append([InlineKeyboardButton(name, callback_data=f"mp:topic:{topic_id}")])
    else:
        buttons.append([InlineKeyboardButton("ALL_ADDED_SHOWS", callback_data=f"mp:topic:{CFG.ALL_ADDED_SHOWS}")])
    buttons.append([InlineKeyboardButton("Skip", callback_data="mp:skip:topic")])
    return InlineKeyboardMarkup(buttons)


def _mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Movie", callback_data="mp:mode:movie")],
        [InlineKeyboardButton("Series", callback_data="mp:mode:series")],
    ])


def _simple_skip_keyboard(step: str, extra: Optional[List[List[InlineKeyboardButton]]] = None) -> InlineKeyboardMarkup:
    rows = extra[:] if extra else []
    rows.append([InlineKeyboardButton("Skip", callback_data=f"mp:skip:{step}")])
    return InlineKeyboardMarkup(rows)


def _preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Post It", callback_data="mp:post")],
        [InlineKeyboardButton("Make Changes", callback_data="mp:edit")],
        [InlineKeyboardButton("Cancel", callback_data="mp:cancel")],
    ])


def _edit_keyboard(draft: "PostDraft") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Poster", callback_data="mp:edit:poster")],
        [InlineKeyboardButton("Title", callback_data="mp:edit:title")],
        [InlineKeyboardButton("Languages", callback_data="mp:edit:languages")],
        [InlineKeyboardButton("Qualities", callback_data="mp:edit:qualities")],
    ]
    if draft.mode == "series":
        rows.append([InlineKeyboardButton("Seasons", callback_data="mp:edit:seasons")])
    rows.append([InlineKeyboardButton("Files", callback_data="mp:edit:files")])
    rows.append([InlineKeyboardButton("Topic", callback_data="mp:edit:topic")])
    rows.append([InlineKeyboardButton("Back", callback_data="mp:edit:back")])
    return InlineKeyboardMarkup(rows)


def _wizard_post_text(draft: PostDraft) -> str:
    """
    Build the final published post's text (also used as the caption when a
    poster image is attached). Labelled, one field per line:

        Title: Dastaar 2026
        Languages: Punjabi
        Quality: 720p

    All dynamic values are HTML-escaped — titles/languages/qualities are
    free-typed admin input and this text is sent with parse_mode=HTML, so an
    unescaped "<" or "&" would silently break the post (same class of bug
    fixed earlier for join names).
    """
    lines = [f"<b>Title:</b> {html_escape(draft.title or 'Untitled')}"]
    if draft.languages:
        lines.append(f"<b>Languages:</b> {html_escape(', '.join(draft.languages))}")
    if draft.mode == "series" and draft.seasons:
        lines.append(f"<b>Seasons:</b> {html_escape(', '.join(draft.seasons))}")
    if draft.qualities:
        if draft.mode == "series":
            # Series posts show the plain requested quality list — the
            # per-episode bundles live behind the season buttons instead.
            lines.append(f"<b>Quality:</b> {html_escape(', '.join(draft.qualities))}")
        else:
            quality_labels = []
            for q in draft.qualities:
                asset = draft.quality_assets.get(q)
                quality_labels.append(asset.label if asset else q)
            lines.append(f"<b>Quality:</b> {html_escape(', '.join(quality_labels))}")
    return "\n".join(lines)


def _bundle_label_for_quality(draft: PostDraft, quality: str) -> str:
    if draft.mode == "series":
        season = draft.current_season() or (draft.seasons[0] if draft.seasons else "S01")
        return f"{season} {quality}"
    return quality


def _topic_name_by_id(topic_id: Optional[int]) -> str:
    if topic_id is None:
        return "(not selected)"
    for name, value in _TOPIC_MAP.items():
        if value == topic_id:
            return name
    if topic_id == CFG.ALL_ADDED_SHOWS:
        return "ALL_ADDED_SHOWS"
    return str(topic_id)


async def _store_uploaded_file_asset(update: Update, context: ContextTypes.DEFAULT_TYPE, draft: PostDraft, payload: dict) -> PostAsset:
    token = secrets.token_hex(8)
    deep_link = make_bot_start_link(token)
    short_url = await _shorten_with_gplinks(deep_link, alias=token)
    record = {
        "token": token,
        "file_id": payload["file_id"],
        "file_type": payload["file_type"],
        "file_name": payload.get("file_name", ""),
        "caption": payload.get("caption", ""),
        "deep_link": deep_link,
        "short_url": short_url,
        "source_chat_id": update.effective_chat.id if update.effective_chat else None,
        "source_message_id": update.effective_message.message_id if update.effective_message else None,
        "created_at": time.time(),
        "downloads_count": 0,
    }
    _store_download(record)
    label = _trim_caption(payload.get("file_name") or payload.get("caption") or payload["file_type"])
    return PostAsset(token=token, short_url=short_url, deep_link=deep_link, label=label, kind="file")


async def _finalize_current_quality_bundle(update: Update, context: ContextTypes.DEFAULT_TYPE, draft: PostDraft) -> Optional[PostAsset]:
    """MOVIE MODE ONLY (kept from v6.1, unchanged): bundle the current
    quality's collected file token(s) into one asset in `draft.quality_assets`."""
    quality = draft.current_quality()
    if not quality:
        return None
    tokens = list(draft.current_quality_files)
    if not tokens:
        return None

    bundle_token = secrets.token_hex(8)
    deep_link = make_bot_start_link(bundle_token)
    short_url = await _shorten_with_gplinks(deep_link, alias=bundle_token)
    label = quality
    _store_bundle({
        "token": bundle_token,
        "label": label,
        "kind": "quality",
        "child_tokens": tokens,
        "deep_link": deep_link,
        "short_url": short_url,
        "source_chat_id": update.effective_chat.id if update.effective_chat else None,
        "source_message_id": update.effective_message.message_id if update.effective_message else None,
        "created_at": time.time(),
        "downloads_count": 0,
    })
    asset = PostAsset(token=bundle_token, short_url=short_url, deep_link=deep_link, label=label, kind="quality")
    draft.quality_assets[quality] = asset
    draft.file_tokens_by_quality[quality] = tokens
    draft.current_quality_files = []
    return asset


async def _finalize_current_series_quality_bundle(update: Update, context: ContextTypes.DEFAULT_TYPE, draft: PostDraft) -> Optional[PostAsset]:
    """
    SERIES MODE (v6.2): bundle the current season + current quality's
    collected episode-file tokens into one asset, stored under
    `draft.season_quality_assets[season][quality]`.
    """
    season = draft.current_season()
    quality = draft.current_quality()
    if not season or not quality:
        return None
    tokens = list(draft.current_quality_files)
    if not tokens:
        return None

    bundle_token = secrets.token_hex(8)
    deep_link = make_bot_start_link(bundle_token)
    short_url = await _shorten_with_gplinks(deep_link, alias=bundle_token)
    label = f"{season} {quality}"
    _store_bundle({
        "token": bundle_token,
        "label": label,
        "kind": "quality",
        "child_tokens": tokens,
        "deep_link": deep_link,
        "short_url": short_url,
        "source_chat_id": update.effective_chat.id if update.effective_chat else None,
        "source_message_id": update.effective_message.message_id if update.effective_message else None,
        "created_at": time.time(),
        "downloads_count": 0,
    })
    asset = PostAsset(token=bundle_token, short_url=short_url, deep_link=deep_link, label=label, kind="quality")
    draft.season_quality_assets.setdefault(season, {})[quality] = asset
    draft.current_quality_files = []
    return asset


async def _finalize_season_bundle(update: Update, context: ContextTypes.DEFAULT_TYPE, draft: PostDraft, season: str) -> Optional[PostAsset]:
    """
    SERIES MODE (v6.2): once every quality for a given season has been
    uploaded, bundle those per-quality assets into one "whole season, every
    quality" asset stored under `draft.season_assets[season]`. This becomes
    the target of that season's button ("S01", "S02", …) on the final post.
    """
    quality_map = draft.season_quality_assets.get(season) or {}
    token_list = [quality_map[q].token for q in draft.qualities if q in quality_map]
    if not token_list:
        return None

    bundle_token = secrets.token_hex(8)
    deep_link = make_bot_start_link(bundle_token)
    short_url = await _shorten_with_gplinks(deep_link, alias=bundle_token)
    _store_bundle({
        "token": bundle_token,
        "label": season,
        "kind": "season",
        "child_tokens": token_list,
        "deep_link": deep_link,
        "short_url": short_url,
        "source_chat_id": update.effective_chat.id if update.effective_chat else None,
        "source_message_id": update.effective_message.message_id if update.effective_message else None,
        "created_at": time.time(),
        "downloads_count": 0,
    })
    asset = PostAsset(token=bundle_token, short_url=short_url, deep_link=deep_link, label=season, kind="season")
    draft.season_assets[season] = asset
    return asset


async def _finalize_send_all_bundle(update: Update, context: ContextTypes.DEFAULT_TYPE, draft: PostDraft) -> Optional[PostAsset]:
    """
    Build the final "everything" bundle:
      • Series mode  → bundles every SEASON asset into one "ALL SEASONS" button.
      • Movie mode   → bundles every QUALITY asset into one "SEND ALL" button
                        (unchanged behaviour from v6.1).
    """
    if draft.mode == "series":
        if not draft.season_assets:
            return None
        token_list = [draft.season_assets[s].token for s in draft.seasons if s in draft.season_assets]
        if not token_list:
            return None
        label = "ALL SEASONS"
    else:
        if not draft.quality_assets:
            return None
        token_list = []
        for q in draft.qualities:
            asset = draft.quality_assets.get(q)
            if asset:
                token_list.append(asset.token)
        if not token_list:
            return None
        label = draft.title or "Send All"

    bundle_token = secrets.token_hex(8)
    deep_link = make_bot_start_link(bundle_token)
    short_url = await _shorten_with_gplinks(deep_link, alias=bundle_token)
    _store_bundle({
        "token": bundle_token,
        "label": label,
        "kind": "sendall",
        "child_tokens": token_list,
        "deep_link": deep_link,
        "short_url": short_url,
        "source_chat_id": update.effective_chat.id if update.effective_chat else None,
        "source_message_id": update.effective_message.message_id if update.effective_message else None,
        "created_at": time.time(),
        "downloads_count": 0,
    })
    return PostAsset(
        token=bundle_token, short_url=short_url, deep_link=deep_link,
        label=("ALL SEASONS" if draft.mode == "series" else "SEND ALL"),
        kind="sendall",
    )


def _draft_preview(draft: PostDraft) -> str:
    topic_name = _topic_name_by_id(draft.topic_id)
    quality_lines = []
    for q in draft.qualities:
        asset = draft.quality_assets.get(q)
        if asset:
            quality_lines.append(asset.label)
        else:
            quality_lines.append(q)
    lines = [
        "📝  <b>Preview</b>",
        f"Poster: <b>{'✅ Attached' if draft.poster_file_id else '— (none)'}</b>",
        f"Title: <b>{html_escape(draft.title or 'Untitled')}</b>",
        f"Languages: <b>{html_escape(', '.join(draft.languages) if draft.languages else '—')}</b>",
    ]
    if draft.mode == "series":
        lines.append(f"Seasons: <b>{html_escape(', '.join(draft.seasons) if draft.seasons else '—')}</b>")
    lines.append(f"Qualities: <b>{html_escape(', '.join(quality_lines) if quality_lines else '—')}</b>")
    lines.append(f"Topic: <b>{html_escape(topic_name)}</b>")
    return "\n".join(lines)


def _current_prompt(draft: PostDraft) -> str:
    if draft.step == "mode":
        return "Choose post type:"
    if draft.step == "poster":
        return "📸 Forward the poster image (or tap Skip):"
    if draft.step == "title":
        return "Send the title:"
    if draft.step == "languages":
        return "Send languages (comma separated) or skip:"
    if draft.step == "qualities":
        return "Send qualities (comma separated) or skip:"
    if draft.step == "seasons":
        return "Send total seasons, e.g. S01, S02, S03 (or skip for S01 only):"
    if draft.step == "files":
        q = draft.current_quality() or "quality"
        if draft.mode == "series":
            return f"Forward episode files for <b>{html_escape(_bundle_label_for_quality(draft, q))}</b>. Press Upload Complete when done."
        return f"Forward the file for <b>{html_escape(q)}</b>."
    if draft.step == "topic":
        return "Choose the topic to post in:"
    if draft.step == "preview":
        return _draft_preview(draft)
    if draft.step == "edit_poster":
        return "Forward a new poster image, or tap Skip to remove the poster:"
    if draft.step.startswith("edit_"):
        return "Send the updated value:"
    return "Continue:"


async def _send_wizard_prompt(bot: Bot, draft: PostDraft) -> None:
    if draft.step == "mode":
        await bot.send_message(chat_id=draft.chat_id, text="Choose post type:", reply_markup=_mode_keyboard())
        return
    if draft.step == "poster":
        await bot.send_message(
            chat_id=draft.chat_id,
            text="📸 Forward the poster image for this post (or tap Skip):",
            reply_markup=_simple_skip_keyboard("poster"),
        )
        return
    if draft.step == "title":
        await bot.send_message(chat_id=draft.chat_id, text="Send the title:")
        return
    if draft.step == "languages":
        await bot.send_message(chat_id=draft.chat_id, text="Send languages (comma separated) or skip:", reply_markup=_simple_skip_keyboard("languages"))
        return
    if draft.step == "qualities":
        await bot.send_message(chat_id=draft.chat_id, text="Send qualities (comma separated) or skip:", reply_markup=_simple_skip_keyboard("qualities"))
        return
    if draft.step == "seasons":
        await bot.send_message(
            chat_id=draft.chat_id,
            text="Send total seasons, e.g. S01, S02, S03 (or skip for S01 only):",
            reply_markup=_simple_skip_keyboard("seasons"),
        )
        return
    if draft.step == "files":
        q = draft.current_quality() or "quality"
        text = f"Forward the file for {q}."
        if draft.mode == "series":
            text = f"Forward episode files for {_bundle_label_for_quality(draft, q)}."
            await bot.send_message(chat_id=draft.chat_id, text=text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Upload Complete", callback_data="mp:upload_done")]]))
            return
        await bot.send_message(chat_id=draft.chat_id, text=text)
        return
    if draft.step == "topic":
        await bot.send_message(chat_id=draft.chat_id, text="Choose the topic to post in:", reply_markup=_topic_buttons())
        return
    if draft.step == "preview":
        if draft.poster_file_id:
            await bot.send_photo(
                chat_id=draft.chat_id,
                photo=draft.poster_file_id,
                caption=_draft_preview(draft),
                parse_mode=ParseMode.HTML,
                reply_markup=_preview_keyboard(),
            )
        else:
            await bot.send_message(chat_id=draft.chat_id, text=_draft_preview(draft), parse_mode=ParseMode.HTML, reply_markup=_preview_keyboard())
        return
    if draft.step.startswith("edit_"):
        await bot.send_message(chat_id=draft.chat_id, text=_current_prompt(draft))
        return


def _start_quality_flow(draft: PostDraft) -> None:
    draft.current_quality_index = 0
    draft.current_quality_files = []
    draft.step = "files" if draft.qualities else "topic"


def _start_season_flow(draft: PostDraft) -> None:
    """SERIES MODE (v6.2): begin the season → quality → files nested loop,
    starting at the first season and first quality."""
    draft.current_season_index = 0
    _start_quality_flow(draft)


def _advance_quality(draft: PostDraft) -> None:
    """MOVIE MODE ONLY (unchanged from v6.1)."""
    draft.current_quality_index += 1
    draft.current_quality_files = []
    if draft.current_quality_index >= len(draft.qualities):
        draft.step = "topic"
    else:
        draft.step = "files"


def _advance_series_quality(draft: PostDraft) -> None:
    """
    SERIES MODE (v6.2): after "Upload Complete" for one (season, quality)
    pair, move to the next quality within the SAME season. Once every
    quality for that season is done, move to the FIRST quality of the NEXT
    season. Once every season is done, move on to topic selection.
    """
    draft.current_quality_index += 1
    draft.current_quality_files = []
    if draft.current_quality_index >= len(draft.qualities):
        draft.current_season_index += 1
        draft.current_quality_index = 0
        if draft.current_season_index >= len(draft.seasons):
            draft.step = "topic"
        else:
            draft.step = "files"
    else:
        draft.step = "files"


def _reset_file_assets_for_quality_change(draft: PostDraft) -> None:
    """MOVIE MODE ONLY (unchanged from v6.1)."""
    draft.current_quality_index = 0
    draft.current_quality_files = []
    draft.quality_assets.clear()
    draft.file_tokens_by_quality.clear()
    draft.send_all_asset = None
    if draft.qualities:
        draft.step = "files"
    else:
        draft.step = "topic"


def _reset_series_file_assets(draft: PostDraft) -> None:
    """
    SERIES MODE (v6.2): whenever seasons or qualities are (re-)edited, wipe
    every previously-collected season/quality asset and restart the
    season → quality → files loop from scratch, since the old bundles no
    longer match the new season/quality list.
    """
    draft.current_season_index = 0
    draft.current_quality_index = 0
    draft.current_quality_files = []
    draft.season_quality_assets.clear()
    draft.season_assets.clear()
    draft.send_all_asset = None
    if draft.seasons and draft.qualities:
        draft.step = "files"
    else:
        draft.step = "topic"


async def _publish_draft_to_group(context: ContextTypes.DEFAULT_TYPE, draft: PostDraft) -> Optional[Message]:
    if not draft.topic_id:
        return None
    if not draft.send_all_asset:
        return None

    buttons: List[List[InlineKeyboardButton]] = []

    if draft.mode == "series":
        for season in draft.seasons:
            asset = draft.season_assets.get(season)
            if not asset:
                continue
            buttons.append([InlineKeyboardButton(season, url=asset.short_url)])
        buttons.append([InlineKeyboardButton("ALL SEASONS", url=draft.send_all_asset.short_url)])
    else:
        for q in draft.qualities:
            asset = draft.quality_assets.get(q)
            if not asset:
                continue
            buttons.append([InlineKeyboardButton(asset.label, url=asset.short_url)])
        buttons.append([InlineKeyboardButton("SEND ALL", url=draft.send_all_asset.short_url)])

    text = _wizard_post_text(draft)

    # ── v6.6: store a single "movie" document capturing everything     ──
    #    needed to recreate this EXACT post inside a Backup group later ──
    #    via the new "🚀 Publish" button (see handle_deploy_callback ──
    #    below). Buttons are flattened from the rows built above; their ──
    #    URLs are the SAME permanent GPLinks/bot deep-links already on   ──
    #    this post (not tied to any message id), so recreating the post ──
    #    elsewhere works unchanged, with zero re-uploading and zero      ──
    #    duplicate MongoDB movie documents (one insert per /makepost).   ──
    movie_buttons = [
        {"label": btn.text, "url": btn.url}
        for row in buttons for btn in row
        if getattr(btn, "url", None)
    ]
    movie_doc = {
        "title": draft.title,
        "mode": draft.mode,
        "poster_file_id": draft.poster_file_id,
        "text": text,
        "topic_id": draft.topic_id,
        "topic_name": draft.topic_name or _topic_name_by_id(draft.topic_id),
        "buttons": movie_buttons,
        "created_at": time.time(),
        "source_chat_id": CFG.GROUP_CHAT_ID,
        "source_message_id": None,
    }
    movie_id = _store_movie(movie_doc)
    buttons.append([InlineKeyboardButton("🚀 Publish", callback_data=f"deploy:{movie_id}")])

    # v6.8/v6.11: best-effort mirror this exact post (poster + full
    # buttons + Publish, unchanged) into every dead MiddleMan clone
    # group's SPECIFIC topic — see `_mirror_post_to_middlemen` for why
    # this makes a primary-MiddleMan ban survivable. The mirror's own
    # ALL_ADDED_SHOWS then gets a lightweight STUB (poster + a single
    # "Open Post" button pointing back at that specific-topic message
    # in the SAME mirror) via `_mirror_crosspost_stub` — matching
    # exactly what the primary MiddleMan gets via
    # `_crosspost_to_all_added_shows` below (search index, not a
    # second copy of every download/Publish button). Skipped entirely
    # when the admin chose ALL_ADDED_SHOWS itself as the topic, same
    # guard the primary crosspost uses, to avoid posting it twice.
    mirror_messages = await _mirror_post_to_middlemen(
        context.application.bot,
        poster_file_id=draft.poster_file_id,
        text=text,
        buttons=buttons,
        topic_name=movie_doc["topic_name"],
    )
    if draft.topic_id is not None and draft.topic_id != CFG.ALL_ADDED_SHOWS:
        await _mirror_crosspost_stub(
            context.application.bot,
            mirror_messages=mirror_messages,
            poster_file_id=draft.poster_file_id,
            text=text,
            title=draft.title,
            specific_topic_name=movie_doc["topic_name"],
        )

    if draft.poster_file_id:
        sent = await safe_send(
            context.application.bot,
            method="send_photo",
            chat_id=CFG.GROUP_CHAT_ID,
            message_thread_id=draft.topic_id,
            photo=draft.poster_file_id,
            caption=text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML,
        )
    else:
        sent = await safe_send(
            context.application.bot,
            chat_id=CFG.GROUP_CHAT_ID,
            message_thread_id=draft.topic_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML,
        )

    if sent:
        _update_movie_source_message(movie_id, sent.message_id)

    return sent


async def _crosspost_to_all_added_shows(context: ContextTypes.DEFAULT_TYPE, draft: PostDraft, original: Message) -> None:
    """
    v6.2: after a wizard post is published into the admin's CHOSEN topic,
    automatically mirror it into ALL_ADDED_SHOWS (topic id = CFG.ALL_ADDED_SHOWS,
    "SEARCH ALL ADDED SHOWS") with the same title/language/quality-or-season
    text (and poster, if one was attached), but with a SINGLE button that
    deep-links straight to the original post the bot just made. Skipped when
    the chosen topic already IS ALL_ADDED_SHOWS, to avoid posting the exact
    same thing twice.
    """
    if draft.topic_id is None or draft.topic_id == CFG.ALL_ADDED_SHOWS:
        return

    link = make_topic_msg_link(CFG.GROUP_CHAT_ID, draft.topic_id, original.message_id)
    button_label = _button_label(draft.title) if draft.title else "Open Post"
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton(button_label, url=link)]])
    text = _wizard_post_text(draft)

    if draft.poster_file_id:
        sent = await safe_send(
            context.application.bot,
            method="send_photo",
            chat_id=CFG.GROUP_CHAT_ID,
            message_thread_id=CFG.ALL_ADDED_SHOWS,
            photo=draft.poster_file_id,
            caption=text,
            reply_markup=buttons,
            parse_mode=ParseMode.HTML,
        )
    else:
        sent = await safe_send(
            context.application.bot,
            chat_id=CFG.GROUP_CHAT_ID,
            message_thread_id=CFG.ALL_ADDED_SHOWS,
            text=text,
            reply_markup=buttons,
            parse_mode=ParseMode.HTML,
        )
    if sent:
        log.info(
            f"[CROSSPOST] mirrored post {original.message_id!r} "
            f"(topic {draft.topic_id}) into ALL_ADDED_SHOWS ({CFG.ALL_ADDED_SHOWS})"
        )
    else:
        log.warning(
            f"[CROSSPOST] failed to mirror post {original.message_id!r} "
            f"into ALL_ADDED_SHOWS ({CFG.ALL_ADDED_SHOWS})"
        )


async def _finalize_and_show_preview(update: Update, context: ContextTypes.DEFAULT_TYPE, draft: PostDraft) -> None:
    draft.send_all_asset = await _finalize_send_all_bundle(update, context, draft)
    draft.step = "preview"
    preview_text = _draft_preview(draft)
    if draft.poster_file_id:
        await context.application.bot.send_photo(
            chat_id=draft.chat_id,
            photo=draft.poster_file_id,
            caption=preview_text,
            parse_mode=ParseMode.HTML,
            reply_markup=_preview_keyboard(),
        )
    else:
        await context.application.bot.send_message(
            chat_id=draft.chat_id,
            text=preview_text,
            parse_mode=ParseMode.HTML,
            reply_markup=_preview_keyboard(),
        )


async def _handle_wizard_text(update: Update, context: ContextTypes.DEFAULT_TYPE, draft: PostDraft) -> bool:
    msg = update.effective_message
    text = extract_text(msg)
    if not text:
        return False

    if draft.step == "title":
        draft.title = text.strip()
        draft.step = "languages"
        await _send_wizard_prompt(context.application.bot, draft)
        return True
    if draft.step == "languages":
        draft.languages = _clean_split_csv(text)
        draft.step = "qualities"
        await _send_wizard_prompt(context.application.bot, draft)
        return True
    if draft.step == "qualities":
        draft.qualities = _clean_split_csv(text) or ["Main"]
        if draft.mode == "series":
            draft.step = "seasons"
        else:
            _start_quality_flow(draft)
        await _send_wizard_prompt(context.application.bot, draft)
        return True
    if draft.step == "seasons":
        draft.seasons = _parse_seasons(text) or ["S01"]
        _start_season_flow(draft)
        await _send_wizard_prompt(context.application.bot, draft)
        return True
    if draft.step == "topic":
        topic_id = _resolve_topic_name(text)
        if topic_id is None and text.strip().isdigit():
            topic_id = int(text.strip())
        if topic_id is None:
            await context.application.bot.send_message(chat_id=draft.chat_id, text="Topic not found. Choose one of the buttons or send a valid topic id.", reply_markup=_topic_buttons())
            return True
        draft.topic_id = topic_id
        draft.topic_name = _topic_name_by_id(topic_id)
        await _finalize_and_show_preview(update, context, draft)
        return True
    if draft.step == "edit_title":
        draft.title = text.strip()
        draft.step = "preview"
        await _finalize_and_show_preview(update, context, draft)
        return True
    if draft.step == "edit_languages":
        draft.languages = _clean_split_csv(text)
        draft.step = "preview"
        await _finalize_and_show_preview(update, context, draft)
        return True
    if draft.step == "edit_qualities":
        draft.qualities = _clean_split_csv(text) or ["Main"]
        if draft.mode == "series":
            _reset_series_file_assets(draft)
        else:
            _reset_file_assets_for_quality_change(draft)
        await _send_wizard_prompt(context.application.bot, draft)
        return True
    if draft.step == "edit_seasons":
        draft.seasons = _parse_seasons(text) or ["S01"]
        _reset_series_file_assets(draft)
        await _send_wizard_prompt(context.application.bot, draft)
        return True
    if draft.step == "edit_topic":
        topic_id = _resolve_topic_name(text)
        if topic_id is None and text.strip().isdigit():
            topic_id = int(text.strip())
        if topic_id is None:
            await context.application.bot.send_message(chat_id=draft.chat_id, text="Topic not found. Try again.", reply_markup=_topic_buttons())
            return True
        draft.topic_id = topic_id
        draft.topic_name = _topic_name_by_id(topic_id)
        draft.step = "preview"
        await _finalize_and_show_preview(update, context, draft)
        return True
    return False


async def _handle_wizard_poster(update: Update, context: ContextTypes.DEFAULT_TYPE, draft: PostDraft, payload: dict) -> None:
    """
    Save the forwarded poster image onto the draft. Only accepts payloads
    Telegram uploaded as an actual photo (compressed image) — a Telegram
    file_id can only be resent via the SAME api method it came from, so a
    "document" (uncompressed image sent as a file) file_id would fail if
    later passed to send_photo(). Rejecting non-photo types up front avoids
    that failure mode entirely rather than surfacing a cryptic Telegram
    error at publish time.
    """
    if payload.get("file_type") != "photo":
        await context.application.bot.send_message(
            chat_id=draft.chat_id,
            text="That doesn't look like a photo. Please send the poster as a photo (not a file), or tap Skip above.",
        )
        return

    is_edit = draft.step == "edit_poster"
    draft.poster_file_id = payload["file_id"]

    if is_edit:
        await context.application.bot.send_message(chat_id=draft.chat_id, text="✅ Poster updated.")
        await _finalize_and_show_preview(update, context, draft)
    else:
        draft.step = "title"
        await context.application.bot.send_message(chat_id=draft.chat_id, text="✅ Poster saved.")
        await _send_wizard_prompt(context.application.bot, draft)


async def _handle_wizard_file(update: Update, context: ContextTypes.DEFAULT_TYPE, draft: PostDraft, payload: dict) -> bool:
    quality = draft.current_quality()
    if not quality:
        return False
    asset = await _store_uploaded_file_asset(update, context, draft, payload)
    draft.current_quality_files.append(asset.token)

    if draft.mode == "movie":
        draft.quality_assets[quality] = PostAsset(token=asset.token, short_url=asset.short_url, deep_link=asset.deep_link, label=quality, kind="quality")
        draft.file_tokens_by_quality[quality] = [asset.token]
        draft.current_quality_files = []
        _advance_quality(draft)
        if draft.step == "topic":
            await context.application.bot.send_message(chat_id=draft.chat_id, text=f"Saved {quality}. Move to topic selection next.")
            await _send_wizard_prompt(context.application.bot, draft)
        else:
            await context.application.bot.send_message(chat_id=draft.chat_id, text=f"Saved {quality}.")
            await _send_wizard_prompt(context.application.bot, draft)
        return True

    # Series: allow multiple episode files until Upload Complete is pressed.
    # NOTE: this confirmation is purely cosmetic — which season/quality the
    # file belongs to is determined ENTIRELY by the wizard's current step
    # (draft.current_season() / draft.current_quality()), never by the
    # file's name. The filename is not inspected here at all.
    await context.application.bot.send_message(
        chat_id=draft.chat_id,
        text=f"✅ Added file for <b>{html_escape(_bundle_label_for_quality(draft, quality))}</b>.",
        parse_mode=ParseMode.HTML,
    )
    return True


async def _finalize_series_quality(update: Update, context: ContextTypes.DEFAULT_TYPE, draft: PostDraft) -> None:
    """
    Handles the "Upload Complete" button press in series mode.

    1. Bundle the files collected for the CURRENT (season, quality) pair.
    2. If that was the LAST quality for this season, also bundle the whole
       season (all its qualities) into one season-level asset.
    3. Advance to the next quality, or the next season, or to topic
       selection if everything has been collected — then send whatever
       prompt matches the new state.
    """
    season = draft.current_season()
    quality = draft.current_quality()
    if not season or not quality:
        return
    if not draft.current_quality_files:
        await context.application.bot.send_message(
            chat_id=draft.chat_id,
            text=f"No files received for {season} {quality}. Forward at least one file first.",
        )
        return

    await _finalize_current_series_quality_bundle(update, context, draft)

    is_last_quality_for_season = (draft.current_quality_index + 1) >= len(draft.qualities)
    if is_last_quality_for_season:
        season_asset = await _finalize_season_bundle(update, context, draft, season)
        if season_asset is None:
            await context.application.bot.send_message(
                chat_id=draft.chat_id,
                text=f"⚠️ Could not finalize {season} — no episode files were saved for it.",
            )

    _advance_series_quality(draft)
    await _send_wizard_prompt(context.application.bot, draft)


# ──────────────────────────────────────────────────────────
#  CALLBACK-QUERY REPLY HELPER
#
#  `update.callback_query.message` is typed by python-telegram-bot
#  as `MaybeInaccessibleMessage | None` — NOT a full `Message`.
#  A "MaybeInaccessibleMessage" is what PTB hands back when the
#  original message is too old / was deleted / became otherwise
#  inaccessible; it does NOT expose `.reply_text()`, and the type
#  can also simply be None. Calling `query.message.reply_text(...)`
#  directly is therefore both a static-analysis error AND a real
#  runtime crash risk. Always reply via `update.effective_chat.id`
#  instead, which is stable regardless of message accessibility.
# ──────────────────────────────────────────────────────────
async def _wizard_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs) -> None:
    chat = update.effective_chat
    chat_id = chat.id if chat else None
    if chat_id is None:
        return
    try:
        await context.application.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except TelegramError as exc:
        log.error(f"[WIZARD] reply failed: {exc}")
        stats.inc("errors")


async def _handle_wizard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("mp:"):
        return False

    try:
        await query.answer()
    except TelegramError as exc:
        # Callback queries expire after ~30s (or the button message was
        # deleted) — this is expected and must never crash the handler.
        log.debug(f"[WIZARD] query.answer() failed (likely expired): {exc}")

    user = update.effective_user
    if not user:
        return True
    draft = _get_draft(user.id)
    if not draft:
        await _wizard_reply(update, context, "No active draft. Send /makepost to start.")
        return True

    parts = query.data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else ""

    if action == "mode":
        draft.mode = value
        draft.step = "poster"
        await _send_wizard_prompt(context.application.bot, draft)
        return True
    if action == "skip":
        if value == "poster":
            draft.poster_file_id = None
            draft.step = "title"
        elif value == "languages":
            draft.languages = []
            draft.step = "qualities"
        elif value == "qualities":
            draft.qualities = ["Main"]
            if draft.mode == "series":
                draft.step = "seasons"
            else:
                _start_quality_flow(draft)
        elif value == "seasons":
            draft.seasons = ["S01"]
            _start_season_flow(draft)
        elif value == "edit_poster":
            draft.poster_file_id = None
            await _finalize_and_show_preview(update, context, draft)
            return True
        elif value == "topic":
            draft.topic_id = CFG.ALL_ADDED_SHOWS
            draft.topic_name = _topic_name_by_id(CFG.ALL_ADDED_SHOWS)
            await _finalize_and_show_preview(update, context, draft)
            return True
        await _send_wizard_prompt(context.application.bot, draft)
        return True
    if action == "topic":
        draft.topic_id = int(value)
        draft.topic_name = _topic_name_by_id(draft.topic_id)
        await _finalize_and_show_preview(update, context, draft)
        return True
    if action == "upload_done":
        await _finalize_series_quality(update, context, draft)
        return True
    if action == "post":
        if not draft.topic_id:
            await _wizard_reply(update, context, "Choose a topic first.")
            return True
        if not draft.send_all_asset:
            draft.send_all_asset = await _finalize_send_all_bundle(update, context, draft)
        sent = await _publish_draft_to_group(context, draft)
        if sent:
            await _crosspost_to_all_added_shows(context, draft, sent)
            await _wizard_reply(update, context, "✅ Posted successfully.")
            _clear_draft(user.id)
        else:
            await _wizard_reply(update, context, "❌ Could not post.")
        return True
    if action == "edit":
        if value == "":
            draft.step = "preview"
            await _wizard_reply(update, context, "Choose a field to edit:", reply_markup=_edit_keyboard(draft))
            return True
        if value == "back":
            draft.step = "preview"
            await _finalize_and_show_preview(update, context, draft)
            return True
        draft.step = f"edit_{value}"
        if value == "poster":
            await _wizard_reply(
                update, context,
                "Forward a new poster image, or tap Skip to remove the poster:",
                reply_markup=_simple_skip_keyboard("edit_poster"),
            )
        else:
            await _wizard_reply(update, context, _current_prompt(draft))
        return True
    if action == "cancel":
        _clear_draft(user.id)
        await _wizard_reply(update, context, "Draft cancelled.")
        return True
    return True


async def cmd_makepost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user = update.effective_user
    if not msg or not user:
        return
    if user.id not in CFG.ADMIN_IDS:
        return
    draft = PostDraft(user_id=user.id, chat_id=msg.chat.id)
    _POST_DRAFTS[user.id] = draft
    await msg.reply_text("Choose post type:", reply_markup=_mode_keyboard())


# ──────────────────────────────────────────────────────────
#  "🚀 Publish"  →  DEPLOY TO BACKUP  (v6.6, extended in v6.7)
#
#  MiddleMan (GROUP_CHAT_ID) remains the permanent, private archive.
#  EVERY movie post — both /makepost wizard posts AND automatically
#  indexed uploads (`process_batch`) — is stored as a single lightweight
#  MongoDB "movie" document (see the "movies collection" section above)
#  and gets an admin-only "🚀 Publish" button. Pressing it:
#
#    1. Validates the presser is an admin — non-admins get an alert
#       reading "You are not authorized." (silently ignored nowhere;
#       Publish always answers the callback either way).
#    2. Loads the movie document from MongoDB via the id embedded in the
#       callback_data ("deploy:<mongo_id>") — NOT re-uploaded, NOT
#       duplicated; this is the exact same document the MiddleMan post
#       was built from.
#    3. Recreates the post from scratch (never forwarded/copied — Telegram
#       strips inline keyboards from forwards) as a brand-new message
#       inside BACKUP_GROUP_CHAT_ID, in the topic resolved by
#       `_backup_topic_id()` (identity remap by default; only consults
#       BACKUP_TOPIC_MAP if that's configured), reusing `safe_send` for
#       retries/closed-topic handling exactly like every other post in
#       this bot.
#    4. For wizard posts that live in their own topic, also creates a
#       matching Search-index entry inside BACKUP_ALL_ADDED_SHOWS whose
#       button points at the NEW Backup post — never back to MiddleMan.
#       Auto-indexed posts already ARE an index post, so this step is
#       skipped for them (no duplicate index entry).
#    5. Stores a tiny "publish_records" document (movie_id + where the
#       Backup copy landed + timestamp) and relabels the button
#       "✅ Published" on the MiddleMan post — the callback_data is left
#       untouched, so it can be pressed again any time.
#
#  If Backup ever gets banned: create Backup 2, update
#  BACKUP_GROUP_CHAT_ID / BACKUP_ALL_ADDED_SHOWS (and BACKUP_TOPIC_MAP,
#  only if topic ids don't line up) in .env, restart, and press
#  "🚀 Publish" again on each MiddleMan post — no re-upload, no manual
#  editing, no forwarding, and still only ONE MongoDB movie document
#  per movie. Any failure is caught, logged, and reported back to the
#  admin without crashing — just press Publish again to retry.
# ──────────────────────────────────────────────────────────
async def _deploy_movie_to_backup(context: ContextTypes.DEFAULT_TYPE, movie: dict) -> Optional[Message]:
    """Recreate a stored movie document as a brand-new message in Backup.

    If the movie has a real topic_id (a /makepost wizard post that lives
    in its own named topic), that topic is remapped via `_backup_topic_id`
    (identity by default). If topic_id is None — an auto-indexed post
    that already lives directly inside ALL_ADDED_SHOWS — it's recreated
    directly inside BACKUP_ALL_ADDED_SHOWS instead.
    """
    topic_id = movie.get("topic_id")
    target_topic_id = _backup_topic_id(topic_id) if topic_id else CFG.BACKUP_ALL_ADDED_SHOWS
    poster_file_id = movie.get("poster_file_id")
    text = movie.get("text") or f"<b>{html_escape(movie.get('title') or 'Untitled')}</b>"

    keyboard = [
        [InlineKeyboardButton(b["label"], url=b["url"])]
        for b in (movie.get("buttons") or [])
        if b.get("label") and b.get("url")
    ]
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    if poster_file_id:
        return await safe_send(
            context.application.bot,
            method="send_photo",
            chat_id=CFG.BACKUP_GROUP_CHAT_ID,
            message_thread_id=target_topic_id,
            photo=poster_file_id,
            caption=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
        )
    return await safe_send(
        context.application.bot,
        chat_id=CFG.BACKUP_GROUP_CHAT_ID,
        message_thread_id=target_topic_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )


async def _post_backup_search_index(context: ContextTypes.DEFAULT_TYPE, movie: dict, backup_msg: Message) -> Optional[Message]:
    """Create the matching Search index entry inside BACKUP_ALL_ADDED_SHOWS,
    pointing at the freshly-created Backup post — never back to MiddleMan."""
    topic_id = _backup_topic_id(movie.get("topic_id"))
    if topic_id:
        link = make_topic_msg_link(CFG.BACKUP_GROUP_CHAT_ID, topic_id, backup_msg.message_id)
    else:
        link = make_msg_link(CFG.BACKUP_GROUP_CHAT_ID, backup_msg.message_id)

    title = movie.get("title") or "Untitled"
    button_label = _button_label(title)
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton(button_label, url=link)]])
    text = movie.get("text") or f"<b>{html_escape(title)}</b>"
    poster_file_id = movie.get("poster_file_id")

    if poster_file_id:
        return await safe_send(
            context.application.bot,
            method="send_photo",
            chat_id=CFG.BACKUP_GROUP_CHAT_ID,
            message_thread_id=CFG.BACKUP_ALL_ADDED_SHOWS,
            photo=poster_file_id,
            caption=text,
            reply_markup=buttons,
            parse_mode=ParseMode.HTML,
        )

    return await safe_send(
        context.application.bot,
        chat_id=CFG.BACKUP_GROUP_CHAT_ID,
        message_thread_id=CFG.BACKUP_ALL_ADDED_SHOWS,
        text=text,
        reply_markup=buttons,
        parse_mode=ParseMode.HTML,
    )


async def _mark_publish_button_done(query, movie_id: str) -> None:
    """Best-effort: relabel the pressed button '✅ Published' on the
    MiddleMan post, keeping the SAME callback_data so re-publishing
    later (e.g. after the Backup group is replaced) still works."""
    msg = query.message
    if not msg or not msg.reply_markup:
        return
    new_rows = []
    changed = False
    for row in msg.reply_markup.inline_keyboard:
        new_row = []
        for btn in row:
            if btn.callback_data == f"deploy:{movie_id}":
                new_row.append(InlineKeyboardButton("✅ Published", callback_data=btn.callback_data))
                changed = True
            else:
                new_row.append(btn)
        new_rows.append(new_row)
    if not changed:
        return
    try:
        await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(new_rows))
    except TelegramError as exc:
        log.debug(f"[DEPLOY] could not relabel Publish button: {exc}")


async def _publish_movie_core(context: ContextTypes.DEFAULT_TYPE, movie_id: str, movie: dict) -> Tuple[bool, str, Optional[Message]]:
    """
    Shared publish logic used by BOTH the per-post "🚀 Publish" button
    (`handle_deploy_callback`) and the bulk `/republishall` admin command.

    Deploys `movie` into the CURRENT Backup group/topic (reading
    BACKUP_GROUP_CHAT_ID / BACKUP_ALL_ADDED_SHOWS / BACKUP_TOPIC_MAP fresh
    from CFG every time — so it always targets whatever Backup group is
    configured right now), creates the matching Search-index entry when
    needed, and stores a lightweight publish record.

    Returns (success, status_note, backup_message).
    """
    backup_msg = await _deploy_movie_to_backup(context, movie)
    if not backup_msg:
        return False, "could not post to the Backup group", None

    # Auto-indexed posts (topic_id is None) already ARE the search-index
    # post — recreating a second index entry inside BACKUP_ALL_ADDED_SHOWS
    # would just duplicate it. Only /makepost wizard posts (real topic_id)
    # need the separate index-post step.
    index_msg = None
    if movie.get("topic_id"):
        index_msg = await _post_backup_search_index(context, movie, backup_msg)

    stats.inc("backup_indexed")

    _store_publish_record({
        "movie_id": movie_id,
        "middleman_message_id": movie.get("source_message_id"),
        "public_group_id": CFG.BACKUP_GROUP_CHAT_ID,
        "public_topic_id": _backup_topic_id(movie.get("topic_id")) if movie.get("topic_id") else CFG.BACKUP_ALL_ADDED_SHOWS,
        "public_message_id": backup_msg.message_id,
        "publish_timestamp": time.time(),
    })

    if movie.get("topic_id") and not index_msg:
        return True, "posted, but the Search index entry failed", backup_msg
    return True, "published", backup_msg


async def handle_deploy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("deploy:"):
        return

    user = update.effective_user
    if not user or user.id not in CFG.ADMIN_IDS:
        try:
            await query.answer("You are not authorized.", show_alert=True)
        except TelegramError:
            pass
        return

    try:
        await query.answer()
    except TelegramError as exc:
        log.debug(f"[DEPLOY] query.answer() failed (likely expired): {exc}")

    chat = update.effective_chat
    reply_chat_id = chat.id if chat else None
    movie_id = query.data.split(":", 1)[1] if ":" in query.data else ""

    # Everything below is wrapped so a failure (network blip, Telegram
    # error, bad topic id, etc.) is logged and reported back to the
    # admin WITHOUT crashing the bot or the update loop — the admin can
    # simply press "🚀 Publish" again to retry, nothing is left in a
    # half-published state since the publish record is only written on
    # full success.
    try:
        movie = _get_movie(movie_id) if movie_id else None
        if not movie:
            if reply_chat_id is not None:
                await context.application.bot.send_message(
                    chat_id=reply_chat_id, text="❌ Movie not found in database."
                )
            return

        success, note, backup_msg = await _publish_movie_core(context, movie_id, movie)
        if not success or backup_msg is None:
            if reply_chat_id is not None:
                await context.application.bot.send_message(
                    chat_id=reply_chat_id,
                    text=f"❌ {note.capitalize()}. You can retry by pressing Publish again.",
                )
            return

        await _mark_publish_button_done(query, movie_id)

        if reply_chat_id is not None:
            if note == "published":
                await context.application.bot.send_message(
                    chat_id=reply_chat_id,
                    text=f"✅ Published — {movie.get('title') or 'Untitled'!r}.",
                )
            else:
                await context.application.bot.send_message(
                    chat_id=reply_chat_id,
                    text=f"⚠️ {note.capitalize()}. Check BACKUP_ALL_ADDED_SHOWS.",
                )

        log.info(
            f"[DEPLOY] movie_id={movie_id} title={movie.get('title')!r} "
            f"→ backup_message_id={backup_msg.message_id}"
        )
    except Exception as exc:  # noqa: BLE001 — never let Publish crash the bot
        log.exception(f"[DEPLOY] Publish failed for movie_id={movie_id!r}: {exc}")
        if reply_chat_id is not None:
            try:
                await context.application.bot.send_message(
                    chat_id=reply_chat_id,
                    text="❌ Publish failed unexpectedly. Check logs and press Publish again to retry.",
                )
            except TelegramError:
                pass


# ──────────────────────────────────────────────────────────
#  "/republishall"  →  BULK PUBLISH  (v6.8, disaster recovery)
#
#  For when the Backup group itself gets banned and there are hundreds
#  of MiddleMan posts to push to the NEW Backup group — clicking
#  "🚀 Publish" one post at a time isn't practical. This admin-only
#  command walks EVERY movie document in MongoDB (from the primary
#  MiddleMan, a mirror, or wherever it's run from — it only reads
#  MongoDB, never the chat it's called from) and republishes each one
#  via the exact same `_publish_movie_core` used by the single-post
#  button, so behaviour is identical:
#
#    • Same topic-name-based remap via BACKUP_TOPIC_MAP for wizard
#      posts (each lands in the SAME named topic in the new Backup
#      group as it was in MiddleMan — no confusion, no manual mapping).
#    • Auto-indexed posts go straight to BACKUP_ALL_ADDED_SHOWS.
#    • Runs as a background task so it never blocks other bot activity
#      (indexing, /makepost, etc.) while it works through the catalog.
#    • Only one run at a time (a second /republishall while one is
#      already running is rejected, not queued or stacked).
#    • Paced with a short delay between posts, on top of `safe_send`'s
#      own flood-wait retry handling, to stay well clear of Telegram's
#      per-chat rate limits.
#    • Never crashes: any single movie failing is logged and counted,
#      the run continues, and a final summary (with up to 15 failure
#      reasons) is posted back when done.
# ──────────────────────────────────────────────────────────
_REPUBLISH_LOCK = threading.Lock()
_REPUBLISH_RUNNING = False


async def _run_republish_all(context: ContextTypes.DEFAULT_TYPE, reply_chat_id: int) -> None:
    global _REPUBLISH_RUNNING
    bot = context.application.bot
    try:
        col = _movies_collection()
        with _DB_LOCK:
            docs = list(col.find({}).sort("created_at", 1))

        total = len(docs)
        if total == 0:
            await bot.send_message(chat_id=reply_chat_id, text="No movies found in MongoDB — nothing to republish.")
            return

        await bot.send_message(
            chat_id=reply_chat_id,
            text=(
                f"🚀 Republishing {total} MiddleMan post(s) into the CURRENT "
                f"Backup group ({CFG.BACKUP_GROUP_CHAT_ID}) — this runs in the "
                f"background, progress every 25 posts."
            ),
        )

        ok = 0
        failed = 0
        failures: List[str] = []

        for i, doc in enumerate(docs, start=1):
            movie_id = str(doc["_id"])
            doc = dict(doc)
            doc["_id"] = movie_id
            title = doc.get("title") or movie_id

            try:
                success, note, _backup_msg = await _publish_movie_core(context, movie_id, doc)
                if success:
                    ok += 1
                else:
                    failed += 1
                    failures.append(f"{title}: {note}")
            except Exception as exc:  # noqa: BLE001 — one bad movie must not kill the whole run
                failed += 1
                failures.append(f"{title}: {exc}")
                log.exception(f"[REPUBLISH-ALL] unexpected error on movie_id={movie_id}: {exc}")

            if i % 25 == 0 or i == total:
                try:
                    await bot.send_message(
                        chat_id=reply_chat_id,
                        text=f"⏳ Progress: {i}/{total}  ✅ {ok}  ❌ {failed}",
                    )
                except TelegramError:
                    pass

            # Gentle pacing on top of safe_send's own flood-wait retries.
            await asyncio.sleep(1.2)

        summary = f"✅ Republish complete — {ok}/{total} succeeded, {failed} failed."
        if failures:
            preview = "\n".join(f"• {f}" for f in failures[:15])
            more = f"\n… and {len(failures) - 15} more (see logs)" if len(failures) > 15 else ""
            summary += f"\n\nFailures:\n{preview}{more}"

        await bot.send_message(chat_id=reply_chat_id, text=summary[:4000])
        log.info(f"[REPUBLISH-ALL] done — {ok}/{total} ok, {failed} failed")

    except Exception as exc:  # noqa: BLE001 — the whole run must never crash the bot
        log.exception(f"[REPUBLISH-ALL] fatal error: {exc}")
        try:
            await bot.send_message(chat_id=reply_chat_id, text=f"❌ /republishall crashed: {exc}")
        except TelegramError:
            pass
    finally:
        with _REPUBLISH_LOCK:
            _REPUBLISH_RUNNING = False


async def cmd_republishall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user = update.effective_user
    if not msg or not user:
        return
    if user.id not in CFG.ADMIN_IDS:
        return

    global _REPUBLISH_RUNNING
    with _REPUBLISH_LOCK:
        if _REPUBLISH_RUNNING:
            await msg.reply_text("⏳ A /republishall run is already in progress — please wait for it to finish.")
            return
        _REPUBLISH_RUNNING = True

    # Fire-and-forget as a background task so this command returns
    # immediately and never blocks other bot activity while it runs.
    asyncio.create_task(_run_republish_all(context, msg.chat.id))
    await msg.reply_text("🚀 Starting bulk republish — I'll post progress here.")


# ──────────────────────────────────────────────────────────
#  PRIVATE UPLOAD + REDEMPTION HANDLERS
# ──────────────────────────────────────────────────────────
async def handle_private_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle any non-command private message from an admin.

    Two distinct flows share this entry point:

    1. /makepost WIZARD — if the admin has an active PostDraft (started via
       /makepost), every subsequent private message they send is an answer
       to the wizard's current step.

       ⚠️  BUG FIX (v6.2): previously, ANY step that wasn't "files" ran the
       incoming message through `_handle_wizard_text`, which calls
       `extract_text(msg)` — and `extract_text()` falls back to the
       DOCUMENT'S FILENAME when the message has no text/caption. If an
       admin forwarded episode files a moment before the wizard had
       actually advanced to its "files" step (e.g. immediately after
       tapping "Skip" on season/topic), that file's filename got silently
       consumed as the text answer for whatever step the wizard was still
       on — corrupting that field (you'd see a season/topic name that was
       actually a filename) AND permanently dropping that first file,
       since it was never stored as an episode asset.

       The fix: check whether the incoming message actually carries a
       supported file BEFORE deciding how to route it. If it does, and the
       wizard is NOT on the "files" step, the file is never treated as
       text — the admin is told to finish the current step first, and can
       simply re-forward the file once the wizard is ready for it. This
       also guarantees filenames are NEVER used to drive wizard state —
       only the explicit "Upload Complete" button press does that.

       (v6.4): the new "poster" / "edit_poster" steps get the same
       file-vs-text routing treatment, via their own dedicated branch.

    2. STANDALONE QUICK-LINK UPLOAD — with no active draft, any forwarded/
       uploaded file from an admin gets turned into a single GPLinks deep
       link (unchanged legacy behaviour).
    """
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat or chat.type != "private":
        return
    if user.is_bot or user.id not in CFG.ADMIN_IDS:
        return

    # ── 1) Active /makepost wizard takes priority ──────────────────────
    draft = _get_draft(user.id)
    if draft is not None:
        payload = _supported_private_payload(msg)

        if draft.step in ("poster", "edit_poster"):
            if payload:
                await _handle_wizard_poster(update, context, draft, payload)
            else:
                await context.application.bot.send_message(
                    chat_id=chat.id,
                    text="Please forward the poster image, or tap Skip above to continue.",
                )
            return

        if draft.step == "files":
            if payload:
                await _handle_wizard_file(update, context, draft, payload)
            else:
                await context.application.bot.send_message(
                    chat_id=chat.id,
                    text="Please forward a file for this quality, or use the buttons above.",
                )
            return

        if payload is not None:
            # A file arrived while the wizard is still waiting on a TEXT
            # step (title/languages/qualities/seasons/topic/etc). Do NOT
            # let its filename get swallowed as that step's answer — this
            # is exactly the bug described above. Tell the admin to finish
            # the current step; they can re-forward the file afterwards.
            await context.application.bot.send_message(
                chat_id=chat.id,
                text=(
                    "⏳ Please finish the current step first (see the prompt "
                    "above) — I'll ask you to forward files once we get there. "
                    "Your file was NOT saved; please resend it when prompted."
                ),
            )
            return

        handled = await _handle_wizard_text(update, context, draft)
        if handled:
            return
        # A step that only accepts button taps (e.g. "mode") got a text
        # message instead — nudge the admin rather than silently falling
        # through to the standalone upload flow, which would be confusing
        # mid-wizard.
        await context.application.bot.send_message(
            chat_id=chat.id,
            text="Please use the buttons above, or send /makepost to restart.",
        )
        return

    # ── 2) No active draft: standalone quick-link upload ────────────────
    payload = _supported_private_payload(msg)
    if not payload:
        return

    token = secrets.token_hex(8)
    deep_link = make_bot_start_link(token)
    short_url = await _shorten_with_gplinks(deep_link, alias=token)

    record = {
        "token": token,
        "file_id": payload["file_id"],
        "file_type": payload["file_type"],
        "file_name": payload.get("file_name", ""),
        "caption": payload.get("caption", ""),
        "deep_link": deep_link,
        "short_url": short_url,
        "source_chat_id": chat.id,
        "source_message_id": msg.message_id,
        "created_at": time.time(),
        "downloads_count": 0,
    }
    _store_download(record)
    stats.inc("indexed")
    title = _trim_caption(payload.get("file_name") or payload.get("caption") or "File")
    safe_title = html_escape(title)
    reply_text = (
        f"✅  <b>Link generated</b>\n"
        f"📄  <b>{safe_title}</b>\n"
        f"🔗  <code>{short_url}</code>\n\n"
        f"🧩  <i>Deep link backup:</i>\n"
        f"<code>{deep_link}</code>\n\n"
        f"📌  <i>Paste the short link into your group post.</i>"
    )
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("Open short link", url=short_url)]])
    await safe_send(
        context.application.bot,
        chat_id=chat.id,
        text=reply_text,
        reply_markup=buttons,
        parse_mode=ParseMode.HTML,
    )
    log.info(f"[LINK-GEN] admin={user.id} token={token} type={payload['file_type']} file={title!r}")


async def _redeem_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or chat.type != "private":
        return

    token = ""
    if context.args:
        token = context.args[0].strip()
    if not token:
        await msg.reply_text(
            "Send a valid GPLinks/Telegram deep link token.\n"
            "Example: /start abc123",
        )
        return

    record = _get_download(token)
    if record:
        sent = await _deliver_file(context.application.bot, chat.id, record)
        if sent:
            _increment_download_count(token)
            await _send_delivery_notice(context, chat.id, user, [sent])
            log.info(f"[REDEEM] token={token} delivered to user={chat.id}")
            return
        await msg.reply_text("❌  I could not deliver that file format.")
        return

    bundle = _get_bundle(token)
    if bundle:
        delivered_msgs = await _deliver_token(context.application.bot, chat.id, token)
        if delivered_msgs:
            await _send_delivery_notice(context, chat.id, user, delivered_msgs)
            log.info(f"[REDEEM-BUNDLE] token={token} delivered {len(delivered_msgs)} item(s) to user={chat.id}")
            return
        await msg.reply_text("❌  I could not deliver that bundle.")
        return

    await msg.reply_text(
        "❌  Invalid or expired token.",
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _redeem_token(update, context)


async def cmd_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _redeem_token(update, context)


# ──────────────────────────────────────────────────────────
#  MESSAGE HANDLER  (admin indexing)
# ──────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg  = update.effective_message
    user = update.effective_user

    if not msg or not user:
        return
    if msg.chat.id != CFG.GROUP_CHAT_ID:
        return
    # Only index messages from known admins; bots are always ignored
    if user.is_bot or user.id not in CFG.ADMIN_IDS:
        return

    thread_id = getattr(msg, "message_thread_id", None)
    # Don't re-index posts the bot itself makes in the index topic
    if thread_id == CFG.ALL_ADDED_SHOWS:
        return

    # Ignore configured topics entirely (e.g. "# General" id=1,
    # "GENERAL CHAT (GC)" id=1796). Messages posted here are never
    # batched/forwarded into ALL_ADDED_SHOWS. Configure via the
    # IGNORED_TOPICS env var (comma-separated topic ids).
    if thread_id is not None and thread_id in CFG.IGNORED_TOPICS:
        log.debug(f"[IGNORED-TOPIC] thread_id={thread_id} - skipping")
        return

    text = extract_text(msg)
    mgid = getattr(msg, "media_group_id", None)

    async with _pending_lock:

        # ── Album continuation: attach to the existing batch ──────────
        if mgid and mgid in _mgid_map:
            key = _mgid_map[mgid]
            if key in _pending:
                _pending[key].msgs.append(msg)
                _pending[key].touch()
                if thread_id:
                    _pending[key].thread_ids.add(thread_id)
                log.debug(f"[ALBUM-CONT] key={key!r} mgid={mgid}")
                return

        # ── File-only / filename-only messages ────────────────────────
        if not text or is_filename(text):
            if text:
                fname_key = movie_key(text)
                if fname_key:
                    if fname_key in _pending:
                        _pending[fname_key].msgs.append(msg)
                        _pending[fname_key].touch()
                        if thread_id:
                            _pending[fname_key].thread_ids.add(thread_id)
                        log.debug(f"[FILE-ATTACH] key={fname_key!r}")

                    elif _msg_is_split_part(msg):
                        _pending[fname_key] = Batch()
                        _pending[fname_key].msgs.append(msg)
                        _pending[fname_key].touch()
                        if thread_id:
                            _pending[fname_key].thread_ids.add(thread_id)
                        if mgid:
                            _mgid_map[mgid] = fname_key
                            _pending[fname_key].media_group_ids.add(mgid)
                        log.debug(f"[ZIP-START] key={fname_key!r} file={text!r}")
            return

        # ── Normal text / captioned message ───────────────────────────
        key = movie_key(text)
        if not key:
            return

        if mgid:
            _mgid_map[mgid] = key

        if key not in _pending:
            _pending[key] = Batch()

        batch = _pending[key]
        batch.msgs.append(msg)
        batch.touch()
        if thread_id:
            batch.thread_ids.add(thread_id)
        if mgid:
            batch.media_group_ids.add(mgid)

    log.debug(f"[BUFFER] key={key!r} total={len(_pending[key].msgs)}")


# ──────────────────────────────────────────────────────────
#  BACKUP GROUP — FORWARDED POST DETECTION  (v6.5, LEGACY)
#
#  NOTE (v6.6): this handler is kept registered for backward
#  compatibility but is SUPERSEDED by the "🚀 Publish" deploy
#  button above. Forwarding strips inline keyboards, so a forwarded
#  MiddleMan post never carries its quality/season buttons into
#  Backup — only its title text. The new deploy flow recreates the
#  post from MongoDB with buttons intact, which is why it's the
#  recommended way to populate a Backup group going forward. This
#  handler is left ACTIVE (not removed) per "do not break anything
#  that currently works" — if you no longer manually forward posts
#  into Backup, it will simply never fire.
#
#  MiddleMan (CFG.GROUP_CHAT_ID) is the single source of truth. The bot
#  builds every movie/series/search post there exactly as before — this
#  section changes NOTHING about that flow.
#
#  Backup (CFG.BACKUP_GROUP_CHAT_ID) is the group real members are in.
#  The bot NEVER posts movies there on its own via THIS handler. When a
#  forwarded message lands, this handler:
#
#    1. Confirms it arrived in Backup AND is a genuine forwarded message
#       (checked via `forward_origin`, the Bot API 7.0+ field that
#       replaced the old forward_from/forward_from_chat pair).
#    2. Extracts the same title text the original MiddleMan post used
#       (via the existing `extract_text()` helper — works for both plain
#       text posts and poster-photo posts with an HTML caption).
#    3. Builds a small Search-index entry using the SAME title-cleaning /
#       icon / quality-tag helpers the main indexer already uses
#       (`clean_title_for_display`, `_determine_icon`, `extract_quality_tags`).
#    4. Posts that entry into BACKUP_ALL_ADDED_SHOWS with a single button
#       that links to the FORWARDED message's own message_id inside
#       Backup — built with the existing `make_msg_link` / 
#       `make_topic_msg_link` helpers — NEVER back to MiddleMan.
# ──────────────────────────────────────────────────────────
async def handle_backup_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg  = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    # Only react inside the Backup group.
    if msg.chat.id != CFG.BACKUP_GROUP_CHAT_ID:
        return

    # Only the admin manually forwarding archive posts should trigger this.
    if user.is_bot or user.id not in CFG.ADMIN_IDS:
        return

    # Must be a genuine forwarded message. `forward_origin` (Bot API 7.0+,
    # exposed by python-telegram-bot v22) is set on ANY forwarded message
    # regardless of whether the original sender/channel allows forward
    # attribution — unlike the older forward_from/forward_from_chat pair,
    # which could be None for privacy-protected forwards.
    if not getattr(msg, "forward_origin", None):
        return

    thread_id = getattr(msg, "message_thread_id", None)

    # Don't create a Search entry for something forwarded directly INTO
    # the Backup Search topic itself (nothing meaningful to index there).
    if thread_id == CFG.BACKUP_ALL_ADDED_SHOWS:
        return

    text = extract_text(msg)
    if not text or is_filename(text):
        # Forwarded file with no meaningful title text (e.g. a bare
        # document forward) — nothing to build a Search entry from.
        log.debug("[BACKUP-FORWARD] no usable title text — skipping")
        return

    key = movie_key(text)
    if not key:
        return

    # Separate namespace from the MiddleMan seen_cache keys so a title
    # already indexed in MiddleMan doesn't block its Backup Search entry,
    # and vice versa.
    backup_key = f"backup:{key}"
    if seen_cache.has(backup_key):
        log.debug(f"[BACKUP-DUPE] {key!r}")
        stats.inc("skipped_dupes")
        return
    seen_cache.set(backup_key)

    display_title, year = clean_title_for_display(text)
    year_badge   = f"  <code>{year}</code>" if year else ""
    icon         = _determine_icon(text)
    quality_tags = extract_quality_tags(text)
    quality_line = f"\n🎞  <i>{quality_tags}</i>" if quality_tags else ""
    thread_note  = f"\n📌  <code>Thread #{thread_id}</code>" if thread_id else ""

    post_text = (
        f"{icon}  <b>{display_title[:200]}</b>{year_badge}"
        f"{quality_line}"
        f"{thread_note}\n"
        f"{_SEP}\n"
        f"👇  <i>Tap to open the post</i>"
    )

    # Link points at the FORWARDED message's own id inside Backup — using
    # the same link builders the main indexer already relies on.
    if thread_id:
        link = make_topic_msg_link(msg.chat.id, thread_id, msg.message_id)
    else:
        link = make_msg_link(msg.chat.id, msg.message_id)

    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("📥  Open Post", url=link)]])

    sent = await safe_send(
        context.application.bot,
        chat_id=CFG.BACKUP_GROUP_CHAT_ID,
        message_thread_id=CFG.BACKUP_ALL_ADDED_SHOWS,
        text=post_text,
        reply_markup=buttons,
        parse_mode=ParseMode.HTML,
    )
    if sent:
        stats.inc("backup_indexed")
        log.info(f"[BACKUP-INDEX] {display_title!r} link={link}")
    else:
        log.warning(f"[BACKUP-INDEX] failed to post Search entry for {display_title!r}")


# ──────────────────────────────────────────────────────────
#  ADMIN COMMANDS
#
#  Every command below is decorated with @_admin_only which
#  silently ignores requests from non-admin users.
#
#  In addition, on_startup() hides ALL commands from regular
#  users via BotCommandScope so they never even see the menu.
# ──────────────────────────────────────────────────────────
def _admin_only(fn):
    """Decorator: silently ignore requests from non-admins."""
    @wraps(fn)
    async def _inner(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in CFG.ADMIN_IDS:
            return   # silent ignore
        return await fn(update, context)
    return _inner


_CMD_SEP = "━" * 24


@_admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show runtime statistics."""
    msg = update.message
    if not msg:
        return
    s = stats.snapshot()
    await msg.reply_text(
        f"📊  <b>Bot Status — v6.6</b>\n"
        f"{_CMD_SEP}\n"
        f"🎬  Indexed:         <b>{s['indexed']}</b>\n"
        f"📦  ZIP hints:       <b>{s['zip_hints']}</b>\n"
        f"⏭   Skipped dupes:  <b>{s['skipped_dupes']}</b>\n"
        f"❌  Errors:          <b>{s['errors']}</b>\n"
        f"🗂   Seen cache:      <b>{len(seen_cache)}</b> entries\n"
        f"⏳  Pending:         <b>{len(_pending)}</b> batches\n"
        f"👋  Welcomes sent:   <b>{s['welcomes_sent']}</b>\n"
        f"🗑   Auto-deleted:    <b>{s['auto_deleted']}</b> file msgs\n"
        f"📤  Backup indexed:  <b>{s['backup_indexed']}</b>\n"
        f"⏱   Uptime:          <b>{s['uptime_sec']}s</b>\n"
        f"👥  Admins:          <b>{len(CFG.ADMIN_IDS)}</b>\n"
        f"⚡  Debounce:        <b>{CFG.DEBOUNCE_SEC}s</b>\n"
        f"🕑  Delete delay:    <b>{CFG.DELETE_DELAY_SEC}s</b>",
        parse_mode=ParseMode.HTML,
    )


@_admin_only
async def cmd_clearcache(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all in-memory caches so titles can be re-indexed."""
    msg  = update.message
    user = update.effective_user
    if not msg or not user:
        return
    seen_cache.clear()
    zip_cache.clear()
    async with _pending_lock:
        _mgid_map.clear()
    await msg.reply_text(
        "✅  <b>All caches cleared.</b>\n"
        "Every title can now be re-indexed.\n",
        parse_mode=ParseMode.HTML,
    )
    log.info(f"Caches cleared by admin {user.id}")


@_admin_only
async def cmd_flush(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-flush all pending batches immediately without waiting for debounce."""
    msg = update.message
    if not msg:
        return

    async with _pending_lock:
        keys    = list(_pending.keys())
        batches = {k: _pending.pop(k) for k in keys}

    if not batches:
        await msg.reply_text("📭  No pending batches to flush.")
        return

    await msg.reply_text(
        f"⚡  Flushing <b>{len(batches)}</b> batch(es)…",
        parse_mode=ParseMode.HTML,
    )
    for batch in batches.values():
        try:
            await process_batch(context.application, batch)
        except Exception as exc:
            log.exception(f"process_batch error during manual flush: {exc}")
            stats.inc("errors")
    await msg.reply_text("✅  All batches flushed.")


@_admin_only
async def cmd_reindex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /reindex <movie title>
    Remove a title from the seen-cache so posting it again will re-index it.
    """
    msg = update.message
    if not msg:
        return
    if not context.args:
        await msg.reply_text(
            "Usage: <code>/reindex movie title here</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    query    = " ".join(context.args)
    key      = movie_key(query)
    was_seen = seen_cache.has(key)

    if was_seen:
        seen_cache.delete(key)
        zip_cache.delete(key)
        await msg.reply_text(
            f"🔄  Removed from seen cache:\n<code>{key}</code>\n\n"
            f"Post the content again to re-index it.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await msg.reply_text(
            f"ℹ️  Not found in seen cache:\n<code>{key}</code>",
            parse_mode=ParseMode.HTML,
        )


@_admin_only
async def cmd_testdeletemessage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /testdeletemessage  (admin-only, DM or group)

    Lets an admin preview EXACTLY what a member sees after redeeming a file
    or bundle — using the admin's own name — without touching any real
    download/bundle token. It also fires a live demo of the auto-delete
    behaviour: a small test message is sent and genuinely deleted after the
    same CFG.DELETE_DELAY_SEC delay a real delivered file would be, so the
    admin can confirm deletion actually works (and that the bot has
    permission to delete its own messages in that chat).

    Nothing in the group or any member's chat is touched — every message
    this command sends/deletes lives only in the chat where /testdeletemessage
    was run.
    """
    msg = update.message
    user = update.effective_user
    if not msg or not user:
        return

    preview_text = _build_delivery_notice_text(user)

    await msg.reply_text(
        "🧪  <b>Preview mode</b> — this is exactly what a member sees after "
        "redeeming a file or bundle (shown here with YOUR name).",
        parse_mode=ParseMode.HTML,
    )
    await msg.reply_text(preview_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    demo = await msg.reply_text(
        f"🗑  Demo — this message will actually auto-delete in "
        f"{CFG.DELETE_DELAY_SEC}s, exactly like a delivered file would."
    )
    if demo:
        asyncio.create_task(
            _schedule_delete(context.application.bot, demo.chat.id, demo.message_id, CFG.DELETE_DELAY_SEC),
            name=f"autodelete-test-{demo.chat.id}-{demo.message_id}",
        )


@_admin_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display admin help message."""
    msg = update.message
    if not msg:
        return
    await msg.reply_text(
        f"🤖  <b>Media Indexer Bot v6.6  —  Admin Help</b>\n"
        f"{_CMD_SEP}\n"
        f"/status               — Runtime stats\n"
        f"/flush                — Force-process all pending batches now\n"
        f"/clearcache           — Clear seen / zip caches\n"
        f"/reindex &lt;title&gt;      — Remove title from cache; re-post to re-index\n"
        f"/makepost             — Build a post via the guided wizard (DM only)\n"
        f"/testdeletemessage    — Preview the file-delivery deletion notice\n"
        f"/help                 — This message\n\n"
        f"{_CMD_SEP}\n"
        f"🧠  <b>v6.6 Smart Behaviour</b>\n"
        f"• 1 post per movie — qualities as sorted buttons (4K first)\n"
        f"• .zip.001 / .part01.rar uploads trigger index posts ✅\n"
        f"• <b>Closed topics supported</b> — bot reopens, posts, re-closes\n"
        f"  (requires bot to be admin with 'Manage Topics' permission)\n"
        f"• New joiners get a welcome + deep-link into topic #{CFG.SEARCH_SHOWS_HERE}\n"
        f"• Private uploads from admins generate GPLinks + deep links\n"
        f"• <b>/makepost now asks for a poster image</b> right after you pick\n"
        f"  Movie/Series, before the title — it's attached to the final post\n"
        f"  (and its ALL_ADDED_SHOWS mirror) as the photo, with the post text\n"
        f"  as the caption. Skippable.\n"
        f"• <b>/makepost posts are now labelled</b> — Title: / Languages: /\n"
        f"  Quality: (or Seasons: for series) each on their own line\n"
        f"• <b>/makepost series posts support multiple seasons</b> —\n"
        f"  send total seasons like <code>S01, S02, S03</code>, upload each\n"
        f"  season's episodes, tap Upload Complete — buttons come out as\n"
        f"  <code>S01</code> / <code>S02</code> / … / <code>ALL SEASONS</code>\n"
        f"• <b>Auto cross-post</b>: every /makepost post also gets mirrored\n"
        f"  into ALL_ADDED_SHOWS with a single button back to the original\n"
        f"• <b>Self-destructing deliveries</b>: every file/bundle a member\n"
        f"  redeems via /start or /send now gets auto-deleted after\n"
        f"  <code>{CFG.DELETE_DELAY_SEC}s</code>, with a warning telling them to\n"
        f"  forward it to Saved Messages first — preview with /testdeletemessage\n"
        f"• <b>NEW — 🚀 Publish</b>: EVERY movie post — /makepost wizard posts\n"
        f"  AND automatically-indexed uploads — now carries an admin-only\n"
        f"  <code>🚀 Publish</code> button. Pressing it recreates that EXACT\n"
        f"  post (same text, poster, and buttons — never forwarded/copied) as\n"
        f"  a brand-new message inside the Backup group (wizard posts also get\n"
        f"  a matching Search entry inside BACKUP_ALL_ADDED_SHOWS pointing at\n"
        f"  the new Backup post — never back to MiddleMan). Nothing is\n"
        f"  re-uploaded and no duplicate movie document is ever created in\n"
        f"  MongoDB — only a tiny publish record. The button then relabels to\n"
        f"  <code>✅ Published</code> (still pressable again any time). If\n"
        f"  Backup ever gets banned: spin up Backup 2, update\n"
        f"  BACKUP_GROUP_CHAT_ID / BACKUP_ALL_ADDED_SHOWS (and\n"
        f"  BACKUP_TOPIC_MAP, only if topic ids differ) in .env, restart, and\n"
        f"  press <code>🚀 Publish</code> again on each MiddleMan post.\n"
        f"• <b>NEW — /republishall</b>: bulk-version of Publish. Walks EVERY\n"
        f"  movie in MongoDB and republishes each one into the CURRENT Backup\n"
        f"  group/topic — for when Backup gets banned and there are too many\n"
        f"  posts to Publish one at a time. Runs in the background, posts\n"
        f"  progress every 25 movies, and a final success/failure summary.\n"
        f"  Only one run at a time.\n"
        f"• <b>NEW — Dead MiddleMan mirror groups</b>: if the PRIMARY\n"
        f"  MiddleMan itself ever gets banned, configure up to 8 private\n"
        f"  \"dead clone\" groups via <code>MIDDLEMAN_MIRROR_1_CHAT_ID</code> /\n"
        f"  <code>MIDDLEMAN_MIRROR_1_TOPIC_MAP</code> (and _2_, _3_, …) — every\n"
        f"  post is best-effort mirrored into all of them with the SAME\n"
        f"  working <code>🚀 Publish</code> button. If the primary dies, open\n"
        f"  any mirror and Publish (or /republishall) from there instead —\n"
        f"  nothing depends on the primary surviving."
        f"{'  Currently configured: ' + str([m['chat_id'] for m in _MIDDLEMAN_MIRRORS]) if _MIDDLEMAN_MIRRORS else ' Currently NONE configured.'}\n"
        f"• Ignored topics (never indexed): {sorted(CFG.IGNORED_TOPICS) or 'none'}\n"
        f"• All commands are admin-only and hidden from regular users\n"
        f"• Link/bundle/movie storage all run on MongoDB Atlas (persistent)\n"
        f"• Bot token is NEVER revealed in logs",
        parse_mode=ParseMode.HTML,
    )


# ──────────────────────────────────────────────────────────
#  FLUSH LOOP  (background asyncio task)
# ──────────────────────────────────────────────────────────
async def flush_loop(tg_app: Application) -> None:
    """
    Background task that drains ready batches from `_pending`
    and periodically purges expired TTL-cache entries.
    """
    log.info("Flush loop started")
    tick = 0

    while True:
        try:
            await asyncio.sleep(CFG.FLUSH_INTERVAL)
            tick += 1

            async with _pending_lock:
                ready = {
                    k: b for k, b in _pending.items()
                    if b.is_ready(CFG.DEBOUNCE_SEC)
                }
                for k in ready:
                    del _pending[k]

            for batch_key, batch in ready.items():
                try:
                    await process_batch(tg_app, batch)
                except Exception as exc:
                    log.exception(f"process_batch error for {batch_key!r}: {exc}")
                    stats.inc("errors")

            # Every 60 ticks: purge expired cache entries + trim mgid map
            if tick % 60 == 0:
                n_seen = seen_cache.purge_expired()
                n_zip  = zip_cache.purge_expired()
                if n_seen or n_zip:
                    log.debug(
                        f"Cache purge: {n_seen} seen, {n_zip} zip entries expired"
                    )
                async with _pending_lock:
                    if len(_mgid_map) > 1_000:
                        overflow = list(_mgid_map.keys())[:-500]
                        for k in overflow:
                            _mgid_map.pop(k, None)

        except asyncio.CancelledError:
            log.info("Flush loop shutting down")
            break
        except Exception as exc:
            log.exception(f"Unexpected flush loop error: {exc}")
            stats.inc("errors")
            await asyncio.sleep(5)


# ──────────────────────────────────────────────────────────
#  STARTUP HOOK
# ──────────────────────────────────────────────────────────
async def on_startup(tg_app: Application) -> None:
    """
    1. Initialise shared asyncio lock.
    2. Connect to MongoDB Atlas (raises + aborts startup on failure).
    3. Register admin command scopes (hides commands from regular users).
    4. Start the background flush loop.
    """
    global _pending_lock, _BOT_USERNAME
    _pending_lock = asyncio.Lock()

    # Resolve bot username for deep links if not supplied via env
    if not _BOT_USERNAME:
        try:
            me = await tg_app.bot.get_me()
            _BOT_USERNAME = (me.username or "").strip()
            if _BOT_USERNAME:
                log.info(f"Resolved bot username: @{_BOT_USERNAME}")
        except Exception as exc:
            log.warning(f"Could not resolve bot username: {exc}")

    # ── Connect to MongoDB Atlas — fail loudly, don't start half-broken ──
    try:
        init_link_store()
    except Exception as exc:
        log.critical(
            f"[MONGO] Could not connect to MongoDB Atlas — bot cannot start "
            f"without persistent link storage. Check MONGODB_URI / "
            f"MONGODB_DB_NAME in .env, your Atlas IP whitelist (Network "
            f"Access → Add current IP / 0.0.0.0/0 for testing), and your "
            f"cluster's user credentials. Error: {exc}"
        )
        raise

    # ── Register admin command scopes ──────────────────────────────────
    # Strategy:
    #   a) Delete the DEFAULT scope → regular users see NO command menu.
    #   b) Set commands for the GROUP's administrators via
    #      BotCommandScopeChatAdministrators — all group admins see the menu
    #      when they type "/" in the group.
    #   c) Set commands for each individual admin's private DM with the bot
    #      via BotCommandScopeChat — admins can also use commands in DMs.
    # ──────────────────────────────────────────────────────────────────
    admin_cmds = [
        BotCommand("status",            "Stats & runtime info"),
        BotCommand("flush",             "Force-flush all pending batches"),
        BotCommand("clearcache",        "Clear seen / zip caches"),
        BotCommand("reindex",           "Remove a title — re-post to re-index"),
        BotCommand("makepost",          "Build a post via the guided wizard (DM)"),
        BotCommand("testdeletemessage", "Preview the file-delivery deletion notice"),
        BotCommand("help",              "Show admin help"),
    ]

    # a) Hide commands globally
    try:
        await tg_app.bot.delete_my_commands(scope=BotCommandScopeDefault())
        log.info("Global commands cleared — regular users see no command menu")
    except Exception as exc:
        log.warning(f"Could not clear global commands: {exc}")

    # b) Visible to all current/future group admins inside the group
    try:
        await tg_app.bot.set_my_commands(
            commands=admin_cmds,
            scope=BotCommandScopeChatAdministrators(chat_id=CFG.GROUP_CHAT_ID),
        )
        log.info(f"Admin commands set for group {CFG.GROUP_CHAT_ID} administrators")
    except Exception as exc:
        log.warning(f"Could not set group-admin commands: {exc}")

    # c) Visible in each admin's private DM with the bot
    for admin_id in CFG.ADMIN_IDS:
        try:
            await tg_app.bot.set_my_commands(
                commands=admin_cmds,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as exc:
            log.warning(f"Could not set DM commands for admin {admin_id}: {exc}")

    if not CFG.GROUP_JOIN_LINK.strip():
        log.warning(
            "GROUP_JOIN_LINK is not set — the group name/join-link footer on "
            "delivery notices will be omitted. Set GROUP_NAME and "
            "GROUP_JOIN_LINK in .env to enable it."
        )

    log.info("══════════════════════════════════════════════")
    log.info("  Media Indexer Bot v6.6  —  starting up")
    log.info(f"  Group:       {CFG.GROUP_CHAT_ID}")
    log.info(f"  IndexTopic:  {CFG.ALL_ADDED_SHOWS}  (SEARCH ALL ADDED SHOWS — closed topic, auto reopen/close)")
    log.info(f"  ChatTopic:   {CFG.SEARCH_SHOWS_HERE}  (welcome deep-link target)")
    log.info(f"  BackupGroup: {CFG.BACKUP_GROUP_CHAT_ID}")
    log.info(f"  BackupIndexTopic: {CFG.BACKUP_ALL_ADDED_SHOWS}")
    log.info(f"  IgnoredTopics: {set(CFG.IGNORED_TOPICS) or '(none)'}")
    log.info(f"  Admins:      {set(CFG.ADMIN_IDS)}")
    log.info(f"  Debounce:    {CFG.DEBOUNCE_SEC}s")
    log.info(f"  SeenTTL:     {CFG.SEEN_TTL_SEC}s")
    log.info(f"  DeleteDelay: {CFG.DELETE_DELAY_SEC}s")
    log.info(f"  MongoDB:     {CFG.MONGODB_DB_NAME} (connected)")
    log.info("  Token:       ***REDACTED***")
    log.info("  Mode:        v6.11 (MongoDB Atlas persistent store · poster support + labelled post text · self-destructing deliveries · multi-season /makepost · auto cross-post · closed-topic support · admin-only commands · backup-group forward auto-index (legacy) · 🚀 Publish backup-deploy for BOTH /makepost and auto-indexed posts)")
    log.info("══════════════════════════════════════════════")

    asyncio.create_task(flush_loop(tg_app), name="flush_loop")


# ──────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────
def main() -> None:
    threading.Thread(
        target=_run_flask,
        daemon=True,
        name="flask-health",
    ).start()
    log.info(f"Health server starting on port {CFG.PORT}")

    application = (
        ApplicationBuilder()
        .token(CFG.BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    # ── Welcome on member join (non-admin users) ───────────────────────
    application.add_handler(
        ChatMemberHandler(
            handle_chat_member,
            chat_member_types=ChatMemberHandler.CHAT_MEMBER,
            block=False,
        )
    )

    # ── Private link generation + /makepost wizard input ───────────────
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("send", cmd_send))
    application.add_handler(CommandHandler("makepost", cmd_makepost))
    application.add_handler(CallbackQueryHandler(_handle_wizard_callback, pattern=r"^mp:"))
    application.add_handler(CallbackQueryHandler(handle_deploy_callback, pattern=r"^deploy:"))
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_upload)
    )

    # ── Admin indexing: all non-command messages from admins ───────────
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_message)
    )

    # ── Backup group forward detection (v6.5, legacy — see docstring    ─
    #    above handle_backup_forward). Registered in its OWN handler     ─
    #    group (group=1) so it runs INDEPENDENTLY of `handle_message`    ─
    #    above (group=0). Within a single PTB handler group only the     ─
    #    first matching handler runs per update; using a separate group  ─
    #    guarantees both handlers still get a chance to inspect every    ─
    #    incoming message without interfering with each other.           ─
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_backup_forward),
        group=1,
    )

    # ── Admin commands (all guarded by @_admin_only) ───────────────────
    application.add_handler(CommandHandler("status",            cmd_status))
    application.add_handler(CommandHandler("flush",             cmd_flush))
    application.add_handler(CommandHandler("clearcache",        cmd_clearcache))
    application.add_handler(CommandHandler("reindex",           cmd_reindex))
    application.add_handler(CommandHandler("testdeletemessage", cmd_testdeletemessage))
    application.add_handler(CommandHandler("republishall",      cmd_republishall))
    application.add_handler(CommandHandler("help",              cmd_help))

    log.info("Bot polling started (drop_pending_updates=True)")
    application.run_polling(        
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()