"""Deal Hunter commands — run scans, DM results, manage watches, track preferences."""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from config import OWNER_ID, AUTHORIZED_IDS

logger = logging.getLogger("zrbot.deals")

DEAL_HUNTER_PATH = Path(os.getenv("DEAL_HUNTER_PATH", Path(__file__).resolve().parent.parent.parent / "weekendmaxxing"))
DEAL_DB_PATH = DEAL_HUNTER_PATH / "data" / "deals.db"


def _ensure_deal_hunter_on_path():
    path_str = str(DEAL_HUNTER_PATH)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)


def _fresh_imports():
    """Clear cached weekendmaxxing modules so we always get fresh state."""
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith(("orchestrator", "agents", "scrapers", "utils.llm",
                                "utils.json_extractor", "utils.rate_limiter",
                                "config.settings", "notifications", "apis")):
            del sys.modules[mod_name]


# ── Embed builders ──────────────────────────────────────────────────────────


def _score_color(score: float) -> int:
    if score >= 70:
        return 0x22C55E
    if score >= 55:
        return 0xEAB308
    return 0xEF4444


def _confidence_label(conf: str) -> str:
    return {"high": "\U0001f7e2 verified", "medium": "\U0001f7e1 ~estimated", "low": "\U0001f534 unverified"}.get(conf, "\U0001f534 unverified")


def _build_deal_embed(deal: dict, rank: int) -> discord.Embed:
    is_train = deal.get("transport_type") == "train"
    icon = "\U0001f682" if is_train else "\u2708\ufe0f"
    ttype = "Train" if is_train else "Flight"
    dest = deal.get("destination", "?")
    score = deal.get("score", 0)
    price = deal.get("price_usd", 0)
    carrier = deal.get("airline", deal.get("operator", "?"))
    layovers = deal.get("layovers", 0)
    stops = "Direct" if is_train and layovers == 0 else ("Nonstop" if layovers == 0 else f"{layovers} stop")
    hotel = deal.get("best_stay", {})
    total = deal.get("estimated_total", deal.get("total_trip_cost", 0))
    extras = deal.get("estimated_extras", 0)
    hours = deal.get("hours_at_destination", 0)
    conf = deal.get("price_confidence", "low")

    embed = discord.Embed(
        title=f"{icon} #{rank} \u2014 {dest}",
        description=f"**Score: {score}/100** | {ttype} via {carrier} | {_confidence_label(conf)}",
        color=_score_color(score),
    )
    embed.add_field(name=f"{icon} Transport", value=f"**${price:.0f}** RT | {stops}\n{carrier}", inline=True)
    embed.add_field(name="\U0001f3e8 Hotel", value=f"**{hotel.get('name', 'N/A')}**\n${hotel.get('total_price', 0):.0f} | {''.join(['\u2b50'] * int(hotel.get('rating', 0)))}", inline=True)
    embed.add_field(name="\U0001f4b0 Est. Total", value=f"**${total:.0f}**\n(+${extras:.0f} food/transit)", inline=True)
    embed.add_field(name="\U0001f4c5 Dates", value=f"{deal.get('outbound_date', '')} \u2192 {deal.get('return_date', '')}", inline=True)
    embed.add_field(name="\u23f0 Schedule", value=f"Leave {deal.get('outbound_depart', '?')}, arrive {deal.get('outbound_arrive', '?')}\nReturn {deal.get('return_depart', '?')}, home {deal.get('return_arrive', '?')}", inline=True)
    embed.add_field(name="\u231b Time", value=f"{hours:.0f}h", inline=True)

    drop = deal.get("price_drop")
    if drop and drop > 0:
        embed.set_footer(text=f"\u2b07\ufe0f Price dropped ${drop:.0f} since last scan!")

    # Preference tag
    pref = deal.get("preference_boost", 0)
    if pref >= 3:
        embed.description += " | \u2764\ufe0f Your type of deal"

    recs = deal.get("recommendations", "")
    if recs and len(recs) > 20:
        embed.add_field(name="\U0001f4dd Recommendations", value=recs[:1000] + ("..." if len(recs) > 1000 else ""), inline=False)

    return embed


