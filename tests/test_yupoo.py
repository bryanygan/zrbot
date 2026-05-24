"""Tests for the Yupoo album scraper and image downloader."""

import asyncio
import io
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from utils.yupoo import (
    DISCORD_MAX_FILE_SIZE,
    DISCORD_MAX_FILES_PER_MSG,
    DownloadedImage,
    YupooAlbum,
    YupooImage,
    chunk_for_discord,
    download_image,
    download_images,
    extract_album_title,
    extract_images_from_html,
    fetch_album_images,
    fetch_album_page,
    parse_album_url,
    scrape_album,
)


# ---------------------------------------------------------------------------
# URL parsing tests
# ---------------------------------------------------------------------------


class TestParseAlbumUrl(unittest.TestCase):
    def test_standard_url(self):
        result = parse_album_url("https://rmqc.x.yupoo.com/albums/239263830")
        self.assertEqual(result, ("rmqc", "239263830"))

    def test_url_with_uid(self):
        result = parse_album_url("https://rmqc.x.yupoo.com/albums/239263830?uid=1")
        self.assertEqual(result, ("rmqc", "239263830"))

    def test_url_with_extra_params(self):
        result = parse_album_url(
            "https://tmf001.x.yupoo.com/albums/239220972?uid=1&isSubCate=false&referrercate=5229899"
        )
        self.assertEqual(result, ("tmf001", "239220972"))

    def test_different_vendors(self):
        vendors = ["rmqc", "tmf001", "tmf002", "zengshuaige", "my-store_123"]
        for vendor in vendors:
            result = parse_album_url(f"https://{vendor}.x.yupoo.com/albums/12345")
            self.assertIsNotNone(result, f"Failed for vendor: {vendor}")
            self.assertEqual(result[0], vendor)
            self.assertEqual(result[1], "12345")

    def test_http_url(self):
        result = parse_album_url("http://rmqc.x.yupoo.com/albums/123")
        self.assertEqual(result, ("rmqc", "123"))

    def test_invalid_url_no_albums(self):
        result = parse_album_url("https://rmqc.x.yupoo.com/categories/123")
        self.assertIsNone(result)

    def test_invalid_url_not_yupoo(self):
        result = parse_album_url("https://example.com/albums/123")
        self.assertIsNone(result)

    def test_invalid_url_empty(self):
        result = parse_album_url("")
        self.assertIsNone(result)

    def test_invalid_url_random_text(self):
        result = parse_album_url("not a url at all")
        self.assertIsNone(result)

    def test_url_embedded_in_text(self):
        """parse_album_url uses .search() so it finds URLs in surrounding text."""
        result = parse_album_url("check this out https://rmqc.x.yupoo.com/albums/999 cool right?")
        self.assertEqual(result, ("rmqc", "999"))

    def test_album_id_with_many_digits(self):
        result = parse_album_url("https://rmqc.x.yupoo.com/albums/1234567890123")
        self.assertEqual(result, ("rmqc", "1234567890123"))

    def test_listing_url_no_album_id(self):
        """A listing page URL without a numeric album ID should fail."""
        result = parse_album_url("https://rmqc.x.yupoo.com/albums")
        self.assertIsNone(result)

    def test_url_with_tab_param(self):
        result = parse_album_url("https://tmf002.x.yupoo.com/albums/123?tab=gallery")
        self.assertEqual(result, ("tmf002", "123"))


# ---------------------------------------------------------------------------
# HTML extraction tests
# ---------------------------------------------------------------------------

