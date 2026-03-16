import asyncio
import weakref

from .config import DATABASE_PATH
from .models import PendingQuestionState, RiskyRollState
from .store import StateStore

store: StateStore = StateStore(DATABASE_PATH)

active_games: dict[str, RiskyRollState] = {}        # game_id -> state
pending_questions: dict[str, PendingQuestionState] = {}  # game_id -> state
ping_roles: dict[int, int] = {}
auto_close_tasks: dict[str, asyncio.Task] = {}       # game_id -> task

_channel_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
_game_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def get_channel_lock(channel_id: int) -> asyncio.Lock:
    """Per-channel lock — used for operations that span all games in a channel (start, reset)."""
    lock = _channel_locks.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        _channel_locks[channel_id] = lock
    return lock


def get_game_lock(game_id: str) -> asyncio.Lock:
    """Per-game lock — used for roll/close operations on a specific game."""
    lock = _game_locks.get(game_id)
    if lock is None:
        lock = asyncio.Lock()
        _game_locks[game_id] = lock
    return lock
