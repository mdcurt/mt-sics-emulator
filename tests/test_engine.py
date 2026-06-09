"""
Tests for the MT-SICS engine and scale state machine.

Each test function is narrow: one command, one scenario. This makes failures
easy to read and debug without needing to trace through shared state.

Run with:  pytest tests/ -v
"""
import pytest

from mtsics.core.state import ScaleConfig, ScaleState
from mtsics.protocol.engine import CRLF, MTSICSEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state() -> ScaleState:
    cfg = ScaleConfig(
        capacity=150.0,
        graduation=0.05,
        unit="kg",
        model="Defender 5000",
        serial_number="B123456789",
        sw_version="1.0.0",
    )
    return ScaleState(config=cfg)


@pytest.fixture
def engine(state: ScaleState) -> MTSICSEngine:
    return MTSICSEngine(state)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def assert_ends_crlf(response: str) -> None:
    assert response.endswith(CRLF), f"Response missing CRLF: {response!r}"


# ---------------------------------------------------------------------------
# Weight queries — SI
# ---------------------------------------------------------------------------

class TestSI:
    def test_si_stable_zero(self, engine, state):
        state.set_weight(0.0, stable=True)
        r = engine.handle("SI")
        assert r == f"SI S       0.00 kg{CRLF}"

    def test_si_stable_positive(self, engine, state):
        state.set_weight(12.5, stable=True)
        r = engine.handle("SI")
        assert r == f"SI S      12.50 kg{CRLF}"

    def test_si_dynamic(self, engine, state):
        state.set_weight(7.25, stable=False)
        r = engine.handle("SI")
        assert r == f"SI D       7.25 kg{CRLF}"

    def test_si_overrange(self, engine, state):
        state.set_weight(200.0)
        r = engine.handle("SI")
        assert r == f"SI +{CRLF}"

    def test_si_underrange(self, engine, state):
        state.set_weight(-5.0)
        r = engine.handle("SI")
        assert r == f"SI -{CRLF}"

    def test_si_case_insensitive(self, engine, state):
        state.set_weight(1.0)
        assert engine.handle("si") == engine.handle("SI")

    def test_si_ends_crlf(self, engine, state):
        assert_ends_crlf(engine.handle("SI"))


# ---------------------------------------------------------------------------
# Weight queries — S
# ---------------------------------------------------------------------------

class TestS:
    def test_s_stable(self, engine, state):
        state.set_weight(50.0, stable=True)
        r = engine.handle("S")
        assert r == f"S S      50.00 kg{CRLF}"

    def test_s_dynamic_returns_dynamic(self, engine, state):
        state.set_weight(50.0, stable=False)
        r = engine.handle("S")
        assert r.startswith("S D")

    def test_s_overrange(self, engine, state):
        state.set_weight(999.0)
        assert engine.handle("S") == f"S +{CRLF}"


# ---------------------------------------------------------------------------
# Weight queries — SIR
# ---------------------------------------------------------------------------

class TestSIR:
    def test_sir_sets_flag(self, engine, state):
        state.set_weight(1.0)
        engine.handle("SIR")
        assert engine.sir_active is True

    def test_sir_returns_immediate_reading(self, engine, state):
        state.set_weight(3.0)
        r = engine.handle("SIR")
        assert "3.00" in r

    def test_reset_clears_sir(self, engine, state):
        engine.handle("SIR")
        engine.handle("@")
        assert engine.sir_active is False


# ---------------------------------------------------------------------------
# Zero — Z
# ---------------------------------------------------------------------------

class TestZ:
    def test_z_stable_within_range(self, engine, state):
        state.set_weight(0.5, stable=True)   # well within 2% of 150 = 3.0
        assert engine.handle("Z") == f"Z A{CRLF}"

    def test_z_zeros_the_reading(self, engine, state):
        state.set_weight(1.0, stable=True)
        engine.handle("Z")
        assert engine.handle("SI") == f"SI S       0.00 kg{CRLF}"

    def test_z_unstable_refused(self, engine, state):
        state.set_weight(0.5, stable=False)
        assert engine.handle("Z") == f"Z I{CRLF}"

    def test_z_overrange(self, engine, state):
        state.set_weight(200.0)
        assert engine.handle("Z") == f"Z +{CRLF}"

    def test_z_outside_zero_range_refused(self, engine, state):
        # 2% of 150 = 3.0; 10 kg is outside that range
        state.set_weight(10.0, stable=True)
        assert engine.handle("Z") == f"Z I{CRLF}"


