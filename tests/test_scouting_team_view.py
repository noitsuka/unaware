from unaware.scouting import OpponentScout, _merge


def game(species, lead, won=False, source="replay", identity="gen9ou-1", result=None):
    team = {"species": species, "lead_species": lead, "switches": 4, "revealed": [],
            "archetype": {"archetype": "hyper_offense" if "clefable" in species else "balance"}}
    row = {"id": identity, "turns": 20, "team": team, "won": won, "source": source}
    if result:
        row["our_result"] = result
    return row


OLD = ["clefable", "zoroark", "regieleki", "garchomp", "kingambit", "greattusk"]
NEW = ["blaziken", "serperior", "zoroarkhisui", "toxapex", "staraptor", "regieleki"]


def test_old_team_lead_and_style_are_not_shown_for_a_new_team():
    games = [game(OLD, "clefable", identity=f"gen9ou-{i}") for i in range(3)] + [game(NEW, "zoroarkhisui", identity="gen9ou-9")]
    profile = {**OpponentScout.profile("b3nii", "gen9ou", games), "raw_games": games}
    assert "clefable" in profile["leads"]
    view = OpponentScout.for_team(profile, NEW)
    assert view["team_games"] == 1 and "clefable" not in view["leads"]
    assert view["archetype"] == "balance" and "this team in 1 of 4" in view["summary"]


def test_brand_new_team_reports_no_style_or_lead():
    games = [game(OLD, "clefable", identity=f"gen9ou-{i}") for i in range(3)]
    view = OpponentScout.for_team({**OpponentScout.profile("x", "gen9ou", games), "raw_games": games},
                                  ["kingambit", "gholdengo", "greattusk", "dragapult", "gliscor", "ogerpon"])
    assert view["team_games"] == 0 and view["leads"] == {} and view["archetype"] == "unknown"
    assert "new team" in view["summary"]


def test_our_record_against_the_player_and_no_double_count():
    local = [game(NEW, "blaziken", source="local", identity="battle-gen9ou-2698871192-abc", result="loss")]
    public = [game(NEW, "blaziken", identity="gen9ou-2698871192"), game(NEW, "blaziken", identity="gen9ou-555555555")]
    games = _merge(public, local)
    assert len(games) == 2
    view = OpponentScout.for_team({**OpponentScout.profile("x", "gen9ou", games), "raw_games": games}, NEW)
    assert view["record"] == {"loss": 1} and "vs us: 0W 1L" in view["summary"]


def test_local_replay_gives_the_full_opponent_team(tmp_path):
    log = "\n".join(["|player|p1|Noitsuka|1", "|player|p2|Rival|2", "|poke|p1|Zamazenta|", "|poke|p2|Kingambit, M|",
                     "|poke|p2|Gholdengo|", "|start", "|switch|p1a: Zamazenta|Zamazenta|100/100",
                     "|switch|p2a: Kingambit|Kingambit, M|100/100", "|turn|1",
                     "|move|p2a: Kingambit|Sucker Punch|p1a: Zamazenta", "|-enditem|p2a: Kingambit|Air Balloon", "|turn|2"])
    path = tmp_path / "r.html"
    path.write_text(f'<script type="text/plain" class="battle-log-data">{log}\n</script>', encoding="utf-8")
    team = OpponentScout._local_team(str(path), "rival")
    assert team["lead_species"] == "kingambit" and set(team["species"]) == {"kingambit", "gholdengo"}
    assert "suckerpunch" in team["revealed"][0]["moves"]

