"""The OAuth invite link, shared by ``/invite`` and the startup log.

Kept in one place so the permission set the bot asks for can only ever be
changed in one spot.
"""

import discord

# Exactly what the bot needs: see the channel (without it every button still
# works but nothing the bot does on its own can reach the channel), post the
# round message and its embed, and follow a round into a thread if one is
# opened on it. Nothing privileged.
INVITE_PERMISSIONS = discord.Permissions(
    view_channel=True,
    send_messages=True,
    embed_links=True,
    create_public_threads=True,
    send_messages_in_threads=True,
)

# ``bot`` adds the bot user; ``applications.commands`` registers slash commands.
INVITE_SCOPES: tuple[str, ...] = ("bot", "applications.commands")


def invite_url(application_id: int) -> str:
    return discord.utils.oauth_url(
        application_id,
        permissions=INVITE_PERMISSIONS,
        scopes=INVITE_SCOPES,
    )
