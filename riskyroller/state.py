import asyncio
import weakref

from .config import DATABASE_PATH
from .models import PendingQuestionState, RiskyRollState
from .store import StateStore

store: StateStore = StateStore(DATABASE_PATH)

active_games: dict[int, RiskyRollState] = {}
pending_questions: dict[int, PendingQuestionState] = {}
ping_roles: dict[int, int] = {}
auto_close_tasks: dict[int, asyncio.Task] = {}

_channel_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()


def get_channel_lock(channel_id: int) -> asyncio.Lock:
    lock = _channel_locks.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        _channel_locks[channel_id] = lock
    return lock
