import unittest
from unittest.mock import patch

from memory_cache import MemCache


class MemoryCacheTest(unittest.TestCase):
    def test_uses_monotonic_ttl_and_removes_expired_value(self):
        cache = MemCache(max_entries=2)
        with patch("memory_cache.time.monotonic", side_effect=[100.0, 104.0, 106.0]):
            cache.set("quote", {"price": 100}, ttl=5)
            self.assertEqual({"price": 100}, cache.get("quote"))
            self.assertIsNone(cache.get("quote"))

    def test_capacity_is_bounded_and_prefers_soonest_expiring_entry(self):
        cache = MemCache(max_entries=2)
        with patch("memory_cache.time.monotonic", return_value=100.0):
            cache.set("long", 1, ttl=100)
            cache.set("short", 2, ttl=10)
            cache.set("new", 3, ttl=50)
            self.assertEqual(2, len(cache))
            self.assertEqual(1, cache.get("long"))
            self.assertIsNone(cache.get("short"))
            self.assertEqual(3, cache.get("new"))

    def test_non_positive_ttl_deletes_existing_value(self):
        cache = MemCache()
        cache.set("temporary", 1)
        cache.set("temporary", 2, ttl=0)
        self.assertIsNone(cache.get("temporary"))


if __name__ == "__main__":
    unittest.main()
