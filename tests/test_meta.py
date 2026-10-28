import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from unaware.meta import BASE_URL, MetaStore, MetaUpdateError


# Deliberately small synthetic data: no live network access is needed in tests.
PAYLOAD = json.dumps({"info": {"metagame": "gen9ou"}, "data": {
    "Great Tusk": {"Moves": {"earthquake": 3, "rapidspin": 2}, "Items": {"leftovers": 5}},
    "Landorus-Therian": {"Moves": {"uturn": 10}},
}})


class MetaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MetaStore(Path(self.directory.name))

    def routes(self, rating=1695, month="2026-08"):
        return {
            BASE_URL: '<a href="2026-07/">old</a><a href="2026-08/">latest</a>',
            f"{BASE_URL}{month}/chaos/": f'<a href="gen9ou-{rating}.json">stats</a>',
            f"{BASE_URL}{month}/chaos/gen9ou-{rating}.json": PAYLOAD,
        }

    def test_latest_published_month_used_not_current_calendar_month(self):
        routes = self.routes()
        with patch.object(self.store, "_fetch", side_effect=routes.__getitem__):
            snapshot = self.store.update("gen9ou")
        self.assertEqual(snapshot.month, "2026-08")
        self.assertEqual(snapshot.rating, 1695)
        self.assertEqual(snapshot.species("great-tusk")["Moves"]["earthquake"], 3)
        self.assertEqual(snapshot.species("landorustherian")["Moves"]["uturn"], 10)
        self.assertEqual(snapshot.species("Unknown Species"), {})
        self.assertTrue(snapshot.downloaded_at)

    def test_rating_fallback_is_format_specific_and_visible(self):
        routes = self.routes(rating=1630)
        routes[f"{BASE_URL}2026-08/chaos/"] += '<a href="gen9ou-1760.json">high</a><a href="gen9uu-1695.json">other format</a>'
        with patch.object(self.store, "_fetch", side_effect=routes.__getitem__):
            snapshot = self.store.update("gen9ou", rating=1695)
        self.assertEqual(snapshot.rating, 1630)
        self.assertTrue(any("1695 unavailable" in warning for warning in snapshot.warnings))

    def test_missing_format_in_latest_month_falls_back_with_warning(self):
        routes = self.routes(month="2026-07")
        routes[f"{BASE_URL}2026-08/chaos/"] = '<a href="gen9uu-1630.json">different format</a>'
        with patch.object(self.store, "_fetch", side_effect=routes.__getitem__):
            snapshot = self.store.update("gen9ou")
        self.assertEqual(snapshot.month, "2026-07")
        self.assertTrue(any("latest published month is 2026-08" in warning for warning in snapshot.warnings))

    def test_successful_download_cache_load_retains_source_and_warns(self):
        routes = self.routes()
        with patch.object(self.store, "_fetch", side_effect=routes.__getitem__):
            original = self.store.update("gen9ou")
        loaded = self.store.load("gen9ou")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.data, original.data)
        self.assertEqual(loaded.source_url, original.source_url)
        self.assertTrue(any("Local cache" in warning for warning in loaded.warnings))

    def test_failed_refresh_preserves_existing_cache(self):
        routes = self.routes()
        with patch.object(self.store, "_fetch", side_effect=routes.__getitem__):
            self.store.update("gen9ou")
        path = Path(self.directory.name) / "gen9ou.json"
        previous = path.read_bytes()
        with patch.object(self.store, "_fetch", side_effect=urllib.error.URLError("offline")):
            with self.assertRaises(MetaUpdateError):
                self.store.update("gen9ou")
        self.assertEqual(path.read_bytes(), previous)
        self.assertIsNotNone(self.store.load("gen9ou"))

    def test_bad_remote_json_is_not_saved(self):
        routes = self.routes()
        routes[f"{BASE_URL}2026-08/chaos/gen9ou-1695.json"] = '{"data": []}'
        with patch.object(self.store, "_fetch", side_effect=routes.__getitem__):
            with self.assertRaises(MetaUpdateError):
                self.store.update("gen9ou")
        self.assertIsNone(self.store.load("gen9ou"))

    def test_corrupt_cache_is_rejected_without_fabricating_snapshot(self):
        (Path(self.directory.name) / "gen9ou.json").write_text("{broken", encoding="utf-8")
        self.assertIsNone(self.store.load("gen9ou"))
        self.assertIn("Invalid", self.store.last_error)

    def test_random_battle_has_no_invented_usage_meta(self):
        with patch.object(self.store, "_fetch") as fetch:
            with self.assertRaisesRegex(MetaUpdateError, "Random Battle"):
                self.store.update("gen9randombattle")
            fetch.assert_not_called()

    def test_unsupported_format_cannot_escape_cache_directory(self):
        with self.assertRaises(ValueError):
            self.store.load("../../private")
        with self.assertRaises(ValueError):
            self.store.update("gen9doublesou")

    def test_incompatible_cache_schema_rejected(self):
        (Path(self.directory.name) / "gen9ou.json").write_text('{"schema":99}', encoding="utf-8")
        self.assertIsNone(self.store.load("gen9ou"))


if __name__ == "__main__":
    unittest.main()
