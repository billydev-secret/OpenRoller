import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

import discord

from riskyroller import state as app_state
from riskyroller.formatters import build_embed, build_pending_prompt_content, format_room_mentions
from riskyroller.models import PendingQuestionState, PostedQuestionState, PromptKind, RiskyRollState
from riskyroller.views import QuestionReplyModal, RiskyRollView, SixtyNineQuestionModal


def _http_error(cls, status, text):
    return cls(Mock(status=status, reason=text), text)


class RoomMentionExclusionTests(unittest.TestCase):
    """A37: the room ping list shouldn't repeat the asker, who is already
    named on the line right below it ("<@id> asks:")."""

    def test_excludes_the_named_id(self):
        self.assertEqual("<@1> <@3>", format_room_mentions({1, 2, 3}, exclude=2))

    def test_excluding_an_id_still_reports_the_right_leftover_count(self):
        ids = set(range(1, 82))  # 81 ids, one more than the cap of 50

        mentions = format_room_mentions(ids, exclude=1)

        self.assertNotIn("<@1>", mentions)
        self.assertTrue(mentions.endswith(" and 30 more"))


class TwoQuestionerGrammarTests(unittest.TestCase):
    """A104: "can each fire a question" is wrong once only one asker is left."""

    def _state(self, **overrides) -> PendingQuestionState:
        defaults = dict(
            channel_id=1,
            guild_id=2,
            winner_id=10,
            participant_user_ids={20},
            game_id=str(uuid.uuid4()),
            extra_questioner_id=11,
            prompt_kind=PromptKind.TWO_QUESTIONERS,
        )
        defaults.update(overrides)
        return PendingQuestionState(**defaults)

    def test_both_questioners_still_owed_keeps_the_plural(self):
        content = build_pending_prompt_content(self._state())

        self.assertIn("<@10> and <@11> can each fire a question at <@20>.", content)

    def test_one_questioner_already_asked_switches_to_singular(self):
        content = build_pending_prompt_content(self._state(questioners_asked={10}))

        self.assertIn("<@11> can fire a question at <@20>.", content)
        self.assertNotIn("can each fire", content)


class RollsFieldCapTests(unittest.TestCase):
    """A8: a full roster of long escaped names must not cross Discord's
    1024-character embed field limit (build_embed had no cap at all)."""

    def setUp(self) -> None:
        app_state.guild_display_names.clear()
        self.addCleanup(app_state.guild_display_names.clear)

    def test_a_full_room_of_long_names_is_split_under_the_limit(self) -> None:
        rolls = {}
        for i in range(25):
            uid = 9000 + i
            rolls[uid] = i + 2  # never 1, 69 or 100 — irrelevant here since the round is still open
            # A 32-character nickname full of markdown-special characters:
            # escape_markdown doubles every "_", so this renders at 64
            # characters, the regression c64684b introduced by switching the
            # roster from a ~22-character <@id> mention to the escaped name.
            app_state.guild_display_names[(2, uid)] = "_" * 32
        state = RiskyRollState(channel_id=1, guild_id=2, opener_id=1, rolls=rolls)

        embed = build_embed(state)

        roll_fields = [f for f in embed.fields if (f.name or "").startswith("Rolls")]
        self.assertGreater(len(roll_fields), 1, "the roster should have needed more than one field")
        for field in roll_fields:
            self.assertLessEqual(len(field.value or ""), 1024)
        # Nobody's roll silently disappeared into the split.
        self.assertEqual(25, sum((field.value or "").count("🎲") for field in roll_fields))


def _interaction(user_id: int, channel=None) -> Mock:
    interaction = Mock()
    interaction.user = Mock()
    interaction.user.id = user_id
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock(return_value=Mock(id=777))
    interaction.channel = channel if channel is not None else Mock()
    interaction.client = Mock()
    return interaction


def _content_call(mock_send: AsyncMock):
    """The one call among possibly several that actually posted content."""
    return next(c for c in mock_send.await_args_list if "content" in c.kwargs)


