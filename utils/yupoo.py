"""Yupoo album scraper — fetch QC images from Yupoo albums."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

logger = logging.getLogger("zrbot.yupoo")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Regex to parse album URLs like https://rmqc.x.yupoo.com/albums/239263830?uid=1
ALBUM_URL_RE = re.compile(
    r"https?://([a-zA-Z0-9_-]+)\.x\.yupoo\.com/albums/(\d+)",
)

# Regex to extract image hashes from photo.yupoo.com URLs in HTML
PHOTO_URL_RE = re.compile(
    r"https?://photo\.yupoo\.com/([a-zA-Z0-9_-]+)/([a-f0-9]+)/(\w+)\.(jpe?g|png|webp)",
)

# Headers that make Yupoo treat us as a legitimate browser
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Discord limits
DISCORD_MAX_FILES_PER_MSG = 10
DISCORD_MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB per file

# Rate limiting
REQUEST_DELAY_SECONDS = 1.0
IMAGE_DOWNLOAD_TIMEOUT = 30
PAGE_FETCH_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class YupooImage:
    """A single image extracted from a Yupoo album."""
    vendor: str
    image_hash: str
    url: str  # full URL to medium image

    @property
    def filename(self) -> str:
        return f"{self.image_hash}.jpg"


@dataclass
class YupooAlbum:
    """Parsed Yupoo album metadata and images."""
    vendor: str
    album_id: str
    title: Optional[str] = None
    images: list[YupooImage] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://{self.vendor}.x.yupoo.com/albums/{self.album_id}?uid=1"


@dataclass
class DownloadedImage:
    """An image that has been downloaded into memory."""
    filename: str
    data: bytes
    content_type: str = "image/jpeg"

    @property
    def size(self) -> int:
        return len(self.data)


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

def parse_album_url(url: str) -> tuple[str, str] | None:
    """Parse a Yupoo album URL into (vendor, album_id).

    Returns None if the URL doesn't match the expected format.
    """
    match = ALBUM_URL_RE.search(url)
    if match:
        return match.group(1), match.group(2)
    return None


# ---------------------------------------------------------------------------
# HTML scraping
# ---------------------------------------------------------------------------

def extract_images_from_html(html: str, vendor: str) -> list[YupooImage]:
    """Extract all unique images from a Yupoo album HTML page.

    Finds all photo.yupoo.com URLs in the HTML, deduplicates by hash,
    and returns them normalized to medium.jpg for reliable access.
    """
    seen_hashes: set[str] = set()
    images: list[YupooImage] = []

    for match in PHOTO_URL_RE.finditer(html):
        img_vendor = match.group(1)
        img_hash = match.group(2)

        if img_hash in seen_hashes:
            continue
        seen_hashes.add(img_hash)

        # Use medium.jpg — it's the most reliable size for hotlinking
        url = f"https://photo.yupoo.com/{img_vendor}/{img_hash}/medium.jpg"
        images.append(YupooImage(vendor=img_vendor, image_hash=img_hash, url=url))

    return images


def extract_album_title(html: str) -> Optional[str]:
    """Extract the album title from HTML.

    Tries multiple patterns since Yupoo has changed formats over time.
    """
    # Pattern 1: <div id="infoCarry" data-name="..."> (with quotes)
    match = re.search(r'id=["\']?infoCarry["\']?[^>]*data-name="([^"]+)"', html)
    if not match:
        # Pattern 1b: unquoted data-name (e.g. data-name=4782792)
        match = re.search(r'id=["\']?infoCarry["\']?[^>]*data-name=([^"\s>]+)', html)
    if match:
        return match.group(1)

    # Pattern 2: <title>...</title> — extract the meaningful part
    match = re.search(r"<title>([^<]+)</title>", html)
    if match:
        title = match.group(1).strip()
        # Remove common suffixes like " _ Yupoo" or " - Yupoo"
        title = re.sub(r"\s*[_\-|]\s*Yupoo\s*$", "", title, flags=re.IGNORECASE)
        # Yupoo titles often look like "Name | 相册 | vendor | ..."
        # Take just the first segment before any pipe/dash separator
        title = re.split(r"\s*[|_\-]\s*", title)[0].strip()
        if title:
            return title

    return None


# ---------------------------------------------------------------------------
# Async HTTP fetching
# ---------------------------------------------------------------------------

async def fetch_album_page(
    session: aiohttp.ClientSession,
    vendor: str,
    album_id: str,
) -> str:
    """Fetch the HTML of a Yupoo album page."""
    url = f"https://{vendor}.x.yupoo.com/albums/{album_id}?uid=1"
    headers = {
        **DEFAULT_HEADERS,
        "Referer": f"https://{vendor}.x.yupoo.com/albums",
    }

    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=PAGE_FETCH_TIMEOUT)) as resp:
        resp.raise_for_status()
        return await resp.text()


async def download_image(
    session: aiohttp.ClientSession,
    image: YupooImage,
) -> DownloadedImage | None:
    """Download a single image. Returns None on failure."""
    headers = {
        **DEFAULT_HEADERS,
        "Referer": f"https://{image.vendor}.x.yupoo.com/",
    }

    try:
        async with session.get(
            image.url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=IMAGE_DOWNLOAD_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                logger.warning("Failed to download %s: HTTP %d", image.url, resp.status)
                return None

            data = await resp.read()

            # Skip if the image is too large for Discord
            if len(data) > DISCORD_MAX_FILE_SIZE:
                logger.warning(
                    "Image %s too large (%d bytes), skipping",
                    image.image_hash, len(data),
                )
                return None

            content_type = resp.headers.get("Content-Type", "image/jpeg")
            return DownloadedImage(
                filename=image.filename,
                data=data,
                content_type=content_type,
            )
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning("Error downloading %s: %s", image.url, exc)
        return None


async def download_images(
    session: aiohttp.ClientSession,
    images: list[YupooImage],
    delay: float = REQUEST_DELAY_SECONDS,
    max_images: Optional[int] = None,
) -> list[DownloadedImage]:
    """Download multiple images with rate limiting.

    Args:
        session: aiohttp session to use.
        images: List of images to download.
        delay: Seconds to wait between requests.
        max_images: Cap on how many images to download (None = all).

    Returns:
        List of successfully downloaded images.
    """
    to_download = images[:max_images] if max_images is not None and max_images > 0 else images
    downloaded: list[DownloadedImage] = []

    for i, image in enumerate(to_download):
        result = await download_image(session, image)
        if result:
            downloaded.append(result)

        # Rate limit — don't delay after the last image
        if i < len(to_download) - 1:
            await asyncio.sleep(delay)

    return downloaded


# ---------------------------------------------------------------------------
# Discord chunking
# ---------------------------------------------------------------------------

def chunk_for_discord(
    images: list[DownloadedImage],
    max_per_message: int = DISCORD_MAX_FILES_PER_MSG,
    max_total_size: int = DISCORD_MAX_FILE_SIZE,
) -> list[list[DownloadedImage]]:
    """Split images into chunks that fit within Discord's limits.

    Each chunk has at most `max_per_message` images, and the total
    combined size of images in a chunk doesn't exceed `max_total_size`.
    """
    chunks: list[list[DownloadedImage]] = []
    current_chunk: list[DownloadedImage] = []
    current_size = 0

    for img in images:
        # Would adding this image exceed limits?
        if (
            len(current_chunk) >= max_per_message
            or (current_size + img.size > max_total_size and current_chunk)
        ):
            chunks.append(current_chunk)
            current_chunk = []
            current_size = 0

        current_chunk.append(img)
        current_size += img.size

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


# ---------------------------------------------------------------------------
# High-level API
# ---------------------------------------------------------------------------

async def scrape_album(url: str) -> YupooAlbum:
    """Scrape a Yupoo album: fetch the page and extract image metadata.

    Args:
        url: Full Yupoo album URL.

    Returns:
        YupooAlbum with images populated.

    Raises:
        ValueError: If the URL format is invalid.
        aiohttp.ClientError: If the page can't be fetched.
    """
    parsed = parse_album_url(url)
    if not parsed:
        raise ValueError(
            f"Invalid Yupoo album URL: {url}\n"
            f"Expected format: https://VENDOR.x.yupoo.com/albums/ALBUM_ID"
        )

    vendor, album_id = parsed

    async with aiohttp.ClientSession() as session:
        html = await fetch_album_page(session, vendor, album_id)

    title = extract_album_title(html)
    images = extract_images_from_html(html, vendor)

    return YupooAlbum(
        vendor=vendor,
        album_id=album_id,
        title=title,
        images=images,
    )


async def fetch_album_images(
    url: str,
    max_images: Optional[int] = None,
    delay: float = REQUEST_DELAY_SECONDS,
) -> tuple[YupooAlbum, list[DownloadedImage]]:
    """Full pipeline: scrape album, download images, return both.

    Args:
        url: Yupoo album URL.
        max_images: Max images to download (None = all).
        delay: Rate limit delay between downloads.

    Returns:
        Tuple of (album metadata, downloaded images).
    """
    parsed = parse_album_url(url)
    if not parsed:
        raise ValueError(
            f"Invalid Yupoo album URL: {url}\n"
            f"Expected format: https://VENDOR.x.yupoo.com/albums/ALBUM_ID"
        )

    vendor, album_id = parsed

    async with aiohttp.ClientSession() as session:
        # Fetch and parse album page
        html = await fetch_album_page(session, vendor, album_id)
        title = extract_album_title(html)
        images = extract_images_from_html(html, vendor)

        album = YupooAlbum(
            vendor=vendor,
            album_id=album_id,
            title=title,
            images=images,
        )

        if not images:
            return album, []

        # Download images with rate limiting
        downloaded = await download_images(session, images, delay=delay, max_images=max_images)

    return album, downloaded
