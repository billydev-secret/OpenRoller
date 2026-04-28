# Question Reply Button — Design

**Status:** approved
**Date:** 2026-04-28

## Goal

Replace the current Discord-native reply detection on bot-posted question messages with a structured Reply button + modal flow that produces a single clean embed showing the question and the recipient's reply.

## Non-goals

- Changing how the 69-rule (`PromptKind.ROOM`) thread-based discussion works. The Reply button is **not** added to thread-based questions.
- Allowing more than one reply per question message.
- Editing or recalling a reply once submitted.
- Cleaning up stale `posted_questions` rows on a TTL.

## User flow

1. A round closes; the asker clicks **Ask Question** and submits the existing `SixtyNineQuestionModal`.
2. The bot posts the question message (current behavior) **plus** a persistent **Reply** button attached to that message. The plain-text format of the question post is unchanged.
3. The recipient (one of the `participant_user_ids`) clicks **Reply**.
4. A modal opens with a single paragraph text input (max 300 chars).
5. On submit, the question message is edited in place to a clean embed showing question + reply, and the Reply button is removed.

If the click comes from anyone outside the allowed list, an ephemeral error is shown and the message is unchanged.

## Architecture

### New dataclass — `PostedQuestionState`

In `riskyroller/models.py`:

```python
@dataclass
class PostedQuestionState:
    message_id: int
    channel_id: int
    guild_id: int
    asker_id: int
    allowed_replier_ids: set[int]
    question_text: str
    asker_rolled_100: bool = False
    target_rolled_1: bool = False
```

`message_id` is the primary key. The two boolean flags drive the `⭐` / `☠️` markers in the reply embed and are set at post-time from the round state — the embed builder does not need to look up the closed round.

### New SQLite table — `posted_questions`

In `riskyroller/store.py` `_initialize`:

```sql
CREATE TABLE IF NOT EXISTS posted_questions (
    message_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    asker_id INTEGER NOT NULL,
    allowed_replier_ids TEXT NOT NULL,
    question_text TEXT NOT NULL,
    asker_rolled_100 INTEGER NOT NULL DEFAULT 0,
    target_rolled_1 INTEGER NOT NULL DEFAULT 0
);
```

`allowed_replier_ids` is serialized via the existing `serialize_user_ids` helper. No migration of existing tables is required.

### New store methods

Mirroring `save_pending_question` / `delete_pending_question` / `load_pending_questions`:

- `save_posted_question(state: PostedQuestionState) -> None` — UPSERT keyed on `message_id`.
- `delete_posted_question(message_id: int) -> None`.
- `load_posted_questions() -> list[PostedQuestionState]`.

### New in-memory state

In `riskyroller/state.py`:

```python
posted_questions: dict[int, PostedQuestionState] = {}
```

Plus a new lock helper:

```python
_message_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()

def get_message_lock(message_id: int) -> asyncio.Lock: ...
```

### Removed code

The native-reply ping is removed entirely:

- `Bot.on_message` in `riskyroller/bot.py` — deleted.
- `app_state.question_messages` — deleted.
- `app_state.remember_question_message` — deleted.
- `QUESTION_MESSAGE_CACHE_LIMIT` — deleted.

The two existing call sites of `app_state.remember_question_message` (in `SixtyNineQuestionModal.on_submit`, in the `TWO_QUESTIONERS` and `DIRECT` branches) are replaced with `app_state.posted_questions[msg.id] = state` plus `await app_state.store.save_posted_question(state)`.

### New view — `QuestionReplyView`

In `riskyroller/views.py`:

```python
class QuestionReplyView(BaseRiskyRollView):
    def __init__(self):
        super().__init__(game_id="")  # not keyed by game_id; lookup is by message_id

    @discord.ui.button(
        label="Reply",
        style=discord.ButtonStyle.primary,
        custom_id="riskyroller:question_reply",
        emoji="✏️",
    )
    async def reply_button(self, interaction, button):
        state = app_state.posted_questions.get(interaction.message.id)
        if state is None:
            await interaction.response.send_message(
                "This reply window has closed.", ephemeral=True
            )
            return
        if interaction.user.id not in state.allowed_replier_ids:
            await interaction.response.send_message(
                "Only the question's recipient can reply.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            QuestionReplyModal(message_id=interaction.message.id)
        )
```

`custom_id` is a static string (required for view persistence). State is looked up by `interaction.message.id` rather than by view-instance attributes. `BaseRiskyRollView`'s `game_id` field is unused for this view — passing `""` is acceptable.

### New modal — `QuestionReplyModal`

