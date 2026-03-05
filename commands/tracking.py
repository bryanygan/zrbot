"""Slash commands for USPS package tracking."""

import discord
from discord import app_commands
from discord.ext import commands

from config import AUTHORIZED_IDS


def _is_authorized(interaction: discord.Interaction) -> bool:
    return interaction.user.id in AUTHORIZED_IDS


def setup(bot: commands.Bot):
    @bot.tree.command(name="track", description="Start tracking a USPS package")
    @app_commands.describe(
        tracking_number="USPS tracking number",
        user="The Discord user this package is for",
    )
    async def track_command(
        interaction: discord.Interaction,
        tracking_number: str,
        user: discord.User,
    ):
        if not _is_authorized(interaction):
            return await interaction.response.send_message(
                "You are not authorized.", ephemeral=True
            )

        monitor = getattr(bot, "tracking_monitor", None)
        if not monitor:
            return await interaction.response.send_message(
                "Tracking monitor is not configured. Check USPS API credentials.",
                ephemeral=True,
            )

        tn = tracking_number.strip().upper()

        if tn in monitor.tracking_data:
            return await interaction.response.send_message(
                f"`{tn}` is already being tracked.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=False)

        result = await monitor.check_single(tn)

        if not result or "error" in result or result.get("statusCode") == "404":
            error_detail = ""
            if result and "error" in result:
                errs = result["error"].get("errors", [])
                if errs:
                    error_detail = f"\n{errs[0].get('detail', '')}"
            return await interaction.followup.send(
                f"Could not find tracking info for `{tn}`.{error_detail}",
                ephemeral=True,
            )

        from utils.tracking_monitor import build_tracking_embed, _save_tracking, USPS_LOGO_URL

        embed = build_tracking_embed(tn, result, user.id, logo_url=USPS_LOGO_URL)
        msg = await interaction.followup.send(embed=embed, wait=True)
        await monitor.add(tn, user.id, channel_id=msg.channel.id, message_id=msg.id)

        entry = monitor.tracking_data[tn]
        entry["last_status_category"] = result.get("statusCategory")
        entry["last_status"] = result.get("status")
        _save_tracking(monitor.tracking_data)

    @bot.tree.command(name="untrack", description="Stop tracking a USPS package")
    @app_commands.describe(tracking_number="USPS tracking number to stop tracking")
    async def untrack_command(
        interaction: discord.Interaction,
        tracking_number: str,
    ):
        if not _is_authorized(interaction):
            return await interaction.response.send_message(
                "You are not authorized.", ephemeral=True
            )

        monitor = getattr(bot, "tracking_monitor", None)
        if not monitor:
            return await interaction.response.send_message(
                "Tracking monitor is not configured.", ephemeral=True
            )

        tn = tracking_number.strip().upper()
        if monitor.remove(tn):
            await interaction.response.send_message(
                f"Stopped tracking `{tn}`.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"`{tn}` is not being tracked.", ephemeral=True
            )

    @bot.tree.command(name="trackinglist", description="Show all tracked packages")
    async def trackinglist_command(interaction: discord.Interaction):
        if not _is_authorized(interaction):
            return await interaction.response.send_message(
                "You are not authorized.", ephemeral=True
            )

        monitor = getattr(bot, "tracking_monitor", None)
        if not monitor:
            return await interaction.response.send_message(
                "Tracking monitor is not configured.", ephemeral=True
            )

        data = monitor.list_all()
        if not data:
            return await interaction.response.send_message(
                "No packages are currently being tracked.", ephemeral=True
            )

        from utils.tracking_monitor import STATUS_CONFIG, DEFAULT_STATUS_CONFIG

        lines = []
        for tn, entry in data.items():
            cat = entry.get("last_status_category") or "Unknown"
            _, emoji, label = STATUS_CONFIG.get(cat, DEFAULT_STATUS_CONFIG)
            mode = "channel" if entry.get("channel_id") else "DM"
            user_mention = f"<@{entry['user_id']}>"
            lines.append(f"{emoji} `{tn}` \u2014 {label} \u2014 {user_mention} ({mode})")

        embed = discord.Embed(
            title=f"\U0001f4e6 Tracked Packages ({len(data)})",
            description="\n".join(lines),
            color=0x5865F2,
        )
        embed.set_footer(text=f"Polling every {monitor._poll_interval_minutes} min")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="trackrefresh", description="Force refresh all tracked packages now")
    async def trackrefresh_command(interaction: discord.Interaction):
        if not _is_authorized(interaction):
            return await interaction.response.send_message(
                "You are not authorized.", ephemeral=True
            )

        monitor = getattr(bot, "tracking_monitor", None)
        if not monitor:
            return await interaction.response.send_message(
                "Tracking monitor is not configured.", ephemeral=True
            )

        if not monitor.tracking_data:
            return await interaction.response.send_message(
                "No packages to refresh.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        await monitor.force_poll()
        await interaction.followup.send(
            f"Refreshed {len(monitor.tracking_data)} package(s).", ephemeral=True
        )

    @bot.tree.command(name="trackinfo", description="Get current tracking info for a package")
    @app_commands.describe(tracking_number="USPS tracking number")
    async def trackinfo_command(
        interaction: discord.Interaction,
        tracking_number: str,
    ):
        if not _is_authorized(interaction):
            return await interaction.response.send_message(
                "You are not authorized.", ephemeral=True
            )

        monitor = getattr(bot, "tracking_monitor", None)
        if not monitor:
            return await interaction.response.send_message(
                "Tracking monitor is not configured.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        tn = tracking_number.strip().upper()
        result = await monitor.check_single(tn)

        if not result or "error" in result:
            return await interaction.followup.send(
                f"Could not find tracking info for `{tn}`.", ephemeral=True
            )

        entry = monitor.tracking_data.get(tn)
        user_id = entry.get("user_id") if entry else None

        from utils.tracking_monitor import build_tracking_embed, USPS_LOGO_URL
        embed = build_tracking_embed(tn, result, user_id, logo_url=USPS_LOGO_URL)
        await interaction.followup.send(embed=embed, ephemeral=True)
