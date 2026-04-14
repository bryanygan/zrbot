"""Deal Hunter commands — run weekend deal scans and DM results to owner."""

import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import OWNER_ID, AUTHORIZED_IDS

logger = logging.getLogger("zrbot.deals")

# Path to the weekendmaxxing project
DEAL_HUNTER_PATH = Path(__file__).resolve().parent.parent.parent / "weekendmaxxing"
DEAL_DB_PATH = DEAL_HUNTER_PATH / "data" / "deals.db"


def _ensure_deal_hunter_on_path():
    """Add weekendmaxxing to sys.path so we can import from it."""
    path_str = str(DEAL_HUNTER_PATH)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)


def _build_deal_embed(deal: dict, rank: int) -> discord.Embed:
    """Build a rich Discord embed for a single deal."""
    is_train = deal.get("transport_type") == "train"
    transport_emoji = "\U0001f682" if is_train else "\u2708\ufe0f"
    transport_type = "Train" if is_train else "Flight"

    dest = deal.get("destination", "Unknown")
    score = deal.get("score", 0)
    price = deal.get("price_usd", 0)
    carrier = deal.get("airline", deal.get("operator", "?"))
    layovers = deal.get("layovers", 0)
    stops = "Direct" if is_train and layovers == 0 else ("Nonstop" if layovers == 0 else f"{layovers} stop")
    outbound = deal.get("outbound_date", "")
    ret = deal.get("return_date", "")
    hours = deal.get("hours_at_destination", 0)

    hotel = deal.get("best_stay", {})
    hotel_name = hotel.get("name", "N/A")
    hotel_price = hotel.get("total_price", 0)
    hotel_rating = hotel.get("rating", 0)
    stars = "\u2b50" * int(hotel_rating)

    total = deal.get("estimated_total", deal.get("total_trip_cost", 0))
    extras = deal.get("estimated_extras", 0)

    # Color based on score
    if score >= 70:
        color = 0x22C55E  # green
    elif score >= 55:
        color = 0xEAB308  # yellow
    else:
        color = 0xEF4444  # red

    embed = discord.Embed(
        title=f"{transport_emoji} #{rank} — {dest}",
        description=f"**Score: {score}/100** | {transport_type} via {carrier}",
        color=color,
    )

    embed.add_field(
        name=f"{transport_emoji} Transport",
        value=f"**${price:.0f}** RT | {stops}\n{carrier}",
        inline=True,
    )
    embed.add_field(
        name="\U0001f3e8 Hotel",
        value=f"**{hotel_name}**\n${hotel_price:.0f} total | {stars}",
        inline=True,
    )
    embed.add_field(
        name="\U0001f4b0 Est. Total",
        value=f"**${total:.0f}**\n(+${extras:.0f} food/transit)",
        inline=True,
    )

    embed.add_field(
        name="\U0001f4c5 Dates",
        value=f"{outbound} \u2192 {ret}",
        inline=True,
    )
    embed.add_field(
        name="\u23f0 Schedule",
        value=(
            f"Leave {deal.get('outbound_depart', '?')}, arrive {deal.get('outbound_arrive', '?')}\n"
            f"Return {deal.get('return_depart', '?')}, home {deal.get('return_arrive', '?')}"
        ),
        inline=True,
    )
    embed.add_field(
        name="\u231b Time There",
        value=f"{hours:.0f} hours",
        inline=True,
    )

    # Price drop indicator
    drop = deal.get("price_drop")
    if drop and drop > 0:
        embed.set_footer(text=f"\u2b07\ufe0f Price dropped ${drop:.0f} since last scan!")
    elif drop and drop < 0:
        embed.set_footer(text=f"\u2b06\ufe0f Price increased ${abs(drop):.0f} since last scan")

    # Recommendations as a truncated field
    recs = deal.get("recommendations", "")
    if recs and len(recs) > 20:
        embed.add_field(
            name="\U0001f4dd Recommendations",
            value=recs[:1000] + ("..." if len(recs) > 1000 else ""),
            inline=False,
        )

    return embed


def _build_summary_embed(deals: list[dict], elapsed_min: float) -> discord.Embed:
    """Build a summary embed for the scan results."""
    if not deals:
        return discord.Embed(
            title="\u2708\ufe0f Weekend Deal Hunter",
            description="No deals found above the score threshold this run.",
            color=0x888888,
        )

    trains = sum(1 for d in deals if d.get("transport_type") == "train")
    flights = len(deals) - trains
    cheapest = min(deals, key=lambda d: d.get("estimated_total", 9999))
    best = max(deals, key=lambda d: d.get("score", 0))

    embed = discord.Embed(
        title="\u2708\ufe0f Weekend Deal Hunter \u2014 Scan Complete",
        description=f"Found **{len(deals)} deals** ({flights} flights, {trains} trains) in {elapsed_min:.1f} min",
        color=0x22C55E,
        timestamp=datetime.now(),
    )
    embed.add_field(
        name="\U0001f3c6 Best Score",
        value=f"{best.get('destination', '?')} ({best.get('score', 0)}/100)",
        inline=True,
    )
    embed.add_field(
        name="\U0001f4b5 Cheapest",
        value=f"{cheapest.get('destination', '?')} (${cheapest.get('estimated_total', 0):.0f})",
        inline=True,
    )
    embed.set_footer(text="Origin: PHL | Powered by Ollama llama3.1:8b")
    return embed


