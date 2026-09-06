# RiskyRollaBot

`RiskyRollaBot` is a Discord slash-command bot for running "Risky Rolls" rounds in a channel.

## Features
- Interactive round flow with `Roll`, `How to Play` and `Close Round` buttons.
- One roll per user, per round.
- Ties for highest (or lowest) are settled by an automatic rolloff.
- Winner/loser resolution when a round closes, including the `69`, `100` and `1` special rules below.
- Winner follow-up question flow, answered with its own `Reply` button.
- SQLite-backed persistence for active rounds and pending prompts across restarts.

## Game Rules and Round Flow
### Round Start
- Any server member can run `/risky_start` in a channel to open a round.
- Two optional settings control how the round ends on its own: `auto_close_players` (default `25`) closes it once that many players have rolled, and `auto_close_minutes` (default `120`) closes it after that many minutes, whichever comes first. Either can be set to `0` to turn that trigger off.
- `/risky_start_no_ping` opens a round the same way but skips the ping role and the minimum game time described under Closing below.
- If a ping role is configured with `/risky_set_ping`, that role is mentioned when `/risky_start` opens a round.
- The round message carries three buttons: **Roll**, **How to Play** (an ephemeral summary of everything on this page), and **Close Round**.

### Rolling
- Each player can submit exactly one roll in a round, by pressing **Roll**.
- Rolls are random integers from `1` to `100` (inclusive).
- The round embed lists every submitted roll, highest to lowest.

### Closing and Winner Resolution
- **Close Round** can be pressed by whoever opened the round, or by a server admin; anyone else is told who can close it instead.
- Both **Close Round** and the player-count auto-close (once its threshold is reached) are held back by a minimum game time — `1800` seconds (30 minutes) by default, from when the round opened. It's configurable per server with `/risky_set_min_game_time`, applies equally to the opener and to admins, and is skipped entirely for rounds started with `/risky_start_no_ping`. The minutes-based auto-close is *not* held back by this minimum — it always fires on its own clock.
- A round needs at least two rolls to produce a result; one closed early (by auto-close) with fewer just ends with no winner.
- If the highest roll is unique, that player wins the round; the lowest roll is the loser.
- Ties for highest (or for lowest) are settled by an automatic rolloff: the tied players re-roll among themselves until one is left, repeated separately for each side that ties.

### Winner Question Prompt
Once a round closes, the bot posts a prompt with an **Ask Question** button; who it's for depends on what was rolled:
- **Standard outcome**: the winner asks the loser one question.
- 🔥 **Someone rolled `69`**: that player asks the whole room instead of the loser — their question opens its own thread rather than posting in the loser's place.
- ⭐ **The winner rolled `100`**: their question goes to the loser *and* a second player — whoever rolled lowest among everyone except the winner and loser (a rolloff settles a tie for that spot too). Only kicks in when a distinct third player exists to ask.
- ☠️ **The loser rolled `1`**: the loser gets asked by *two* questioners instead of one — the winner, and a second questioner: whoever rolled highest among everyone except the winner and loser (again settled by rolloff if tied). The winner and the second questioner each get their own question. Only kicks in when a distinct third player exists to ask.
- The `100` and `1` rules can both apply in the same round, if the winner rolled `100` and the loser rolled `1`.
- Only whoever the prompt names may press **Ask Question**; anyone else pressing it is turned away.

### Answering
- Once a question is posted, whoever it's addressed to sees a **Reply** button on it and must use that to answer — there's no other way to reply through the bot.

### Reset Behavior
- Admins can run `/risky_reset_state` to clear a channel's active round, any pending winner prompt, and any already-posted question still waiting on a reply.

## Command Reference

