"""USPS package tracking monitor with polling and Discord notifications."""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord.ext import tasks

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OWNER_ID = 745694160002089130

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TRACKING_FILE = DATA_DIR / "tracking.json"

USPS_TOKEN_URL = "https://api.usps.com/oauth2/v3/token"
USPS_TRACKING_URL = "https://apis.usps.com/tracking/v3r2/tracking"
BATCH_SIZE = 35  # USPS max per request

BASE_POLL_MINUTES = 30
MIN_POLL_MINUTES = 15
MAX_POLL_MINUTES = 60
PRIORITY_POLL_MINUTES = 10  # Faster polling for high-priority packages
MAX_TRACKING_DAYS = 60  # Auto-remove packages older than this

# Packages near final delivery — polled more frequently
HIGH_PRIORITY_CATEGORIES = {"Out for Delivery", "Delivery Attempt", "Available for Pickup"}

# Packages with no movement — polled less frequently
LOW_PRIORITY_CATEGORIES = {"Pre-Shipment", "Shipping Label Created", "USPS Awaiting Item", "Waiting for USPS"}
LOW_PRIORITY_POLL_MINUTES = 60

# USPS logo thumbnail — swap between the two logos here:
# "USPS-Logo.png" (icon only) or "USPS_Logo_Text.webp" (icon + text)
USPS_LOGO_URL = "https://raw.githubusercontent.com/bryanygan/zrbot/main/assets/USPS_Logo_Text.webp"

# Status category → (color, emoji, short label)
STATUS_CONFIG = {
    "Delivered":           (0x57F287, "\U0001f4ec", "Delivered"),           # green, 📬
    "Out for Delivery":    (0x3498DB, "\U0001f69a", "Out for Delivery"),    # blue, 🚚
    "On the Way":          (0x5865F2, "\U0001f4e6", "In Transit"),          # blurple, 📦
    "In Transit":          (0x5865F2, "\U0001f4e6", "In Transit"),
    "International Transit":(0x9B59B6, "\U0001f30d", "International Transit"),  # purple, 🌍
    "USPS Awaiting Item":  (0x95A5A6, "\U0001f3f7\ufe0f", "Awaiting Pickup"),  # gray, 🏷️
    "Shipping Label Created": (0x95A5A6, "\U0001f3f7\ufe0f", "Label Created"),
    "Alert":               (0xED4245, "\u26a0\ufe0f", "Alert"),              # red, ⚠️
    "Return to Sender":    (0xED4245, "\u21a9\ufe0f", "Returned"),           # red, ↩️
    "Delivery Attempt":    (0xE67E22, "\U0001f4cb", "Delivery Attempted"),  # orange, 📋
    "Available for Pickup":(0xE67E22, "\U0001f4ee", "Ready for Pickup"),    # orange, 📮
    "Pre-Shipment":        (0x95A5A6, "\U0001f4dd", "Pre-Shipment"),       # gray, 📝
    "Waiting for USPS":    (0x95A5A6, "\u23f3", "Waiting for USPS"),     # gray, ⏳
}

DEFAULT_STATUS_CONFIG = (0x95A5A6, "\U0001f4e6", "Unknown Status")  # gray, 📦

# Categories that should trigger a DM notification
DM_TRIGGER_CATEGORIES = {
    "Delivered", "Out for Delivery",
    "Alert", "Return to Sender", "Delivery Attempt", "Available for Pickup",
}

# Categories that are terminal (auto-remove after notification)
TERMINAL_CATEGORIES = {"Delivered", "Return to Sender"}


# ---------------------------------------------------------------------------
# Token management (shared with address_parser)
# ---------------------------------------------------------------------------

_usps_token: str | None = None
_usps_token_expires: float = 0


