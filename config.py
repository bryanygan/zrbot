"""Configuration for the bot — all settings loaded from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()

# Discord
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '')
CLIENT_ID = os.getenv('CLIENT_ID', '')
GUILD_ID = os.getenv('GUILD_ID', '')
OWNER_ID = int(os.getenv('OWNER_ID', '745694160002089130'))
AUTHORIZED_IDS = {OWNER_ID, 1108031578208219326}

# Vouch counter channels
TARGET_CHANNEL_ID = os.getenv('TARGET_CHANNEL_ID', '1108034288986898458')
NOTIFICATION_CHANNEL_ID = os.getenv('NOTIFICATION_CHANNEL_ID', '1377459975089295392')

# USPS API
USPS_CONSUMER_KEY = os.getenv('USPS_CONSUMER_KEY', '')
USPS_CONSUMER_SECRET = os.getenv('USPS_CONSUMER_SECRET', '')

# Shipping "From" address (for CSV generation)
SHIP_FROM_NAME = 'ZR Fulfillment'
SHIP_FROM_STREET = '2930 Chestnut St'
SHIP_FROM_STREET2 = ''
SHIP_FROM_CITY = 'Philadelphia'
SHIP_FROM_STATE = 'PA'
SHIP_FROM_ZIP = '19104'
