import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from riskyroller import state as app_state
from riskyroller.models import PostedQuestionState
from riskyroller.views import QuestionReplyModal


def _http_error(cls, status, text):
    return cls(Mock(status=status, reason=text), text)


class ReplyModalTests(unittest.IsolatedAsyncioTestCase):
    """The reply path must work through the interaction alone.

    Editing the question by looking the channel up first failed wherever the
    bot lacked View Channel, even though the Reply button itself worked there.
    """

    def setUp(self) -> None:
        app_state.posted_questions.clear()
        self.addCleanup(app_state.posted_questions.clear)
        # Never let the test reach the real store (its path is the working
        # directory's database).
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

    def _interaction(self, user_id: int = 20) -> Mock:
        interaction = Mock()
        interaction.user.id = user_id
        interaction.response.is_done.return_value = False
        interaction.response.edit_message = AsyncMock()
        interaction.response.send_message = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction

    async def _submit(self, interaction: Mock, text: str = "Blue.") -> None:
        modal = QuestionReplyModal(message_id=555)
        modal.reply._value = text
        with patch(
            "riskyroller.views.get_text_channel",
            side_effect=AssertionError("the reply path must not resolve the channel"),
        ):
            await modal.on_submit(interaction)

    async def test_reply_updates_the_origin_message_through_the_interaction(self) -> None:
        interaction = self._interaction()

        await self._submit(interaction)

        interaction.response.edit_message.assert_awaited_once()
        kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertIn("<@20>: Blue.", kwargs["content"])
        self.assertIsNone(kwargs["view"])
        self.assertNotIn(555, app_state.posted_questions)
        interaction.followup.send.assert_awaited_once_with("Reply sent.", ephemeral=True)

    async def test_rejected_edit_keeps_the_question_open_and_echoes_the_reply(self) -> None:
        interaction = self._interaction()
        interaction.response.edit_message.side_effect = _http_error(
            discord.HTTPException, 500, "Internal Server Error"
        )

        await self._submit(interaction, "My long answer")

        self.assertIn(555, app_state.posted_questions)
        sent = interaction.response.send_message.await_args
        self.assertIn("My long answer", sent.args[0])
        self.assertTrue(sent.kwargs["ephemeral"])

    async def test_deleted_question_message_clears_the_state(self) -> None:
        interaction = self._interaction()
        interaction.response.edit_message.side_effect = _http_error(
            discord.NotFound, 404, "Unknown Message"
        )

        await self._submit(interaction)

        self.assertNotIn(555, app_state.posted_questions)

    async def test_non_recipient_is_refused_without_touching_the_message(self) -> None:
        interaction = self._interaction(user_id=99)

        await self._submit(interaction)

        interaction.response.edit_message.assert_not_awaited()
        self.assertIn(555, app_state.posted_questions)


if __name__ == "__main__":
    unittest.main()
