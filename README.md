# RiskyRollaBot

`RiskyRollaBot` is a Discord slash-command bot for running "Risky Rolls" rounds in a channel.

## Features
- Interactive round flow with `Roll` and `Close` buttons.
- One roll per user, per round.
- Ties for highest (or lowest) are settled by an automatic rolloff.
- Winner/loser resolution when a round closes.
- Winner follow-up prompt flow (including special `69` winner behavior).
- SQLite-backed persistence for active rounds and pending prompts across restarts.

## Game Rules and Round Flow
### Round Start
- Any server member can run `/risky_start` in a channel.
- The bot posts a round message with `Roll` and `Close` buttons.
- If a ping role is configured with `/risky_set_ping`, that role is mentioned on round start.

### Rolling
- Each player can submit exactly one roll in a round.
- Rolls are random integers from `1` to `100` (inclusive).
- The round embed tracks all submitted rolls.

### Closing and Winner Resolution
- Pressing `Close` ends the round and resolves results.
- If the highest roll is unique, that player is the winner.
- The lowest roll is tracked as the loser.
- If multiple players tie for highest, the bot runs an automatic rolloff among them until a single winner exists (the same applies to a tie for lowest).

### Winner Question Prompt
- After a winner is resolved, the bot opens a question prompt for the winner.
- Standard outcome: winner can ask a question to the lowest-rolling player.
- Special `69` outcome: if the winner rolled `69`, winner can ask a question to everyone who rolled in the round.
- Only the winner can submit the question.

### Reset Behavior
- Admins can run `/risky_reset_state` to clear active round state and pending winner prompts for the current channel.

## Command Reference

| Command | Description | Permissions |
|---|---|---|
| `/risky_start` | Open a new Risky Rolls round (pings the configured role). | Server members |
| `/risky_start_no_ping` | Open a new round without pinging and without a minimum game time. | Server members |
| `/risky_set_ping <role>` | Set the role pinged when a new round starts. | Administrator |
| `/risky_set_min_game_time <seconds>` | Set how long a round must stay open before it can close, by the opener or by auto-close (0 disables). | Administrator |
| `/risky_set_max_games <count>` | Set how many rounds can be open in one channel at a time (0 restores the default of 10). | Administrator |
| `/risky_reset_state` | Clear active rounds and pending prompts in the current channel. | Administrator |
| `/invite` | Get an invite link to add the bot to your server. | Anyone |
| `/support` | Get a link to the support Discord server. | Anyone |

## Requirements
- Python 3.10 or newer.
- A Discord application with a bot user: [Developer Portal](https://discord.com/developers/applications) → New Application → Bot → Reset Token. No privileged intents are needed.

## Installation
```bash
git clone https://github.com/billydev-secret/OpenRoller.git
cd OpenRoller
python3 -m venv .venv              # Windows: py -m venv .venv
source .venv/bin/activate          # Linux / macOS
.venv\Scripts\activate             # Windows (cmd / PowerShell)
pip install -r requirements.txt
```

`python` and `pip` below mean the ones inside the activated virtualenv.

## Configuration
Copy the example and fill in your token:

```bash
cp .env.example .env               # Windows: copy .env.example .env
```

`.env.example` documents every setting; only `DISCORD_TOKEN` is required. The bot reads `.env` from the directory it is started in, and the same file works as a Docker Compose `env_file` and a systemd `EnvironmentFile`.

### Environment Variables
- `DISCORD_TOKEN` (required): bot token from the Discord Developer Portal.
- `STATE_DB_PATH` (optional): SQLite file path. Defaults to `riskyroller.sqlite3`, relative to the directory the bot is started in.
- `SYNC_COMMANDS_ON_STARTUP` (optional): `true`/`false`, defaults to `true`. Registers the slash commands globally on every start; global registration can take up to an hour to appear in Discord.
- `DEBUG` (optional): `true`/`false`, defaults to `false`. Copies the commands into the single server named by `GUILD_ID`, where they appear instantly — for development. Startup fails if `GUILD_ID` is missing.
- `GUILD_ID` (optional): the numeric server ID used by `DEBUG` mode. Leave it unset otherwise.
- `DEFAULT_MIN_GAME_SECONDS` (optional): how long a round must stay open before it can close — by the opener's Close button or by the player-threshold auto-close — for servers that haven't set their own with `/risky_set_min_game_time`. Defaults to `1800`. `/risky_start_no_ping` skips the minimum for that round.
- `DEFAULT_MAX_GAMES_PER_CHANNEL` (optional): open rounds allowed per channel, for servers that haven't set their own with `/risky_set_max_games`. Defaults to `10`.

Booleans accept `1`/`true`/`yes`/`on` (case-insensitive); anything else is false.

## Inviting the bot
The `/invite` command only works once the bot is already in a server, so for the first one build the link yourself. In the Developer Portal open your application → General Information and copy the **Application ID**, then open:

```
https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=bot+applications.commands&permissions=309237664768
```

That grants exactly what the bot needs: View Channel, Send Messages, Embed Links, Create Public Threads, Send Messages in Threads. Channels hidden from `@everyone` still need the bot's role added to them. Globally registered slash commands can take up to an hour to appear; set `DEBUG=true` and `GUILD_ID` while testing to have them appear in one server immediately.

## Running
```bash
python main.py
```

## Data Storage
The bot stores state in SQLite:
- `guild_settings`
- `active_rounds`
- `round_rolls`
- `pending_questions`
- `posted_questions`

Schema initialization and lightweight migrations run on startup. Posted questions and unanswered question prompts older than 7 days are swept on startup. When the bot is removed from a guild, all of that guild's data is deleted.

The database runs in WAL mode, so `-wal` and `-shm` files sit next to the `.sqlite3` file while the bot is running; include them if you copy the database while the bot is up.

## Operational Notes
- Slash commands are guild-only.
- In the channel a round runs in, the bot needs **View Channel**, **Send Messages** and **Embed Links**; `/risky_start` refuses and names whatever is missing. View Channel matters most: without it the buttons still work but the bot cannot post the prompt when a round auto-closes. Members-only channels that hide from `@everyone` need the bot's role added explicitly.
- No privileged Discord intents are currently enabled.
- State cleanup can be forced per-channel with `/risky_reset_state`.

## Development
- Entrypoint: `main.py`.
- Tests are plain `unittest`, no extra packages needed:
  ```bash
  python -m unittest discover -s tests -v
  ```
  `pytest tests/` also works if you have pytest installed.
- Logging: standard Python logging at `INFO`, with discord.py's own logger at `WARNING`.