SAMPLE_ALBUM_HTML = """
<!DOCTYPE html>
<html>
<head><title>Test Album _ Yupoo</title></head>
<body>
<div id="infoCarry" data-name="Nike Dunk Low"></div>
<div class="showalbum__children">
  <div class="showalbum__children__box">
    <img alt="" data-type="photo"
         src="https://photo.yupoo.com/rmqc/0b395c3bcb/medium.jpeg">
  </div>
  <div class="showalbum__children__box">
    <img alt="" data-type="photo"
         src="https://photo.yupoo.com/rmqc/9c90e3b8ce/small.jpeg">
  </div>
  <div class="showalbum__children__box">
    <img alt="" data-type="photo"
         data-src="https://photo.yupoo.com/rmqc/5b63ea4c8e/big.jpeg">
  </div>
</div>
</body>
</html>
"""

SAMPLE_HTML_DIFFERENT_VENDOR = """
<html>
<head><title>NB 603 _ Yupoo</title></head>
<body>
<img src="https://photo.yupoo.com/tmf001/de4ba164ce/medium.jpeg">
<img src="https://photo.yupoo.com/tmf001/3cd0594b23/small.jpeg">
<img src="https://photo.yupoo.com/tmf001/3d170309a8/small.jpeg">
</body>
</html>
"""

SAMPLE_HTML_NO_IMAGES = """
<html>
<head><title>Empty Album _ Yupoo</title></head>
<body>
<div>No images here</div>
</body>
</html>
"""

SAMPLE_HTML_DUPLICATE_HASHES = """
<html>
<body>
<img src="https://photo.yupoo.com/rmqc/aabbccddee/small.jpeg">
<img src="https://photo.yupoo.com/rmqc/aabbccddee/medium.jpeg">
<img src="https://photo.yupoo.com/rmqc/aabbccddee/big.jpeg">
<img src="https://photo.yupoo.com/rmqc/1122334455/small.jpeg">
</body>
</html>
"""


class TestExtractImages(unittest.TestCase):
    def test_extracts_all_unique_images(self):
        images = extract_images_from_html(SAMPLE_ALBUM_HTML, "rmqc")
        self.assertEqual(len(images), 3)

    def test_normalizes_to_medium_jpg(self):
        images = extract_images_from_html(SAMPLE_ALBUM_HTML, "rmqc")
        for img in images:
            self.assertIn("/medium.jpg", img.url)

    def test_preserves_vendor(self):
        images = extract_images_from_html(SAMPLE_ALBUM_HTML, "rmqc")
        for img in images:
            self.assertEqual(img.vendor, "rmqc")

    def test_different_vendor(self):
        images = extract_images_from_html(SAMPLE_HTML_DIFFERENT_VENDOR, "tmf001")
        self.assertEqual(len(images), 3)
        for img in images:
            self.assertEqual(img.vendor, "tmf001")
            self.assertIn("tmf001", img.url)

    def test_no_images(self):
        images = extract_images_from_html(SAMPLE_HTML_NO_IMAGES, "rmqc")
        self.assertEqual(len(images), 0)

    def test_deduplicates_same_hash(self):
        images = extract_images_from_html(SAMPLE_HTML_DUPLICATE_HASHES, "rmqc")
        self.assertEqual(len(images), 2)
        hashes = {img.image_hash for img in images}
        self.assertEqual(hashes, {"aabbccddee", "1122334455"})

    def test_extracts_correct_hashes(self):
        images = extract_images_from_html(SAMPLE_ALBUM_HTML, "rmqc")
        hashes = {img.image_hash for img in images}
        self.assertEqual(hashes, {"0b395c3bcb", "9c90e3b8ce", "5b63ea4c8e"})

    def test_image_filename(self):
        images = extract_images_from_html(SAMPLE_ALBUM_HTML, "rmqc")
        for img in images:
            self.assertTrue(img.filename.endswith(".jpg"))
            self.assertEqual(img.filename, f"{img.image_hash}.jpg")

    def test_empty_html(self):
        images = extract_images_from_html("", "rmqc")
        self.assertEqual(len(images), 0)

    def test_malformed_html(self):
        """Should not crash on malformed HTML, just find no images."""
        images = extract_images_from_html("<div><img broken", "rmqc")
        self.assertEqual(len(images), 0)

    def test_mixed_extensions(self):
        html = """
        <img src="https://photo.yupoo.com/test/aabb001122/medium.jpg">
        <img src="https://photo.yupoo.com/test/ccdd003344/medium.jpeg">
        <img src="https://photo.yupoo.com/test/eeff005566/medium.png">
        """
        images = extract_images_from_html(html, "test")
        self.assertEqual(len(images), 3)


