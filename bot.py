"""ZR Bot — vouch counter + USPS shipping tools."""

import json
import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from config import (
    DISCORD_TOKEN, OWNER_ID, GUILD_ID,
    TARGET_CHANNEL_ID, NOTIFICATION_CHANNEL_ID,
    USPS_CONSUMER_KEY, USPS_CONSUMER_SECRET,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("zrbot")

# ---------------------------------------------------------------------------
# Simple JSON file database (ported from vouch_counter_bot.js)
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).resolve().parent / "data"
VOUCHES_FILE = DATA_DIR / "vouches.json"


def _load_vouches() -> dict:
    if VOUCHES_FILE.exists():
        try:
            return json.loads(VOUCHES_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"vouches": {}}


def _save_vouches(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = VOUCHES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(VOUCHES_FILE)


# Migrate old vouches.json from project root if data/ version doesn't exist
_old_vouches = Path(__file__).resolve().parent / "vouches.json"
if _old_vouches.exists() and not VOUCHES_FILE.exists():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy2(_old_vouches, VOUCHES_FILE)
    logger.info("Migrated vouches.json to data/vouches.json")

vouches_db = _load_vouches()

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------------------------
# Vouch counter — message listener
# ---------------------------------------------------------------------------

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if str(message.channel.id) == TARGET_CHANNEL_ID:
        image_attachments = [
            a for a in message.attachments
            if a.content_type and a.content_type.startswith("image/")
        ]
        if image_attachments:
            uid = str(message.author.id)
            current = vouches_db.get("vouches", {}).get(uid, 0)
            new_count = current + 1
            vouches_db.setdefault("vouches", {})[uid] = new_count
            _save_vouches(vouches_db)

            notify_channel = bot.get_channel(int(NOTIFICATION_CHANNEL_ID))
            if notify_channel:
                try:
                    await notify_channel.send(
                        f"@{message.author.name} now has {new_count} vouches"
                    )
                except Exception as e:
                    logger.error("Failed to send vouch notification: %s", e)

    await bot.process_commands(message)

# ---------------------------------------------------------------------------
# Vouch slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="setvouches", description="Override a user's vouches")
@app_commands.describe(user="The user to set vouches for", vouches="Number of vouches")
@app_commands.default_permissions(administrator=True)
async def setvouches(interaction: discord.Interaction, user: discord.User, vouches: int):
    vouches_db.setdefault("vouches", {})[str(user.id)] = vouches
    _save_vouches(vouches_db)
    await interaction.response.send_message(
        f"Set <@{user.id}>'s vouches to **{vouches}**.", ephemeral=True
    )


@bot.tree.command(name="checkvouches", description="Show your current vouch total")
@app_commands.describe(user="Optional: user to check vouches for")
async def checkvouches(interaction: discord.Interaction, user: discord.User = None):
    target = user or interaction.user
    if target.id != interaction.user.id:
        if not interaction.guild or not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "You do not have permission to check others' vouches.", ephemeral=True
            )
    count = vouches_db.get("vouches", {}).get(str(target.id), 0)
    word = "vouch" if count == 1 else "vouches"
    who = "You have" if target.id == interaction.user.id else f"<@{target.id}> has"
    await interaction.response.send_message(f"{who} **{count}** {word}.", ephemeral=True)