def _build_summary_embed(deals: list[dict], elapsed_min: float) -> discord.Embed:
    if not deals:
        return discord.Embed(title="\u2708\ufe0f Weekend Deal Hunter", description="No deals found.", color=0x888888)
    trains = sum(1 for d in deals if d.get("transport_type") == "train")
    flights = len(deals) - trains
    cheapest = min(deals, key=lambda d: d.get("estimated_total", 9999))
    best = max(deals, key=lambda d: d.get("score", 0))
    embed = discord.Embed(
        title="\u2708\ufe0f Weekend Deal Hunter \u2014 Scan Complete",
        description=f"Found **{len(deals)} deals** ({flights} flights, {trains} trains) in {elapsed_min:.1f} min",
        color=0x22C55E, timestamp=datetime.now(),
    )
    embed.add_field(name="\U0001f3c6 Best Score", value=f"{best.get('destination', '?')} ({best.get('score', 0)}/100)", inline=True)
    embed.add_field(name="\U0001f4b5 Cheapest", value=f"{cheapest.get('destination', '?')} (${cheapest.get('estimated_total', 0):.0f})", inline=True)
    embed.set_footer(text="Origin: PHL | Powered by Ollama llama3.1:8b")
    return embed


# ── Reaction buttons view ───────────────────────────────────────────────────


class DealReactionView(discord.ui.View):
    """Persistent reaction buttons for deal DMs."""

    def __init__(self, deal_data: str):
        super().__init__(timeout=None)
        self.deal_data = deal_data  # JSON string of the deal dict

    @discord.ui.button(label="Interested", emoji="\U0001f44d", style=discord.ButtonStyle.success, custom_id="deal_thumbs_up")
    async def thumbs_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        _ensure_deal_hunter_on_path()
        _fresh_imports()
        from orchestrator.preference_engine import record_feedback, rebuild_profile
        deal = json.loads(self.deal_data)
        record_feedback(deal, "thumbs_up")
        rebuild_profile()
        await interaction.response.send_message("\U0001f44d Noted! I'll prioritize similar deals.", ephemeral=True)

    @discord.ui.button(label="Not for me", emoji="\U0001f44e", style=discord.ButtonStyle.danger, custom_id="deal_thumbs_down")
    async def thumbs_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        _ensure_deal_hunter_on_path()
        _fresh_imports()
        from orchestrator.preference_engine import record_feedback, rebuild_profile
        deal = json.loads(self.deal_data)
        record_feedback(deal, "thumbs_down")
        rebuild_profile()
        await interaction.response.send_message("\U0001f44e Got it. I'll deprioritize this type of deal.", ephemeral=True)

    @discord.ui.button(label="Booked it!", emoji="\U0001f389", style=discord.ButtonStyle.primary, custom_id="deal_booked")
    async def booked(self, interaction: discord.Interaction, button: discord.ui.Button):
        _ensure_deal_hunter_on_path()
        _fresh_imports()
        from orchestrator.preference_engine import record_feedback, rebuild_profile
        deal = json.loads(self.deal_data)
        record_feedback(deal, "booked")
        rebuild_profile()
        await interaction.response.send_message("\U0001f389 Awesome! Booking confirmed. I'll learn from this.", ephemeral=True)


# ── Main setup ──────────────────────────────────────────────────────────────