In `riskyroller/views.py`:

```python
class QuestionReplyModal(discord.ui.Modal, title="Reply"):
    reply = discord.ui.TextInput(
        label="Your reply",
        style=discord.TextStyle.paragraph,
        max_length=300,
    )

    def __init__(self, message_id: int):
        super().__init__()
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        async with app_state.get_message_lock(self.message_id):
            state = app_state.posted_questions.get(self.message_id)
            if state is None:
                await interaction.response.send_message(
                    "Someone already replied to this question.", ephemeral=True
                )
                return
            if interaction.user.id not in state.allowed_replier_ids:
                await interaction.response.send_message(
                    "Only the question's recipient can reply.", ephemeral=True
                )
                return

            reply_text = self.reply.value.strip()
            if not reply_text:
                await interaction.response.send_message(
                    "Enter a reply before sending it.", ephemeral=True
                )
                return

            await interaction.response.defer(ephemeral=True)

            embed = build_question_reply_embed(state, interaction.user.id, reply_text)
            channel = await get_text_channel(interaction.client, state.channel_id)
            if channel is None:
                await interaction.followup.send(
                    "Could not update the question message; your reply wasn't recorded — please try again.",
                    ephemeral=True,
                )
                return

            try:
                await channel.get_partial_message(self.message_id).edit(
                    content="",
                    embed=embed,
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.NotFound:
                # Message was deleted — drop the row and tell the user.
                app_state.posted_questions.pop(self.message_id, None)
                await app_state.store.delete_posted_question(self.message_id)
                await interaction.followup.send(
                    "The question message no longer exists.", ephemeral=True
                )
                return
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Failed to edit question message %s.", self.message_id)
                await interaction.followup.send(
                    "Could not update the question message; your reply wasn't recorded — please try again.",
                    ephemeral=True,
                )
                return

            app_state.posted_questions.pop(self.message_id, None)
            await app_state.store.delete_posted_question(self.message_id)

            await interaction.followup.send("Reply sent.", ephemeral=True)
```

### New embed builder — `build_question_reply_embed`

In `riskyroller/formatters.py`:

```python
def build_question_reply_embed(
    state: PostedQuestionState,
    replier_id: int,
    reply_text: str,
) -> discord.Embed:
    embed = discord.Embed(title="🎲 Question", color=discord.Color(0x546E7A))

    asker_label = f"<@{state.asker_id}>"
    if state.asker_rolled_100:
        asker_label += " ⭐"

    target_ids = sorted(state.allowed_replier_ids)
    if state.target_rolled_1:
        target_parts = [f"<@{tid}> ☠️" for tid in target_ids]
    else:
        target_parts = [f"<@{tid}>" for tid in target_ids]
    answers_label = " and ".join(target_parts)

    embed.add_field(name="Asks", value=asker_label, inline=True)
    embed.add_field(name="Answers", value=answers_label, inline=True)
    embed.add_field(name="Question", value=f"> {state.question_text}", inline=False)

    if len(state.allowed_replier_ids) > 1:
        reply_value = f"<@{replier_id}>\n> {reply_text}"
    else:
        reply_value = f"> {reply_text}"
    embed.add_field(name="Reply", value=reply_value, inline=False)

    return embed
```

Discord field **names** do not render mentions, so when there are multiple targets the replier mention goes in the field **value** instead.

### Bot startup wiring

In `riskyroller/bot.py setup_hook`, extend the `asyncio.gather` to also load posted questions:

```python
ping_roles, min_game_times, active_rounds, pending_questions, posted_questions = await asyncio.gather(
    app_state.store.load_ping_roles(),
    app_state.store.load_min_game_times(),
    app_state.store.load_active_rounds(),
    app_state.store.load_pending_questions(),
    app_state.store.load_posted_questions(),
)
```

After the existing `pending_questions` re-registration loop, add:

```python
for state in posted_questions:
    app_state.posted_questions[state.message_id] = state
    self.add_view(QuestionReplyView(), message_id=state.message_id)
```

### Question-posting integration

In `SixtyNineQuestionModal.on_submit`, in **only** the `DIRECT` and `TWO_QUESTIONERS` branches:

1. Pass `view=QuestionReplyView()` to the `interaction.followup.send(...)` call that sends the question message.
2. Replace the existing `app_state.remember_question_message(question_msg.id, asker_id)` line with:

