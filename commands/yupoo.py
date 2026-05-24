"""Slash commands for Yupoo QC image fetching."""

import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import AUTHORIZED_IDS
from utils.yupoo import (
    fetch_album_images,
    chunk_for_discord,
    parse_album_url,
)

logger = logging.getLogger("zrbot.yupoo")

MAX_IMAGES_DEFAULT = 50  # safety cap per album


def setup(bot: commands.Bot):
    @bot.tree.command(name="qc", description="Fetch QC pictures from a Yupoo album")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        url="Yupoo album URL (e.g. https://rmqc.x.yupoo.com/albums/239263830)",
        max_images="Max images to fetch (default: all, up to 50)",
    )
    async def qc_command(
        interaction: discord.Interaction,
        url: str,
        max_images: int = None,
    ):
        if interaction.user.id not in AUTHORIZED_IDS:
            return await interaction.response.send_message(
                "You are not authorized.", ephemeral=True
            )

        # Validate URL format before deferring
        parsed = parse_album_url(url)
        if not parsed:
            return await interaction.response.send_message(
                "Invalid Yupoo album URL. Expected format:\n"
                "`https://VENDOR.x.yupoo.com/albums/ALBUM_ID`",
                ephemeral=True,
            )

        vendor, album_id = parsed
        cap = min(max_images, MAX_IMAGES_DEFAULT) if max_images else MAX_IMAGES_DEFAULT

        # Defer ephemerally so the command invocation (with URL) stays hidden
        await interaction.response.defer(ephemeral=True)

        try:
            album, downloaded = await fetch_album_images(url, max_images=cap)
        except Exception as exc:
            logger.error("Failed to fetch album %s: %s", url, exc, exc_info=True)
            return await interaction.followup.send(
                f"Failed to fetch album: {exc}", ephemeral=True
            )

        if not album.images:
            return await interaction.followup.send(
                f"No images found in album `{album_id}` from `{vendor}`.",
                ephemeral=True,
            )

        if not downloaded:
            return await interaction.followup.send(
                f"Found {len(album.images)} image(s) but all failed to download.",
                ephemeral=True,
            )

        # Send images directly to the channel (not as a reply to the command)
        channel = interaction.channel
        chunks = chunk_for_discord(downloaded)

        for chunk in chunks:
            files = [
                discord.File(fp=io.BytesIO(img.data), filename=img.filename)
                for img in chunk
            ]
            await channel.send(files=files)

        await interaction.followup.send(
            f"Sent {len(downloaded)} QC image(s).", ephemeral=True
        )

    logger.info("Yupoo commands registered (qc)")
