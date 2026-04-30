# How To Play Button — Design

**Status:** approved
**Date:** 2026-04-29

## Goal

Add a **How to Play** button to the main round embed (`RiskyRollView`), positioned to the right of the **Roll** button. Clicking it shows a concise, ephemeral rules summary so players who are unfamiliar with the game can self-serve without cluttering the channel.

## Non-goals

- Slash command equivalent (e.g. `/risky_help`).
- Per-guild customizable rules text.
- README changes.
- Localization / i18n.
- Adding the button to other views (`SixtyNineQuestionView`, `QuestionReplyView`, etc.).

## User flow

1. A round is opened in a channel; the round embed shows **Roll**, **How to Play**, **Close Round** (in that order).
2. Any user clicks **How to Play**.
3. The bot responds with an ephemeral message containing a rules embed visible only to the clicker.
4. The round message itself is unchanged. The button can be clicked any number of times.

## Architecture

### New embed builder — `build_how_to_play_embed`

In `riskyroller/formatters.py`:

```python
def build_how_to_play_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎲 How to Play",
        color=NOTICE_EMBED_COLOR,
    )
    embed.description = (
        "**Roll** — Each player presses **Roll** once. You roll a number from **1** to **100**.\n"
        "**Win** — Highest unique roll wins the round; lowest roll is the loser.\n"
        "**Ties for highest** — Tied players auto-reroll until one wins.\n"
        "**Question** — The winner asks the loser a question; the loser must reply.\n"
        "🔥 **Rolled 69** — The winner asks the whole room (in a thread).\n"
        "⭐ **Rolled 100** — The winner asks the bottom two players.\n"
        "☠️ **Rolled 1** — The top two players each ask the loser.\n"
        "**Close** — Only the round opener (or an admin) can close early."
    )
    return embed
```

The function is parameterless and returns a fresh embed each call. It is colocated with the other notice / prompt embed builders and reuses `NOTICE_EMBED_COLOR` (`0x546E7A`) for visual consistency.

### New button on `RiskyRollView`

In `riskyroller/views.py`, between the existing `roll_button` and `close_button`:

```python
@discord.ui.button(
    label="How to Play",
    style=discord.ButtonStyle.secondary,
    custom_id="riskyroller:how_to_play",
    emoji="❓",
)
async def how_to_play_button(self, interaction: discord.Interaction, button: discord.ui.Button):
    await interaction.response.send_message(
        embed=build_how_to_play_embed(),
        ephemeral=True,
    )
```

Discord renders buttons in source order within a single action row, so placing the decorator between `roll_button` and `close_button` produces the order `[Roll] [How to Play] [Close Round]`.

The handler:
- Does not touch round state or take any lock — it's pure read.
- Uses `interaction.response.send_message(..., ephemeral=True)` so only the clicker sees the response.
- Has no early returns or error paths beyond `BaseRiskyRollView.on_error` for unexpected failures.

### Persistence & restart

None required. The button is stateless and identified by its static `custom_id` (`"riskyroller:how_to_play"`), which means it works automatically on views restored after a bot restart through the existing `bot.add_view(RiskyRollView(...))` re-registration in `bot.py`.

### Disabled state

`BaseRiskyRollView.disable_all_items()` already iterates over all `Button` / `Select` children, so the new button will be greyed out automatically on closed rounds (matching Roll/Close). No code changes needed there.

## Tests

Add to `tests/test_formatters.py` (or a new test module if none exists):

- `build_how_to_play_embed()` returns a `discord.Embed` whose:
  - `title` contains "How to Play".
  - `description` mentions **Roll**, **69**, **100**, and **1** — the four key rules a player needs to know.
  - `color` matches `NOTICE_EMBED_COLOR`.

No view-interaction test. The existing `RiskyRollView` buttons (`roll_button`, `close_button`) aren't covered by interaction tests; adding one solely for this stateless handler would set a new precedent and isn't proportionate.

## Out of scope (deferred)

- A `/risky_help` slash command.
- Per-guild customization of the rules text.
- Surfacing rules from a config file rather than inlining the strings.
- Adding "How to Play" to other views.
