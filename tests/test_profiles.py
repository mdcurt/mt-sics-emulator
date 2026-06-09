"""
Tests for the scale profiles module.

Covers: loading, listing, decimal-place derivation, and that each profile
produces correctly formatted weight responses through the MT-SICS engine.

Run with:  pytest tests/test_profiles.py -v
"""
import math

import pytest

from mtsics.core.state import ScaleState
from mtsics.profiles import DEFAULT_PROFILE, list_profiles, load, summary_table
from mtsics.protocol.engine import MTSICSEngine


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_list_profiles_returns_sorted(self):
        names = list_profiles()
        assert names == sorted(names)

    def test_list_profiles_nonempty(self):
        assert len(list_profiles()) >= 6

    def test_default_profile_exists(self):
        assert DEFAULT_PROFILE in list_profiles()

    def test_summary_table_contains_all_profiles(self):
        table = summary_table()
        for name in list_profiles():
            assert name in table

    def test_unknown_profile_raises_key_error(self):
        with pytest.raises(KeyError, match="Unknown profile"):
            load("nonexistent-scale-xyz")

    def test_error_message_lists_available_profiles(self):
        with pytest.raises(KeyError) as exc_info:
            load("bogus")
        assert "ohaus-defender5000" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Individual profile correctness
# ---------------------------------------------------------------------------

EXPECTED = {
    "ohaus-defender5000": dict(
        capacity=150.0, graduation=0.05, unit="kg",
        model="Defender 5000", decimal_places=2,
    ),
    "ohaus-ranger7000": dict(
        capacity=35.0, graduation=0.005, unit="kg",
        model="Ranger 7000", decimal_places=3,
    ),
    "ohaus-scout": dict(
        capacity=4200.0, graduation=0.1, unit="g",
        model="Scout SPX4201", decimal_places=1,
    ),
    "mt-xs204": dict(
        capacity=220.0, graduation=0.0001, unit="g",
        model="XS204", decimal_places=4,
    ),
    "mt-ms3002s": dict(
        capacity=3100.0, graduation=0.01, unit="g",
        model="MS3002S", decimal_places=2,
    ),
    "mt-ind570": dict(
        capacity=300.0, graduation=0.1, unit="kg",
        model="IND570", decimal_places=1,
    ),
    "sartorius-quintix224": dict(
        capacity=220.0, graduation=0.0001, unit="g",
        model="Quintix 224-1S", decimal_places=4,
    ),
    "sartorius-practum6100": dict(
        capacity=6100.0, graduation=0.1, unit="g",
        model="Practum 6100-1S", decimal_places=1,
    ),
}


@pytest.mark.parametrize("name,spec", EXPECTED.items())
class TestProfileSpecs:
    def test_capacity(self, name, spec):
        cfg = load(name)
        assert math.isclose(cfg.capacity, spec["capacity"])

    def test_graduation(self, name, spec):
        cfg = load(name)
        assert math.isclose(cfg.graduation, spec["graduation"])

    def test_unit(self, name, spec):
        assert load(name).unit == spec["unit"]

    def test_model(self, name, spec):
        assert load(name).model == spec["model"]

    def test_decimal_places(self, name, spec):
        assert load(name).decimal_places == spec["decimal_places"]

    def test_serial_number_nonempty(self, name, spec):
        assert load(name).serial_number


# ---------------------------------------------------------------------------
# Engine integration — every profile produces valid MT-SICS responses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", list_profiles())
class TestProfileEngineIntegration:
    def _rig(self, name):
        cfg = load(name)
        state = ScaleState(config=cfg)
        engine = MTSICSEngine(state)
        return state, engine

    def test_i1_returns_model(self, name):
        cfg = load(name)
        _, engine = self._rig(name)
        resp = engine.handle("I1")
        assert cfg.model in resp

    def test_si_zero_weight(self, name):
        state, engine = self._rig(name)
        state.set_weight(0.0, stable=True)
        resp = engine.handle("SI")
        assert resp.startswith("SI S")
        assert resp.endswith(f" {load(name).unit}\r\n")

    def test_si_contains_correct_decimal_places(self, name):
        cfg = load(name)
        state, engine = self._rig(name)
        state.set_weight(0.0, stable=True)
        resp = engine.handle("SI")
        # Extract the numeric part and check decimal places
        numeric = resp.split()[2]  # e.g. "0.0000" or "0.00"
        if "." in numeric:
            actual_dp = len(numeric.split(".")[1])
            assert actual_dp == cfg.decimal_places

    def test_tare_then_net_zero(self, name):
        cfg = load(name)
        state, engine = self._rig(name)
        state.set_weight(cfg.capacity * 0.1, stable=True)
        engine.handle("T")
        resp = engine.handle("SI")
        # After tare, net should be ~zero regardless of what was on the scale
        assert "0." in resp

    def test_overrange(self, name):
        cfg = load(name)
        state, engine = self._rig(name)
        state.set_weight(cfg.capacity * 1.1, stable=True)
        resp = engine.handle("SI")
        assert "+" in resp

    def test_underrange(self, name):
        cfg = load(name)
        state, engine = self._rig(name)
        state.set_weight(-cfg.graduation * 2, stable=True)
        resp = engine.handle("SI")
        assert "-" in resp

    def test_zero_within_range(self, name):
        cfg = load(name)
        state, engine = self._rig(name)
        # Put a tiny weight within the 2% zeroing range
        state.set_weight(cfg.capacity * 0.01, stable=True)
        resp = engine.handle("Z")
        assert resp.strip() == "Z A"

    def test_reset(self, name):
        cfg = load(name)
        _, engine = self._rig(name)
        resp = engine.handle("@")
        assert cfg.serial_number in resp


# ---------------------------------------------------------------------------
# Rounding to graduation
# ---------------------------------------------------------------------------

class TestRounding:
    def test_analytical_rounds_to_0_1mg(self):
        """XS204 at 0.0001 g graduation rounds correctly."""
        cfg = load("mt-xs204")
        state = ScaleState(config=cfg)
        engine = MTSICSEngine(state)
        state.set_weight(1.00005, stable=True)  # half graduation above 1.0000
        resp = engine.handle("SI")
        assert "1.0001" in resp or "1.0000" in resp  # rounds to nearest 0.0001

    def test_industrial_rounds_to_0_1kg(self):
        """IND570 at 0.1 kg graduation rounds correctly."""
        cfg = load("mt-ind570")
        state = ScaleState(config=cfg)
        engine = MTSICSEngine(state)
        state.set_weight(12.34, stable=True)
        resp = engine.handle("SI")
        assert "12.3" in resp  # 12.34 rounds to 12.3 at 0.1 kg graduation