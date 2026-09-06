import unittest
import uuid

from riskyroller import state as app_state
from riskyroller.formatters import build_embed, build_pending_prompt_content, format_room_mentions
from riskyroller.models import PendingQuestionState, PromptKind, RiskyRollState


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
        app_state.display_names.clear()
        self.addCleanup(app_state.display_names.clear)

    def test_a_full_room_of_long_names_is_split_under_the_limit(self) -> None:
        rolls = {}
        for i in range(25):
            uid = 9000 + i
            rolls[uid] = i + 2  # never 1, 69 or 100 — irrelevant here since the round is still open
            # A 32-character nickname full of markdown-special characters:
            # escape_markdown doubles every "_", so this renders at 64
            # characters, the regression c64684b introduced by switching the
            # roster from a ~22-character <@id> mention to the escaped name.
            app_state.display_names[uid] = "_" * 32
        state = RiskyRollState(channel_id=1, guild_id=2, opener_id=1, rolls=rolls)

        embed = build_embed(state)

        roll_fields = [f for f in embed.fields if (f.name or "").startswith("Rolls")]
        self.assertGreater(len(roll_fields), 1, "the roster should have needed more than one field")
        for field in roll_fields:
            self.assertLessEqual(len(field.value or ""), 1024)
        # Nobody's roll silently disappeared into the split.
        self.assertEqual(25, sum((field.value or "").count("🎲") for field in roll_fields))


if __name__ == "__main__":
    unittest.main()