# ---------------------------------------------------------------------------
# Zero — ZI
# ---------------------------------------------------------------------------

class TestZI:
    def test_zi_unstable_succeeds(self, engine, state):
        state.set_weight(0.5, stable=False)
        assert engine.handle("ZI") == f"ZI A{CRLF}"

    def test_zi_zeros_reading(self, engine, state):
        state.set_weight(2.0, stable=False)
        engine.handle("ZI")
        r = engine.handle("SI")
        assert "0.00" in r

    def test_zi_overrange_refused(self, engine, state):
        state.set_weight(200.0)
        assert engine.handle("ZI") == f"ZI I{CRLF}"


# ---------------------------------------------------------------------------
# Tare — T
# ---------------------------------------------------------------------------

class TestT:
    def test_t_stable_sets_tare(self, engine, state):
        state.set_weight(5.0, stable=True)
        r = engine.handle("T")
        assert r == f"T S       5.00 kg{CRLF}"

    def test_t_stable_net_becomes_zero(self, engine, state):
        state.set_weight(5.0, stable=True)
        engine.handle("T")
        assert engine.handle("SI") == f"SI S       0.00 kg{CRLF}"

    def test_t_unstable_refused(self, engine, state):
        state.set_weight(5.0, stable=False)
        assert engine.handle("T") == f"T I{CRLF}"

    def test_t_net_tracks_load_above_tare(self, engine, state):
        state.set_weight(5.0, stable=True)
        engine.handle("T")
        state.set_weight(8.0, stable=True)
        r = engine.handle("SI")
        assert r == f"SI S       3.00 kg{CRLF}"


# ---------------------------------------------------------------------------
# Tare — TI
# ---------------------------------------------------------------------------

class TestTI:
    def test_ti_unstable_tares(self, engine, state):
        state.set_weight(3.0, stable=False)
        r = engine.handle("TI")
        assert r.startswith("TI D")
        assert "3.00" in r

    def test_ti_stable_tares(self, engine, state):
        state.set_weight(3.0, stable=True)
        r = engine.handle("TI")
        assert r.startswith("TI S")

    def test_ti_net_zero_after_tare(self, engine, state):
        state.set_weight(3.0, stable=True)
        engine.handle("TI")
        assert "0.00" in engine.handle("SI")


# ---------------------------------------------------------------------------
# Tare — TAR
# ---------------------------------------------------------------------------

class TestTAR:
    def test_tar_sets_explicit_tare(self, engine, state):
        state.set_weight(10.0, stable=True)
        assert engine.handle("TAR 5.0") == f"TAR A{CRLF}"

    def test_tar_affects_net(self, engine, state):
        state.set_weight(10.0)
        engine.handle("TAR 5.0")
        r = engine.handle("SI")
        assert r == f"SI S       5.00 kg{CRLF}"

    def test_tar_negative_refused(self, engine, state):
        assert engine.handle("TAR -1.0") == f"TAR I{CRLF}"

    def test_tar_exceeds_capacity_refused(self, engine, state):
        assert engine.handle("TAR 200.0") == f"TAR I{CRLF}"

    def test_tar_missing_arg_syntax_error(self, engine, state):
        assert engine.handle("TAR") == f"ES{CRLF}"

    def test_tar_bad_float_syntax_error(self, engine, state):
        assert engine.handle("TAR abc") == f"ES{CRLF}"


# ---------------------------------------------------------------------------
# Tare — TAC
# ---------------------------------------------------------------------------

