import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import discord

from riskyroller import __main__ as entrypoint
from riskyroller import config

# riskyroller/config.py's .env lookup runs once, at import time, so testing
# it against a chosen working directory means importing the real module in a
# fresh subprocess with that directory as cwd -- there is no way to make the
# already-imported module in *this* process re-run its own import statement.
_REPO_ROOT = Path(config.__file__).resolve().parent.parent


class MainTests(unittest.TestCase):
    """Configuration failures exit with code 2 and a message that says what to do."""

    def setUp(self) -> None:
        # Keep the entrypoint from re-binding the root logger to a captured stream.
        patcher = mock.patch.object(entrypoint, "configure_logging")
        patcher.start()
        self.addCleanup(patcher.stop)

        # FINDING A95: config.CONFIG_ERRORS/DEBUG/DEBUG_GUILD_ID are computed
        # once from the real environment (and whatever .env is found) at
        # import time, so without a known baseline these tests would pass or
        # fail depending on whatever happens to be set wherever the suite
        # runs. Give every test the same clean slate; a test that cares about
        # a different value patches it itself, layered on top of this one.
        config.CONFIG_ERRORS.clear()
        self.addCleanup(config.CONFIG_ERRORS.clear)
        for name, value in (("DEBUG", False), ("DEBUG_GUILD_ID", None)):
            baseline = mock.patch.object(config, name, value)
            baseline.start()
            self.addCleanup(baseline.stop)

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


class DotenvLookupTests(unittest.TestCase):
    """FINDING C0-8: config.py's .env lookup must still find an install's
    .env when the process working directory has none of its own, so an
    existing install whose launcher never `cd`s into the repo (a systemd unit
    with no WorkingDirectory=, a cron line, a scheduled task with "Start in"
    blank) keeps working after an upgrade. Exercises the real riskyroller
    package (via a repo-root .env, since that is what config.py's own
    file-relative search walks up to), not a copy of its lookup line."""

    def setUp(self) -> None:
        # The package is copied into a throwaway install root rather than
        # tested in place: the file-relative search walks up from config.py's
        # own directory, so exercising it needs a .env one level above the
        # package -- and writing one into the checkout would sit next to (and
        # on cleanup delete) the operator's real .env.
        install_root = tempfile.TemporaryDirectory()
        self.addCleanup(install_root.cleanup)
        self._install_root = Path(install_root.name)
        shutil.copytree(_REPO_ROOT / "riskyroller", self._install_root / "riskyroller")
        self._repo_env = self._install_root / ".env"

    def _run_probe(self, cwd: str) -> str:
        # A real script file, not `python -c`: dotenv's own find_dotenv()
        # treats a `-c` invocation as interactive (no __main__.__file__) and
        # always searches from the cwd, which would mask exactly the
        # file-relative fallback these tests exist to check -- and does not
        # match how the bot is actually started (`-m riskyroller`, the
        # installed console script, or `python main.py`, all of which have a
        # real __main__.__file__).
        probe = Path(cwd) / "probe.py"
        probe.write_text("import riskyroller.config, os\nprint(os.environ.get('DOTENV_LOOKUP_PROBE', ''))\n")
        result = subprocess.run(
            [sys.executable, str(probe)],
            cwd=cwd,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(self._install_root)},  # no ambient DOTENV_LOOKUP_PROBE
            check=True,
        )
        return result.stdout.strip()

    def test_falls_back_to_file_relative_search_when_cwd_has_no_env(self) -> None:
        self._repo_env.write_text("DOTENV_LOOKUP_PROBE=from-repo\n")

        with tempfile.TemporaryDirectory() as cwd_dir:
            self.assertEqual("from-repo", self._run_probe(cwd_dir))

    def test_working_directory_env_wins_when_present(self) -> None:
        # FINDING C0-10: when both exist, the working-directory .env is
        # found first and wins -- documented here, not just in the comment.
        self._repo_env.write_text("DOTENV_LOOKUP_PROBE=from-repo\n")

        with tempfile.TemporaryDirectory() as cwd_dir:
            (Path(cwd_dir) / ".env").write_text("DOTENV_LOOKUP_PROBE=from-cwd\n")

            self.assertEqual("from-cwd", self._run_probe(cwd_dir))


if __name__ == "__main__":
    unittest.main()
