import importlib.util
import unittest

from unaware.teams import TeamMember, export_team, parse_team, to_id, validate_team


EXPORT = """=== [gen9ou] Example ===

Sparky (Pikachu) (F) @ Light Ball
Ability: Lightning Rod
Level: 50
Shiny: Yes
Tera Type: Flying
EVs: 252 SpA / 4 SpD / 252 Spe
Timid Nature
IVs: 0 Atk / 30 Def
- Thunderbolt
- Grass Knot
- Volt Switch
- Protect

Great Tusk @ Leftovers
Ability: Protosynthesis
EVs: 252 HP / 252 Def / 4 Spe
Impish Nature
- Earthquake
- Rapid Spin
- Knock Off
- Stealth Rock
"""


class TeamTests(unittest.TestCase):
    def test_export_round_trip_preserves_all_supported_fields(self):
        members = parse_team(EXPORT)
        self.assertEqual(len(members), 2)
        pikachu = members[0]
        self.assertEqual((pikachu.species, pikachu.nickname, pikachu.gender), ("Pikachu", "Sparky", "F"))
        self.assertEqual(pikachu.evs, {"hp": 0, "atk": 0, "def": 0, "spa": 252, "spd": 4, "spe": 252})
        self.assertEqual(pikachu.ivs["atk"], 0)
        self.assertEqual(pikachu.ivs["hp"], 31)
        self.assertTrue(pikachu.shiny)
        self.assertEqual(pikachu.tera_type, "Flying")
        self.assertEqual(parse_team(export_team(members)), members)

    def test_defaults_and_independent_stat_dictionaries(self):
        first, second = TeamMember("Pikachu"), TeamMember("Raichu")
        first.ivs["atk"] = 0
        self.assertEqual(second.ivs["atk"], 31)
        self.assertEqual(first.level, 100)
        self.assertEqual(first.evs["hp"], 0)

    def test_out_of_range_stats_are_validation_errors_not_clamped(self):
        text = "Pikachu\nEVs: 253 HP / 252 Atk / 20 Spe\nIVs: 32 Atk / -1 Spe\n- Thunderbolt"
        errors = validate_team(text, "gen9ou")
        self.assertTrue(any("EVs must" in error for error in errors))
        self.assertTrue(any("IVs must" in error for error in errors))

    def test_duplicate_stat_rejected(self):
        with self.assertRaisesRegex(ValueError, "repeated"):
            parse_team("Pikachu\nEVs: 252 Atk / 12 Atk\n- Thunderbolt")

    def test_unrecognized_fields_not_silently_dropped(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            parse_team("Pikachu\nMood: Angry\n- Thunderbolt")

    def test_missing_blank_separator_is_actionable(self):
        with self.assertRaisesRegex(ValueError, "Separate Pokémon"):
            parse_team("Pikachu\n- Thunderbolt\nRaichu\n- Surf")

    def test_multiple_exported_teams_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "one team at a time"):
            parse_team("=== first ===\nPikachu\n- Thunderbolt\n\n=== second ===\nRaichu\n- Surf")

    def test_random_battle_requires_no_custom_team(self):
        self.assertEqual(validate_team("", "gen9randombattle"), [])
        self.assertTrue(any("come from the server" in error for error in validate_team(EXPORT, "gen9randombattle")))

    def test_little_cup_level_and_duplicate_moves(self):
        errors = validate_team("Pikachu\n- Thunderbolt\n- thunderbolt", "gen9lc")
        self.assertTrue(any("Level: 5" in error for error in errors))
        self.assertTrue(any("repeated moves" in error for error in errors))

    def test_name_normalization_handles_accents_gender_and_punctuation(self):
        self.assertEqual(to_id("Flabébé"), "flabebe")
        self.assertEqual(to_id("Nidoran♀"), "nidoranf")
        self.assertEqual(to_id("Landorus-Therian"), "landorustherian")

    @unittest.skipUnless(importlib.util.find_spec("poke_env"), "poke-env not installed")
    def test_known_team_passes_local_validation(self):
        self.assertEqual(validate_team(EXPORT, "gen9ou"), [])

    @unittest.skipUnless(importlib.util.find_spec("poke_env"), "poke-env not installed")
    def test_unknown_species_and_moves_are_reported(self):
        errors = validate_team("Imaginarymon\n- Imaginary Move", "gen9ou")
        self.assertTrue(any("species not found" in error for error in errors))
        self.assertTrue(any("unknown move" in error for error in errors))


if __name__ == "__main__":
    unittest.main()


def test_standard_export_fields_from_sites_are_accepted():
    from unaware.teams import parse_team
    text = "Kingambit @ Leftovers\nAbility: Supreme Overlord\nGender: M\nHappiness: 255\nTera Type: Dark\nEVs: 252 Atk / 4 SpD / 252 Spe\nAdamant Nature\n- Swords Dance\n- Kowtow Cleave\n- Sucker Punch\n- Iron Head\n"
    member = parse_team(text)[0]
    assert member.gender == "M" and member.moves[0] == "Swords Dance"