class TestTAC:
    def test_tac_clears_tare(self, engine, state):
        state.set_weight(5.0)
        engine.handle("T")
        engine.handle("TAC")
        r = engine.handle("SI")
        assert r == f"SI S       5.00 kg{CRLF}"

    def test_tac_ack(self, engine, state):
        assert engine.handle("TAC") == f"TAC A{CRLF}"


# ---------------------------------------------------------------------------
# Reset — @
# ---------------------------------------------------------------------------

class TestReset:
    def test_reset_returns_id(self, engine, state):
        r = engine.handle("@")
        assert r == f'I4 A "B123456789"{CRLF}'

    def test_reset_clears_tare(self, engine, state):
        state.set_weight(5.0)
        engine.handle("T")
        engine.handle("@")
        state.set_weight(5.0)
        r = engine.handle("SI")
        assert r == f"SI S       5.00 kg{CRLF}"

    def test_reset_clears_zero_offset(self, engine, state):
        state.set_weight(1.0, stable=True)
        engine.handle("Z")
        engine.handle("@")
        r = engine.handle("SI")
        assert r == f"SI S       1.00 kg{CRLF}"


# ---------------------------------------------------------------------------
# Inquiry commands
# ---------------------------------------------------------------------------

class TestInquiry:
    def test_i1_model(self, engine):
        assert engine.handle("I1") == f'I1 A "Defender 5000"{CRLF}'

    def test_i2_serial(self, engine):
        assert engine.handle("I2") == f'I2 A "B123456789"{CRLF}'

    def test_i3_version(self, engine):
        assert engine.handle("I3") == f'I3 A "1.0.0"{CRLF}'

    def test_i4_sw_id(self, engine):
        assert engine.handle("I4") == f'I4 A "B123456789"{CRLF}'

    def test_i0_contains_model_and_capacity(self, engine):
        r = engine.handle("I0")
        assert "Defender 5000" in r
        assert "150.0" in r
        assert "0.05" in r


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrors:
    def test_empty_input(self, engine):
        assert engine.handle("") == f"ES{CRLF}"

    def test_whitespace_only(self, engine):
        assert engine.handle("   ") == f"ES{CRLF}"

    def test_unknown_command(self, engine):
        assert engine.handle("BOGUS") == f"ES{CRLF}"

    def test_unknown_command_with_args(self, engine):
        assert engine.handle("XYZ 1 2 3") == f"ES{CRLF}"


# ---------------------------------------------------------------------------
# Graduation rounding
# ---------------------------------------------------------------------------

class TestRounding:
    def test_weight_rounds_to_graduation(self, state, engine):
        # graduation is 0.05; 12.47 should round to 12.45
        state.set_weight(12.47)
        r = engine.handle("SI")
        assert "12.45" in r

    def test_weight_rounds_up(self, state, engine):
        # 12.48 should round to 12.50
        state.set_weight(12.48)
        r = engine.handle("SI")
        assert "12.50" in r


# ---------------------------------------------------------------------------
# Workflow: zero → tare → weigh
# ---------------------------------------------------------------------------

class TestWorkflow:
    def test_tare_then_weigh(self, engine, state):
        """Put a container on, tare it, weigh contents."""
        state.set_weight(2.0, stable=True)   # empty container
        engine.handle("T")
        state.set_weight(7.0, stable=True)   # container + contents
        r = engine.handle("SI")
        assert r == f"SI S       5.00 kg{CRLF}"

    def test_zero_then_tare_then_weigh(self, engine, state):
        """Zero, then tare a container, then weigh."""
        state.set_weight(0.1, stable=True)   # small drift
        engine.handle("Z")
        state.set_weight(2.1, stable=True)   # container on zeroed scale
        engine.handle("T")
        state.set_weight(7.1, stable=True)   # contents added
        r = engine.handle("SI")
        assert r == f"SI S       5.00 kg{CRLF}"

    def test_reset_restores_clean_state(self, engine, state):
        """After a full workflow, @ should bring the scale back to baseline."""
        state.set_weight(5.0, stable=True)
        engine.handle("T")
        engine.handle("@")
        state.set_weight(5.0, stable=True)
        r = engine.handle("SI")
        assert r == f"SI S       5.00 kg{CRLF}"