class TestExtractAlbumTitle(unittest.TestCase):
    def test_infocarry_pattern(self):
        title = extract_album_title(SAMPLE_ALBUM_HTML)
        self.assertEqual(title, "Nike Dunk Low")

    def test_title_tag_fallback(self):
        html = '<html><head><title>Jordan 4 Retro _ Yupoo</title></head><body></body></html>'
        title = extract_album_title(html)
        self.assertEqual(title, "Jordan 4 Retro")

    def test_title_tag_strips_dash_yupoo(self):
        html = '<html><head><title>Air Force 1 - Yupoo</title></head></html>'
        title = extract_album_title(html)
        self.assertEqual(title, "Air Force 1")

    def test_title_tag_strips_pipe_yupoo(self):
        html = '<html><head><title>Dunks | Yupoo</title></head></html>'
        title = extract_album_title(html)
        self.assertEqual(title, "Dunks")

    def test_title_tag_with_yupoo_format(self):
        """Real Yupoo title format: 'ProductName | 相册 | vendor | ...'"""
        html = '<html><head><title>4782792 | 相册 | rmqc | Supplier Product Catalog</title></head></html>'
        title = extract_album_title(html)
        self.assertEqual(title, "4782792")

    def test_no_title(self):
        title = extract_album_title("<html><body>no title</body></html>")
        self.assertIsNone(title)

    def test_empty_html(self):
        title = extract_album_title("")
        self.assertIsNone(title)

    def test_infocarry_takes_priority_over_title(self):
        html = """
        <html>
        <head><title>Wrong Title _ Yupoo</title></head>
        <body><div id="infoCarry" data-name="Correct Title"></div></body>
        </html>
        """
        title = extract_album_title(html)
        self.assertEqual(title, "Correct Title")


# ---------------------------------------------------------------------------
# Discord chunking tests
# ---------------------------------------------------------------------------


def _make_image(size: int = 1000, name: str = "test.jpg") -> DownloadedImage:
    return DownloadedImage(filename=name, data=b"\x00" * size)


