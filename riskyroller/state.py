import asyncio
import weakref

from .config import DATABASE_PATH
from .models import PendingQuestionState, PostedQuestionState, RiskyRollState
from .store import StateStore

store: StateStore = StateStore(DATABASE_PATH)

active_games: dict[str, RiskyRollState] = {}
pending_questions: dict[str, PendingQuestionState] = {}
posted_questions: dict[int, PostedQuestionState] = {}
ping_roles: dict[int, int] = {}
min_game_seconds: dict[int, int] = {}
max_games_per_channel: dict[int, int] = {}
auto_close_tasks: dict[str, asyncio.Task] = {}

# (guild_id, user_id) -> display name, captured when a player rolls so the
# roster embed can print names instead of raw <@id> mentions (embeds don't
# resolve mentions for members the viewer's client hasn't cached — mainly
# people who've left). Scoped per guild — a nickname only holds in the
# server it was captured in, so a bare user_id key would leak one server's
# name into another's roster. Cleared per guild in bot.py's on_guild_remove.
guild_display_names: dict[tuple[int, int], str] = {}

_channel_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
_game_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_message_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()


def get_channel_lock(channel_id: int) -> asyncio.Lock:
    lock = _channel_locks.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        _channel_locks[channel_id] = lock
    return lock


def get_game_lock(game_id: str) -> asyncio.Lock:
    lock = _game_locks.get(game_id)
    if lock is None:
        lock = asyncio.Lock()
        _game_locks[game_id] = lock
    return lock


def get_message_lock(message_id: int) -> asyncio.Lock:
    lock = _message_locks.get(message_id)
    if lock is None:
        lock = asyncio.Lock()
        _message_locks[message_id] = lock
    return lock
