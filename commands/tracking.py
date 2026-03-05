"""Slash commands for USPS package tracking."""

import json

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone

from config import AUTHORIZED_IDS


def _is_authorized(interaction: discord.Interaction) -> bool:
    return interaction.user.id in AUTHORIZED_IDS


def setup(bot: commands.Bot):
    @bot.tree.command(name="track", description="Start tracking a USPS package")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        tracking_number="USPS tracking number",
        user="The Discord user this package is for (auto-detected in DMs)",
        label="Optional nickname for this package (e.g., 'Jordan 4s for @user')",
    )
    async def track_command(
        interaction: discord.Interaction,
        tracking_number: str,
        user: discord.User = None,
        label: str = None,
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

        # Resolve recipient: explicit user > DM recipient > None
        if user is None and interaction.guild is None:
            # In a DM — the other participant is the recipient
            channel = interaction.channel
            if hasattr(channel, "recipient") and channel.recipient:
                user = channel.recipient

        user_id = user.id if user else None

        tn = tracking_number.strip().upper()

        if tn in monitor.tracking_data:
            return await interaction.response.send_message(
                f"`{tn}` is already being tracked.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=False)

        result = await monitor.check_single(tn)

        from utils.tracking_monitor import build_tracking_embed, build_tracking_view, build_dm_tracking_view, _save_tracking, _log_to_channel, USPS_LOGO_URL

        usps_not_found = not result or "error" in result or result.get("statusCode") == "404"

        if usps_not_found:
            result = {
                "statusCategory": "Waiting for USPS",
                "status": "Waiting for USPS",
                "statusSummary": "Label has been created but USPS hasn't registered this package yet. It will update automatically once USPS scans it.",
                "trackingEvents": [],
            }

        is_dm = interaction.guild is None
        embed = build_tracking_embed(tn, result, user_id, logo_url=USPS_LOGO_URL, package_label=label)

        if is_dm:
            # DM context: static embed with opt-in for live updates
            embed.set_footer(text="USPS Tracking \u2022 Use 'Get Live Updates' for automatic updates")
            view = build_dm_tracking_view(tn)
            msg = await interaction.followup.send(embed=embed, view=view, wait=True)
            # Store without channel/message so we don't try to edit in user-user DM
            await monitor.add(tn, user_id, channel_id=None, message_id=None, label=label)
        else:
            # Channel context: live-updating embed
            view = build_tracking_view(tn)
            msg = await interaction.followup.send(embed=embed, view=view, wait=True)
            await monitor.add(tn, user_id, channel_id=msg.channel.id, message_id=msg.id, label=label)

        entry = monitor.tracking_data[tn]
        entry["last_status_category"] = result.get("statusCategory")
        entry["last_status"] = result.get("status")
        _save_tracking(monitor.tracking_data)

        # Log to activity channel
        pkg_display = label or tn
        user_mention = f"<@{user_id}>" if user_id else "Unknown"
        await _log_to_channel(bot, f"\U0001f4e6 New package tracked: **{pkg_display}** (`{tn}`) for {user_mention}")

    @bot.tree.command(name="untrack", description="Stop tracking a USPS package")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
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
        entry = monitor.tracking_data.get(tn)
        if monitor.remove(tn):
            from utils.tracking_monitor import _log_to_channel
            pkg_label = entry.get("label") if entry else None
            pkg_display = pkg_label or tn
            await _log_to_channel(bot, f"\u274c Stopped tracking: **{pkg_display}** (`{tn}`)")
            await interaction.response.send_message(
                f"Stopped tracking `{tn}`.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"`{tn}` is not being tracked.", ephemeral=True
            )

    PACKAGES_PER_PAGE = 8

    def _build_tracking_lines(data: dict) -> list[str]:
        from utils.tracking_monitor import STATUS_CONFIG, DEFAULT_STATUS_CONFIG, HIGH_PRIORITY_CATEGORIES, LOW_PRIORITY_CATEGORIES

        lines = []
        for tn, entry in data.items():
            cat = entry.get("last_status_category") or "Unknown"
            _, emoji, label = STATUS_CONFIG.get(cat, DEFAULT_STATUS_CONFIG)
            mode = "channel" if entry.get("channel_id") else "DM"
            user_mention = f"<@{entry['user_id']}>" if entry.get("user_id") else "Unknown"

            checked_at = entry.get("last_checked_at")
            checked_str = ""
            if checked_at:
                try:
                    checked_ts = int(datetime.fromisoformat(checked_at).timestamp())
                    checked_str = f" \u2022 <t:{checked_ts}:R>"
                except (ValueError, TypeError):
                    pass

            tier = ""
            if cat in HIGH_PRIORITY_CATEGORIES:
                tier = " \U0001f525"
            elif cat in LOW_PRIORITY_CATEGORIES:
                tier = " \U0001f535"

            pkg_label = entry.get("label")
            name_part = f"**{pkg_label}** (`{tn}`)" if pkg_label else f"`{tn}`"
            lines.append(f"{emoji} {name_part} \u2014 {label} \u2014 {user_mention} ({mode}){checked_str}{tier}")
        return lines

    def _build_trackinglist_embed(lines: list[str], page: int, total_pages: int, total: int, poll_min: int) -> discord.Embed:
        start = page * PACKAGES_PER_PAGE
        page_lines = lines[start:start + PACKAGES_PER_PAGE]
        embed = discord.Embed(
            title=f"\U0001f4e6 Tracked Packages ({total})",
            description="\n".join(page_lines),
            color=0x5865F2,
        )
        footer = f"Page {page + 1}/{total_pages} \u2022 Polling every {poll_min} min"
        embed.set_footer(text=footer)
        return embed

    def _build_trackinglist_view(page: int, total_pages: int) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            custom_id=f"tl_prev_{page}",
            label="Previous",
            style=discord.ButtonStyle.secondary,
            disabled=page == 0,
        ))
        view.add_item(discord.ui.Button(
            custom_id=f"tl_next_{page}",
            label="Next",
            style=discord.ButtonStyle.secondary,
            disabled=page >= total_pages - 1,
        ))
        return view

    @bot.tree.command(name="trackinglist", description="Show all tracked packages")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
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

        lines = _build_tracking_lines(data)
        total_pages = max(1, (len(lines) + PACKAGES_PER_PAGE - 1) // PACKAGES_PER_PAGE)

        embed = _build_trackinglist_embed(lines, 0, total_pages, len(data), monitor._poll_interval_minutes)

        if total_pages <= 1:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            view = _build_trackinglist_view(0, total_pages)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @bot.tree.command(name="trackrefresh", description="Force refresh tracked packages")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        tracking_number="Specific tracking number to refresh",
        user="Refresh all packages for this user",
    )
    async def trackrefresh_command(
        interaction: discord.Interaction,
        tracking_number: str = None,
        user: discord.User = None,
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

        if not monitor.tracking_data:
            return await interaction.response.send_message(
                "No packages to refresh.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        from utils.tracking_monitor import build_tracking_embed, build_tracking_view, _save_tracking, USPS_LOGO_URL

        if tracking_number:
            # Refresh a single tracking number
            tn = tracking_number.strip().upper()
            entry = monitor.tracking_data.get(tn)
            if not entry:
                return await interaction.followup.send(f"`{tn}` is not being tracked.", ephemeral=True)

            result = await monitor.check_single(tn)
            if result and "error" not in result and result.get("statusCode") != "404":
                entry["last_status_category"] = result.get("statusCategory", "")
                entry["last_status"] = result.get("status", "")
                entry["last_checked_at"] = datetime.now(timezone.utc).isoformat()
                if entry.get("channel_id") and entry.get("message_id"):
                    category = result.get("statusCategory", "")
                    is_delivered = category == "Delivered"
                    try:
                        channel = bot.get_channel(entry["channel_id"]) or await bot.fetch_channel(entry["channel_id"])
                        message = await channel.fetch_message(entry["message_id"])
                        embed = build_tracking_embed(tn, result, entry.get("user_id"), logo_url=USPS_LOGO_URL, package_label=entry.get("label"))
                        view = build_tracking_view(tn, delivered=is_delivered)
                        await message.edit(embed=embed, view=view)
                    except Exception:
                        pass
                _save_tracking(monitor.tracking_data)
                await interaction.followup.send(f"Refreshed `{tn}`.", ephemeral=True)
            else:
                await interaction.followup.send(f"Could not fetch data for `{tn}`.", ephemeral=True)

        elif user:
            # Refresh all packages for a specific user
            user_packages = {tn: e for tn, e in monitor.tracking_data.items() if e.get("user_id") == user.id}
            if not user_packages:
                return await interaction.followup.send(f"No packages tracked for {user.mention}.", ephemeral=True)

            refreshed = 0
            for tn, entry in user_packages.items():
                result = await monitor.check_single(tn)
                if result and "error" not in result and result.get("statusCode") != "404":
                    entry["last_status_category"] = result.get("statusCategory", "")
                    entry["last_status"] = result.get("status", "")
                    entry["last_checked_at"] = datetime.now(timezone.utc).isoformat()
                    if entry.get("channel_id") and entry.get("message_id"):
                        category = result.get("statusCategory", "")
                        is_delivered = category == "Delivered"
                        try:
                            channel = bot.get_channel(entry["channel_id"]) or await bot.fetch_channel(entry["channel_id"])
                            message = await channel.fetch_message(entry["message_id"])
                            embed = build_tracking_embed(tn, result, entry.get("user_id"), logo_url=USPS_LOGO_URL, package_label=entry.get("label"))
                            view = build_tracking_view(tn, delivered=is_delivered)
                            await message.edit(embed=embed, view=view)
                        except Exception:
                            pass
                    refreshed += 1
            _save_tracking(monitor.tracking_data)
            await interaction.followup.send(f"Refreshed **{refreshed}** package(s) for {user.mention}.", ephemeral=True)

        else:
            # Refresh all
            await monitor.force_poll()
            await interaction.followup.send(
                f"Refreshed {len(monitor.tracking_data)} package(s).", ephemeral=True
            )

    @bot.tree.command(name="trackinfo", description="Get current tracking info for a package")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
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
        embed = build_tracking_embed(tn, result, user_id, logo_url=USPS_LOGO_URL, package_label=entry.get("label") if entry else None)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @bot.tree.command(name="bulktrack", description="Track multiple USPS packages at once")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        tracking_numbers="Comma-separated tracking numbers",
        user="The Discord user these packages are for",
    )
    async def bulktrack_command(
        interaction: discord.Interaction,
        tracking_numbers: str,
        user: discord.User = None,
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

        if user is None and interaction.guild is None:
            channel = interaction.channel
            if hasattr(channel, "recipient") and channel.recipient:
                user = channel.recipient

        user_id = user.id if user else None

        # Parse tracking numbers
        numbers = [n.strip().upper() for n in tracking_numbers.split(",") if n.strip()]
        if not numbers:
            return await interaction.response.send_message(
                "No valid tracking numbers provided.", ephemeral=True
            )
        if len(numbers) > 10:
            return await interaction.response.send_message(
                "Maximum 10 tracking numbers at once.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=False)

        from utils.tracking_monitor import build_tracking_embed, build_tracking_view, build_dm_tracking_view, _save_tracking, _log_to_channel, USPS_LOGO_URL

        is_dm = interaction.guild is None
        added = []
        skipped = []
        for tn in numbers:
            if tn in monitor.tracking_data:
                skipped.append(tn)
                continue

            result = await monitor.check_single(tn)
            usps_not_found = not result or "error" in result or result.get("statusCode") == "404"
            if usps_not_found:
                result = {
                    "statusCategory": "Waiting for USPS",
                    "status": "Waiting for USPS",
                    "statusSummary": "Label created but USPS hasn't registered this package yet.",
                    "trackingEvents": [],
                }

            embed = build_tracking_embed(tn, result, user_id, logo_url=USPS_LOGO_URL)

            if is_dm:
                embed.set_footer(text="USPS Tracking \u2022 Use 'Get Live Updates' for automatic updates")
                view = build_dm_tracking_view(tn)
                msg = await interaction.followup.send(embed=embed, view=view, wait=True)
                await monitor.add(tn, user_id, channel_id=None, message_id=None)
            else:
                view = build_tracking_view(tn)
                msg = await interaction.followup.send(embed=embed, view=view, wait=True)
                await monitor.add(tn, user_id, channel_id=msg.channel.id, message_id=msg.id)

            entry = monitor.tracking_data[tn]
            entry["last_status_category"] = result.get("statusCategory")
            entry["last_status"] = result.get("status")
            added.append(tn)

        _save_tracking(monitor.tracking_data)

        summary = f"Tracked **{len(added)}** package(s)."
        if skipped:
            summary += f" Skipped **{len(skipped)}** (already tracked)."
        await interaction.followup.send(summary, ephemeral=True)

        if added:
            user_mention = f"<@{user_id}>" if user_id else "Unknown"
            await _log_to_channel(bot, f"\U0001f4e6 Bulk tracked **{len(added)}** package(s) for {user_mention}")

    @bot.tree.command(name="stats", description="Show shipping statistics")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_command(interaction: discord.Interaction):
        if not _is_authorized(interaction):
            return await interaction.response.send_message(
                "You are not authorized.", ephemeral=True
            )

        monitor = getattr(bot, "tracking_monitor", None)
        if not monitor:
            return await interaction.response.send_message(
                "Tracking monitor is not configured.", ephemeral=True
            )

        from utils.tracking_monitor import STATUS_CONFIG, DEFAULT_STATUS_CONFIG

        data = monitor.tracking_data
        active = len(data)

        # Count by status category
        category_counts = {}
        for tn, entry in data.items():
            cat = entry.get("last_status_category") or "Unknown"
            category_counts[cat] = category_counts.get(cat, 0) + 1

        # Load stats file for historical data
        from utils.tracking_monitor import STATS_FILE
        stats_path = STATS_FILE
        historical = {}
        if stats_path.exists():
            try:
                historical = json.loads(stats_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        total_shipped = historical.get("total_delivered", 0)
        total_delivery_days = historical.get("total_delivery_days", 0)
        total_with_delivery_time = historical.get("total_with_delivery_time", 0)

        avg_delivery = ""
        if total_with_delivery_time > 0:
            avg = total_delivery_days / total_with_delivery_time
            avg_delivery = f"{avg:.1f} days"

        embed = discord.Embed(
            title="\U0001f4ca Shipping Statistics",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Active Packages", value=str(active), inline=True)
        embed.add_field(name="Total Delivered", value=str(total_shipped), inline=True)
        if avg_delivery:
            embed.add_field(name="Avg Delivery Time", value=avg_delivery, inline=True)

        # Status breakdown
        if category_counts:
            breakdown_lines = []
            for cat, count in sorted(category_counts.items(), key=lambda x: x[1], reverse=True):
                _, emoji, lbl = STATUS_CONFIG.get(cat, DEFAULT_STATUS_CONFIG)
                breakdown_lines.append(f"{emoji} {lbl}: **{count}**")
            embed.add_field(
                name="Active Breakdown",
                value="\n".join(breakdown_lines),
                inline=False,
            )

        embed.set_footer(text=f"Polling every {monitor._poll_interval_minutes} min")
        await interaction.response.send_message(embed=embed, ephemeral=True)
