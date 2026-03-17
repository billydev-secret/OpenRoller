import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("discord").setLevel(logging.WARNING)

from riskyroller.bot import bot
from riskyroller.config import TOKEN

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set in the environment.")
    bot.run(TOKEN)
can