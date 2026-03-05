"""Context menu command for converting a message's address to shipping CSV."""

import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from utils.address_parser import parse_address, validate_address_usps
from config import (
    AUTHORIZED_IDS,
    SHIP_FROM_NAME, SHIP_FROM_STREET, SHIP_FROM_STREET2,
    SHIP_FROM_CITY, SHIP_FROM_STATE, SHIP_FROM_ZIP,
    USPS_CONSUMER_KEY, USPS_CONSUMER_SECRET,
)

_pending_addresses: dict = {}
_TTL_SECONDS = 300


def _cleanup(modal_id: str) -> None:
    _pending_addresses.pop(modal_id, None)


def _store_parsed(modal_id: str, parsed: dict) -> None:
    old = _pending_addresses.get(modal_id)
    if old and "handle" in old:
        old["handle"].cancel()
    loop = asyncio.get_event_loop()
    handle = loop.call_later(_TTL_SECONDS, _cleanup, modal_id)
    _pending_addresses[modal_id] = {"parsed": parsed, "handle": handle}


def _pop_parsed(modal_id: str) -> dict | None:
    entry = _pending_addresses.pop(modal_id, None)
    if entry is None:
        return None
    if "handle" in entry:
        entry["handle"].cancel()
    return entry["parsed"]


def _escape_csv_field(value: str) -> str:
    return f'"{value}"' if ',' in value else value


def _is_authorized(interaction: discord.Interaction) -> bool:
    return interaction.user.id in AUTHORIZED_IDS


def setup(bot: commands.Bot):
    @bot.tree.context_menu(name="Convert Address to CSV")
    async def convert_address_to_csv(
        interaction: discord.Interaction, message: discord.Message
    ):
        if not _is_authorized(interaction):
            return await interaction.response.send_message(
                "You are not authorized.", ephemeral=True
            )

        content = message.content
        if not content or not content.strip():
            return await interaction.response.send_message(
                "That message has no text content to parse.", ephemeral=True
            )

        parsed = parse_address(content)
        if "error" in parsed:
            return await interaction.response.send_message(
                f"Could not parse address: {parsed['error']}", ephemeral=True
            )

        if USPS_CONSUMER_KEY and USPS_CONSUMER_SECRET:
            parsed = await validate_address_usps(
                parsed, USPS_CONSUMER_KEY, USPS_CONSUMER_SECRET
            )

        modal_id = f"address_csv_modal_{interaction.id}"
        _store_parsed(modal_id, parsed)

        modal = discord.ui.Modal(title="Generate Shipping CSV", custom_id=modal_id)
        modal.add_item(
            discord.ui.TextInput(
                label="Package Weight (lbs)",
                style=discord.TextStyle.short,
                required=True,
                placeholder="e.g. 2.5",
                custom_id="weight",
            )
        )

        async def on_modal_submit(modal_interaction: discord.Interaction):
            await _handle_modal_submit(modal_interaction, modal_id)

        modal.on_submit = on_modal_submit
        await interaction.response.send_modal(modal)

    async def _handle_modal_submit(
        interaction: discord.Interaction, modal_id: str
    ):
        to_addr = _pop_parsed(modal_id)
        if to_addr is None:
            return await interaction.response.send_message(
                "Session expired. Please try again.", ephemeral=True
            )

        fields = {}
        for row in interaction.data.get("components", []):
            for comp in row.get("components", []):
                fields[comp["custom_id"]] = comp.get("value", "")

        weight_raw = fields.get("weight", "").strip()
        try:
            weight = float(weight_raw)
            if weight <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return await interaction.response.send_message(
                "Invalid weight. Please enter a positive number (e.g. 2.5).",
                ephemeral=True,
            )

        weight_str = f"{weight:g}"

        csv_fields = [
            weight_str,
            SHIP_FROM_NAME, SHIP_FROM_STREET, SHIP_FROM_STREET2,
            SHIP_FROM_CITY, SHIP_FROM_STATE, SHIP_FROM_ZIP,
            to_addr["name"], to_addr["street"], to_addr.get("street2", ""),
            to_addr["city"], to_addr["state"], to_addr["zip"],
        ]
        csv_line = ",".join(_escape_csv_field(f) for f in csv_fields)

        from_lines = [SHIP_FROM_NAME, SHIP_FROM_STREET]
        if SHIP_FROM_STREET2:
            from_lines.append(SHIP_FROM_STREET2)
        from_lines.append(f"{SHIP_FROM_CITY}, {SHIP_FROM_STATE} {SHIP_FROM_ZIP}")

        to_lines = [to_addr["name"]] if to_addr["name"] else []
        to_lines.append(to_addr["street"])
        if to_addr.get("street2"):
            to_lines.append(to_addr["street2"])
        to_lines.append(f"{to_addr['city']}, {to_addr['state']} {to_addr['zip']}")

        embed = discord.Embed(
            title="\U0001f4e6 Shipping CSV Generated",
            color=0x57F287,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="From", value="\n".join(from_lines), inline=True)
        embed.add_field(name="To", value="\n".join(to_lines), inline=True)
        embed.add_field(name="Weight", value=f"{weight_str} lbs", inline=True)

        await interaction.response.send_message(
            f"```\n{csv_line}\n```", embed=embed, ephemeral=True,
        )
