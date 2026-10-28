"""Official protocol fixtures and honest sampling/date/visibility limits."""
import json
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

from unaware.replay_research import BASE_URL, ReplayResearch, parse_public_log


LOG = """|player|p1|Private-looking public player name|1
|player|p2|Another player name|2
|poke|p1|Kingambit, M|item
|poke|p1|Great Tusk|item
|poke|p1|Dragapult, F|item
|poke|p1|Corviknight, M|item
|poke|p1|Iron Valiant|item
|poke|p1|Gholdengo|item
|poke|p2|Skarmory, M|item
|poke|p2|Blissey, F|item
|poke|p2|Gliscor, M|item
|poke|p2|Alomomola, F|item
|poke|p2|Clodsire, M|item
|poke|p2|Garganacl, M|item
|switch|p1a: Private nickname|Kingambit, M|100/100
|switch|p2a: Bird nickname|Skarmory, M|100/100
|turn|1
|move|p1a: Private nickname|Swords Dance|p1a: Private nickname
|move|p2a: Bird nickname|Whirlwind|p1a: Private nickname
|drag|p1a: Elephant nickname|Great Tusk|100/100
|-activate|p1a: Elephant nickname|ability: Protosynthesis
|turn|2
|move|p1a: Elephant nickname|Rapid Spin|p2a: Bird nickname
|-sideend|p1: Private-looking public player name|move: Stealth Rock
|-damage|p1a: Elephant nickname|85/100|[from] item: Rocky Helmet|[of] p2a: Bird nickname
|move|p2a: Bird nickname|Roost|p2a: Bird nickname
|-heal|p2a: Bird nickname|100/100|[from] item: Leftovers
|turn|3
|move|p2a: Bird nickname|Whirlwind|p1a: Elephant nickname
|turn|4
|move|p2a: Bird nickname|Whirlwind|p1a: Elephant nickname
|-item|p1a: Elephant nickname|Heavy-Duty Boots
|-ability|p2a: Bird nickname|Sturdy
|c|Private-looking public player name|never cache this chat!
|win|Private-looking public player name
"""


class ReplayResearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.store = ReplayResearch(self.base)
        self.now = int(time.time())

    def row(self, number=1, **changes):
        return {"id": f"gen9ou-{number}", "rating": 1850, "uploadtime": self.now - 600,
                "format": "[Gen 9] OU", "private": 0, "password": None,
                "players": ["never persist this player", "nor this player"], **changes}

    def fake_network(self, recent=None, rated=None, logs=None):
        calls = []
        def fetch(url, deadline):
            calls.append(url)
            parsed = urllib.parse.urlparse(url)
            if parsed.path == "/search.json":
                query = urllib.parse.parse_qs(parsed.query)
                if query.get("sort") == ["rating"]:
                    return json.dumps(rated or [])
                return json.dumps((recent or {}).get(int(query["page"][0]), []))
            return (logs or {}).get(parsed.path, LOG)
        return fetch, calls

    def test_public_log_retains_revealed_facts_not_names_chat_or_hidden_builds(self):
        parsed = parse_public_log(LOG)
        self.assertTrue(parsed["finished"])
        self.assertEqual(parsed["turns"], 4)
        self.assertTrue(all(team["preview_complete"] for team in parsed["teams"]))
        foe = next(mon for mon in parsed["teams"][1]["revealed"] if mon["species"] == "skarmory")
        self.assertEqual(foe["moves"], ["roost", "whirlwind"])
        self.assertEqual(foe["items"], ["leftovers", "rockyhelmet"])
        self.assertEqual(foe["abilities"], ["sturdy"])
        tusk = next(mon for mon in parsed["teams"][0]["revealed"] if mon["species"] == "greattusk")
        self.assertNotIn("rockyhelmet", tusk["items"])
        self.assertIn("protosynthesis", tusk["abilities"])
        self.assertEqual(parsed["hazard_removal_events"], {"stealthrock": 1})
        self.assertEqual(parsed["phazing_events"], {"whirlwind": 3})
        serialized = json.dumps(parsed)
        for private in ("Private nickname", "Elephant nickname", "Bird nickname", "player name", "never cache", "EVs", "IVs", "nature", "stats"):
            self.assertNotIn(private, serialized)

    def test_old_top_ratings_low_ratings_private_password_and_other_tiers_are_not_downloaded(self):
        rows = [self.row(1), self.row(2, uploadtime=self.now - 121 * 86400), self.row(3, rating=1600),
                self.row(4, private=1), self.row(5, password="secret"), self.row(6, rating=None),
                self.row(7, id="gen9uu-7"), self.row(8, uploadtime=self.now + 86400),
                self.row(9, id="gen9ou-9-secretpw")]
        fetch, calls = self.fake_network({1: rows}, [self.row(10, rating=2200, uploadtime=self.now - 400 * 86400)])
        with patch.object(self.store, "_fetch", side_effect=fetch):
            result = self.store.refresh("gen9ou")
        logs = [url for url in calls if url.endswith('.log')]
        self.assertEqual(logs, [BASE_URL + "gen9ou-1.log"])
        self.assertEqual(result["sample_size"], 1)
        self.assertEqual(result["sources"][0]["rating"], 1850)
        self.assertIn("uploaded_at", result["sources"][0])
        self.assertIn("Small sample", " ".join(result["warnings"]))
        self.assertNotIn("sort=date", " ".join(calls))

    def test_date_pages_and_recent_rating_complement_are_bounded_and_deduplicated(self):
        fetch, calls = self.fake_network({1: [self.row(1, rating=1600)], 2: [self.row(2)], 3: [self.row(2)]}, [self.row(2), self.row(3, rating=1950)])
        with patch.object(self.store, "_fetch", side_effect=fetch):
            result = self.store.refresh("gen9ou", limit=6)
        self.assertEqual(len([url for url in calls if 'search.json' in url]), 4)
        self.assertEqual(result["sample_size"], 2)
        self.assertEqual({source["id"] for source in result["sources"]}, {"gen9ou-2", "gen9ou-3"})
        self.assertEqual(len([url for url in calls if url.endswith('.log')]), 2)

    def test_move_presence_is_per_replay_not_turn_frequency_and_confidence_stays_small(self):
        fetch, _ = self.fake_network({1: [self.row(1), self.row(2)]})
        with patch.object(self.store, "_fetch", side_effect=fetch):
            result = self.store.refresh("gen9ou", limit=2)
        foe = result["species"]["skarmory"]
        self.assertEqual(foe["moves"]["whirlwind"], 2)
        self.assertEqual(result["tempo"]["phazing_events"]["whirlwind"], 6)
        prior = result["observed_priors"]["species"]["skarmory"]
        self.assertEqual(prior["battles"], 2)
        self.assertEqual(prior["moves"]["whirlwind"], 1)
        self.assertLess(prior["confidence"], .1)
        self.assertTrue(all(info["inferred"] for info in result["archetypes"].values()))
        self.assertEqual(result["archetypes"]["unknown"]["confidence"], 0)
        pair = next(entry for entry in result["pairs"] if set(entry["species"]) == {"kingambit", "greattusk"})
        self.assertEqual(pair["teams"], 2)

    def test_cache_only_load_never_connects_and_keeps_formats_separate(self):
        fetch, _ = self.fake_network({1: [self.row()]})
        with patch.object(self.store, "_fetch", side_effect=fetch):
            self.store.refresh("gen9ou", limit=1)
        with patch.object(self.store, "_fetch", side_effect=AssertionError('load contacted network')):
            self.assertEqual(self.store.load("gen9ou")["sample_size"], 1)
            self.assertIsNone(self.store.load("gen9uu"))
            self.assertEqual(self.store.observed_priors("gen9uu")["species"], {})
        raw = (self.store.cache_dir / 'gen9ou.json').read_text(encoding='utf-8')
        self.assertNotIn('never persist this player', raw)
        self.assertNotIn('Private nickname', raw)
        self.assertNotIn('never cache this chat', raw)

    def test_expired_cache_does_not_masquerade_as_current_research(self):
        fetch, _ = self.fake_network({1: [self.row()]})
        with patch.object(self.store, "_fetch", side_effect=fetch):
            self.store.refresh("gen9ou", limit=1)
        with patch('unaware.replay_research.time.time', return_value=self.now + 121 * 86400):
            self.assertIsNone(self.store.load('gen9ou'))
            self.assertIn('120-day', self.store.last_error)
        self.assertTrue((self.store.cache_dir / 'gen9ou.json').is_file())

    def test_failed_refresh_keeps_previous_public_sample_and_original_sources(self):
        fetch, _ = self.fake_network({1: [self.row()]})
        with patch.object(self.store, "_fetch", side_effect=fetch):
            original = self.store.refresh("gen9ou", limit=1)
        with patch.object(self.store, "_fetch", side_effect=urllib.error.URLError('offline')):
            fallback = self.store.refresh('gen9ou', limit=1)
        self.assertEqual(fallback['sources'], original['sources'])
        self.assertEqual(fallback['sample_size'], 1)
        self.assertIn('public cache', ' '.join(fallback['warnings']))

    def test_no_qualifying_data_returns_honest_warning_without_creating_fake_cache(self):
        fetch, calls = self.fake_network({1: [self.row(rating=1600)]}, [self.row(2, uploadtime=self.now - 300 * 86400)])
        with patch.object(self.store, '_fetch', side_effect=fetch):
            result = self.store.refresh('gen9ou')
        self.assertEqual(result['sample_size'], 0)
        self.assertEqual(result['observed_priors']['species'], {})
        self.assertFalse(result['fresh'])
        self.assertIn('No recent public replay', ' '.join(result['warnings']))
        self.assertFalse(any(url.endswith('.log') for url in calls))
        self.assertFalse((self.store.cache_dir / 'gen9ou.json').exists())

    def test_new_upload_of_old_battle_is_excluded_if_protocol_reveals_play_time(self):
        fetch, _ = self.fake_network({1: [self.row()]}, logs={'/gen9ou-1.log': f'|t:|{self.now - 200 * 86400}\n' + LOG})
        with patch.object(self.store, '_fetch', side_effect=fetch):
            result = self.store.refresh('gen9ou', limit=1)
        self.assertEqual(result['sample_size'], 0)
        self.assertIn('old or invalid battle date', ' '.join(result['warnings']))

    def test_illusion_transform_and_called_moves_do_not_create_false_native_sets(self):
        illusion = '''|poke|p1|Gholdengo|item
|poke|p1|Zoroark-Hisui|item
|poke|p2|Ditto|item
|switch|p1a: Hidden fox|Gholdengo|100/100
|switch|p2a: Blob|Ditto|100/100
|move|p1a: Hidden fox|Shadow Ball|p2a: Blob
|replace|p1a: Fox nickname|Zoroark-Hisui|100/100
|-transform|p2a: Blob|p1a: Fox nickname
|move|p2a: Blob|Shadow Ball|p1a: Fox nickname
|move|p1a: Fox nickname|Metronome|p1a: Fox nickname
|move|p1a: Fox nickname|Sacred Fire|p2a: Blob|[from] move: Metronome
|win|Some player
'''
        parsed = parse_public_log(illusion)
        fox = parsed['teams'][0]['revealed'][0]
        self.assertEqual(fox['species'], 'zoroarkhisui')
        self.assertEqual(fox['moves'], ['metronome', 'shadowball'])
        self.assertEqual(parsed['teams'][0]['lead_species'], 'zoroarkhisui')
        self.assertEqual(parsed['teams'][1]['revealed'][0]['moves'], [])

    def test_malformed_cache_and_invalid_requests_are_explicit(self):
        self.store.cache_dir.mkdir(parents=True)
        (self.store.cache_dir / 'gen9ou.json').write_text('{bad-json', encoding='utf-8')
        self.assertIsNone(self.store.load('gen9ou'))
        self.assertIn('Invalid', self.store.last_error)
        for request in (lambda: self.store.refresh('../outside'), lambda: self.store.refresh('gen9ou', min_rating=True),
                        lambda: self.store.refresh('gen9ou', limit=100), lambda: self.store.refresh('gen9ou', max_duration=0)):
            with self.assertRaises(ValueError):
                request()

    def test_budget_exhaustion_does_not_begin_another_download(self):
        # Start time=0, first listing=0, then budget has passed. The fake source
        # is instantaneous; this checks scheduling rather than flaky sleeps.
        fetch, calls = self.fake_network({1: [self.row()]})
        with patch.object(self.store, '_fetch', side_effect=fetch), patch('unaware.replay_research.time.monotonic', side_effect=[0, 0, 11, 11, 11]):
            result = self.store.refresh('gen9ou', limit=1, max_duration=10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['sample_size'], 0)
        self.assertIn('time limit', ' '.join(result['warnings']))


if __name__ == '__main__':
    unittest.main()
