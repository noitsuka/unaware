"""Build-based team plans and small, explainable tactical preferences.

The official search still evaluates matchups and resolves battle mechanics.
A team plan supplies a bounded prior, never a prescribed opening or action.
"""
from __future__ import annotations


from poke_env.battle import Move, Pokemon
from poke_env.data import GenData
from poke_env.stats import compute_raw_stats

from .telemetry import action_key
from .teams import STAT_LABELS, to_id


LABELS = {"hyper_offense": "Hyper offense", "bulky_offense": "Bulky offense",
          "balance": "Balance", "semi_stall": "Semi-stall", "stall": "Stall", "weather": "Weather"}
STATS = tuple(STAT_LABELS)
SETUP = {"swordsdance", "nastyplot", "dragondance", "quiverdance", "shellsmash",
         "calmmind", "bulkup", "bellydrum", "agility", "rockpolish", "shiftgear",
         "tidyup", "filletaway", "coil", "tailglow", "victorydance", "noretreat"}
HAZARDS = {"stealthrock", "spikes", "toxicspikes", "stickyweb", "stoneaxe", "ceaselessedge"}
SCREENS = {"reflect", "lightscreen", "auroraveil"}
PIVOTS = {"uturn", "voltswitch", "flipturn", "partingshot", "teleport", "chillyreception"}
REMOVAL = {"rapidspin", "defog", "mortalspin", "tidyup", "courtchange"}
PHASING = {"whirlwind", "roar", "dragontail", "circlethrow", "haze", "clearsmog", "encore"}
OFFENSIVE_ITEMS = {"lifeorb", "choiceband", "choicespecs", "boosterenergy", "blackglasses",
                   "expertbelt", "loadeddice"}
ENTRY_HAZARDS = {"STEALTH_ROCK", "SPIKES", "TOXIC_SPIKES", "STICKY_WEB"}
WEATHER_SETTERS = {"drizzle": "rain", "drought": "sun", "sandstream": "sand", "snowwarning": "snow",
                   "raindance": "rain", "sunnyday": "sun", "sandstorm": "sand", "snowscape": "snow"}
WEATHER_ABILITIES = {"swiftswim": "rain", "raindish": "rain", "dryskin": "rain", "hydration": "rain",
                     "chlorophyll": "sun", "solarpower": "sun", "protosynthesis": "sun",
                     "sandrush": "sand", "sandforce": "sand", "slushrush": "snow", "icebody": "snow"}


def weather_plan(rows) -> dict:
    """Identify actual climate interactions; a lone weather setter is not a team plan."""
    plans = {}
    data = GenData.from_gen(9)
    for climate in ("rain", "sun", "sand", "snow"):
        setters, abusers = [], []
        for row in rows:
            moves, ability = row.get("moves", []), row.get("ability", "")
            if WEATHER_SETTERS.get(ability) == climate or any(WEATHER_SETTERS.get(move) == climate for move in moves):
                setters.append(row["species"])
            attacks = [data.moves.get(move, {}) for move in moves]
            benefited = WEATHER_ABILITIES.get(ability) == climate
            benefited |= climate == "rain" and (any(move in {"thunder", "hurricane", "weatherball"} for move in moves)
                or sum(detail.get("type") == "Water" and detail.get("category") != "Status" for detail in attacks) > 0)
            benefited |= climate == "sun" and (any(move in {"solarbeam", "solarblade", "growth", "hydrosteam"} for move in moves)
                or sum(detail.get("type") == "Fire" and detail.get("category") != "Status" for detail in attacks) > 0)
            benefited |= climate == "snow" and any(move in {"auroraveil", "blizzard"} for move in moves)
            if benefited:
                abusers.append(row["species"])
        plans[climate] = {"weather": climate, "setters": setters, "abusers": abusers,
                          "coherent": bool(setters and len(set(abusers) - set(setters)) >= 2)}
    return max(plans.values(), key=lambda plan: (plan["coherent"], bool(plan["setters"]), len(plan["abusers"])))


