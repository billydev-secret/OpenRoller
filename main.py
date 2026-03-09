import logging

logging.basicConfig(level=logging.INFO)

from riskyroller.bot import bot
from riskyroller.config import TOKEN

if __name__ == "__main__":
    bot.run(TOKEN)
