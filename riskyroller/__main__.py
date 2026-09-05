"""Console entrypoint.

Runs as ``python -m riskyroller``, as the ``riskyroller`` command installed by
``pip install .``, and via the ``main.py`` shim.
"""

import logging
import sys

import discord

DEVELOPER_PORTAL = "https://discord.com/developers/applications"

MISSING_TOKEN_MESSAGE = f"""\
DISCORD_TOKEN is not set, so the bot cannot log in.

  1. Open {DEVELOPER_PORTAL} and pick (or create) your application.
  2. On its "Bot" page click "Reset Token" and copy the token it shows.
  3. Put the token where the bot can read it -- either:
       - a file named .env in the directory you start the bot from
         (copy .env.example to .env and fill in the DISCORD_TOKEN= line), or
       - an environment variable named DISCORD_TOKEN (Docker env_file,
         systemd EnvironmentFile, `export DISCORD_TOKEN=...`).

Then start the bot again.
"""

REJECTED_TOKEN_MESSAGE = f"""\
Discord rejected the token in DISCORD_TOKEN ("Improper token has been passed").

  - Paste the token exactly as the Developer Portal shows it: no quotes, no
    "Bot " prefix, no spaces or line breaks around it.
  - Make sure it is the *bot token* from the "Bot" page of
    {DEVELOPER_PORTAL}, not the application's client secret or public key.
  - If you are not sure the token is still valid, click "Reset Token" there
    and use the new one. Resetting invalidates the old token everywhere.
"""

DEBUG_WITHOUT_GUILD_MESSAGE = """\
DEBUG=true tells the bot to register its slash commands in a single server for
instant testing, but GUILD_ID is not set, so it does not know which server.

  - Set GUILD_ID to that server's ID (Discord -> User Settings -> Advanced ->
    Developer Mode, then right-click the server icon -> Copy Server ID), or
  - remove DEBUG (or set it to false) to register commands globally instead.
"""


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)


def main() -> int:
    """Start the bot. Returns a process exit code (0 ok, 2 configuration problem)."""
    configure_logging()

    # Imported here, not at module top: importing config reads .env, and the
    # checks below want to run before any Discord machinery is touched.
    from .config import CONFIG_ERRORS, DEBUG, DEBUG_GUILD_ID, TOKEN

    if CONFIG_ERRORS:
        sys.stderr.write(
            "The configuration has a problem:\n"
            + "".join(f"  - {error}\n" for error in CONFIG_ERRORS)
            + "\nFix it in .env (or the environment) and start the bot again.\n"
        )
        return 2
    if not TOKEN:
        sys.stderr.write(MISSING_TOKEN_MESSAGE)
        return 2
    if DEBUG and DEBUG_GUILD_ID is None:
        sys.stderr.write(DEBUG_WITHOUT_GUILD_MESSAGE)
        return 2

    from .bot import bot

    try:
        # log_handler=None: the root handler above already receives discord.py's
        # records; letting the library add its own would print each one twice.
        bot.run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        sys.stderr.write(REJECTED_TOKEN_MESSAGE)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