def base_species_id(species: str) -> str:
    """Match a revealed forme to its exported plan without changing state."""
    identifier = to_id(species)
    entry = GenData.from_gen(9).pokedex.get(identifier, {})
    return to_id(entry.get("baseSpecies", identifier))


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _n(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _stats(value, key, default):
    supplied = _get(value, key)
    if isinstance(supplied, dict):
        return [max(0, min(252 if key == "evs" else 31, int(_n(supplied.get(stat), default)))) for stat in STATS]
    if isinstance(supplied, (tuple, list)) and len(supplied) == 6:
        return [int(_n(item, default)) for item in supplied]
    return [default] * 6


def _member(member) -> dict:
    data = GenData.from_gen(9)
    species = to_id(str(_get(member, "species", "")))
    moves = list(dict.fromkeys(to_id(str(move)) for move in (_get(member, "moves", []) or []) if move))
    item, ability = to_id(str(_get(member, "item", ""))), to_id(str(_get(member, "ability", "")))
    entry = data.pokedex.get(species)
    if not entry:
        return {"species": species, "roles": ["unknown"], "offense_score": 0.0, "bulk_score": 0.0,
                "speed_score": 0.0, "moves": moves, "item": item, "ability": ability, "known": False}
    evs, ivs = _stats(member, "evs", 0), _stats(member, "ivs", 31)
    nature = to_id(str(_get(member, "nature", "Serious") or "Serious"))
    level = max(1, min(100, int(_n(_get(member, "level", 100), 100))))
    try:
        computed = dict(zip(STATS, compute_raw_stats(species, evs, ivs, level, nature, data)))
    except (KeyError, ValueError, TypeError):
        computed = dict(zip(STATS, compute_raw_stats(species, evs, ivs, level, "serious", data)))
    # Scale lower-level formats to the same reference for role comparison.
    scale = 100 / level
    hp = max(1, (computed["hp"] - 10) * scale + 10)
    defense = max(1, (computed["def"] - 5) * scale + 5)
    special_defense = max(1, (computed["spd"] - 5) * scale + 5)
    if item == "eviolite":
        defense *= 1.5
        special_defense *= 1.5
    if item == "assaultvest":
        special_defense *= 1.5
    bulk = min(1.2, .4 * hp / 430 + .3 * defense / 330 + .3 * special_defense / 300)
    speed = max(1, (computed["spe"] - 5) * scale + 5) * (1.5 if item == "choicescarf" else 1)
    move_data = [data.moves.get(move, {}) for move in moves]
    attacks = [move for move, detail in zip(moves, move_data)
               if detail.get("category") in {"Physical", "Special"} and move not in REMOVAL]
    heals = [move for move, detail in zip(moves, move_data) if detail.get("heal")]
    if "rest" in moves:
        heals.append("rest")
    reliable_recovery = bool(heals or ability == "regenerator" or ability == "poisonheal" and item == "toxicorb")
    defensive_evs, attack_evs = evs[0] + evs[2] + evs[4], evs[1] + evs[3]
    setup = any(move in SETUP for move in moves) and bool(attacks)
    attacking_stat = max(computed["atk"], computed["spa"]) * scale
    offense = min(1.2, attacking_stat / 400 * .55 + attack_evs / 252 * .35 + len(attacks) / 4 * .2)
    aggressive = bool(attacks) and (attack_evs >= 160 or len(attacks) >= 3
                                    or setup and speed >= 280 and defensive_evs < 256
                                    or item in OFFENSIVE_ITEMS)
    utility_count = len(moves) - len(attacks)
    wall = (bulk >= .82 and (reliable_recovery and defensive_evs >= 192
                            or defensive_evs >= 384 and utility_count >= 2))
    status = any(detail.get("status") or (detail.get("secondary") or {}).get("status") for detail in move_data)
    roles = []
    def role(name, when):
        if when:
            roles.append(name)
    role("hazard_setter", any(move in HAZARDS for move in moves) or ability == "toxicdebris")
    role("web_setter", "stickyweb" in moves)
    role("screens", any(move in SCREENS for move in moves))
    role("suicide_lead", item == "focussash" and defensive_evs < 192
         and ("hazard_setter" in roles or "screens" in roles)
         or "explosion" in moves and "hazard_setter" in roles)
    role("setup_sweeper", setup)
    role("offensive_attacker", aggressive)
    role("bulky_attacker", aggressive and bulk >= .82)
    role("wall", wall)
    role("recovery", reliable_recovery)
    role("status_spreader", status)
    role("pivot", any(move in PIVOTS for move in moves))
    role("hazard_removal", any(move in REMOVAL for move in moves))
    role("wallbreaker", aggressive and (item in {"choiceband", "choicespecs", "lifeorb"}
                                        or setup and offense >= .72))
    priority = any(_n(detail.get("priority")) > 0 and detail.get("category") in {"Physical", "Special"}
                   for detail in move_data)
    role("cleaner", aggressive and (speed >= 330 or priority or setup and "bulky_attacker" in roles))
    role("speed_control", item == "choicescarf" or any(move in {"thunderwave", "stickyweb", "tailwind", "trickroom"} for move in moves))
    role("weather_setter", ability in {"drizzle", "drought", "sandstream", "snowwarning"}
         or any(move in {"raindance", "sunnyday", "sandstorm", "snowscape"} for move in moves))
    return {"species": species, "roles": roles or ["support"], "moves": moves, "item": item,
            "ability": ability, "offense_score": round(offense, 3), "bulk_score": round(bulk, 3),
            "speed_score": round(min(1.5, speed / 400), 3), "known": True,
            "evs": dict(zip(STATS, evs))}


def _infer_archetype(known):
    def count(name):
        return sum(name in row["roles"] for row in known)
    aggressive = count("offensive_attacker")
    offensive_setup = sum("setup_sweeper" in row["roles"] and "offensive_attacker" in row["roles"] for row in known)
    support_lead = count("suicide_lead") + count("web_setter") + count("screens")
    support_lead += sum("hazard_setter" in row["roles"] and "offensive_attacker" in row["roles"] for row in known)
    if known and offensive_setup >= 3 and aggressive >= 4 and support_lead:
        inferred, confidence = "hyper_offense", .9
        reason = "The team pairs field setup with several setup attackers; pressure and getting the sweepers going is the main plan."
    elif count("wall") >= 5 and count("recovery") >= 4 and aggressive <= 1 and count("status_spreader") >= 2 and not count("cleaner"):
        inferred, confidence = "stall", .9
        reason = "Mostly walls with recovery and status, wearing down the opponent's resources and HP over the game."
    elif count("wall") >= 4 and count("recovery") >= 3 and aggressive >= 1 and count("cleaner"):
        inferred, confidence = "semi_stall", .85
        reason = "A defensive core with recovery holds the game so an attacker can close it out."
    elif aggressive >= 3 and (count("bulky_attacker") >= 2 or count("pivot") >= 2) and count("wall") <= 3:
        inferred, confidence = "bulky_offense", .82
        reason = "Attackers, bulk and pivots absorb pressure and open room for the decisive hits."
    else:
        inferred, confidence = "balance", .75 if count("wall") and aggressive else .5
        reason = "The team spreads offensive, defensive and support roles; the plan depends on the revealed matchup."
    if not known:
        confidence = 0.0
        reason = "Import a valid team to identify its roles and win conditions."
    return inferred, confidence, reason


def analyze_team(members, requested="auto") -> dict:
    """Classify exported builds; a manual style keeps the actual build roles."""
    aliases = {to_id(key): key for key in LABELS} | {"auto": "auto", "automatico": "auto",
               "ho": "hyper_offense", "bo": "bulky_offense", "balanced": "balance", "equilibrado": "balance"}
    chosen = aliases.get(to_id(str(requested or "auto")))
    if chosen is None:
        raise ValueError("Unknown strategy.")
    roles = [_member(member) for member in members]
    known = [row for row in roles if row["known"]]
    inferred, confidence, reason = _infer_archetype(known)
    underlying = inferred
    climate = weather_plan(known)
    if climate["coherent"]:
        inferred, confidence = "weather", max(confidence, .88)
        reason = "The team pairs weather setters with several abusers; keeping the weather and its attack windows up is the plan."
    confidence *= len(known) / max(1, len(roles))
    archetype = inferred if chosen == "auto" else chosen
    def lead_score(row):
        names = row["roles"]
        score = 0.0
        if archetype == "weather":
            score += 9 * (row["species"] in climate["setters"])
            score += 2 * ("pivot" in names)
        elif archetype == "hyper_offense":
            score += 8 * ("suicide_lead" in names) + 6 * ("web_setter" in names) + 5 * ("screens" in names)
            score += 3 * ("hazard_setter" in names)
        else:
            score += 2 * ("hazard_setter" in names) + 1.5 * ("pivot" in names)
            score += row["bulk_score"] * (1.5 if archetype in {"stall", "semi_stall"} else .5)
        return score + row["speed_score"] * .5
    leads = sorted(known, key=lead_score, reverse=True)
    if archetype == "stall":
        conditions = [row["species"] for row in known if "wall" in row["roles"] and "status_spreader" in row["roles"]]
    else:
        conditions = [row["species"] for row in known if "setup_sweeper" in row["roles"] and "offensive_attacker" in row["roles"]]
        conditions += [row["species"] for row in known if "cleaner" in row["roles"] and row["species"] not in conditions]
        if archetype == "weather":
            # The bulky abuser converts the weather into KOs (rain Bellibolt:
            # 100% Thunder + Electromorphosis), even without setup or speed.
            conditions += [row["species"] for row in known if row["species"] in climate["abusers"]
                           and row["species"] not in climate["setters"] and "bulky_attacker" in row["roles"]
                           and row["species"] not in conditions]
    if not conditions:
        conditions = [row["species"] for row in sorted(known, key=lambda row: row["offense_score"], reverse=True)[:2]]
    if chosen != "auto":
        reason = f"Chosen strategy: {LABELS[chosen]}. " + reason
    return {"version": 2, "archetype": archetype, "inferred_archetype": inferred,
            "underlying_archetype": underlying, "weather": climate["weather"] if climate["setters"] else "",
            "weather_setters": climate["setters"], "weather_abusers": climate["abusers"],
            "requested": chosen, "label": LABELS[archetype], "roles": roles,
            "lead_species": leads[0]["species"] if leads else "",
            "lead_candidates": [row["species"] for row in leads], "win_conditions": conditions,
            "reason": reason, "confidence": round(confidence, 3)}


def analyze_opponent(battle) -> dict:
    """Infer a provisional plan from publicly revealed set components only.

    Preview species identify roster slots, never their EVs, nature or hidden
    sets. A partial move list therefore contributes proportionally less trust.
    """
    data = GenData.from_gen(9)
    key = lambda mon: base_species_id(mon.species)
    roster = {key(mon): mon for mon in getattr(battle, "teampreview_opponent_team", [])}
    roster.update({key(mon): mon for mon in getattr(battle, "opponent_team", {}).values()})
    active = getattr(battle, "opponent_active_pokemon", None)
    if active is not None:
        roster[key(active)] = active
    rows, revealed = [], 0
    for mon in roster.values():
        moves = list(dict.fromkeys(to_id(move) for move in mon.moves))[:4]
        revealed += len(moves)
        item = to_id(mon.item or "")
        if item == to_id(data.UNKNOWN_ITEM):
            item = ""
        ability = to_id(mon.ability or "")
        details = [data.moves.get(move, {}) for move in moves]
        attacks = [move for move, detail in zip(moves, details)
                   if detail.get("category") in {"Physical", "Special"} and move not in REMOVAL]
        heals = any(detail.get("heal") for detail in details) or "rest" in moves
        recovery = heals or ability == "regenerator" or ability == "poisonheal" and item == "toxicorb"
        setup = bool(attacks) and any(move in SETUP for move in moves)
        aggressive = len(attacks) >= 3 or bool(attacks) and (setup or item in OFFENSIVE_ITEMS)
        wall = bool(recovery) and len(attacks) <= 2 and len(moves) - len(attacks) >= 2
        roles = []
        def role(name, when):
            if when:
                roles.append(name)
        role("hazard_setter", any(move in HAZARDS for move in moves) or ability == "toxicdebris")
        role("web_setter", "stickyweb" in moves)
        role("screens", any(move in SCREENS for move in moves))
        role("suicide_lead", item == "focussash" and not recovery and any(name in roles for name in ("hazard_setter", "screens")))
        role("setup_sweeper", setup)
        role("offensive_attacker", aggressive)
        role("bulky_attacker", aggressive and recovery)
        role("wall", wall)
        role("recovery", recovery)
        role("status_spreader", any(detail.get("status") or (detail.get("secondary") or {}).get("status") for detail in details))
        role("pivot", any(move in PIVOTS for move in moves))
        role("hazard_removal", any(move in REMOVAL for move in moves))
        role("wallbreaker", aggressive and (setup or item in {"choiceband", "choicespecs", "lifeorb"}))
        priority = any(_n(detail.get("priority")) > 0 and detail.get("category") in {"Physical", "Special"} for detail in details)
        role("cleaner", aggressive and (priority or item == "choicescarf" or setup))
        role("speed_control", item == "choicescarf" or any(move in {"thunderwave", "stickyweb", "tailwind", "trickroom"} for move in moves))
        role("weather_setter", ability in {"drizzle", "drought", "sandstream", "snowwarning"}
             or any(move in {"raindance", "sunnyday", "sandstorm", "snowscape"} for move in moves))
        rows.append({"species": mon.species, "roles": roles or ["unknown"], "moves": moves,
                     "item": item, "ability": ability, "known": bool(moves or item or ability),
                     "revealed_move_fraction": len(moves) / 4})
    observed = [row for row in rows if row["known"]]
    inferred, confidence, reason = _infer_archetype(observed)
    underlying = inferred
    climate = weather_plan(observed)
    if climate["coherent"]:
        inferred, confidence = "weather", .88
        reason = "The opponent revealed weather setters and abusers."
    coverage = revealed / max(1, 4 * len(rows))
    return {"version": 2, "archetype": inferred, "inferred_archetype": inferred,
            "underlying_archetype": underlying, "weather": climate["weather"] if climate["setters"] else "",
            "weather_setters": climate["setters"], "weather_abusers": climate["abusers"],
            "requested": "auto", "label": LABELS[inferred], "roles": rows,
            "lead_species": "", "lead_candidates": [],
            "win_conditions": [row["species"] for row in observed if "cleaner" in row["roles"]],
            "confidence": round(confidence * coverage, 3), "revealed_move_fraction": round(coverage, 3),
            "incomplete": True, "reason": "Provisional guess from revealed data. " + reason,
            "assumptions": ["Only revealed moves, items and abilities are used.",
                            "EVs, IVs, nature, stats and hidden move slots stay unknown.",
                            "Preview species identify the members, not their sets."]}


def strategy_action_adjustments(battle, candidates, profile, opponent_profile=None) -> list[dict]:
    """Return priors bounded to ±3 points without mutating search candidates."""
    output = [{"action_key": action_key(candidate), "adjustment": 0.0, "reasons": []} for candidate in candidates]
    forced = getattr(battle, "force_switch", False)
    forced = any(forced) if isinstance(forced, (tuple, list)) else bool(forced)
    if not profile or forced or len(candidates) < 2:
        return output
    # Decisive tactical results have no competing strategic term.
    if any(candidate.score >= 5000 or _n(candidate.diagnostics.get("search_worst")) >= 5000
           or _n(candidate.diagnostics.get("ko_chance")) >= .95 and _n(candidate.diagnostics.get("faint_risk")) <= .1
           for candidate in candidates):
        return output
    own, foe = battle.active_pokemon, battle.opponent_active_pokemon
    if own is None or foe is None:
        return output
    style = profile.get("archetype", "balance")
    roles = {base_species_id(row["species"]): set(row.get("roles", [])) for row in profile.get("roles", [])}
    own_roles = roles.get(base_species_id(own.species), set())
    hp = own.current_hp_fraction if own.max_hp else 1.0
    foe_status = foe.status.name if foe.status else ""
    foe_types = {kind.name for kind in foe.types}
    foe_moves = {move for move, slot in foe.moves.items() if slot.current_pp > 0}
    # Phazing cannot expel the last surviving member; Haze and boost denial
    # still work. Do not suppress its remaining win condition for a failed Roar.
    own_remaining = sum(not mon.fainted for mon in getattr(battle, "team", {}).values())
    counters = PHASING - {"whirlwind", "roar", "dragontail", "circlethrow"} if own_remaining <= 1 else PHASING
    active_counter = foe.ability == "unaware" or bool(foe_moves & counters)
    taunted = any(effect.name == "TAUNT" for effect in foe.effects)
    if taunted:
        active_counter = bool(foe_moves & (counters & {"dragontail", "circlethrow", "clearsmog"})) or foe.ability == "unaware"
    if foe_status in {"SLP", "FRZ"}:
        active_counter = foe.ability == "unaware"
    feasible_attack = any(isinstance(candidate.action, Move) and candidate.action.category.name != "STATUS"
                          and _n(candidate.diagnostics.get("expected_dealt")) > .1
                          and _n(candidate.diagnostics.get("faint_risk")) < .2 for candidate in candidates)
    own_conditions = {condition.name for condition in battle.side_conditions}
    own_hazards = own_conditions & ENTRY_HAZARDS
    # Preview objects may still show a fainted opponent as healthy. Merge by
    # base species with observed roster state taking precedence.
    foe_key = lambda mon: base_species_id(mon.species)
    remaining_foes = {foe_key(mon): mon for mon in getattr(battle, "teampreview_opponent_team", [])}
    remaining_foes.update({foe_key(mon): mon for mon in getattr(battle, "opponent_team", {}).values()})
    foe_count = sum(not mon.fainted for mon in remaining_foes.values())
    active_is_condition = base_species_id(own.species) in {base_species_id(species) for species in profile.get("win_conditions", [])}
    endangered = any(isinstance(candidate.action, Move) and _n(candidate.diagnostics.get("faint_risk")) >= .5 for candidate in candidates)
    opposing_roles = {base_species_id(row["species"]): set(row.get("roles", [])) for row in (opponent_profile or {}).get("roles", [])}
    data = GenData.from_gen(9)
    for candidate, result in zip(candidates, output):
        score, reasons = 0.0, []
        risk = _n(candidate.diagnostics.get("faint_risk"))
        if candidate.diagnostics.get("search_legal") == 0 or risk >= .75:
            continue
        if isinstance(candidate.action, Pokemon):
            switch_roles = roles.get(base_species_id(candidate.action.species), set())
            if style == "hyper_offense" and feasible_attack and hp >= .6 and "offensive_attacker" in own_roles:
                score -= .8
                reasons.append("The current offensive pressure works; a switch with no tactical gain gives up the initiative.")
            if style in {"balance", "bulky_offense"} and "pivot" in switch_roles:
                score += .4
                reasons.append("The pivot keeps options open to reposition the attackers.")
            if style in {"stall", "semi_stall"} and "wall" in switch_roles:
                incoming = _n(candidate.diagnostics.get("expected_received"))
                if incoming < candidate.action.current_hp_fraction * .35:
                    score += .7
                    reasons.append("A parede conserva HP suficiente para sustentar o plano defensivo.")
            if active_is_condition and hp < .4 and endangered and risk < .15:
                score += .6
                reasons.append("Preserving this win condition avoids losing it before a better opening.")
        else:
            move = candidate.action.id
            details = data.moves.get(move, {})
            incoming = _n(candidate.diagnostics.get("expected_received"))
            if move in SETUP and "setup_sweeper" in own_roles and not active_counter and risk < .2 and incoming < hp * .45:
                positives = {stat: value for stat, value in (details.get("boosts") or {}).items() if value > 0}
                uncapped = not positives or any(own.boosts.get(stat, 0) < 6 for stat in positives)
                if uncapped:
                    score += 1.2 if style == "hyper_offense" else .6
                    reasons.append("Setup is available for a win condition, with room to survive the turn.")
            hazard = details.get("sideCondition") or {"stoneaxe": "stealthrock", "ceaselessedge": "spikes"}.get(move, move)
            hazard_id = hazard.upper().replace("_", "")
            existing = {condition.name.replace("_", ""): value for condition, value in battle.opponent_side_conditions.items()}
            fresh_hazard = move in HAZARDS and (hazard_id not in existing
                or hazard_id == "SPIKES" and existing[hazard_id] < 3
                or hazard_id == "TOXICSPIKES" and existing[hazard_id] < 2)
            if style == "hyper_offense" and foe_count >= 3 and fresh_hazard:
                score += 1.0
                reasons.append("Entry pressure helps the attackers finish the opposing team.")
            screen = (details.get("sideCondition") or move).upper().replace("_", "")
            screen_works = move != "auroraveil" or any(condition.name in {"SNOW", "HAIL", "SNOWSCAPE"} for condition in battle.weather)
            if style == "hyper_offense" and move in SCREENS and screen_works and screen not in {name.replace("_", "") for name in own_conditions}:
                score += .8
                reasons.append("Field protection makes room for the setup attackers.")
            if move in PIVOTS and style in {"hyper_offense", "bulky_offense", "balance"}:
                score += .7
                reasons.append("The pivot move keeps the initiative and repositions the team.")
            if move in REMOVAL and own_hazards:
                can_remove = not (move == "rapidspin" and "GHOST" in foe_types and own.ability not in {"scrappy", "mindseye"})
                can_remove &= not (move == "defog" and foe.ability == "goodasgold")
                can_remove &= not (move == "mortalspin" and "STEEL" in foe_types)
                if can_remove:
                    score += .9 if style in {"balance", "stall", "semi_stall"} else .4
                    reasons.append("Removing hazards saves HP for the next switch-ins.")
            if style in {"stall", "semi_stall", "balance"} and (details.get("heal") or move == "rest") and hp < .7 and risk < .2:
                score += .9
                reasons.append("Useful recovery keeps the defensive piece for later turns.")
            if style in {"stall", "semi_stall"} and move in {"protect", "detect", "banefulbunker", "spikyshield"}:
                if foe_status in {"TOX", "BRN", "PSN"} and not getattr(own, "protect_counter", 0):
                    score += .7
                    reasons.append("Protection allows status progress without repeating a low-accuracy sequence.")
            if style in {"stall", "semi_stall"} and details.get("status") and not foe_status:
                status = details["status"]
                immune = status in {"tox", "psn"} and (bool(foe_types & {"STEEL", "POISON"}) or foe.ability in {"immunity", "purifyingsalt"})
                immune |= status == "brn" and "FIRE" in foe_types
                immune |= status == "par" and ("ELECTRIC" in foe_types or move == "thunderwave" and "GROUND" in foe_types)
                if not immune:
                    score += .8
                    reasons.append("Status the target can take makes lasting progress for the defensive plan.")
            if move not in SETUP and "wallbreaker" in own_roles and style in {"balance", "bulky_offense"} and _n(candidate.diagnostics.get("expected_dealt")) >= .25:
                score += .5
                reasons.append("The attacker opens room by pressuring a defensive piece.")
                if "wall" in opposing_roles.get(base_species_id(foe.species), set()):
                    score += .3
                    reasons.append("The target is part of the opponent's defensive core.")
        result["adjustment"] = round(max(-3.0, min(3.0, score)), 3)
        result["reasons"] = reasons
    return output