class TestChunkForDiscord(unittest.TestCase):
    def test_single_image(self):
        images = [_make_image()]
        chunks = chunk_for_discord(images)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 1)

    def test_exactly_ten_images(self):
        """10 images fits in one message — mosaic with hero image."""
        images = [_make_image(name=f"img_{i}.jpg") for i in range(10)]
        chunks = chunk_for_discord(images)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 10)

    def test_exactly_seven_images(self):
        """7 images fits in one message — mosaic with hero image."""
        images = [_make_image(name=f"img_{i}.jpg") for i in range(7)]
        chunks = chunk_for_discord(images)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 7)

    def test_eleven_images_even_split(self):
        """11 images: first=7 (mosaic), second=4. Not 10+1."""
        images = [_make_image(name=f"img_{i}.jpg") for i in range(11)]
        chunks = chunk_for_discord(images)
        self.assertEqual(len(chunks), 2)
        # 7+4 is more even than 10+1
        self.assertEqual(len(chunks[0]), 7)
        self.assertEqual(len(chunks[1]), 4)

    def test_fourteen_images_mosaic_split(self):
        """14 images: first=7 (mosaic), second=7 — perfectly even."""
        images = [_make_image(name=f"img_{i}.jpg") for i in range(14)]
        chunks = chunk_for_discord(images)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0]), 7)
        self.assertEqual(len(chunks[1]), 7)

    def test_seventeen_images_mosaic_split(self):
        """17 images: first=10 (mosaic), remaining=7 — both mosaic-friendly."""
        images = [_make_image(name=f"img_{i}.jpg") for i in range(17)]
        chunks = chunk_for_discord(images)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0]), 10)
        self.assertEqual(len(chunks[1]), 7)

    def test_twenty_images(self):
        """20 images: first=10 (mosaic), second=10 — perfectly even."""
        images = [_make_image(name=f"img_{i}.jpg") for i in range(20)]
        chunks = chunk_for_discord(images)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0]), 10)
        self.assertEqual(len(chunks[1]), 10)

    def test_twenty_five_images(self):
        """25 images: should distribute evenly across 3 messages."""
        images = [_make_image(name=f"img_{i}.jpg") for i in range(25)]
        chunks = chunk_for_discord(images)
        sizes = [len(c) for c in chunks]
        self.assertEqual(sum(sizes), 25)
        # All chunks should be max 10
        self.assertTrue(all(s <= 10 for s in sizes))
        # Should be reasonably even (max-min spread <= 3)
        self.assertLessEqual(max(sizes) - min(sizes), 3)

    def test_respects_size_limit(self):
        # 3 images of 10MB each — total 30MB exceeds 25MB limit
        images = [
            _make_image(size=10 * 1024 * 1024, name=f"big_{i}.jpg")
            for i in range(3)
        ]
        chunks = chunk_for_discord(images)
        # Size limit forces a split regardless of even distribution
        total = sum(len(c) for c in chunks)
        self.assertEqual(total, 3)
        for chunk in chunks:
            chunk_size = sum(img.size for img in chunk)
            self.assertLessEqual(chunk_size, DISCORD_MAX_FILE_SIZE)

    def test_empty_list(self):
        chunks = chunk_for_discord([])
        self.assertEqual(len(chunks), 0)

    def test_single_large_image_gets_own_chunk(self):
        """Two large images that together exceed 25MB should split."""
        images = [
            _make_image(size=20 * 1024 * 1024, name="huge.jpg"),
            _make_image(size=10 * 1024 * 1024, name="also_huge.jpg"),
        ]
        chunks = chunk_for_discord(images)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0]), 1)
        self.assertEqual(len(chunks[1]), 1)

    def test_large_and_small_fit_together(self):
        """A large image and a small one that fit under 25MB stay in one chunk."""
        images = [
            _make_image(size=20 * 1024 * 1024, name="huge.jpg"),
            _make_image(size=1000, name="small.jpg"),
        ]
        chunks = chunk_for_discord(images)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 2)

    def test_many_small_images_even_distribution(self):
        """35 images should distribute evenly, not 10+10+10+5."""
        images = [_make_image(size=100, name=f"tiny_{i}.jpg") for i in range(35)]
        chunks = chunk_for_discord(images)
        sizes = [len(c) for c in chunks]
        self.assertEqual(sum(sizes), 35)
        self.assertTrue(all(s <= 10 for s in sizes))
        # Should be more even than 10+10+10+5
        self.assertLessEqual(max(sizes) - min(sizes), 3)

    def test_all_images_accounted_for(self):
        """Every image should appear exactly once across all chunks."""
        for n in [1, 5, 7, 10, 11, 14, 17, 20, 25, 30, 35, 50]:
            images = [_make_image(name=f"img_{i}.jpg") for i in range(n)]
            chunks = chunk_for_discord(images)
            total = sum(len(c) for c in chunks)
            self.assertEqual(total, n, f"Lost images for n={n}: got {total}")

    def test_first_chunk_is_mosaic_friendly(self):
        """For multi-message albums, first chunk should be 7 or 10."""
        for n in [11, 14, 17, 20, 25, 30]:
            images = [_make_image(name=f"img_{i}.jpg") for i in range(n)]
            chunks = chunk_for_discord(images)
            if len(chunks) > 1:
                self.assertIn(
                    len(chunks[0]), (7, 10),
                    f"First chunk for n={n} was {len(chunks[0])}, expected 7 or 10"
                )


