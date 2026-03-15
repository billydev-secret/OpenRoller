import os

from dotenv import load_dotenv

load_dotenv()

TOKEN: str | None = os.getenv("DISCORD_TOKEN")
DEBUG_GUILD_ID: int | None = int(os.getenv("GUILD_ID")) if os.getenv("GUILD_ID") else None
DATABASE_PATH: str = os.getenv("STATE_DB_PATH", "riskyroller.sqlite3")


def get_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


DEBUG: bool = get_bool_env("DEBUG", default=False)
SYNC_COMMANDS_ON_STARTUP: bool = get_bool_env("SYNC_COMMANDS_ON_STARTUP", default=True)
