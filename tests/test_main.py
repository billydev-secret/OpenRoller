import io
import unittest
from unittest import mock

import discord

from riskyroller import __main__ as entrypoint
from riskyroller import config


class MainTests(unittest.TestCase):
    """Configuration failures exit with code 2 and a message that says what to do."""

    def setUp(self) -> None:
        # Keep the entrypoint from re-binding the root logger to a captured stream.
        patcher = mock.patch.object(entrypoint, "configure_logging")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_main(self) -> tuple[int, str]:
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code = entrypoint.main()
        return code, stderr.getvalue()

    def test_missing_token_exits_with_instructions_without_connecting(self) -> None:
        with mock.patch("riskyroller.config.TOKEN", None), mock.patch("riskyroller.bot.bot.run") as run:
            code, err = self._run_main()

        run.assert_not_called()
        self.assertEqual(code, 2)
        self.assertIn("DISCORD_TOKEN is not set", err)
        self.assertIn("discord.com/developers/applications", err)
        self.assertIn(".env.example", err)

    def test_rejected_token_exits_with_instructions(self) -> None:
        with (
            mock.patch("riskyroller.config.TOKEN", "not-a-real-token"),
            mock.patch(
                "riskyroller.bot.bot.run",
                side_effect=discord.LoginFailure("Improper token has been passed."),
            ) as run,
        ):
            code, err = self._run_main()

        run.assert_called_once()
        self.assertIsNone(run.call_args.kwargs.get("log_handler", "missing"))
        self.assertEqual(code, 2)
        self.assertIn("Discord rejected the token", err)
        self.assertIn("Reset Token", err)

    def test_debug_without_guild_id_exits_before_connecting(self) -> None:
        with (
            mock.patch("riskyroller.config.TOKEN", "not-a-real-token"),
            mock.patch("riskyroller.config.DEBUG", True),
            mock.patch("riskyroller.config.DEBUG_GUILD_ID", None),
            mock.patch("riskyroller.bot.bot.run") as run,
        ):
            code, err = self._run_main()

        run.assert_not_called()
        self.assertEqual(code, 2)
        self.assertIn("GUILD_ID", err)

    def test_config_errors_exit_before_anything_else(self) -> None:
        with (
            mock.patch("riskyroller.config.CONFIG_ERRORS", ["GUILD_ID must be a whole number, got 'abc'."]),
            mock.patch("riskyroller.config.TOKEN", None),
            mock.patch("riskyroller.bot.bot.run") as run,
        ):
            code, err = self._run_main()

        run.assert_not_called()
        self.assertEqual(code, 2)
        self.assertIn("GUILD_ID must be a whole number", err)
        self.assertNotIn("DISCORD_TOKEN is not set", err)

    def test_clean_run_returns_zero(self) -> None:
        with mock.patch("riskyroller.config.TOKEN", "not-a-real-token"), mock.patch("riskyroller.bot.bot.run"):
            code, err = self._run_main()

        self.assertEqual(code, 0)
        self.assertEqual(err, "")


class IntEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        config.CONFIG_ERRORS.clear()
        self.addCleanup(config.CONFIG_ERRORS.clear)

    def test_unset_and_blank_fall_back_to_default(self) -> None:
        self.assertEqual(1800, config.parse_int_env("X", None, 1800))
        self.assertEqual(1800, config.parse_int_env("X", "  ", 1800))
        self.assertIsNone(config.parse_int_env("GUILD_ID", None, None))
        self.assertEqual([], config.CONFIG_ERRORS)

    def test_whitespace_around_a_number_is_fine(self) -> None:
        self.assertEqual(42, config.parse_int_env("X", " 42 ", 0))
        self.assertEqual([], config.CONFIG_ERRORS)

    def test_zero_is_a_value_not_a_fallback(self) -> None:
        self.assertEqual(0, config.parse_int_env("DEFAULT_MIN_GAME_SECONDS", "0", 1800))

    def test_non_number_records_an_error_naming_the_variable(self) -> None:
        # The README's old sample .env shipped exactly this placeholder.
        value = config.parse_int_env("GUILD_ID", "your_debug_guild_id_optional", None)

        self.assertIsNone(value)
        self.assertEqual(1, len(config.CONFIG_ERRORS))
        self.assertIn("GUILD_ID", config.CONFIG_ERRORS[0])
        self.assertIn("your_debug_guild_id_optional", config.CONFIG_ERRORS[0])


if __name__ == "__main__":
    unittest.main()