```python
posted = PostedQuestionState(
    message_id=question_msg.id,
    channel_id=state.channel_id,
    guild_id=state.guild_id,
    asker_id=asker_id,
    allowed_replier_ids=set(state.participant_user_ids),
    question_text=question_text,
    asker_rolled_100=(state.prompt_kind == PromptKind.DIRECT and len(state.participant_user_ids) > 1),
    target_rolled_1=(state.prompt_kind == PromptKind.TWO_QUESTIONERS),
)
app_state.posted_questions[question_msg.id] = posted
await app_state.store.save_posted_question(posted)
```

The `ROOM` branch is unchanged — the Reply button is **not** attached to thread questions.

### Note on the `asker_rolled_100` proxy

`asker_rolled_100=(state.prompt_kind == PromptKind.DIRECT and len(state.participant_user_ids) > 1)` is a proxy: it's `True` exactly when `_build_main_prompt_state` added `second_lowest_user` to the targets. `second_lowest_user` is only set when the winner rolled 100 *and* there were 3+ players. In the rare **2-player round where the winner rolls 100**, `second_lowest_user` is `None`, so the proxy returns `False` and no `⭐` marker is shown on the embed. This intentionally matches the existing behavior in `build_pending_prompt_content`, which also uses `len(participant_user_ids) > 1` as the proxy and similarly under-reports the 100 rule in 2-player rounds.

`target_rolled_1=(state.prompt_kind == PromptKind.TWO_QUESTIONERS)` is exact: `_build_one_rule_prompt_state` only constructs that prompt kind when `state.rolls[state.lowest_user] == 1`.

## Behavior across the special-roll cases

| Case | Prompt kind | `asker_rolled_100` | `target_rolled_1` | Embed result |
|------|-------------|--------------------|--------------------|--------------|
| Standard | DIRECT, 1 target | false | false | Plain Asks/Answers/Question/Reply |
| 100 only | DIRECT, 2 targets | true | false | `⭐` on asker; both targets in Answers; replier mention prepended to Reply value |
| 1 only | TWO_QUESTIONERS | false | true | `☠️` on target |
| 100 + 1 | Two messages: one DIRECT and one TWO_QUESTIONERS | true / false respectively | false / true respectively | Each message gets its own button + embed |
| 69 | ROOM | n/a | n/a | No Reply button — thread-based discussion unchanged |

## Error handling

- **Stale state on click** (`posted_questions[message.id] is None`) → ephemeral *"This reply window has closed."* No edit.
- **Disallowed clicker** (not in `allowed_replier_ids`) → ephemeral *"Only the question's recipient can reply."* No edit.
- **Race on submit** (state already deleted by parallel submit) → ephemeral *"Someone already replied to this question."* The lock makes this rare but not impossible across processes.
- **`discord.NotFound` on edit** → drop the row + ephemeral *"The question message no longer exists."*
- **`discord.Forbidden` / `discord.HTTPException` on edit** → log, ephemeral *"Could not update the question message; your reply wasn't recorded — please try again."* Row stays so the user can retry.
- **Empty / whitespace-only reply** → ephemeral *"Enter a reply before sending it."*

## Concurrency

A `get_message_lock(message_id)` helper (`WeakValueDictionary` of `asyncio.Lock`, mirroring `get_game_lock`) wraps the `read → edit → delete` sequence in `QuestionReplyModal.on_submit`. This prevents two near-simultaneous clicks from both editing the message; the second one finds the row gone and gets the friendly error.

## Persistence & restart

`posted_questions` rows survive bot restarts. On startup, each row's view is re-registered with `bot.add_view(QuestionReplyView(), message_id=...)`, restoring full button functionality.

A row is deleted in two places:
1. Successful reply submission.
2. `discord.NotFound` on edit (message no longer exists).

Rows are not auto-expired. If a question is never replied to, the row sits in the table; the button remains live indefinitely. This is acceptable — the rows are tiny and the volume is low.

## Tests

Add to `tests/test_store.py`:
- `posted_questions` roundtrip — save → load → fields and both boolean flags match.
- Delete removes the row.

Add to `tests/test_game_states.py` (or a new module if it grows):
- Allowed replier in `participant_user_ids` succeeds.
- Disallowed clicker (asker themselves, or a random user) is rejected with the expected ephemeral message.
- Two parallel `on_submit` calls — one wins, one returns the "already replied" message; only one DB delete occurs.
- `100` case: `asker_rolled_100` is set, embed shows `⭐` and replier mention in the Reply value.
- `1` case: `target_rolled_1` is set, embed shows `☠️`.

## Out of scope (deferred)

- TTL / cleanup of stale posted-question rows.
- Editing or retracting a submitted reply.
- Reply button on `PromptKind.ROOM` thread questions.
- Multiple replies per question.