# ---------------------------------------------------------------------------
# Data class tests
# ---------------------------------------------------------------------------


class TestYupooImage(unittest.TestCase):
    def test_filename(self):
        img = YupooImage(vendor="rmqc", image_hash="abc123", url="https://example.com/abc123/medium.jpg")
        self.assertEqual(img.filename, "abc123.jpg")


class TestYupooAlbum(unittest.TestCase):
    def test_url_property(self):
        album = YupooAlbum(vendor="tmf001", album_id="12345")
        self.assertEqual(album.url, "https://tmf001.x.yupoo.com/albums/12345?uid=1")


class TestDownloadedImage(unittest.TestCase):
    def test_size(self):
        img = DownloadedImage(filename="test.jpg", data=b"\x00" * 500)
        self.assertEqual(img.size, 500)

    def test_default_content_type(self):
        img = DownloadedImage(filename="test.jpg", data=b"")
        self.assertEqual(img.content_type, "image/jpeg")


# ---------------------------------------------------------------------------
# Async download tests (mocked)
# ---------------------------------------------------------------------------


class TestDownloadImage(unittest.IsolatedAsyncioTestCase):
    async def test_successful_download(self):
        image = YupooImage(vendor="rmqc", image_hash="abc123", url="https://photo.yupoo.com/rmqc/abc123/medium.jpg")
        fake_data = b"\xff\xd8\xff\xe0" + b"\x00" * 100  # fake JPEG

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=fake_data)
        mock_resp.headers = {"Content-Type": "image/jpeg"}
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        result = await download_image(mock_session, image)
        self.assertIsNotNone(result)
        self.assertEqual(result.filename, "abc123.jpg")
        self.assertEqual(result.data, fake_data)

    async def test_404_returns_none(self):
        image = YupooImage(vendor="rmqc", image_hash="missing", url="https://photo.yupoo.com/rmqc/missing/medium.jpg")

        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        result = await download_image(mock_session, image)
        self.assertIsNone(result)

    async def test_oversized_image_skipped(self):
        image = YupooImage(vendor="rmqc", image_hash="huge", url="https://photo.yupoo.com/rmqc/huge/medium.jpg")
        # 30MB image
        fake_data = b"\x00" * (30 * 1024 * 1024)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=fake_data)
        mock_resp.headers = {"Content-Type": "image/jpeg"}
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        result = await download_image(mock_session, image)
        self.assertIsNone(result)

    async def test_timeout_returns_none(self):
        image = YupooImage(vendor="rmqc", image_hash="slow", url="https://photo.yupoo.com/rmqc/slow/medium.jpg")

        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        result = await download_image(mock_session, image)
        self.assertIsNone(result)

    async def test_connection_error_returns_none(self):
        image = YupooImage(vendor="rmqc", image_hash="err", url="https://photo.yupoo.com/rmqc/err/medium.jpg")

        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("connection refused"))
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        result = await download_image(mock_session, image)
        self.assertIsNone(result)


