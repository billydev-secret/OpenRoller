import asyncio
import weakref

from .config import DATABASE_PATH
from .models import PendingQuestionState, RiskyRollState
from .store import StateStore

store: StateStore = StateStore(DATABASE_PATH)

active_games: dict[str, RiskyRollState] = {}
pending_questions: dict[str, PendingQuestionState] = {}
ping_roles: dict[int, int] = {}
min_game_seconds: dict[int, int] = {}
auto_close_tasks: dict[str, asyncio.Task] = {}
question_messages: dict[int, int] = {}

_channel_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
_game_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


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