async def _get_usps_token(consumer_key: str, consumer_secret: str) -> str:
    global _usps_token, _usps_token_expires

    if _usps_token and time.time() < _usps_token_expires:
        return _usps_token

    async with aiohttp.ClientSession() as session:
        async with session.post(USPS_TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": consumer_key,
            "client_secret": consumer_secret,
        }) as resp:
            if resp.status != 200:
                raise RuntimeError(f"USPS OAuth failed ({resp.status})")
            body = await resp.json()

    _usps_token = body["access_token"]
    _usps_token_expires = time.time() + body.get("expires_in", 28800) - 300
    return _usps_token


# ---------------------------------------------------------------------------
# Persistent storage
# ---------------------------------------------------------------------------

def _load_tracking() -> dict:
    if TRACKING_FILE.exists():
        try:
            return json.loads(TRACKING_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Corrupt tracking file, starting fresh")
    return {}


def _save_tracking(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TRACKING_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(TRACKING_FILE)


# ---------------------------------------------------------------------------
# USPS API
# ---------------------------------------------------------------------------

async def _fetch_tracking_batch(
    tracking_numbers: list[str],
    consumer_key: str,
    consumer_secret: str,
) -> list[dict]:
    """Fetch tracking info for up to 35 tracking numbers."""
    token = await _get_usps_token(consumer_key, consumer_secret)
    payload = [{"trackingNumber": tn} for tn in tracking_numbers]

    async with aiohttp.ClientSession() as session:
        async with session.post(
            USPS_TRACKING_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
        ) as resp:
            if resp.status == 429:
                retry_after = int(resp.headers.get("Retry-After", "60"))
                logger.warning("USPS rate limited, retry after %ds", retry_after)
                raise RateLimitError(retry_after)
            if resp.status != 200:
                # 207 multi-status also possible — body is still valid JSON array
                pass
            body = await resp.json()

    # Normalize: single-item responses may not be a list
    if isinstance(body, dict):
        # Error response for the whole request
        if "error" in body:
            logger.warning("USPS batch error: %s", body["error"].get("message"))
            return []
        return [body]
    return body


class RateLimitError(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Rate limited for {retry_after}s")


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def _clean_mail_class(raw: str) -> str:
    """Strip HTML tags from mail class (e.g. 'Priority Mail<SUP>&reg;</SUP>')."""
    import re
    cleaned = re.sub(r'<[^>]+>', '', raw)
    cleaned = cleaned.replace('&reg;', '\u00ae').replace('&#153;', '\u2122')
    cleaned = cleaned.replace('&amp;', '&')
    return cleaned.strip()


def _format_location(event: dict) -> str:
    city = event.get("eventCity", "")
    state = event.get("eventState", "")
    zip_code = event.get("eventZIPCode", "")
    parts = []
    if city:
        parts.append(city.title())
    if state:
        parts.append(state)
    if zip_code:
        parts.append(zip_code)
    if not parts:
        return "Unknown Location"
    # Format: "City, ST 12345"
    if len(parts) >= 2 and parts[-1].isdigit():
        return f"{', '.join(parts[:-1])} {parts[-1]}"
    return ", ".join(parts)


def _format_event_time(event: dict) -> str:
    ts = event.get("eventTimestamp", "")
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts)
        return f"<t:{int(dt.timestamp())}:f>"
    except (ValueError, TypeError):
        return ts


USPS_TRACKING_PAGE = "https://tools.usps.com/go/TrackConfirmAction?tLabels="

# Progress bar steps
_PROGRESS_STEPS = ["Label Created", "In Transit", "Out for Delivery", "Delivered"]
_PROGRESS_CATEGORY_MAP = {
    "Pre-Shipment": 0,
    "Shipping Label Created": 0,
    "USPS Awaiting Item": 0,
    "Waiting for USPS": 0,
    "On the Way": 1,
    "In Transit": 1,
    "International Transit": 1,
    "Out for Delivery": 2,
    "Delivery Attempt": 2,
    "Available for Pickup": 2,
    "Delivered": 3,
}


def _build_progress_bar(category: str) -> str:
    """Build a visual progress indicator for the package journey."""
    step = _PROGRESS_CATEGORY_MAP.get(category)
    if step is None:
        return ""
    parts = []
    for i, label in enumerate(_PROGRESS_STEPS):
        if i < step:
            parts.append(f"\u2705 ~~{label}~~")
        elif i == step:
            parts.append(f"\u25b6\ufe0f **{label}**")
        else:
            parts.append(f"\u2b1c {label}")
    return " \u2192 ".join(parts)


def _calculate_days_in_transit(events: list[dict]) -> int | None:
    """Calculate days since the first tracking event."""
    if not events:
        return None
    # Find the earliest event
    earliest_ts = None
    for ev in events:
        ts = ev.get("eventTimestamp", "")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
            if earliest_ts is None or dt < earliest_ts:
                earliest_ts = dt
        except (ValueError, TypeError):
            continue
    if earliest_ts is None:
        return None
    delta = datetime.now(timezone.utc) - earliest_ts.astimezone(timezone.utc)
    return max(0, delta.days)


def _build_eta_countdown(delivery_info: dict, category: str) -> str:
    """Build an ETA countdown string (Windows-safe, no %-d)."""
    if category in TERMINAL_CATEGORIES:
        return ""
    exp_date = delivery_info.get("expectedDeliveryDate") or delivery_info.get("predictedDeliveryDate")
    if not exp_date:
        return ""
    try:
        dt = datetime.strptime(exp_date, "%Y-%m-%d")
        today = datetime.now().date()
        days_until = (dt.date() - today).days
        if days_until < 0:
            return "\u26a0\ufe0f **Past expected delivery**"
        elif days_until == 0:
            return "\U0001f389 **Arriving today!**"
        elif days_until == 1:
            return "\U0001f4e8 **Arriving tomorrow!**"
        elif days_until <= 3:
            return f"\U0001f4e6 **{days_until} days away**"
    except ValueError:
        pass
    return ""


def build_tracking_view(tracking_number: str) -> discord.ui.View:
    """Build a persistent button row for a tracking embed."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="Track on USPS",
        style=discord.ButtonStyle.link,
        url=f"{USPS_TRACKING_PAGE}{tracking_number}",
        emoji="\U0001f517",
    ))
    view.add_item(discord.ui.Button(
        custom_id=f"tracking_details_{tracking_number}",
        label="Show Details",
        style=discord.ButtonStyle.secondary,
        emoji="\U0001f4cb",
    ))
    view.add_item(discord.ui.Button(
        custom_id=f"tracking_copy_{tracking_number}",
        label="Copy #",
        style=discord.ButtonStyle.secondary,
        emoji="\U0001f4ce",
    ))
    return view


def build_tracking_embed(
    tracking_number: str,
    data: dict,
    user_id: int | None = None,
    *,
    show_history: bool = True,
    max_events: int = 2,
    logo_url: str | None = None,
) -> discord.Embed:
    """Build a rich embed for a tracking update."""
    category = data.get("statusCategory", "Unknown")
    color, emoji, label = STATUS_CONFIG.get(category, DEFAULT_STATUS_CONFIG)

    summary = data.get("statusSummary", "")
    mail_class = _clean_mail_class(data.get("mailClass", ""))
    events = data.get("trackingEvents", [])
    delivery_info = data.get("deliveryDateExpectation", {})

    # Build description with progress bar
    desc_parts = []
    if summary:
        desc_parts.append(summary)
    progress = _build_progress_bar(category)
    if progress:
        desc_parts.append(f"\n{progress}")

    embed = discord.Embed(
        title=f"{emoji}  {label}",
        description="\n".join(desc_parts) if desc_parts else None,
        color=color,
        url=f"{USPS_TRACKING_PAGE}{tracking_number}",
        timestamp=datetime.now(timezone.utc),
    )

    # USPS logo thumbnail
    if logo_url:
        embed.set_thumbnail(url=logo_url)

    # Header fields
    embed.add_field(name="Tracking #", value=f"[`{tracking_number}`]({USPS_TRACKING_PAGE}{tracking_number})", inline=True)
    if mail_class:
        embed.add_field(name="Service", value=mail_class, inline=True)
    if user_id:
        embed.add_field(name="Recipient", value=f"<@{user_id}>", inline=True)

    # Origin & Destination (city, state only for privacy)
    origin_city = data.get("originCity", "")
    origin_state = data.get("originState", "")
    dest_city = data.get("destinationCity", "")
    dest_state = data.get("destinationState", "")

    origin = f"{origin_city.title()}, {origin_state}" if origin_city and origin_state else ""
    dest = f"{dest_city.title()}, {dest_state}" if dest_city and dest_state else ""

    if origin or dest:
        route = f"{origin or 'Unknown'} \u2192 {dest or 'Unknown'}"
        embed.add_field(name="Route", value=route, inline=True)

    # Expected delivery date + window + countdown
    exp_date = delivery_info.get("expectedDeliveryDate") or delivery_info.get("predictedDeliveryDate")
    if exp_date and category not in TERMINAL_CATEGORIES:
        try:
            dt = datetime.strptime(exp_date, "%Y-%m-%d")
            delivery_text = f"<t:{int(dt.timestamp())}:D>"
            delivery_time = delivery_info.get("expectedDeliveryTime") or delivery_info.get("predictedDeliveryEndTime") or ""
            if delivery_time:
                delivery_text += f"\nby {delivery_time}"
            countdown = _build_eta_countdown(delivery_info, category)
            if countdown:
                delivery_text += f"\n{countdown}"
            embed.add_field(name="Expected Delivery", value=delivery_text, inline=True)
        except ValueError:
            embed.add_field(name="Expected Delivery", value=exp_date, inline=True)

    # Latest location
    if events:
        latest = events[0]
        loc = _format_location(latest)
        embed.add_field(name="Current Location", value=loc, inline=True)

    # Days in transit
    days = _calculate_days_in_transit(events)
    if days is not None and category not in TERMINAL_CATEGORIES:
        day_word = "day" if days == 1 else "days"
        embed.add_field(name="In Transit", value=f"{days} {day_word}", inline=True)

    # Tracking history
    if show_history and events:
        history_lines = []
        for ev in events[:max_events]:
            ev_time = _format_event_time(ev)
            ev_type = ev.get("eventType", "Unknown")
            ev_loc = _format_location(ev)
            if ev_time:
                history_lines.append(f"{ev_time}\n{ev_type} \u2014 {ev_loc}")
            else:
                history_lines.append(f"{ev_type} \u2014 {ev_loc}")

        # Split into chunks if too long for one field (1024 char limit)
        history_text = "\n\n".join(history_lines)
        if len(history_text) <= 1024:
            embed.add_field(
                name="Tracking History",
                value=history_text,
                inline=False,
            )
        else:
            # Truncate to fit
            truncated = []
            total = 0
            for line in history_lines:
                if total + len(line) + 2 > 1000:
                    break
                truncated.append(line)
                total += len(line) + 2
            embed.add_field(
                name="Tracking History",
                value="\n\n".join(truncated) + f"\n*...and {len(events) - len(truncated)} more events*",
                inline=False,
            )

    if category == "Delivered":
        embed.add_field(
            name="Thank You!",
            value="Please leave a vouch if your package arrived safe! If you have any questions/concerns about the package, please feel free to reach out!",
            inline=False,
        )

    embed.set_footer(text="USPS Tracking \u2022 Last checked")

    # Discord embed limit is 6000 chars total — trim history if over
    total = len(embed.title or "") + len(embed.description or "")
    total += sum(len(f.name) + len(f.value) for f in embed.fields)
    total += len(embed.footer.text or "") if embed.footer else 0
    if total > 5900:
        # Remove tracking history field(s) and add a link instead
        embed.remove_field(next(
            (i for i, f in enumerate(embed.fields) if f.name == "Tracking History"), -1
        ))
        embed.add_field(
            name="Tracking History",
            value=f"[View full history on USPS]({USPS_TRACKING_PAGE}{tracking_number})",
            inline=False,
        )

    return embed


# ---------------------------------------------------------------------------
# Core monitor
# ---------------------------------------------------------------------------

class TrackingMonitor:
    """Manages tracked packages and polls USPS for updates."""

    def __init__(self, bot: discord.Client, consumer_key: str, consumer_secret: str):
        self.bot = bot
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.tracking_data = _load_tracking()
        self._lock = asyncio.Lock()
        self._last_full_poll: float = 0
        self._last_low_poll: float = 0
        self._last_error_dm: float = 0
        self._poll_loop.change_interval(minutes=BASE_POLL_MINUTES)

    async def _dm_owner_error(self, title: str, detail: str):
        """Send an error notification DM to the bot owner (rate-limited)."""
        now = time.time()
        if now - self._last_error_dm < 10:
            return
        try:
            owner = await self.bot.fetch_user(OWNER_ID)
            embed = discord.Embed(
                title=f"\u26a0\ufe0f {title}",
                description=detail[:4000],
                color=0xED4245,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(text="ZR Bot Error Notification")
            await owner.send(embed=embed)
            self._last_error_dm = now
        except Exception as exc:
            logger.error("Failed to DM owner about error: %s", exc)

    def start(self):
        if not self._poll_loop.is_running():
            self._poll_loop.start()
            logger.info("Tracking monitor started (interval: %d min)", self._poll_interval_minutes)

    def stop(self):
        if self._poll_loop.is_running():
            self._poll_loop.cancel()

    @property
    def _has_priority_packages(self) -> bool:
        return any(
            entry.get("last_status_category") in HIGH_PRIORITY_CATEGORIES
            for entry in self.tracking_data.values()
        )

    @property
    def _normal_poll_interval_minutes(self) -> int:
        count = len(self.tracking_data)
        if count == 0:
            return MAX_POLL_MINUTES
        if count <= 35:
            return BASE_POLL_MINUTES
        if count <= 70:
            return 20
        return MIN_POLL_MINUTES

    @property
    def _poll_interval_minutes(self) -> int:
        if self._has_priority_packages:
            return PRIORITY_POLL_MINUTES
        return self._normal_poll_interval_minutes

    def _update_interval(self):
        new_interval = self._poll_interval_minutes
        if self._poll_loop.minutes != new_interval:
            self._poll_loop.change_interval(minutes=new_interval)
            logger.info("Poll interval adjusted to %d minutes (%d packages)",
                        new_interval, len(self.tracking_data))

    # -- Public API --

    async def add(
        self,
        tracking_number: str,
        user_id: int,
        channel_id: int | None = None,
        message_id: int | None = None,
    ) -> bool:
        """Add a tracking number. Returns False if already tracked."""
        tn = tracking_number.strip().upper()
        async with self._lock:
            if tn in self.tracking_data:
                return False

            self.tracking_data[tn] = {
                "user_id": user_id,
                "channel_id": channel_id,
                "message_id": message_id,
                "added_at": datetime.now(timezone.utc).isoformat(),
                "last_status_category": None,
                "last_status": None,
                "notified_out_for_delivery": False,
                "notified_delivered": False,
                "notified_alert": False,
                "notified_return": False,
                "notified_delivery_attempt": False,
                "notified_pickup": False,
            }
            _save_tracking(self.tracking_data)
            self._update_interval()
            return True

    def remove(self, tracking_number: str) -> bool:
        tn = tracking_number.strip().upper()
        if tn not in self.tracking_data:
            return False
        del self.tracking_data[tn]
        _save_tracking(self.tracking_data)
        self._update_interval()
        return True

    def list_all(self) -> dict:
        return dict(self.tracking_data)

    async def check_single(self, tracking_number: str) -> dict | None:
        """Fetch tracking for a single number. Returns API response or None."""
        try:
            results = await _fetch_tracking_batch(
                [tracking_number], self.consumer_key, self.consumer_secret,
            )
            for r in results:
                if r.get("trackingNumber", "").upper() == tracking_number.upper():
                    return r
            return results[0] if results else None
        except Exception as exc:
            logger.warning("Failed to check %s: %s", tracking_number, exc)
            return None

    # -- Background loop --

    @tasks.loop(minutes=BASE_POLL_MINUTES)
    async def _poll_loop(self):
        if not self.tracking_data:
            return

        # Remove stale packages that have been tracked too long
        now = datetime.now(timezone.utc)
        stale = []
        for tn, entry in self.tracking_data.items():
            added_at = entry.get("added_at")
            if not added_at:
                continue
            try:
                added_dt = datetime.fromisoformat(added_at)
                if (now - added_dt).days > MAX_TRACKING_DAYS:
                    stale.append(tn)
            except (ValueError, TypeError):
                continue
        for tn in stale:
            del self.tracking_data[tn]
            logger.info("Auto-removed stale package %s (older than %d days)", tn, MAX_TRACKING_DAYS)
        if stale:
            _save_tracking(self.tracking_data)

        if not self.tracking_data:
            return

        # Separate packages into priority tiers
        priority_numbers = []
        normal_numbers = []
        low_priority_numbers = []
        for tn, entry in self.tracking_data.items():
            cat = entry.get("last_status_category")
            if cat in HIGH_PRIORITY_CATEGORIES:
                priority_numbers.append(tn)
            elif cat in LOW_PRIORITY_CATEGORIES:
                low_priority_numbers.append(tn)
            else:
                normal_numbers.append(tn)

        # Determine which tiers to poll this cycle
        now_ts = time.time()
        full_interval_secs = self._normal_poll_interval_minutes * 60
        due_for_full_poll = (now_ts - self._last_full_poll) >= full_interval_secs
        low_interval_secs = LOW_PRIORITY_POLL_MINUTES * 60
        due_for_low_poll = (now_ts - self._last_low_poll) >= low_interval_secs

        if due_for_full_poll:
            tracking_numbers = priority_numbers + normal_numbers
            if due_for_low_poll:
                tracking_numbers += low_priority_numbers
                self._last_low_poll = now_ts
            self._last_full_poll = now_ts
        else:
            tracking_numbers = priority_numbers

        if not tracking_numbers:
            return

        logger.info("Polling %d packages (%d priority, %d normal, %d low-priority)...",
                     len(tracking_numbers), len(priority_numbers),
                     len(normal_numbers) if due_for_full_poll else 0,
                     len(low_priority_numbers) if due_for_low_poll else 0)

        # Process in batches of 35
        for batch_start in range(0, len(tracking_numbers), BATCH_SIZE):
            batch = tracking_numbers[batch_start:batch_start + BATCH_SIZE]
            try:
                results = await _fetch_tracking_batch(
                    batch, self.consumer_key, self.consumer_secret,
                )
            except RateLimitError as e:
                logger.warning("Rate limited, skipping remaining batches. Retry in %ds", e.retry_after)
                await self._dm_owner_error(
                    "USPS Rate Limited",
                    f"Rate limited by USPS API. Retry after **{e.retry_after}s**.\n"
                    f"Skipped remaining batches ({len(tracking_numbers) - batch_start} packages unpolled).",
                )
                return
            except Exception as exc:
                logger.error("Batch fetch error: %s", exc)
                await self._dm_owner_error(
                    "USPS API Error",
                    f"Batch fetch failed for {len(batch)} packages:\n```{exc}```",
                )
                continue

            for result in results:
                tn = result.get("trackingNumber", "").upper()
                if tn not in self.tracking_data:
                    continue

                # Check for API-level errors on this tracking number
                if "error" in result or result.get("statusCode") == "404":
                    continue

                entry = self.tracking_data[tn]
                new_category = result.get("statusCategory", "")
                old_category = entry.get("last_status_category")

                # Update stored status
                entry["last_status_category"] = new_category
                entry["last_status"] = result.get("status", "")
                entry["last_checked_at"] = datetime.now(timezone.utc).isoformat()

                # -- In-channel mode: edit the embed message --
                if entry.get("channel_id") and entry.get("message_id"):
                    await self._update_channel_embed(tn, entry, result)

                # -- DM notifications for trigger categories --
                if new_category != old_category and new_category in DM_TRIGGER_CATEGORIES:
                    await self._send_dm_notification(tn, entry, result, new_category)

            _save_tracking(self.tracking_data)

            # Small delay between batches to be polite to the API
            if batch_start + BATCH_SIZE < len(tracking_numbers):
                await asyncio.sleep(2)

        # Re-evaluate loop speed in case packages gained/lost priority
        self._update_interval()

    @_poll_loop.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    @_poll_loop.error
    async def _poll_loop_error(self, error: Exception):
        """Handle unhandled errors in the poll loop."""
        import traceback
        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        logger.error("Poll loop crashed:\n%s", tb)
        await self._dm_owner_error(
            "Tracking Poll Loop Crashed",
            f"The background polling loop encountered an error:\n```{tb[:3800]}```",
        )

    # -- Notification helpers --

    async def _send_dm_notification(
        self, tn: str, entry: dict, result: dict, category: str,
    ):
        """Send a DM to the owner about a tracking status change."""
        # Determine which notification flag to check/set
        flag_map = {
            "Out for Delivery": "notified_out_for_delivery",
            "Delivered": "notified_delivered",
            "Alert": "notified_alert",
            "Return to Sender": "notified_return",
            "Delivery Attempt": "notified_delivery_attempt",
            "Available for Pickup": "notified_pickup",
        }

        flag = flag_map.get(category)
        if not flag:
            return

        if entry.get(flag):
            return  # Already notified

        try:
            owner = await self.bot.fetch_user(OWNER_ID)
            embed = build_tracking_embed(tn, result, entry.get("user_id"), logo_url=USPS_LOGO_URL)
            view = build_tracking_view(tn)
            await owner.send(embed=embed, view=view)
            logger.info("DM sent for %s → %s", tn, category)
        except Exception as exc:
            logger.error("Failed to DM owner for %s: %s", tn, exc)
            return  # Don't mark as notified if DM failed

        entry[flag] = True

        # Auto-remove terminal packages (update embed one last time first)
        if category in TERMINAL_CATEGORIES:
            if entry.get("channel_id") and entry.get("message_id"):
                await self._update_channel_embed(tn, entry, result)
            del self.tracking_data[tn]
            logger.info("Auto-removed delivered package %s", tn)

    async def _update_channel_embed(
        self, tn: str, entry: dict, result: dict,
    ):
        """Edit the in-channel tracking embed with latest data."""
        channel_id = entry.get("channel_id")
        message_id = entry.get("message_id")
        if not channel_id or not message_id:
            return

        try:
            channel = self.bot.get_channel(channel_id)
            if not channel:
                channel = await self.bot.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
            embed = build_tracking_embed(
                tn, result, entry.get("user_id"), show_history=True, logo_url=USPS_LOGO_URL,
            )
            view = build_tracking_view(tn)
            await message.edit(embed=embed, view=view)
        except (discord.NotFound, discord.Forbidden):
            # Channel or message was deleted/inaccessible — stop trying
            entry["channel_id"] = None
            entry["message_id"] = None
            logger.warning("Channel/message gone for %s, stopping embed updates", tn)
        except Exception as exc:
            logger.error("Failed to update channel embed for %s: %s", tn, exc)

    async def force_poll(self):
        """Manually trigger a poll cycle (for the /trackrefresh command)."""
        self._last_full_poll = 0
        self._last_low_poll = 0
        await self._poll_loop.coro(self)