| Command | Description | Permissions |
|---|---|---|
| `/risky_start [auto_close_players] [auto_close_minutes]` | Open a new Risky Rolls round (pings the configured role). Auto-close defaults: 25 players / 120 minutes; either can be set to 0 to disable it. | Server members |
| `/risky_start_no_ping [auto_close_players] [auto_close_minutes]` | Open a new round without pinging and without a minimum game time. Same auto-close options and defaults as `/risky_start`. | Server members |
| `/risky_set_ping <role>` | Set the role pinged when a new round starts. | Administrator |
| `/risky_set_min_game_time <seconds>` | Set how long a round must stay open before it can close, by the opener or by auto-close (0 disables). | Administrator |
| `/risky_set_max_games <count>` | Set how many rounds can be open in one channel at a time (0 restores the default of 10). | Administrator |
| `/risky_reset_state` | Clear active rounds and pending prompts in the current channel. | Administrator |
| `/invite` | Get an invite link to add the bot to your server. | Anyone |
| `/support` | Get the support server link, if the host has set `SUPPORT_INVITE_URL`. | Anyone |

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
pip install .
```

`python` and `pip` below mean the ones inside the activated virtualenv. `pip install .` installs the bot and its two dependencies and adds a `riskyroller` command to the virtualenv; `pip install -r requirements.txt` still works if you only want the dependencies.

`pip install .` copies the code into the virtualenv at that moment — a later `git pull` doesn't reach it, so the installed `riskyroller` command keeps running the old copy until you run `pip install .` again. Running the bot with `python -m riskyroller` or `python main.py` from the checkout always picks up a `git pull` immediately; only the bare `riskyroller` command can go stale. If you plan on pulling updates into this checkout, `pip install -e .` (editable mode, see Development below) avoids the problem entirely.

## Configuration
Copy the example and fill in your token:

```bash
cp .env.example .env               # Windows: copy .env.example .env
```

`.env.example` documents every setting; only `DISCORD_TOKEN` is required. The bot looks for `.env` in the directory it is started in first, falling back to the directory the installed code lives in if that directory has none — see [Upgrading from Earlier Versions](#upgrading-from-earlier-versions) if that fallback matters to you. The same `.env` file works as a Docker Compose `env_file` and a systemd `EnvironmentFile`.

### Environment Variables
- `DISCORD_TOKEN` (required): bot token from the Discord Developer Portal.
- `STATE_DB_PATH` (optional): SQLite file path. Defaults to `riskyroller.sqlite3`, relative to the directory the bot is started in.
- `SYNC_COMMANDS_ON_STARTUP` (optional): `true`/`false`, defaults to `true`. Registers the slash commands globally on every start; global registration can take up to an hour to appear in Discord.
- `DEBUG` (optional): `true`/`false`, defaults to `false`. Copies the commands into the single server named by `GUILD_ID`, where they appear instantly — for development. Startup fails if `GUILD_ID` is missing.
- `GUILD_ID` (optional): the numeric server ID used by `DEBUG` mode. Leave it unset otherwise.
- `DEFAULT_MIN_GAME_SECONDS` (optional): how long a round must stay open before it can close — by the opener's Close button or by the player-threshold auto-close — for servers that haven't set their own with `/risky_set_min_game_time`. Defaults to `1800`. `/risky_start_no_ping` skips the minimum for that round.
- `DEFAULT_MAX_GAMES_PER_CHANNEL` (optional): open rounds allowed per channel, for servers that haven't set their own with `/risky_set_max_games`. Defaults to `10`.
- `SUPPORT_INVITE_URL` (optional): the invite `/support` hands out. Unset means this copy has no support server, and `/support` says so.

Booleans accept `1`/`true`/`yes`/`on` (case-insensitive); anything else is false.

## Inviting the bot
The `/invite` command only works once the bot is already in a server, so for the first one build the link yourself. In the Developer Portal open your application → General Information and copy the **Application ID**, then open:

```
https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=bot+applications.commands&permissions=309237664768
```

That grants exactly what the bot needs: View Channel, Send Messages, Embed Links, Create Public Threads, Send Messages in Threads. Channels hidden from `@everyone` still need the bot's role added to them.

If you publish an install link — the Developer Portal's **Installation → Default Install Settings**, or a bot-list page — make sure its guild install includes the `bot` scope with those same permissions. A link with `applications.commands` alone installs the slash commands without the bot account, and `/risky_start` will say so instead of running. Globally registered slash commands can take up to an hour to appear; set `DEBUG=true` and `GUILD_ID` while testing to have them appear in one server immediately.

## Running
```bash
riskyroller                        # with the virtualenv activated
python -m riskyroller              # same thing
python main.py                     # still works (it forwards to the same entrypoint)
```

If `DISCORD_TOKEN` is missing, Discord rejects it, `DEBUG=true` has no `GUILD_ID`, or a numeric setting isn't a number, the bot exits with code 2 and a message saying exactly what to fix instead of a traceback.

On the first start the log prints the invite link (as a warning if the bot isn't in any server yet), so the section above is only needed if you want to build it by hand. The same link is available later from `/invite`.

## Upgrading from Earlier Versions
- **A minimum game time of 0 now means 0, not "disabled".** Older versions stored no value at all for `/risky_set_min_game_time 0` and treated that as "no lock on Close Round"; this version reads that same stored absence as the 1800-second (30-minute) default instead, and stores `0` itself when you run the command with `0`. If your server had deliberately turned the minimum off, **Close Round** will be locked for 30 minutes again after upgrading — there's nothing in the database to tell "never configured" and "explicitly disabled" apart, so run `/risky_set_min_game_time 0` once after upgrading if that's you.
- **`.env` is found differently than in some older versions.** It's looked up from the working directory first, then — only if that directory has none — from the directory the installed code lives in, the same place earlier versions always looked. Most installs won't notice; if you start the bot from a directory other than the one holding `.env` and also happen to have an unrelated `.env` sitting in some directory above wherever you start it, that one will be found first, since the working-directory search runs before the code-relative fallback.
- **`STATE_DB_PATH` is resolved the same cwd-relative way.** If working around the point above means starting the bot from a different directory, remember the SQLite database path defaults to `riskyroller.sqlite3` relative to *that* directory too — moving where you start the bot without also moving (or repointing `STATE_DB_PATH` at) the existing `.sqlite3` file starts it on an empty database.

## Data Storage
The bot stores state in SQLite:
- `guild_settings` — per-server configuration only: the ping role, the minimum game time, and the max-rounds-per-channel setting. No member data.
- `active_rounds` / `round_rolls` — an open round: the opener's user ID, and each player's user ID paired with their 1–100 roll. Deleted the moment the round closes.
- `pending_questions` — user IDs only (who's allowed to ask, and of whom) while a just-closed round waits for **Ask Question** to be pressed. Deleted once it is, or swept after 7 days if it never is.
- `posted_questions` — an asked question: the asker's and answerer's user IDs, and **the question text the asker typed**. Kept until answered, or swept 7 days after posting if it never is. The reply itself is written into the Discord message via the **Reply** button rather than back into this table, so the bot's own database never holds it — though the reply remains visible on Discord like any other message.

Schema initialization and lightweight migrations run on startup. When the bot is removed from a guild, every row above scoped to that guild is deleted immediately.

One thing the database above doesn't cover: an in-memory cache of player display names, used so round and question embeds can show a name instead of a raw ID for someone who has since left the server. It's keyed by user ID rather than by guild, holds whatever name was last seen across every server the bot shares with that person, and is **not** cleared when the bot leaves a guild — only a restart clears it.

The database runs in WAL mode, so `-wal` and `-shm` files sit next to the `.sqlite3` file while the bot is running; include them if you copy the database while the bot is up.

## Operational Notes
- `/invite` and `/support` work in a DM with the bot, not just in a server; every other slash command is guild-only, since each one acts on a specific server's rounds or settings.
- In the channel a round runs in, the bot needs **View Channel**, **Send Messages** and **Embed Links**; `/risky_start` refuses and names whatever is missing. View Channel matters most: without it the buttons still work but the bot cannot post the prompt when a round auto-closes. Members-only channels that hide from `@everyone` need the bot's role added explicitly.
- No privileged Discord intents are currently enabled.
- State cleanup can be forced per-channel with `/risky_reset_state`.

## Development
- Entrypoint: `riskyroller/__main__.py` (`main.py` is a shim that forwards to it). `pip install -e .` installs the checkout in editable mode for hacking.
- Tests are plain `unittest`, no extra packages needed:
  ```bash
  python -m unittest discover -s tests -v
  ```
  `pytest tests/` also works if you have pytest installed.
- Logging: standard Python logging at `INFO`, with discord.py's own logger at `WARNING`.

## License
MIT — see `LICENSE`.
