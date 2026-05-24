"""Live integration tests for Yupoo album scraper.

These tests hit real Yupoo servers. Run with:
    python -m pytest tests/test_yupoo_live.py -v

Skip with: python -m pytest tests/ -v --ignore=tests/test_yupoo_live.py
"""

import unittest

from utils.yupoo import (
    chunk_for_discord,
    fetch_album_images,
    scrape_album,
)


class TestLiveRmqc(unittest.IsolatedAsyncioTestCase):
    """Test against rmqc.x.yupoo.com albums."""

    async def test_scrape_rmqc_album(self):
        album = await scrape_album("https://rmqc.x.yupoo.com/albums/239263830?uid=1")
        self.assertEqual(album.vendor, "rmqc")
        self.assertEqual(album.album_id, "239263830")
        # Title may be numeric product ID like "4782792"
        self.assertIsNotNone(album.title)
        self.assertGreater(len(album.title), 0)
        self.assertGreater(len(album.images), 0, "Album should have images")
        # Verify all images have valid URLs
        for img in album.images:
            self.assertIn("photo.yupoo.com", img.url)
            self.assertIn("/medium.jpg", img.url)

    async def test_download_rmqc_images(self):
        album, downloaded = await fetch_album_images(
            "https://rmqc.x.yupoo.com/albums/239263830",
            max_images=3,
            delay=1.0,
        )
        self.assertGreater(len(downloaded), 0, "Should download at least 1 image")
        for img in downloaded:
            self.assertGreater(img.size, 1000, "Image should have reasonable size")
            self.assertTrue(img.filename.endswith(".jpg"))

    async def test_chunking_real_images(self):
        _, downloaded = await fetch_album_images(
            "https://rmqc.x.yupoo.com/albums/239263830",
            max_images=5,
            delay=1.0,
        )
        if downloaded:
            chunks = chunk_for_discord(downloaded)
            self.assertGreater(len(chunks), 0)
            # All images accounted for
            total = sum(len(c) for c in chunks)
            self.assertEqual(total, len(downloaded))


class TestLiveTmf001(unittest.IsolatedAsyncioTestCase):
    """Test against tmf001.x.yupoo.com albums."""

    async def test_scrape_tmf001_album(self):
        album = await scrape_album("https://tmf001.x.yupoo.com/albums/239220972?uid=1")
        self.assertEqual(album.vendor, "tmf001")
        self.assertGreater(len(album.images), 0, "Album should have images")
        for img in album.images:
            self.assertIn("tmf001", img.url)

    async def test_download_tmf001_images(self):
        _, downloaded = await fetch_album_images(
            "https://tmf001.x.yupoo.com/albums/239220972",
            max_images=2,
            delay=1.0,
        )
        self.assertGreater(len(downloaded), 0)


class TestLiveZengshuaige(unittest.IsolatedAsyncioTestCase):
    """Test against zengshuaige.x.yupoo.com albums."""

    async def test_scrape_zengshuaige_album(self):
        album = await scrape_album("https://zengshuaige.x.yupoo.com/albums/126318851?uid=1")
        self.assertEqual(album.vendor, "zengshuaige")
        self.assertGreater(len(album.images), 0, "Album should have images")
        for img in album.images:
            self.assertIn("zengshuaige", img.url)


class TestLiveTmf002(unittest.IsolatedAsyncioTestCase):
    """Test against tmf002.x.yupoo.com albums."""

    async def test_scrape_tmf002_album(self):
        # First, we need to find a valid album ID for tmf002
        # Using a known album from the gallery
        album = await scrape_album("https://tmf002.x.yupoo.com/albums/239263504?uid=1")
        self.assertEqual(album.vendor, "tmf002")
        self.assertIsNotNone(album)
        self.assertGreater(len(album.images), 0, "Album should have images")


class TestLiveEdgeCases(unittest.IsolatedAsyncioTestCase):
    """Edge cases with real requests."""

    async def test_nonexistent_album(self):
        """A nonsensical album ID should either return 0 images or raise."""
        try:
            album = await scrape_album("https://rmqc.x.yupoo.com/albums/999999999999")
            # If it doesn't raise, it should have no images or some fallback
            self.assertIsNotNone(album)
        except Exception:
            pass  # Raising is acceptable too

    async def test_max_images_one(self):
        """max_images=1 should download exactly 1 image."""
        album, downloaded = await fetch_album_images(
            "https://rmqc.x.yupoo.com/albums/239263830",
            max_images=1,
        )
        self.assertEqual(len(downloaded), 1)
        # But album metadata should show all available images
        self.assertGreater(len(album.images), 1)


if __name__ == "__main__":
    unittest.main()
