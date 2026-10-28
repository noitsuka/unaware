"""Live win chance: calibrated material model, network nudge, smoothing."""
from unaware.neural.winchance import combine, material_probability


def test_material_model_orders_positions_sensibly():
    even = material_probability(6, 6, 6.0, 6.0)
    assert .3 < even < .55
    assert material_probability(6, 5, 6.0, 5.0) > even > material_probability(5, 6, 5.0, 6.0)
    assert material_probability(3, 1, 2.4, .5) > .8
    assert material_probability(1, 3, .5, 2.4) < .2


def test_network_nudges_and_smoothing_limits_swings():
    assert combine(.5, None, None) == .5
    assert .5 < combine(.5, .9, None) < .9
    assert combine(.9, None, .3) < .9  # one turn cannot jump all the way


def test_even_start_is_fifty_percent_whatever_the_network_thinks():
    from unaware.neural.winchance import material_probability
    material = material_probability(6, 6, 6.0, 6.0)
    assert material == .5
    assert abs(combine(material, .33, None, baseline=.33) - .5) < 1e-9
    assert combine(material, .5, None, baseline=.33) > .55  # its opinion improved since the start