class TestDownloadImages(unittest.IsolatedAsyncioTestCase):
    async def test_respects_max_images(self):
        images = [
            YupooImage(vendor="rmqc", image_hash=f"hash{i}", url=f"https://photo.yupoo.com/rmqc/hash{i}/medium.jpg")
            for i in range(10)
        ]
        fake_data = b"\xff\xd8" + b"\x00" * 50

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=fake_data)
        mock_resp.headers = {"Content-Type": "image/jpeg"}
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        # Only download first 3
        downloaded = await download_images(mock_session, images, delay=0, max_images=3)
        self.assertEqual(len(downloaded), 3)

    async def test_skips_failed_images(self):
        images = [
            YupooImage(vendor="rmqc", image_hash="good", url="https://photo.yupoo.com/rmqc/good/medium.jpg"),
            YupooImage(vendor="rmqc", image_hash="bad", url="https://photo.yupoo.com/rmqc/bad/medium.jpg"),
            YupooImage(vendor="rmqc", image_hash="good2", url="https://photo.yupoo.com/rmqc/good2/medium.jpg"),
        ]

        call_count = 0

        def make_resp(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_resp = AsyncMock()
            if call_count == 2:  # second image fails
                mock_resp.status = 500
            else:
                mock_resp.status = 200
                mock_resp.read = AsyncMock(return_value=b"\xff\xd8\x00")
                mock_resp.headers = {"Content-Type": "image/jpeg"}
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)
            return mock_resp

        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=make_resp)

        downloaded = await download_images(mock_session, images, delay=0)
        self.assertEqual(len(downloaded), 2)

    async def test_empty_list(self):
        mock_session = AsyncMock()
        downloaded = await download_images(mock_session, [], delay=0)
        self.assertEqual(len(downloaded), 0)


# ---------------------------------------------------------------------------
# Fetch album page tests (mocked)
# ---------------------------------------------------------------------------


class TestFetchAlbumPage(unittest.IsolatedAsyncioTestCase):
    async def test_returns_html(self):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value="<html>test</html>")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        html = await fetch_album_page(mock_session, "rmqc", "12345")
        self.assertEqual(html, "<html>test</html>")

        # Check that correct URL was requested
        call_args = mock_session.get.call_args
        self.assertIn("rmqc.x.yupoo.com/albums/12345", call_args[0][0])

    async def test_sets_referer_header(self):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        await fetch_album_page(mock_session, "tmf001", "999")
        call_kwargs = mock_session.get.call_args[1]
        self.assertIn("Referer", call_kwargs["headers"])
        self.assertIn("tmf001", call_kwargs["headers"]["Referer"])


# ---------------------------------------------------------------------------
# High-level API tests (mocked)
# ---------------------------------------------------------------------------


class TestScrapeAlbum(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_url_raises(self):
        with self.assertRaises(ValueError):
            await scrape_album("https://not-yupoo.com/albums/123")

    @patch("utils.yupoo.aiohttp.ClientSession")
    async def test_scrapes_album(self, mock_session_cls):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value=SAMPLE_ALBUM_HTML)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session

        album = await scrape_album("https://rmqc.x.yupoo.com/albums/239263830")
        self.assertEqual(album.vendor, "rmqc")
        self.assertEqual(album.album_id, "239263830")
        self.assertEqual(album.title, "Nike Dunk Low")
        self.assertEqual(len(album.images), 3)