class QuestionMentionSecurityTests(unittest.IsolatedAsyncioTestCase):
    """A10 + A22: player-typed question text goes out under the bot's own
    identity. In discord.py 2.7.1, AllowedMentions(users=True) leaves
    everyone/roles unset, which defaults to a truthy sentinel — so it also
    allows @everyone, @here and role mentions embedded in that text, and the
    text itself could carry a masked markdown link rendered as the bot's own
    message. Both must be neutralised at every send.
    """

    def setUp(self) -> None:
        app_state.pending_questions.clear()
        app_state.posted_questions.clear()
        app_state.active_games.clear()
        self.addCleanup(app_state.pending_questions.clear)
        self.addCleanup(app_state.posted_questions.clear)
        self.addCleanup(app_state.active_games.clear)
        self.fake_store = Mock(
            save_pending_question=AsyncMock(),
            delete_pending_question=AsyncMock(),
            save_posted_question=AsyncMock(),
            delete_round=AsyncMock(),
            save_round=AsyncMock(),
        )
        patcher = patch.object(app_state, "store", self.fake_store)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_direct_question_send_scrubs_mentions_and_masked_links(self) -> None:
        state = PendingQuestionState(
            channel_id=100,
            guild_id=200,
            winner_id=10,
            participant_user_ids={20},
            game_id="g-direct",
            prompt_kind=PromptKind.DIRECT,
        )
        app_state.pending_questions["g-direct"] = state
        modal = SixtyNineQuestionModal("g-direct")
        modal.question._value = (
            "Hey <@999999999> [Claim your Discord Nitro prize](https://phishing.example)"
        )
        interaction = _interaction(user_id=10)

        await modal.on_submit(interaction)

        kwargs = _content_call(interaction.followup.send).kwargs
        allowed = kwargs["allowed_mentions"].to_dict()
        self.assertNotIn("everyone", allowed.get("parse", []))
        self.assertNotIn("roles", allowed.get("parse", []))
        self.assertEqual({10, 20}, set(allowed.get("users", [])))
        self.assertTrue(kwargs["suppress_embeds"])
        # The masked link's opening bracket is escaped, so it can no longer
        # render as a clickable label hiding the real URL.
        self.assertIn("\\[Claim your Discord Nitro prize]", kwargs["content"])

    async def test_room_question_thread_send_scrubs_mentions_and_masked_links(self) -> None:
        state = PendingQuestionState(
            channel_id=100,
            guild_id=200,
            winner_id=10,
            participant_user_ids={10, 20, 30},
            game_id="g-room",
            prompt_kind=PromptKind.ROOM,
        )
        app_state.pending_questions["g-room"] = state
        modal = SixtyNineQuestionModal("g-room")
        modal.question._value = "Ping <@424242> [free nitro](https://phishing.example)"
        channel = Mock(spec=discord.TextChannel)
        thread = Mock()
        thread.send = AsyncMock(return_value=Mock(id=555))
        channel.create_thread = AsyncMock(return_value=thread)
        interaction = _interaction(user_id=10, channel=channel)

        await modal.on_submit(interaction)

        kwargs = thread.send.await_args.kwargs
        allowed = kwargs["allowed_mentions"].to_dict()
        self.assertNotIn("everyone", allowed.get("parse", []))
        self.assertNotIn("roles", allowed.get("parse", []))
        self.assertEqual({10, 20, 30}, set(allowed.get("users", [])))
        self.assertNotIn(424242, allowed.get("users", []))
        self.assertTrue(kwargs["suppress_embeds"])
        self.assertIn("\\[free nitro]", kwargs["content"])
        # A37: the asker (10) is named once, on the "asks:" line — not also
        # repeated in the room mention list above it.
        self.assertEqual(1, kwargs["content"].count("<@10>"))
        self.assertNotIn("<@10>", kwargs["content"].splitlines()[0])

    async def test_room_question_channel_fallback_scrubs_mentions_too(self) -> None:
        """Same content, same allow-list, when the thread can't be created."""
        state = PendingQuestionState(
            channel_id=100,
            guild_id=200,
            winner_id=10,
            participant_user_ids={10, 20, 30},
            game_id="g-room-fallback",
            prompt_kind=PromptKind.ROOM,
        )
        app_state.pending_questions["g-room-fallback"] = state
        modal = SixtyNineQuestionModal("g-room-fallback")
        modal.question._value = "Ping <@424242> please"
        channel = Mock(spec=discord.TextChannel)
        channel.create_thread = AsyncMock(side_effect=_http_error(discord.HTTPException, 403, "Forbidden"))
        interaction = _interaction(user_id=10, channel=channel)

        # The thread failure is logged; asserting on it keeps a passing run
        # from printing its traceback.
        with self.assertLogs("riskyroller.views", level="ERROR"):
            await modal.on_submit(interaction)

        kwargs = _content_call(interaction.followup.send).kwargs
        allowed = kwargs["allowed_mentions"].to_dict()
        self.assertNotIn("everyone", allowed.get("parse", []))
        self.assertNotIn(424242, allowed.get("users", []))
        self.assertTrue(kwargs["suppress_embeds"])

    async def test_two_questioners_waiting_prompt_edit_scrubs_mentions(self) -> None:
        """The prompt-message edit that names who's still owed a question."""
        state = PendingQuestionState(
            channel_id=100,
            guild_id=200,
            winner_id=10,
            participant_user_ids={99},
            game_id="g-2q",
            prompt_kind=PromptKind.TWO_QUESTIONERS,
            extra_questioner_id=11,
            prompt_message_id=555,
        )
        app_state.pending_questions["g-2q"] = state
        modal = SixtyNineQuestionModal("g-2q")
        modal.question._value = "Ping <@424242> please"
        channel = Mock(spec=discord.TextChannel)
        channel.get_partial_message.return_value.edit = AsyncMock()
        interaction = _interaction(user_id=10, channel=channel)

        with patch("riskyroller.views.get_text_channel", AsyncMock(return_value=channel)):
            await modal.on_submit(interaction)

        kwargs = channel.get_partial_message.return_value.edit.await_args.kwargs
        allowed = kwargs["allowed_mentions"].to_dict()
        self.assertNotIn("everyone", allowed.get("parse", []))
        self.assertNotIn("roles", allowed.get("parse", []))
        self.assertNotIn(424242, allowed.get("users", []))
        self.assertEqual({10, 11, 99}, set(allowed.get("users", [])))

    async def test_round_close_prompt_send_scrubs_mentions(self) -> None:
        """The main winner prompt sent when a round closes (Close Round)."""
        state = RiskyRollState(
            channel_id=100,
            guild_id=200,
            opener_id=1,
            game_id="g-close",
            message_id=999,
            rolls={1: 90, 999999999: 10},
            skip_min_game_time=True,
        )
        app_state.active_games["g-close"] = state
        view = RiskyRollView("g-close")
        interaction = _interaction(user_id=1)
        interaction.guild = None
        interaction.edit_original_response = AsyncMock()
        interaction.response.is_done.return_value = True

        await view.close_button.callback(interaction)

        kwargs = _content_call(interaction.followup.send).kwargs
        allowed = kwargs["allowed_mentions"].to_dict()
        self.assertNotIn("everyone", allowed.get("parse", []))
        self.assertNotIn("roles", allowed.get("parse", []))
        self.assertEqual({1, 999999999}, set(allowed.get("users", [])))


