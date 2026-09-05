import os

from dotenv import find_dotenv, load_dotenv

# Search for .env from the working directory, not from this file. Once the
# package is installed into site-packages a search from here would never reach
# the .env in the directory the bot is started from, which is where the README
# says it lives.
load_dotenv(find_dotenv(usecwd=True))

# Problems found while reading the environment. Recorded rather than raised so
# importing this module never crashes; the entrypoint prints them and exits
# before touching Discord.
CONFIG_ERRORS: list[str] = []


def parse_int_env(name: str, raw: str | None, default: int | None) -> int | None:
    """Parse a whole-number setting, recording a readable error on bad input.

    Empty or unset falls back to *default*; so does a value that is not a
    whole number, with a line naming the variable added to CONFIG_ERRORS.
    """
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        CONFIG_ERRORS.append(f"{name} must be a whole number, got {raw.strip()!r}.")
        return default


def _int_env(name: str, default: int) -> int:
    value = parse_int_env(name, os.getenv(name), default)
    return default if value is None else value


def get_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


TOKEN: str | None = os.getenv("DISCORD_TOKEN")
DEBUG_GUILD_ID: int | None = parse_int_env("GUILD_ID", os.getenv("GUILD_ID"), None)
DATABASE_PATH: str = os.getenv("STATE_DB_PATH", "riskyroller.sqlite3")
DEBUG: bool = get_bool_env("DEBUG", default=False)
SYNC_COMMANDS_ON_STARTUP: bool = get_bool_env("SYNC_COMMANDS_ON_STARTUP", default=True)
DEFAULT_MIN_GAME_SECONDS: int = _int_env("DEFAULT_MIN_GAME_SECONDS", 1800)
DEFAULT_MAX_GAMES_PER_CHANNEL: int = _int_env("DEFAULT_MAX_GAMES_PER_CHANNEL", 10)