class TestFetchAlbumImages(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_url_raises(self):
        with self.assertRaises(ValueError):
            await fetch_album_images("bad-url")

    @patch("utils.yupoo.aiohttp.ClientSession")
    async def test_full_pipeline(self, mock_session_cls):
        fake_image_data = b"\xff\xd8\xff\xe0" + b"\x00" * 50

        mock_page_resp = AsyncMock()
        mock_page_resp.raise_for_status = MagicMock()
        mock_page_resp.text = AsyncMock(return_value=SAMPLE_ALBUM_HTML)
        mock_page_resp.__aenter__ = AsyncMock(return_value=mock_page_resp)
        mock_page_resp.__aexit__ = AsyncMock(return_value=False)

        mock_img_resp = AsyncMock()
        mock_img_resp.status = 200
        mock_img_resp.read = AsyncMock(return_value=fake_image_data)
        mock_img_resp.headers = {"Content-Type": "image/jpeg"}
        mock_img_resp.__aenter__ = AsyncMock(return_value=mock_img_resp)
        mock_img_resp.__aexit__ = AsyncMock(return_value=False)

        call_count = 0

        def route_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_page_resp  # first call is the album page
            return mock_img_resp  # subsequent calls are image downloads

        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=route_get)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session

        album, downloaded = await fetch_album_images(
            "https://rmqc.x.yupoo.com/albums/239263830",
            delay=0,
        )
        self.assertEqual(album.vendor, "rmqc")
        self.assertEqual(len(album.images), 3)
        self.assertEqual(len(downloaded), 3)

    @patch("utils.yupoo.aiohttp.ClientSession")
    async def test_max_images_cap(self, mock_session_cls):
        fake_image_data = b"\xff\xd8" + b"\x00" * 50

        mock_page_resp = AsyncMock()
        mock_page_resp.raise_for_status = MagicMock()
        mock_page_resp.text = AsyncMock(return_value=SAMPLE_ALBUM_HTML)
        mock_page_resp.__aenter__ = AsyncMock(return_value=mock_page_resp)
        mock_page_resp.__aexit__ = AsyncMock(return_value=False)

        mock_img_resp = AsyncMock()
        mock_img_resp.status = 200
        mock_img_resp.read = AsyncMock(return_value=fake_image_data)
        mock_img_resp.headers = {"Content-Type": "image/jpeg"}
        mock_img_resp.__aenter__ = AsyncMock(return_value=mock_img_resp)
        mock_img_resp.__aexit__ = AsyncMock(return_value=False)

        call_count = 0

        def route_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_page_resp
            return mock_img_resp

        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=route_get)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session

        album, downloaded = await fetch_album_images(
            "https://rmqc.x.yupoo.com/albums/239263830",
            max_images=2,
            delay=0,
        )
        self.assertEqual(len(album.images), 3)  # all found
        self.assertEqual(len(downloaded), 2)  # but only 2 downloaded

    @patch("utils.yupoo.aiohttp.ClientSession")
    async def test_no_images_in_album(self, mock_session_cls):
        mock_page_resp = AsyncMock()
        mock_page_resp.raise_for_status = MagicMock()
        mock_page_resp.text = AsyncMock(return_value=SAMPLE_HTML_NO_IMAGES)
        mock_page_resp.__aenter__ = AsyncMock(return_value=mock_page_resp)
        mock_page_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_page_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session

        album, downloaded = await fetch_album_images(
            "https://rmqc.x.yupoo.com/albums/999",
            delay=0,
        )
        self.assertEqual(len(album.images), 0)
        self.assertEqual(len(downloaded), 0)


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases(unittest.TestCase):
    def test_webp_images_extracted(self):
        html = '<img src="https://photo.yupoo.com/vendor/aabb112233/medium.webp">'
        images = extract_images_from_html(html, "vendor")
        self.assertEqual(len(images), 1)

    def test_png_images_extracted(self):
        html = '<img src="https://photo.yupoo.com/vendor/aabb112233/medium.png">'
        images = extract_images_from_html(html, "vendor")
        self.assertEqual(len(images), 1)

    def test_protocol_relative_urls_not_matched(self):
        """Protocol-relative URLs (//photo.yupoo.com/...) should not match our regex
        since they lack https?:// prefix."""
        html = '<img src="//photo.yupoo.com/rmqc/aabb112233/medium.jpeg">'
        images = extract_images_from_html(html, "rmqc")
        self.assertEqual(len(images), 0)

    def test_very_long_hash(self):
        html = '<img src="https://photo.yupoo.com/rmqc/aabbccddeeff1122/medium.jpeg">'
        images = extract_images_from_html(html, "rmqc")
        self.assertEqual(len(images), 1)

    def test_chunk_single_oversized_image(self):
        """A single image that's right at the limit should get its own chunk."""
        img = _make_image(size=DISCORD_MAX_FILE_SIZE - 1, name="barely_fits.jpg")
        chunks = chunk_for_discord([img])
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 1)

    def test_url_with_uppercase_vendor(self):
        """Vendor names in URLs might have mixed case."""
        result = parse_album_url("https://RmQc.x.yupoo.com/albums/123")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "RmQc")


if __name__ == "__main__":
    unittest.main()
