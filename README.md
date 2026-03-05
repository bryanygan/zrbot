# ZR Bot

A Discord bot created for ZRServer for vouch tracking, USPS package tracking, and payment management.

## Features

### Vouch Counter

- Automatically counts vouches when users post images in a designated channel
- Sends notifications to a configured channel when a user receives a new vouch
- Backfill support to count vouches from existing messages

**Commands:**

| Command | Description | Permission |
|---------|-------------|------------|
| `/checkvouches [user]` | Check your vouch count (or another user's) | Everyone (others require Manage Guild) |
| `/setvouches <user> <count>` | Override a user's vouch count | Administrator |
| `/clearvouches [user]` | Clear vouches for a user or all users | Administrator |
| `/leaderboard [limit]` | Show top vouch earners | Everyone |
| `/backfill` | Recount vouches from message history | Manage Guild |

### USPS Package Tracking

- Real-time package tracking via the USPS API
- Live-updating embeds that automatically edit with the latest status on each poll cycle
- Rich embeds with tracking history, service type, current location, and expected delivery date
- "Last checked" timestamp displayed in each user's local timezone
- Automatic DM notifications when packages are out for delivery, delivered, or have issues
- Delivered packages show a message prompting the recipient to leave a vouch
- Adaptive polling intervals based on the number of tracked packages (15-60 min)
- Auto-removal of delivered/returned packages after notification

**Commands:**

| Command | Description | Permission |
|---------|-------------|------------|
| `/track <tracking_number> <user>` | Start tracking a package for a user (posts a live-updating embed) | Authorized users |
| `/untrack <tracking_number>` | Stop tracking a package | Authorized users |
| `/trackinglist` | Show all currently tracked packages | Authorized users |
| `/trackinfo <tracking_number>` | Get current tracking details for any tracking number | Authorized users |
| `/trackrefresh` | Force refresh all tracked packages immediately | Authorized users |

### Address Parsing

- Right-click a message containing an address to generate a shipping CSV
- Addresses are validated and standardized via the USPS Address API
- Modal prompt for package weight input
- Outputs a formatted CSV line with from/to addresses and weight

**Commands:**

| Command | Description | Permission |
|---------|-------------|------------|
| `Convert Address to CSV` (context menu) | Parse a message's address into a shipping CSV | Authorized users |

### Payment Methods

- Displays available payment options with interactive buttons
- Supports multiple Zelle and PayPal accounts
- Copyable payment info via button interaction

**Commands:**

| Command | Description | Permission |
|---------|-------------|------------|
| `/payments` | Display payment method buttons | Everyone |

## Setup

### Prerequisites

- Python 3.10+
- A Discord bot token
- USPS API credentials (optional, for tracking/address features)

### Environment Variables

Create a `.env` file in the project root:

```env
DISCORD_TOKEN=your_discord_bot_token
CLIENT_ID=your_bot_client_id
GUILD_ID=your_guild_id
OWNER_ID=your_discord_user_id

TARGET_CHANNEL_ID=channel_for_vouch_images
NOTIFICATION_CHANNEL_ID=channel_for_vouch_notifications

USPS_CONSUMER_KEY=your_usps_consumer_key
USPS_CONSUMER_SECRET=your_usps_consumer_secret
```

### Installation

```bash
pip install -r requirements.txt
python bot.py
```

### Deployment

A `Procfile` is included for deploying to platforms like Heroku or Railway.

## Project Structure

```
zrbot/
├── bot.py                    # Main bot entry point
├── config.py                 # Configuration and environment variables
├── requirements.txt          # Python dependencies
├── Procfile                  # Deployment process file
├── commands/
│   ├── tracking.py           # /track, /untrack, /trackinglist, etc.
│   └── address.py            # Address-to-CSV context menu command
├── utils/
│   ├── tracking_monitor.py   # USPS polling, embeds, and notifications
│   └── address_parser.py     # Address parsing and USPS validation
└── data/                     # Auto-created runtime data
    ├── vouches.json
    └── tracking.json
```
