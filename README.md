# ZR Bot

A Discord bot created for ZRServer for vouch tracking, USPS package tracking, QC image fetching, deal hunting, and payment management.

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

### USPS Package Tracking (optional — gated by `TRACKING_ENABLED`)

- Real-time package tracking via the USPS API
- Live-updating embeds that automatically edit with the latest status on each poll cycle
- Rich embeds with progress bar, tracking history, route, current location, and ETA countdown
- Package labels/nicknames shown in embeds (e.g., "Jordan 4s for @user")
- Smart input parsing: accepts `Name : TrackingNumber` format for automatic labeling
- DM support: static embeds in user-to-user DMs with opt-in live updates via bot DM
- "Confirm Received" button on delivered packages prompting customers to leave a vouch
- "Get Live Updates" / "Stop Live Updates" buttons for customer-controlled DM tracking
- Automatic DM notifications to owner for deliveries, alerts, and status changes
- 3-tier priority polling: high (10 min), normal (30 min), low (60 min)
- Activity logging to a Discord channel (new tracks, deliveries, status changes, errors)
- Shipping statistics with historical delivery data
- Auto-removal of delivered/returned packages after notification
- Periodic backups of tracking data (every 6 hours, keeps last 10)
- Graceful shutdown with state save

**Commands:**

| Command | Description | Permission |
|---------|-------------|------------|
| `/track <tracking_number> [user] [label]` | Start tracking a package (supports `Name : TN` format) | Authorized users |
| `/bulktrack <tracking_numbers> [user]` | Track multiple packages at once (comma, space, or `Name : TN` format) | Authorized users |
| `/untrack <tracking_number>` | Stop tracking a package | Authorized users |
| `/trackinglist` | Show all tracked packages (paginated, 8 per page) | Authorized users |
| `/trackinfo <tracking_number>` | Get current tracking details for any tracking number | Authorized users |
| `/trackrefresh [tracking_number] [user]` | Force refresh all, one, or a user's packages | Authorized users |
| `/stats` | Show shipping statistics (active, delivered, avg delivery time) | Authorized users |

### Yupoo QC Images

- Fetch QC (quality check) pictures from any Yupoo album and send them as Discord messages
- Supports multiple vendors (rmqc, tmf001, tmf002, zengshuaige, and any other Yupoo store)
- Images are sent as standalone bot messages — the Yupoo URL stays hidden from other users
- Smart image distribution across messages using Discord's mosaic layout (7 or 10 images per first message for a hero image + grid)
- Remaining images split evenly across follow-up messages (no more 10+10+1 splits)
- Rate-limited downloads (1s between requests) to avoid Yupoo throttling
- Handles Discord limits: max 10 files per message, 25MB per file
- Configurable max image cap (default 50)

**Commands:**

| Command | Description | Permission |
|---------|-------------|------------|
| `/qc <url> [max_images]` | Fetch QC images from a Yupoo album URL | Authorized users |

### Weekend Deal Hunter

- Automated weekend travel deal scanning powered by Ollama
- Searches for flights and trains from PHL to configured destinations
- Rich deal embeds with transport, hotel, pricing, and schedule details
- Reaction buttons for deal feedback (Interested / Not for me / Booked it!)
- Preference learning from feedback to personalize future recommendations
- City watch list with price drop alerts (max 5 watches)
- API quota tracking for Amadeus, Kiwi, and SerpAPI

**Commands:**

| Command | Description | Permission |
|---------|-------------|------------|
| `/deals [destinations]` | Run a deal scan and DM results | Authorized users |
| `/dealstatus` | Show deal hunter status and stats | Everyone |
| `/watch <city> [date]` | Add a city to your watch list (max 5) | Authorized users |
| `/unwatch <city>` | Remove a city from your watch list | Authorized users |
| `/watchlist` | Show active deal watches | Everyone |
| `/preferences [action]` | View or reset learned deal preferences | Authorized users |

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

# Optional: enable USPS tracking (disabled by default)
TRACKING_ENABLED=1

# Optional: TrackingMore API fallback for tracking
TRACKINGMORE_API_KEY=your_trackingmore_api_key

# Optional: persistent data directory (e.g., Railway Volume mount)
DATA_PATH=/path/to/persistent/data

# Optional: path to deal hunter project
DEAL_HUNTER_PATH=/path/to/weekendmaxxing
```

### Installation

```bash
pip install -r requirements.txt
python bot.py
```

### Deployment

A `Procfile` is included for deploying to platforms like Railway or Heroku.

For persistent data on Railway, create a Volume and set `DATA_PATH` to the mount path.

## Project Structure

```
zrbot/
├── bot.py                    # Main bot entry point
├── config.py                 # Configuration and environment variables
├── requirements.txt          # Python dependencies
├── Procfile                  # Deployment process file
├── commands/
│   ├── address.py            # Address-to-CSV context menu command
│   ├── deals.py              # /deals, /watch, /preferences, etc.
│   ├── tracking.py           # /track, /bulktrack, /untrack, /trackinglist, etc.
│   └── yupoo.py              # /qc — Yupoo QC image fetching
├── utils/
│   ├── address_parser.py     # Address parsing and USPS validation
│   ├── tracking_monitor.py   # USPS polling, embeds, and notifications
│   └── yupoo.py              # Yupoo album scraper and image downloader
├── tests/
│   ├── test_yupoo.py         # Unit tests for Yupoo scraper
│   └── test_yupoo_live.py    # Live integration tests against real Yupoo albums
├── assets/                   # Static assets (USPS logos)
└── data/                     # Auto-created runtime data (gitignored)
    ├── vouches.json
    ├── tracking.json
    ├── stats.json
    └── backups/              # Periodic tracking backups
```