class ReplyMentionSecurityTests(unittest.IsolatedAsyncioTestCase):
    """A22 also applies to a reply: it's posted under the bot's identity too."""

    def setUp(self) -> None:
        app_state.posted_questions.clear()
        self.addCleanup(app_state.posted_questions.clear)
        fake_store = Mock(delete_posted_question=AsyncMock())
        patcher = patch.object(app_state, "store", fake_store)
        patcher.start()
        self.addCleanup(patcher.stop)
        app_state.posted_questions[555] = PostedQuestionState(
            message_id=555,
            channel_id=100,
            guild_id=200,
            asker_id=10,
            allowed_replier_ids={20},
            question_text="Favourite colour?",
        )

    async def test_reply_escapes_a_masked_link_and_suppresses_embeds(self) -> None:
        interaction = Mock()
        interaction.user.id = 20
        interaction.response.is_done.return_value = False
        interaction.response.edit_message = AsyncMock()
        interaction.response.send_message = AsyncMock()
        interaction.followup.send = AsyncMock()
        modal = QuestionReplyModal(message_id=555)
        modal.reply._value = "[Click here for a prize](https://phishing.example)"

        with patch(
            "riskyroller.views.get_text_channel",
            side_effect=AssertionError("the reply path must not resolve the channel"),
        ):
            await modal.on_submit(interaction)

        kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertTrue(kwargs["suppress_embeds"])
        self.assertIn("\\[Click here for a prize]", kwargs["content"])


if __name__ == "__main__":
    unittest.main()