async def _run_deal_scan(bot: commands.Bot, interaction: discord.Interaction = None) -> list[dict]:
    """Run the deal pipeline in a thread and return deals."""
    _ensure_deal_hunter_on_path()

    # Import inside function to avoid path issues at module load time
    import importlib
    # Force fresh imports
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith(("orchestrator", "agents", "scrapers", "utils.llm", "utils.json_extractor", "config.settings", "notifications")):
            del sys.modules[mod_name]

    from orchestrator.pipeline import run_pipeline
    from orchestrator.state_manager import get_recent_deals

    # Run the blocking pipeline in a thread so we don't block the bot
    loop = asyncio.get_event_loop()
    summary = await loop.run_in_executor(None, run_pipeline)

    deals = get_recent_deals(limit=20)
    return deals


def setup(bot: commands.Bot):
    """Register deal commands with the bot."""

    @bot.tree.command(name="deals", description="Run a weekend deal scan and DM results")
    @app_commands.describe(destinations="Comma-separated cities to search (blank = all)")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    async def deals_cmd(interaction: discord.Interaction, destinations: str = ""):
        if interaction.user.id not in AUTHORIZED_IDS:
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return

        await interaction.response.send_message(
            "\U0001f50d **Starting deal scan...** This takes 15-30 minutes.\n"
            "I'll DM you the results when it's done.",
            ephemeral=True,
        )

        try:
            import time
            start = time.time()

            # Optionally filter destinations
            if destinations.strip():
                _ensure_deal_hunter_on_path()
                dest_path = DEAL_HUNTER_PATH / "config" / "destinations.json"
                data = json.loads(dest_path.read_text(encoding="utf-8"))
                target_cities = [c.strip() for c in destinations.split(",")]
                filtered = [d for d in data["destinations"]
                            if any(t.lower() in d["city"].lower() for t in target_cities)]
                if filtered:
                    backup = data.copy()
                    data["destinations"] = filtered
                    dest_path.write_text(json.dumps(data, indent=2))

            deals = await _run_deal_scan(bot, interaction)
            elapsed = (time.time() - start) / 60

            # Restore destinations if we filtered
            if destinations.strip() and filtered:
                backup["destinations"] = [d for d in backup.get("destinations", data["destinations"])]
                # Just reload from backup
                dest_path = DEAL_HUNTER_PATH / "config" / "destinations_full_backup.json"
                if dest_path.exists():
                    import shutil
                    shutil.copy2(dest_path, DEAL_HUNTER_PATH / "config" / "destinations.json")

            # DM the owner
            owner = await bot.fetch_user(OWNER_ID)

            # Send summary first
            sorted_deals = sorted(deals, key=lambda d: d.get("score", 0), reverse=True)
            summary_embed = _build_summary_embed(sorted_deals, elapsed)
            await owner.send(embed=summary_embed)

            # Send top deals as individual embeds
            for i, deal in enumerate(sorted_deals[:10], 1):
                embed = _build_deal_embed(deal, i)
                await owner.send(embed=embed)
                await asyncio.sleep(0.5)  # Rate limit safety

            if not deals:
                await owner.send("No deals found above the score threshold. Try lowering it or expanding destinations.")

            logger.info("Deal scan complete: %d deals sent to owner in %.1f min", len(deals), elapsed)

        except Exception as exc:
            logger.error("Deal scan failed: %s", exc, exc_info=True)
            try:
                owner = await bot.fetch_user(OWNER_ID)
                await owner.send(f"\u274c **Deal scan failed:**\n```{exc}```")
            except Exception:
                pass

    @bot.tree.command(name="dealstatus", description="Check if a deal scan is running")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    async def dealstatus_cmd(interaction: discord.Interaction):
        db_exists = DEAL_DB_PATH.exists()
        if db_exists:
            import sqlite3
            conn = sqlite3.connect(str(DEAL_DB_PATH))
            try:
                deals = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
                runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                last_run = conn.execute("SELECT finished_at FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            finally:
                conn.close()
            last = last_run[0] if last_run else "Never"
            await interaction.response.send_message(
                f"\U0001f4ca **Deal Hunter Status**\n"
                f"Database: {deals} deals across {runs} runs\n"
                f"Last run: {last}",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "\U0001f4ca No deal data yet. Run `/deals` to start a scan.",
                ephemeral=True,
            )

    logger.info("Deal commands registered")
