import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

import discord

from riskyroller.bot import Bot
from riskyroller.invite import INVITE_PERMISSIONS, INVITE_SCOPES, invite_url


class InviteUrlTests(unittest.TestCase):
    def test_url_carries_application_id_scopes_and_permissions(self) -> None:
        url = invite_url(123456789012345678)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.netloc, "discord.com")
        self.assertEqual(query["client_id"], ["123456789012345678"])
        self.assertEqual(set(query["scope"][0].split()), set(INVITE_SCOPES))
        self.assertEqual(query["permissions"], [str(INVITE_PERMISSIONS.value)])

    def test_scopes_cover_bot_user_and_slash_commands(self) -> None:
        self.assertIn("bot", INVITE_SCOPES)
        self.assertIn("applications.commands", INVITE_SCOPES)

    def test_permission_set_is_exactly_what_the_bot_needs(self) -> None:
        expected = discord.Permissions(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            create_public_threads=True,
            send_messages_in_threads=True,
        )
        self.assertEqual(INVITE_PERMISSIONS, expected)
        self.assertTrue(INVITE_PERMISSIONS.view_channel)
        self.assertFalse(INVITE_PERMISSIONS.administrator)


class StartupInviteLogTests(unittest.TestCase):
    """The first on_ready logs the invite link once; reconnects do not repeat it."""

    APPLICATION_ID = 42

    def setUp(self) -> None:
        self.bot = Bot()

    def _ready(self, guilds: list) -> list[str]:
        with (
            mock.patch.object(Bot, "application_id", new_callable=mock.PropertyMock, return_value=self.APPLICATION_ID),
            mock.patch.object(Bot, "guilds", new_callable=mock.PropertyMock, return_value=guilds),
            self.assertLogs("riskyroller.bot", level="INFO") as captured,
        ):
            self.bot._log_invite_link_once()
        return captured.output

    def test_no_guilds_warns_with_the_link(self) -> None:
        lines = self._ready(guilds=[])
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("WARNING"))
        self.assertIn(invite_url(self.APPLICATION_ID), lines[0])

    def test_with_guilds_logs_at_info(self) -> None:
        lines = self._ready(guilds=[object()])
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("INFO"))
        self.assertIn(invite_url(self.APPLICATION_ID), lines[0])

    def test_logged_only_once_across_reconnects(self) -> None:
        self._ready(guilds=[])
        with (
            mock.patch.object(Bot, "application_id", new_callable=mock.PropertyMock, return_value=self.APPLICATION_ID),
            mock.patch.object(Bot, "guilds", new_callable=mock.PropertyMock, return_value=[]),
            self.assertNoLogs("riskyroller.bot", level="INFO"),
        ):
            self.bot._log_invite_link_once()

    def test_not_logged_before_login_supplies_an_application_id(self) -> None:
        with (
            mock.patch.object(Bot, "application_id", new_callable=mock.PropertyMock, return_value=None),
            mock.patch.object(Bot, "user", new_callable=mock.PropertyMock, return_value=None),
            self.assertNoLogs("riskyroller.bot", level="INFO"),
        ):
            self.bot._log_invite_link_once()
        self.assertFalse(self.bot._invite_logged)


if __name__ == "__main__":
    unittest.main()