def setup(bot: commands.Bot):
    """Register deal commands with the bot."""

    # Register persistent view so buttons work after restart
    bot.add_view(DealReactionView("{}"))

    # ── /deals ──────────────────────────────────────────────────────────

    @bot.tree.command(name="deals", description="Run a weekend deal scan and DM results")
    @app_commands.describe(destinations="Comma-separated cities to search (blank = all)")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    async def deals_cmd(interaction: discord.Interaction, destinations: str = ""):
        if interaction.user.id not in AUTHORIZED_IDS:
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return

        await interaction.response.send_message(
            "\U0001f50d **Starting deal scan...** This takes 15-30 minutes.\nI'll DM you the results when done.",
            ephemeral=True,
        )

        try:
            import time
            start = time.time()

            _ensure_deal_hunter_on_path()
            _fresh_imports()

            if destinations.strip():
                dest_path = DEAL_HUNTER_PATH / "config" / "destinations.json"
                data = json.loads(dest_path.read_text(encoding="utf-8"))
                original_dests = data["destinations"]
                target_cities = [c.strip() for c in destinations.split(",")]
                filtered = [d for d in data["destinations"]
                            if any(t.lower() in d["city"].lower() for t in target_cities)]
                if filtered:
                    data["destinations"] = filtered
                    dest_path.write_text(json.dumps(data, indent=2))

            from orchestrator.pipeline import run_pipeline
            from orchestrator.state_manager import get_recent_deals

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, run_pipeline)
            deals = get_recent_deals(limit=30)

            # Restore destinations
            if destinations.strip() and filtered:
                data["destinations"] = original_dests
                dest_path.write_text(json.dumps(data, indent=2))

            elapsed = (time.time() - start) / 60
            owner = await bot.fetch_user(OWNER_ID)

            sorted_deals = sorted(deals, key=lambda d: d.get("score", 0), reverse=True)
            await owner.send(embed=_build_summary_embed(sorted_deals, elapsed))

            for i, deal in enumerate(sorted_deals[:10], 1):
                embed = _build_deal_embed(deal, i)
                deal_json = json.dumps(deal, default=str)
                view = DealReactionView(deal_json)
                await owner.send(embed=embed, view=view)
                await asyncio.sleep(0.5)

            if not deals:
                await owner.send("No deals found. Try lowering the threshold or expanding destinations.")

        except Exception as exc:
            logger.error("Deal scan failed: %s", exc, exc_info=True)
            try:
                owner = await bot.fetch_user(OWNER_ID)
                await owner.send(f"\u274c **Deal scan failed:**\n```{exc}```")
            except Exception:
                pass

    # ── /dealstatus ─────────────────────────────────────────────────────

    @bot.tree.command(name="dealstatus", description="Deal hunter status and stats")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    async def dealstatus_cmd(interaction: discord.Interaction):
        _ensure_deal_hunter_on_path()
        _fresh_imports()

        lines = ["\U0001f4ca **Deal Hunter Status**\n"]

        if DEAL_DB_PATH.exists():
            import sqlite3
            conn = sqlite3.connect(str(DEAL_DB_PATH))
            try:
                deal_count = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
                run_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                last = conn.execute("SELECT finished_at FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            finally:
                conn.close()
            lines.append(f"**Deals:** {deal_count} across {run_count} runs")
            lines.append(f"**Last run:** {last[0] if last else 'Never'}")
        else:
            lines.append("No deal data yet. Run `/deals` to start.")

        try:
            from orchestrator.quota_manager import get_daily_usage, get_monthly_usage
            lines.append(f"\n**API Quota (today/month):**")
            for api in ["amadeus", "kiwi", "serpapi"]:
                lines.append(f"  {api}: {get_daily_usage(api)}/day, {get_monthly_usage(api)}/month")
        except Exception:
            pass

        try:
            from orchestrator.watch_manager import get_watch_count, get_watches
            wc = get_watch_count()
            lines.append(f"\n**Watches:** {wc}/5")
            for w in get_watches():
                lines.append(f"  \U0001f440 {w['destination']} (last: ${w['last_price']:.0f})" if w.get('last_price') else f"  \U0001f440 {w['destination']}")
        except Exception:
            pass

        try:
            from orchestrator.preference_engine import get_feedback_count
            lines.append(f"\n**Feedback:** {get_feedback_count()} reactions recorded")
        except Exception:
            pass

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ── /watch ──────────────────────────────────────────────────────────

    @bot.tree.command(name="watch", description="Add a city to your deal watch list (max 5)")
    @app_commands.describe(city="City name to watch", date="Specific weekend date (optional, YYYY-MM-DD)")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    async def watch_cmd(interaction: discord.Interaction, city: str, date: str = ""):
        if interaction.user.id not in AUTHORIZED_IDS:
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        _ensure_deal_hunter_on_path()
        _fresh_imports()
        from orchestrator.watch_manager import add_watch
        success = add_watch(city, date if date else None)
        if success:
            await interaction.response.send_message(f"\U0001f440 Now watching **{city}**" + (f" for {date}" if date else "") + ". I'll alert you on price drops.", ephemeral=True)
        else:
            await interaction.response.send_message(f"\u274c Couldn't add watch. Either at max (5) or already watching {city}.", ephemeral=True)

    # ── /unwatch ────────────────────────────────────────────────────────

    @bot.tree.command(name="unwatch", description="Remove a city from your watch list")
    @app_commands.describe(city="City name to stop watching")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    async def unwatch_cmd(interaction: discord.Interaction, city: str):
        if interaction.user.id not in AUTHORIZED_IDS:
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        _ensure_deal_hunter_on_path()
        _fresh_imports()
        from orchestrator.watch_manager import remove_watch
        success = remove_watch(city)
        if success:
            await interaction.response.send_message(f"\u2705 Stopped watching **{city}**.", ephemeral=True)
        else:
            await interaction.response.send_message(f"\u274c **{city}** is not on your watch list.", ephemeral=True)

    # ── /watchlist ──────────────────────────────────────────────────────

    @bot.tree.command(name="watchlist", description="Show your active deal watches")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    async def watchlist_cmd(interaction: discord.Interaction):
        _ensure_deal_hunter_on_path()
        _fresh_imports()
        from orchestrator.watch_manager import get_watches
        watches = get_watches()
        if not watches:
            await interaction.response.send_message("\U0001f440 No active watches. Use `/watch <city>` to add one.", ephemeral=True)
            return
        lines = ["\U0001f440 **Active Watches:**\n"]
        for w in watches:
            price_str = f" | Last: ${w['last_price']:.0f}" if w.get("last_price") else ""
            date_str = f" ({w['specific_date']})" if w.get("specific_date") else ""
            checked = f" | Checked: {w['last_checked_at'][:16]}" if w.get("last_checked_at") else ""
            lines.append(f"  **{w['destination']}**{date_str}{price_str}{checked}")
        lines.append(f"\n{len(watches)}/5 watch slots used.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ── /preferences ────────────────────────────────────────────────────

    @bot.tree.command(name="preferences", description="Show your learned deal preferences")
    @app_commands.describe(action="'reset' to wipe preferences, blank to view")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    async def preferences_cmd(interaction: discord.Interaction, action: str = ""):
        if interaction.user.id not in AUTHORIZED_IDS:
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        _ensure_deal_hunter_on_path()
        _fresh_imports()
        from orchestrator.preference_engine import get_profile_summary, get_feedback_count, init_preferences_db
        import sqlite3

        if action.lower() == "reset":
            init_preferences_db()
            from orchestrator.preference_engine import PREFS_DB_PATH
            conn = sqlite3.connect(PREFS_DB_PATH)
            try:
                conn.execute("DELETE FROM feedback")
                conn.execute("DELETE FROM preference_profile")
                conn.commit()
            finally:
                conn.close()
            await interaction.response.send_message("\U0001f5d1\ufe0f Preferences reset. Starting fresh.", ephemeral=True)
            return

        count = get_feedback_count()
        if count == 0:
            await interaction.response.send_message(
                "\U0001f9e0 **No preferences yet.**\n"
                "React to deal DMs with \U0001f44d/\U0001f44e or use the Booked button to start teaching me what you like.",
                ephemeral=True,
            )
            return

        summary = get_profile_summary()
        lines = [f"\U0001f9e0 **Your Deal Preferences** ({count} reactions)\n"]

        top_dests = summary.get("top_destinations", [])
        if top_dests:
            lines.append("**Favorite destinations:**")
            for d in top_dests[:5]:
                lines.append(f"  \u2764\ufe0f {d}")

        transport = summary.get("top_transport")
        if transport:
            lines.append(f"\n**Preferred transport:** {transport}")

        blacklist = summary.get("blacklisted_airlines", [])
        if blacklist:
            lines.append(f"\n**Avoided airlines:** {', '.join(blacklist)}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    logger.info("Deal commands registered (deals, dealstatus, watch, unwatch, watchlist, preferences)")
