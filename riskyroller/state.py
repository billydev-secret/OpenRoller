import asyncio
import collections
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

QUESTION_MESSAGE_CACHE_LIMIT = 5000
question_messages: collections.OrderedDict[int, int] = collections.OrderedDict()


def remember_question_message(message_id: int, asker_id: int) -> None:
    question_messages[message_id] = asker_id
    while len(question_messages) > QUESTION_MESSAGE_CACHE_LIMIT:
        question_messages.popitem(last=False)

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
