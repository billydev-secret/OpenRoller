import os
import datetime
import discord
from discord import app_commands
from dotenv import load_dotenv
import random
import logging

# ==============================
# Configuration
# ==============================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))

ping_roles = {}  # {guild_id: role_id}

DEBUG = False  # Set False to go global
DEBUG_FAKE_PLAYERS = 25

# Roles that bypass spoiler enforcement
active_games = {}  # {channel_id: RiskyRollState}
log = logging.getLogger("Risky Roller")  # your bot namespace

logging.basicConfig(
    level=logging.INFO,
)

# ==============================
# Intents
# ==============================

intents = discord.Intents.default()
# intents.members = True
# intents.message_content = True  # Required for attachment enforcement

# ==============================
# Bot Class
# ==============================
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        log.info(f"Bots Awake!")

    async def setup_hook(self):
        if DEBUG:
            guild = discord.Object(id=GUILD_ID)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to development guild.")
        else:
            await self.tree.sync()
            log.info("Synced commands globally.")

bot = Bot()

class RiskyRollState:
    def __init__(self, opener_id):
        self.opener_id = opener_id
        self.rolls = {}  # {user_id: roll}
        self.is_open = True
        self.highest_user = None
        self.lowest_user = None

    def add_roll(self, user_id, value):
        self.rolls[user_id] = value

    def resolve(self):
        if len(self.rolls) < 2:
            return False

        highest = max(self.rolls, key=self.rolls.get)
        lowest = min(self.rolls, key=self.rolls.get)

        self.highest_user = highest
        self.lowest_user = lowest
        self.is_open = False
        return True


class RiskyRollView(discord.ui.View):
    def __init__(self, channel_id):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        log.info(f"View created {discord.Interaction}")

    @discord.ui.button(label="Roll", style=discord.ButtonStyle.primary, emoji="🎲")
    async def roll_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = active_games.get(self.channel_id)

        log.info(f"Roll button {discord.Interaction}")

        if not state or not state.is_open:
            await interaction.response.send_message(
                "This round is closed.", ephemeral=True
            )
            return

        if interaction.user.id in state.rolls and not DEBUG:
            await interaction.response.send_message(
                "You've already rolled this round.", ephemeral=True
            )
            return

        roll = random.randint(1, 100)
        state.add_roll(interaction.user.id, roll)

        # Auto-generate fake players in debug mode
        if DEBUG:
            for i in range(DEBUG_FAKE_PLAYERS):
                fake_id = 900000000000000000 + i  # High dummy IDs
                fake_roll = random.randint(1, 100)
                state.add_roll(fake_id + random.randint(0, 10000), fake_roll)


        embed = build_embed(state, interaction.guild)

        await interaction.response.edit_message(
            embed=embed,
            view=self,
            allowed_mentions=discord.AllowedMentions.none()
        )

    @discord.ui.button(label="Close Round", style=discord.ButtonStyle.danger, emoji="🔒")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = active_games.get(self.channel_id)

        if not state:
            await interaction.response.send_message("No active game.", ephemeral=True)
            return

        if interaction.user.id != state.opener_id:
            await interaction.response.send_message(
                "Only the round opener can close this round.",
                ephemeral=True
            )
            return

        if not state.is_open:
            await interaction.response.send_message(
                "Round already closed.",
                ephemeral=True
            )
            return

        if not state.resolve():
            await interaction.response.send_message(
                "At least 2 players must roll.",
                ephemeral=True
            )
            return

        embed = build_embed(state, interaction.guild)

        # Disable roll button
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.label == "Roll":
                child.disabled = True

        # Clear active state
        del active_games[self.channel_id]

        # First response (required)
        await interaction.response.edit_message(embed=embed, view=self)

        # Followup for ping
        await interaction.followup.send(
            content=f"🎤 <@{state.highest_user}> asks\n💬 <@{state.lowest_user}> answers",
            allowed_mentions=discord.AllowedMentions(users=True)
        )





# ==============================
# Logic
# ==============================
def build_embed(state: RiskyRollState, guild):
    embed = discord.Embed(
        title="🎲 Risky Rolls",
        color=discord.Color.gold()
    )

    if state.is_open:
        embed.description = "Press **Roll** to join this round."
    else:
        embed.description = "🔒 Round Closed"

    # Rolls list
    if not state.rolls:
        embed.add_field(name="Rolls", value="No rolls yet.", inline=False)
    else:
        sorted_rolls = sorted(state.rolls.items(), key=lambda x: x[1], reverse=True)

        lines = []
        for user_id, roll in sorted_rolls:
            mention = f"<@{user_id}>"
            lines.append(f"**{roll}** — {mention}")

        embed.add_field(name="Rolls", value="\n".join(lines), inline=False)

    # Resolution
    if not state.is_open and state.highest_user:

            high_mention = f"<@{state.highest_user}>"
            low_mention = f"<@{state.lowest_user}>"

            embed.add_field(
                name="Result",
                value=f"🎤 {high_mention} asks\n💬 {low_mention} answers",
                inline=False
            )
    
    return embed


# ==============================
# Events
# ==============================
@bot.event
async def on_ready():
    log.info(f"Bot Ready")

# ==============================
# Command
# ==============================
@bot.tree.command(
    name="risky_start",
    guild=discord.Object(id=GUILD_ID)
)
async def risky_start(interaction: discord.Interaction):
    if interaction.channel.id in active_games:
        await interaction.response.send_message(
            "A game is already active in this channel.", ephemeral=True
        )
        return

    state = RiskyRollState(opener_id=interaction.user.id)
    active_games[interaction.channel.id] = state

    embed = build_embed(state, interaction.guild)
    view = RiskyRollView(interaction.channel.id)

    role_id = ping_roles.get(interaction.guild.id)

    content = None
    allowed = discord.AllowedMentions.none()

    if role_id:
        content = f"# <@&{role_id}> A new Risky Rolls round has begun!"
        allowed = discord.AllowedMentions(roles=True)

    await interaction.response.send_message(
        content=content,
        embed=embed,
        view=view,
        allowed_mentions=allowed
    )

@bot.tree.command(
    name="risky_set_ping",
    description="Set the role to ping when a new Risky Roll starts",
    guild=discord.Object(id=GUILD_ID)
)
@app_commands.checks.has_permissions(administrator=True)
async def risky_set_ping(interaction: discord.Interaction, role: discord.Role):
    ping_roles[interaction.guild.id] = role.id

    await interaction.response.send_message(
        f"✅ Ping role set to {role.mention}",
        allowed_mentions=discord.AllowedMentions(roles=True),
        ephemeral=True
    )

# ==============================
# run it
# ==============================
if __name__ == "__main__":
    bot.run(TOKEN)