@bot.tree.command(name="leaderboard", description="Show top vouch earners")
@app_commands.describe(limit="Number of users to display (default 10)")
async def leaderboard(interaction: discord.Interaction, limit: int = 10):
    all_vouches = vouches_db.get("vouches", {})
    entries = sorted(all_vouches.items(), key=lambda x: x[1], reverse=True)

    if not entries:
        return await interaction.response.send_message(
            "No vouches have been recorded yet.", ephemeral=True
        )

    entries = entries[:limit]
    lines = []
    for i, (uid, count) in enumerate(entries, 1):
        word = "vouch" if count == 1 else "vouches"
        lines.append(f"{i}. <@{uid}> \u2014 {count} {word}")

    content = "\n".join(lines)
    if len(content) > 2000:
        # Send as file
        file_lines = []
        for i, (uid, count) in enumerate(entries, 1):
            word = "vouch" if count == 1 else "vouches"
            try:
                u = await bot.fetch_user(int(uid))
                name = u.name
            except Exception:
                name = f"Unknown ({uid})"
            file_lines.append(f"{i}. {name} \u2014 {count} {word}")
        file_content = "\n".join(file_lines)
        await interaction.response.send_message(
            f"Leaderboard ({len(entries)} users):",
            file=discord.File(fp=__import__("io").BytesIO(file_content.encode()), filename="leaderboard.txt"),
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(content, ephemeral=True)


@bot.tree.command(name="backfill", description="Backfill vouches from existing messages")
@app_commands.default_permissions(manage_guild=True)
async def backfill(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    channel = bot.get_channel(int(TARGET_CHANNEL_ID))
    if not channel:
        return await interaction.followup.send("Target channel not found.", ephemeral=True)

    processed = 0
    async for message in channel.history(limit=None):
        has_image = any(
            a.content_type and a.content_type.startswith("image/")
            for a in message.attachments
        )
        if has_image:
            uid = str(message.author.id)
            current = vouches_db.get("vouches", {}).get(uid, 0)
            vouches_db.setdefault("vouches", {})[uid] = current + 1
        processed += 1

    _save_vouches(vouches_db)
    await interaction.followup.send(
        f"Processed **{processed}** messages and updated vouches.", ephemeral=True
    )


@bot.tree.command(name="clearvouches", description="Clear vouches for a user or all users")
@app_commands.describe(user="Optional: user to clear vouches for")
@app_commands.default_permissions(administrator=True)
async def clearvouches(interaction: discord.Interaction, user: discord.User = None):
    if user:
        vouches_db.setdefault("vouches", {})[str(user.id)] = 0
        _save_vouches(vouches_db)
        await interaction.response.send_message(
            f"Cleared vouches for <@{user.id}>.", ephemeral=True
        )
    else:
        vouches_db["vouches"] = {}
        _save_vouches(vouches_db)
        await interaction.response.send_message(
            "Cleared vouches for all users.", ephemeral=True
        )


@bot.tree.command(name="payments", description="Display payment methods")
async def payments(interaction: discord.Interaction):
    embed = discord.Embed(
        title="ZR's Payments",
        description="Select which payment method you would like to use!",
        color=0x9932CC,
    )
    row1 = discord.ui.View()
    row1.add_item(discord.ui.Button(
        custom_id="payment_zelle1", label="Zelle 1", style=discord.ButtonStyle.danger, emoji="\U0001f3e6"
    ))
    row1.add_item(discord.ui.Button(
        custom_id="payment_zelle2", label="Zelle 2", style=discord.ButtonStyle.danger, emoji="\U0001f3e6"
    ))
    row1.add_item(discord.ui.Button(
        custom_id="payment_paypal1", label="PayPal 1", style=discord.ButtonStyle.success, emoji="\U0001f49a"
    ))
    row1.add_item(discord.ui.Button(
        custom_id="payment_paypal2", label="PayPal 2", style=discord.ButtonStyle.success, emoji="\U0001f49a"
    ))
    await interaction.response.send_message(embed=embed, view=row1)


# Payment button data
PAYMENT_INFO = {
    "payment_zelle1": {"title": "Zelle Payment 1", "color": 0x6534D1, "field": "Phone Number:", "value": "857-756-2574", "note": "Send as Friends & Family"},
    "payment_zelle2": {"title": "Zelle Payment 2", "color": 0x6534D1, "field": "Email:", "value": "richardxu1400@gmail.com", "note": "Send as Friends & Family"},
    "payment_paypal1": {"title": "PayPal Payment 1", "color": 0x00CF31, "field": "Email:", "value": "richardxu1400@gmail.com", "note": "Friends & Family, no notes"},
    "payment_paypal2": {"title": "PayPal Payment 2", "color": 0x00CF31, "field": "Email:", "value": "testtesttestmaverick@gmail.com", "note": "Friends & Family, no notes"},
}


@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return

    custom_id = interaction.data.get("custom_id", "")

    if custom_id in PAYMENT_INFO:
        info = PAYMENT_INFO[custom_id]
        embed = discord.Embed(title=f"\U0001f4b3 {info['title']}", color=info["color"])
        embed.add_field(name=info["field"], value=f"```{info['value']}```", inline=False)
        embed.add_field(name="\U0001f4dd Note:", value=info["note"], inline=False)

        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            custom_id=f"copyable_{custom_id}",
            label="\U0001f4cb Get Copyable Info",
            style=discord.ButtonStyle.secondary,
        ))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    elif custom_id.startswith("copyable_payment_"):
        payment_key = custom_id.replace("copyable_", "")
        info = PAYMENT_INFO.get(payment_key)
        if info:
            await interaction.response.send_message(info["value"], ephemeral=True)

    # -- Tracking embed buttons --
    elif custom_id.startswith("tracking_details_"):
        tn = custom_id.removeprefix("tracking_details_")
        monitor = getattr(bot, "tracking_monitor", None)
        if not monitor:
            return await interaction.response.send_message("Tracking monitor is not configured.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        result = await monitor.check_single(tn)
        if not result or "error" in result:
            return await interaction.followup.send(f"Could not fetch details for `{tn}`.", ephemeral=True)

        entry = monitor.tracking_data.get(tn)
        user_id = entry.get("user_id") if entry else None

        from utils.tracking_monitor import build_tracking_embed, USPS_LOGO_URL
        embed = build_tracking_embed(tn, result, user_id, logo_url=USPS_LOGO_URL, max_events=50, package_label=entry.get("label") if entry else None)
        await interaction.followup.send(embed=embed, ephemeral=True)

    elif custom_id.startswith("tracking_copy_"):
        tn = custom_id.removeprefix("tracking_copy_")
        await interaction.response.send_message(tn)

    elif custom_id.startswith("tracking_confirm_"):
        tn = custom_id.removeprefix("tracking_confirm_")
        embed = discord.Embed(
            title="\u2705 Package Received!",
            description=(
                "Thank you for confirming! We're glad your package arrived safely.\n\n"
                "If you're happy with your purchase, **please leave a vouch** "
                "to help us build trust with future customers!\n\n"
                "If you have any questions or concerns about the package, "
                "please feel free to reach out!"
            ),
            color=0x57F287,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        from utils.tracking_monitor import _log_to_channel
        await _log_to_channel(bot, f"\u2705 **{interaction.user}** confirmed receipt of `{tn}`")


# ---------------------------------------------------------------------------
# Register USPS command modules
# ---------------------------------------------------------------------------

from commands import address as address_commands
from commands import tracking as tracking_commands

address_commands.setup(bot)
tracking_commands.setup(bot)

# Set up tracking monitor if USPS credentials are configured
if USPS_CONSUMER_KEY and USPS_CONSUMER_SECRET:
    from utils.tracking_monitor import TrackingMonitor
    bot.tracking_monitor = TrackingMonitor(bot, USPS_CONSUMER_KEY, USPS_CONSUMER_SECRET)
else:
    bot.tracking_monitor = None
    logger.warning("USPS tracking monitor disabled (no credentials)")


# ---------------------------------------------------------------------------
# Global error handlers — DM owner on any error
# ---------------------------------------------------------------------------

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Catch all slash command errors and DM the owner."""
    cmd_name = interaction.command.name if interaction.command else "unknown"
    logger.error("Command error in /%s: %s", cmd_name, error)
    try:
        owner = await bot.fetch_user(OWNER_ID)
        embed = discord.Embed(
            title="\u26a0\ufe0f Slash Command Error",
            description=(
                f"**Command:** `/{cmd_name}`\n"
                f"**User:** {interaction.user} (`{interaction.user.id}`)\n"
                f"**Error:**\n```{error}```"
            )[:4000],
            color=0xED4245,
            timestamp=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        embed.set_footer(text="ZR Bot Error Notification")
        await owner.send(embed=embed)
    except Exception as dm_exc:
        logger.error("Failed to DM owner about command error: %s", dm_exc)

    try:
        if interaction.response.is_done():
            await interaction.followup.send("Something went wrong. The bot owner has been notified.", ephemeral=True)
        else:
            await interaction.response.send_message("Something went wrong. The bot owner has been notified.", ephemeral=True)
    except Exception:
        pass


@bot.event
async def on_error(event: str, *args, **kwargs):
    """Catch unhandled errors in event handlers and DM the owner."""
    import traceback
    error_tb = traceback.format_exc()
    logger.error("Unhandled error in event %s:\n%s", event, error_tb)
    try:
        owner = await bot.fetch_user(OWNER_ID)
        embed = discord.Embed(
            title="\u26a0\ufe0f Unhandled Bot Error",
            description=f"**Event:** `{event}`\n```{error_tb[:3800]}```",
            color=0xED4245,
            timestamp=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        embed.set_footer(text="ZR Bot Error Notification")
        await owner.send(embed=embed)
    except Exception as dm_exc:
        logger.error("Failed to DM owner about event error: %s", dm_exc)


# ---------------------------------------------------------------------------
# Bot ready + command sync
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    logger.info("Logged in as %s", bot.user)

    # Start tracking monitor and register persistent views for existing tracked packages
    if bot.tracking_monitor:
        bot.tracking_monitor.start()
        logger.info("USPS tracking monitor started")
        from utils.tracking_monitor import build_tracking_view
        for tn, entry in bot.tracking_monitor.tracking_data.items():
            is_delivered = entry.get("last_status_category") == "Delivered"
            bot.add_view(build_tracking_view(tn, delivered=is_delivered))

    # Sync commands to the guild and globally (global needed for DM support)
    await bot.tree.sync()
    logger.info("Commands synced globally")
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        logger.info("Commands synced to guild %s", GUILD_ID)

    # Log vouch stats
    all_vouches = vouches_db.get("vouches", {})
    total = sum(all_vouches.values())
    logger.info("Vouches loaded: %d total across %d users", total, len(all_vouches))

    # Log startup to activity channel
    from utils.tracking_monitor import _log_to_channel
    tracking_count = len(bot.tracking_monitor.tracking_data) if bot.tracking_monitor else 0
    await _log_to_channel(bot, f"\U0001f7e2 **ZR Bot online** — tracking {tracking_count} package(s)")


# ---------------------------------------------------------------------------
# Run (with graceful shutdown)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Missing DISCORD_TOKEN in .env")
        exit(1)
    try:
        bot.run(DISCORD_TOKEN)
    finally:
        # Save tracking state on any shutdown (SIGINT, SIGTERM, crash)
        if getattr(bot, "tracking_monitor", None):
            bot.tracking_monitor.save_state()
            logger.info("Tracking state saved on shutdown")
