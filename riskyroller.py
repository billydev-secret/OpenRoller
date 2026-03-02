import logging
import os
import random
import sqlite3
from dataclasses import dataclass, field

import discord
from discord import app_commands
from dotenv import load_dotenv

# ==============================
# Configuration
# ==============================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEBUG_GUILD_ID = int(os.getenv("GUILD_ID")) if os.getenv("GUILD_ID") else None
DATABASE_PATH = os.getenv("STATE_DB_PATH", "riskyroller.sqlite3")

DEBUG = False  # Set True to sync commands only to DEBUG_GUILD_ID

ping_roles: dict[int, int] = {}  # {guild_id: role_id}
active_games: dict[int, "RiskyRollState"] = {}  # {channel_id: RiskyRollState}
log = logging.getLogger("Risky Roller")

logging.basicConfig(level=logging.INFO)

# ==============================
# Intents
# ==============================
intents = discord.Intents.default()


def serialize_user_ids(user_ids: set[int]) -> str | None:
    if not user_ids:
        return None
    return ",".join(str(user_id) for user_id in sorted(user_ids))


def deserialize_user_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    return {int(part) for part in raw.split(",") if part}


class StateStore:
    def __init__(self, path: str):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    ping_role_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS active_rounds (
                    channel_id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    opener_id INTEGER NOT NULL,
                    message_id INTEGER,
                    is_open INTEGER NOT NULL DEFAULT 1,
                    highest_user INTEGER,
                    lowest_user INTEGER,
                    reroll_user_ids TEXT
                );

                CREATE TABLE IF NOT EXISTS round_rolls (
                    channel_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    roll INTEGER NOT NULL,
                    PRIMARY KEY (channel_id, user_id),
                    FOREIGN KEY (channel_id) REFERENCES active_rounds(channel_id) ON DELETE CASCADE
                );
                """
            )

            active_round_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(active_rounds)").fetchall()
            }
            if "reroll_user_ids" not in active_round_columns:
                conn.execute("ALTER TABLE active_rounds ADD COLUMN reroll_user_ids TEXT")

    def load_ping_roles(self) -> dict[int, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT guild_id, ping_role_id FROM guild_settings WHERE ping_role_id IS NOT NULL"
            ).fetchall()
        return {int(row["guild_id"]): int(row["ping_role_id"]) for row in rows}

    def set_ping_role(self, guild_id: int, role_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO guild_settings (guild_id, ping_role_id)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET ping_role_id = excluded.ping_role_id
                """,
                (guild_id, role_id),
            )

    def save_round(self, state: "RiskyRollState") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO active_rounds (
                    channel_id,
                    guild_id,
                    opener_id,
                    message_id,
                    is_open,
                    highest_user,
                    lowest_user,
                    reroll_user_ids
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    guild_id = excluded.guild_id,
                    opener_id = excluded.opener_id,
                    message_id = excluded.message_id,
                    is_open = excluded.is_open,
                    highest_user = excluded.highest_user,
                    lowest_user = excluded.lowest_user,
                    reroll_user_ids = excluded.reroll_user_ids
                """,
                (
                    state.channel_id,
                    state.guild_id,
                    state.opener_id,
                    state.message_id,
                    int(state.is_open),
                    state.highest_user,
                    state.lowest_user,
                    serialize_user_ids(state.reroll_user_ids),
                ),
            )

            conn.execute("DELETE FROM round_rolls WHERE channel_id = ?", (state.channel_id,))
            for user_id, roll in state.rolls.items():
                conn.execute(
                    """
                    INSERT INTO round_rolls (channel_id, user_id, roll)
                    VALUES (?, ?, ?)
                    ON CONFLICT(channel_id, user_id) DO UPDATE SET roll = excluded.roll
                    """,
                    (state.channel_id, user_id, roll),
                )

    def delete_round(self, channel_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM active_rounds WHERE channel_id = ?", (channel_id,))

    def load_active_rounds(self) -> list["RiskyRollState"]:
        with self._connect() as conn:
            round_rows = conn.execute(
                """
                SELECT
                    channel_id,
                    guild_id,
                    opener_id,
                    message_id,
                    is_open,
                    highest_user,
                    lowest_user,
                    reroll_user_ids
                FROM active_rounds
                WHERE is_open = 1
                """
            ).fetchall()

            states = {
                int(row["channel_id"]): RiskyRollState(
                    channel_id=int(row["channel_id"]),
                    guild_id=int(row["guild_id"]),
                    opener_id=int(row["opener_id"]),
                    message_id=int(row["message_id"]) if row["message_id"] is not None else None,
                    is_open=bool(row["is_open"]),
                    highest_user=int(row["highest_user"]) if row["highest_user"] is not None else None,
                    lowest_user=int(row["lowest_user"]) if row["lowest_user"] is not None else None,
                    reroll_user_ids=deserialize_user_ids(row["reroll_user_ids"]),
                )
                for row in round_rows
            }

            roll_rows = conn.execute(
                "SELECT channel_id, user_id, roll FROM round_rolls ORDER BY roll DESC"
            ).fetchall()

        for row in roll_rows:
            channel_id = int(row["channel_id"])
            state = states.get(channel_id)
            if state is None:
                continue
            state.rolls[int(row["user_id"])] = int(row["roll"])

        return list(states.values())


@dataclass
class RiskyRollState:
    channel_id: int
    guild_id: int
    opener_id: int
    message_id: int | None = None
    rolls: dict[int, int] = field(default_factory=dict)
    is_open: bool = True
    highest_user: int | None = None
    lowest_user: int | None = None
    reroll_user_ids: set[int] = field(default_factory=set)

    def add_roll(self, user_id: int, value: int) -> None:
        self.rolls[user_id] = value
        if self.reroll_user_ids:
            completed_rerolls = {
                reroll_user for reroll_user in self.reroll_user_ids if reroll_user in self.rolls
            }
            if completed_rerolls == self.reroll_user_ids:
                self.reroll_user_ids.clear()

    def can_roll(self, user_id: int) -> bool:
        if self.reroll_user_ids:
            return user_id in self.reroll_user_ids and user_id not in self.rolls
        return user_id not in self.rolls

    def prepare_reroll(self, user_ids: list[int]) -> None:
        self.reroll_user_ids = set(user_ids)
        for user_id in self.reroll_user_ids:
            self.rolls.pop(user_id, None)
        self.highest_user = None
        self.lowest_user = None

    def pending_reroll_mentions(self) -> str:
        pending_user_ids = [user_id for user_id in self.reroll_user_ids if user_id not in self.rolls]
        return ", ".join(f"<@{user_id}>" for user_id in pending_user_ids)

    def resolve(self) -> str:
        if self.reroll_user_ids:
            pending_user_ids = [user_id for user_id in self.reroll_user_ids if user_id not in self.rolls]
            if pending_user_ids:
                return "waiting_for_rerolls"

        if len(self.rolls) < 2:
            return "not_enough"

        max_value = max(self.rolls.values())
        min_value = min(self.rolls.values())

        highest_users = [user_id for user_id, roll in self.rolls.items() if roll == max_value]
        if len(highest_users) > 1:
            return "tie"

        sixtyniners = [user_id for user_id, roll in self.rolls.items() if roll == 69]
        if sixtyniners:
            self.highest_user = highest_users[0]
            self.lowest_user = None
            self.is_open = False
            return "sixtynine"

        lowest_users = [user_id for user_id, roll in self.rolls.items() if roll == min_value]

        self.highest_user = highest_users[0]
        self.lowest_user = lowest_users[0]
        self.is_open = False
        return "ok"


def build_embed(state: RiskyRollState) -> discord.Embed:
    embed = discord.Embed(title="Risky Rolls", color=discord.Color.gold())
    if state.is_open:
        if state.reroll_user_ids:
            embed.description = f"Waiting for {state.pending_reroll_mentions()} to reroll."
        else:
            embed.description = "Press **Roll** to join this round."
    else:
        embed.description = "Round closed."

    if not state.rolls:
        embed.add_field(name="Rolls (0)", value="No rolls yet.", inline=False)
        return embed

    sorted_rolls = sorted(state.rolls.items(), key=lambda item: item[1], reverse=True)
    lines = [f"**{roll}** - <@{user_id}>" for user_id, roll in sorted_rolls]
    embed.add_field(name=f"Rolls ({len(state.rolls)})", value="\n".join(lines), inline=False)

    if not state.is_open and state.highest_user:
        high_mention = f"<@{state.highest_user}>"
        if state.lowest_user is None:
            result = f"69 rolled.\n{high_mention} asks one shared question for everyone."
        else:
            result = f"{high_mention} asks\n<@{state.lowest_user}> answers"
        embed.add_field(name="Result", value=result, inline=False)

    return embed


async def disable_round_message(state: RiskyRollState, channel: discord.abc.GuildChannel | discord.Thread) -> None:
    if state.message_id is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return

    try:
        message = await channel.fetch_message(state.message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return

    view = RiskyRollView(state.channel_id)
    view.disable_all_items()

    try:
        await message.edit(embed=build_embed(state), view=view)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return


# ==============================
# Bot Class
# ==============================
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.store = StateStore(DATABASE_PATH)
        log.info("Bot is starting.")

    async def setup_hook(self):
        self.store.initialize()
        ping_roles.update(self.store.load_ping_roles())

        for state in self.store.load_active_rounds():
            active_games[state.channel_id] = state
            if state.message_id is not None:
                self.add_view(RiskyRollView(state.channel_id), message_id=state.message_id)
            else:
                log.warning("Active round in channel %s is missing a message_id.", state.channel_id)

        if DEBUG:
            if DEBUG_GUILD_ID is None:
                raise RuntimeError("DEBUG is enabled but GUILD_ID is missing from the environment.")
            guild = discord.Object(id=DEBUG_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to development guild %s.", DEBUG_GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Synced commands globally.")


bot = Bot()


# ==============================
# Logic
# ==============================
class RiskyRollView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    def disable_all_items(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True

    @discord.ui.button(
        label="Roll",
        style=discord.ButtonStyle.primary,
        custom_id="riskyroller:roll",
    )
    async def roll_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = active_games.get(self.channel_id)
        if not state or not state.is_open:
            await interaction.response.send_message("No open round to roll in.", ephemeral=True)
            return

        if not state.can_roll(interaction.user.id):
            if state.reroll_user_ids:
                await interaction.response.send_message("You cannot reroll right now.", ephemeral=True)
                return

            await interaction.response.send_message("You already rolled this round.", ephemeral=True)
            return

        roll = random.randint(1, 100)
        state.add_roll(interaction.user.id, roll)
        bot.store.save_round(state)

        await interaction.response.edit_message(embed=build_embed(state), view=self)

    @discord.ui.button(
        label="Close Round",
        style=discord.ButtonStyle.danger,
        custom_id="riskyroller:close",
    )
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = active_games.get(self.channel_id)
        if not state:
            await interaction.response.send_message("No active game.", ephemeral=True)
            return

        if interaction.user.id != state.opener_id:
            await interaction.response.send_message(
                "Only the round opener can close this round.",
                ephemeral=True,
            )
            return

        result = state.resolve()
        if result == "waiting_for_rerolls":
            await interaction.response.send_message(
                f"Still waiting for {state.pending_reroll_mentions()} to reroll.",
                allowed_mentions=discord.AllowedMentions(users=True),
                ephemeral=True,
            )
            return

        if result == "not_enough":
            await interaction.response.send_message("At least 2 players must roll.", ephemeral=True)
            return

        if result == "tie":
            max_value = max(state.rolls.values())
            tied_user_ids = [user_id for user_id, roll in state.rolls.items() if roll == max_value]
            tied_users = [f"<@{user_id}>" for user_id in tied_user_ids]
            state.prepare_reroll(tied_user_ids)
            bot.store.save_round(state)
            await interaction.response.send_message(
                f"Tie for highest roll ({max_value}).\n{', '.join(tied_users)} must reroll.",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            await interaction.message.edit(embed=build_embed(state), view=self)
            return

        self.disable_all_items()
        await interaction.response.edit_message(embed=build_embed(state), view=self)

        bot.store.delete_round(self.channel_id)
        del active_games[self.channel_id]

        if result == "sixtynine":
            await interaction.followup.send(
                content=f"69 rolled.\n<@{state.highest_user}> asks one shared question for everyone.",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            return

        await interaction.followup.send(
            content=f"<@{state.highest_user}> asks\n<@{state.lowest_user}> answers",
            allowed_mentions=discord.AllowedMentions(users=True),
        )


# ==============================
# Events
# ==============================
@bot.event
async def on_ready():
    log.info("Bot ready in %s guild(s).", len(bot.guilds))


# ==============================
# Commands
# ==============================
@bot.tree.command(name="risky_start", description="Start a Risky Rolls round")
@app_commands.guild_only()
async def risky_start(interaction: discord.Interaction):
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message(
            "This command can only be used in a server channel.",
            ephemeral=True,
        )
        return

    if interaction.channel.id in active_games:
        await interaction.response.send_message(
            "A game is already active in this channel.",
            ephemeral=True,
        )
        return

    state = RiskyRollState(
        channel_id=interaction.channel.id,
        guild_id=interaction.guild.id,
        opener_id=interaction.user.id,
    )
    active_games[interaction.channel.id] = state
    bot.store.save_round(state)

    role_id = ping_roles.get(interaction.guild.id)
    content = None
    allowed_mentions = discord.AllowedMentions.none()

    if role_id:
        content = f"# <@&{role_id}> A new Risky Rolls round has begun!"
        allowed_mentions = discord.AllowedMentions(roles=True)

    view = RiskyRollView(interaction.channel.id)
    try:
        await interaction.response.send_message(
            content=content,
            embed=build_embed(state),
            view=view,
            allowed_mentions=allowed_mentions,
        )
    except Exception:
        active_games.pop(interaction.channel.id, None)
        bot.store.delete_round(interaction.channel.id)
        raise

    message = await interaction.original_response()
    state.message_id = message.id
    bot.store.save_round(state)


@bot.tree.command(
    name="risky_set_ping",
    description="Set the role to ping when a new Risky Roll starts",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def risky_set_ping(interaction: discord.Interaction, role: discord.Role):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return

    ping_roles[interaction.guild.id] = role.id
    bot.store.set_ping_role(interaction.guild.id, role.id)

    await interaction.response.send_message(
        f"Ping role set to {role.mention}",
        allowed_mentions=discord.AllowedMentions(roles=True),
        ephemeral=True,
    )


@bot.tree.command(
    name="risky_reset_state",
    description="Force-clear the active Risky Rolls round in this channel",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def risky_reset_state(interaction: discord.Interaction):
    if interaction.channel is None:
        await interaction.response.send_message(
            "This command can only be used in a server channel.",
            ephemeral=True,
        )
        return

    state = active_games.pop(interaction.channel.id, None)
    if state is None:
        bot.store.delete_round(interaction.channel.id)
        await interaction.response.send_message(
            "No active round was found in this channel.",
            ephemeral=True,
        )
        return

    state.is_open = False
    await disable_round_message(state, interaction.channel)
    bot.store.delete_round(interaction.channel.id)

    await interaction.response.send_message(
        "Reset the active Risky Rolls state for this channel.",
        ephemeral=True,
    )


# ==============================
# Run it
# ==============================
if __name__ == "__main__":
    bot.run(TOKEN)
