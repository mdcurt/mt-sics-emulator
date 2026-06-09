"""
Tests for WeightSimulator.

Strategy
--------
Deterministic tests (noise_sigma=0.0)
  Test settle curve, stability transitions, snap_to, and target redirect
  without randomness. These tests make exact assertions.

Seeded noise tests (seed=42)
  Test that noise is actually present and has the right statistical
  character.  Assertions are loose (order-of-magnitude) to avoid
  fragility under different Python versions.

Integration tests
  Wire simulator → state → engine and run realistic scale workflows.

Run with:  pytest tests/test_simulator.py -v
"""
import math

import pytest

from mtsics.core.simulator import SimConfig, WeightSimulator
from mtsics.core.state import ScaleConfig, ScaleState
from mtsics.protocol.engine import MTSICSEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state() -> ScaleState:
    cfg = ScaleConfig(capacity=150.0, graduation=0.05, unit="kg")
    return ScaleState(config=cfg)


def noiseless_sim(state: ScaleState, **kwargs) -> WeightSimulator:
    """Helper: simulator with no noise for deterministic tests."""
    cfg = SimConfig(noise_sigma=0.0, **kwargs)
    return WeightSimulator(state, cfg=cfg, seed=0)


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

class TestInitialState:
    def test_initial_weight_zero(self, state):
        assert state.gross == 0.0

    def test_initial_stable(self, state):
        assert state.stable is True

    def test_initial_target_zero(self, state):
        sim = noiseless_sim(state)
        assert sim.target == 0.0

    def test_initial_current_zero(self, state):
        sim = noiseless_sim(state)
        assert sim.current == 0.0

    def test_elapsed_starts_at_zero(self, state):
        sim = noiseless_sim(state)
        assert sim.elapsed == 0.0


# ---------------------------------------------------------------------------
# Settle curve
# ---------------------------------------------------------------------------

class TestSettleCurve:
    def test_one_tau_covers_63_percent(self, state):
        """After 1τ the current should be ~63% of the step."""
        tau = 0.4
        sim = noiseless_sim(state, settle_tau=tau)
        sim.set_target(100.0)
        sim.tick(tau)
        # e^(-1) ≈ 0.368, so 1 - e^(-1) ≈ 0.632
        assert 60.0 < sim.current < 67.0

    def test_five_tau_covers_99_percent(self, state):
        """After 5τ convergence should be > 99%."""
        tau = 0.4
        sim = noiseless_sim(state, settle_tau=tau)
        sim.set_target(50.0)
        sim.tick(tau * 5)
        assert abs(sim.current - 50.0) < 0.5   # within 1%

    def test_current_monotonically_approaches_target(self, state):
        """Each tick should bring current closer to target (no noise)."""
        sim = noiseless_sim(state)
        sim.set_target(20.0)
        prev = 0.0
        for _ in range(20):
            sim.tick(0.1)
            assert sim.current > prev or math.isclose(sim.current, 20.0, abs_tol=1e-9)
            prev = sim.current

    def test_negative_dt_is_noop(self, state):
        sim = noiseless_sim(state)
        sim.set_target(10.0)
        sim.tick(-1.0)
        assert sim.current == 0.0
        assert sim.elapsed == 0.0

    def test_zero_dt_is_noop(self, state):
        sim = noiseless_sim(state)
        sim.set_target(10.0)
        sim.tick(0.0)
        assert sim.current == 0.0


# ---------------------------------------------------------------------------
# Stability transitions
# ---------------------------------------------------------------------------

class TestStability:
    def test_stable_at_rest(self, state):
        sim = noiseless_sim(state)
        # Several ticks at zero — no change, should stay stable
        for _ in range(30):
            sim.tick(0.05)
        assert state.stable is True

    def test_unstable_during_settle(self, state):
        """After a large step, readings change rapidly → unstable."""
        sim = noiseless_sim(state)
        sim.set_target(100.0)
        # Two ticks: both readings are within the stability window and
        # differ substantially → peak-to-peak > graduation → unstable
        sim.tick(0.05)
        sim.tick(0.05)
        assert state.stable is False

    def test_stable_after_convergence(self, state):
        """After advancing to stable, state should report stable=True."""
        sim = noiseless_sim(state)
        sim.set_target(25.0)
        sim.advance_to_stable()
        assert state.stable is True

    def test_final_weight_near_target(self, state):
        """After advance_to_stable, gross weight should equal target (±1 grad)."""
        sim = noiseless_sim(state)
        sim.set_target(75.0)
        sim.advance_to_stable()
        assert abs(state.gross - 75.0) <= state.config.graduation

    def test_stability_lost_on_target_change(self, state):
        """Changing target after stability should make the scale unstable again."""
        sim = noiseless_sim(state)
        sim.set_target(10.0)
        sim.advance_to_stable()
        assert state.stable is True
        sim.set_target(40.0)
        sim.tick(0.05)
        sim.tick(0.05)
        assert state.stable is False

    def test_re_stabilises_at_new_target(self, state):
        """After a target change, scale should stabilise at the new target."""
        sim = noiseless_sim(state)
        sim.set_target(10.0)
        sim.advance_to_stable()
        sim.set_target(40.0)
        sim.advance_to_stable()
        assert state.stable is True
        assert abs(state.gross - 40.0) <= state.config.graduation


# ---------------------------------------------------------------------------
# snap_to
# ---------------------------------------------------------------------------

class TestSnapTo:
    def test_snap_sets_current_immediately(self, state):
        sim = noiseless_sim(state)
        sim.snap_to(30.0)
        assert sim.current == 30.0

    def test_snap_sets_target(self, state):
        sim = noiseless_sim(state)
        sim.snap_to(30.0)
        assert sim.target == 30.0

    def test_snap_clears_history(self, state):
        sim = noiseless_sim(state)
        sim.set_target(100.0)
        for _ in range(10):
            sim.tick(0.1)
        sim.snap_to(5.0)
        assert len(sim._history) == 0

    def test_snap_state_weight_updated(self, state):
        sim = noiseless_sim(state)
        sim.snap_to(55.0)
        assert abs(state._raw_weight - 55.0) < 1e-9

    def test_snap_then_stable_after_one_window(self, state):
        """After snap, one window of ticks at same value → stable."""
        sim = noiseless_sim(state)
        sim.snap_to(20.0)
        # Fill the stability window (1.0s at 0.05s steps = 20 ticks)
        for _ in range(20):
            sim.tick(0.05)
        assert state.stable is True


# ---------------------------------------------------------------------------
# Target redirect mid-settle
# ---------------------------------------------------------------------------

class TestTargetRedirect:
    def test_redirect_changes_destination(self, state):
        sim = noiseless_sim(state)
        sim.set_target(100.0)
        # Partially settle toward 100
        for _ in range(5):
            sim.tick(0.1)
        midpoint = sim.current
        assert 10.0 < midpoint < 90.0   # partway there

        # Redirect to 10
        sim.set_target(10.0)
        sim.advance_to_stable()
        assert abs(state.gross - 10.0) <= state.config.graduation

    def test_redirect_to_zero(self, state):
        sim = noiseless_sim(state)
        sim.set_target(50.0)
        sim.advance_to_stable()
        sim.set_target(0.0)
        sim.advance_to_stable()
        assert abs(state.gross) <= state.config.graduation


# ---------------------------------------------------------------------------
# Noise behaviour
# ---------------------------------------------------------------------------

class TestNoise:
    def test_noiseless_no_variation_after_settle(self, state):
        """With sigma=0, readings at rest should be identical."""
        sim = noiseless_sim(state)
        sim.snap_to(50.0)
        for _ in range(20):
            sim.tick(0.05)
        # All readings in history should be the same value (no noise)
        values = [w for _, w in sim._history]
        assert max(values) - min(values) < 1e-9

    def test_noise_present_with_nonzero_sigma(self, state):
        """With nonzero sigma, readings should vary."""
        cfg = SimConfig(noise_sigma=0.5, stability_threshold=10.0)
        sim = WeightSimulator(state, cfg=cfg, seed=42)
        sim.snap_to(50.0)
        readings = []
        for _ in range(50):
            sim.tick(0.05)
            readings.append(state._raw_weight)
        spread = max(readings) - min(readings)
        assert spread > 0.1   # definitely some noise

    def test_noise_sigma_defaults_to_graduation_over_ten(self, state):
        """Default noise_sigma should be graduation / 10."""
        sim = WeightSimulator(state, seed=0)
        expected = state.config.graduation / 10.0
        assert math.isclose(sim._noise_sigma, expected)

    def test_noise_mean_near_target(self, state):
        """Mean of many readings should be close to target (unbiased noise)."""
        cfg = SimConfig(noise_sigma=0.1, stability_threshold=10.0)
        sim = WeightSimulator(state, cfg=cfg, seed=99)
        sim.snap_to(100.0)
        readings = []
        for _ in range(200):
            sim.tick(0.05)
            readings.append(state._raw_weight)
        mean = sum(readings) / len(readings)
        # Mean should be within 2% of target
        assert abs(mean - 100.0) < 2.0

    def test_seeded_rng_reproducible(self, state):
        """Same seed → same sequence of readings."""
        def run(seed):
            cfg = SimConfig(noise_sigma=0.1, stability_threshold=10.0)
            st = ScaleState(config=ScaleConfig())
            sim = WeightSimulator(st, cfg=cfg, seed=seed)
            sim.snap_to(50.0)
            readings = []
            for _ in range(10):
                sim.tick(0.1)
                readings.append(st._raw_weight)
            return readings

        assert run(42) == run(42)
        assert run(42) != run(99)


# ---------------------------------------------------------------------------
# advance_to_stable
# ---------------------------------------------------------------------------

class TestAdvanceToStable:
    def test_returns_positive_elapsed_time(self, state):
        sim = noiseless_sim(state)
        sim.set_target(20.0)
        elapsed = sim.advance_to_stable()
        assert elapsed > 0.0

    def test_state_stable_after_call(self, state):
        sim = noiseless_sim(state)
        sim.set_target(60.0)
        sim.advance_to_stable()
        assert state.stable is True

    def test_does_not_exceed_max_sim_time(self, state):
        """If max_sim_time is tiny, it should stop and not hang."""
        sim = noiseless_sim(state)
        sim.set_target(1000.0)   # unreachable (overrange), will keep going
        elapsed = sim.advance_to_stable(max_sim_time=0.5)
        assert elapsed <= 0.55   # within one step of limit

    def test_already_stable_exits_quickly(self, state):
        """Starting already at target with no noise → exits in one step."""
        sim = noiseless_sim(state)
        sim.snap_to(0.0)   # already at target=0
        elapsed = sim.advance_to_stable()
        assert elapsed <= 0.1   # exits almost immediately


# ---------------------------------------------------------------------------
# elapsed time
# ---------------------------------------------------------------------------

class TestElapsed:
    def test_elapsed_accumulates_across_ticks(self, state):
        sim = noiseless_sim(state)
        sim.tick(0.5)
        sim.tick(0.3)
        assert math.isclose(sim.elapsed, 0.8, abs_tol=1e-9)

    def test_advance_to_stable_increments_elapsed(self, state):
        sim = noiseless_sim(state)
        sim.set_target(10.0)
        elapsed = sim.advance_to_stable()
        assert math.isclose(sim.elapsed, elapsed, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# Integration: simulator → state → engine
# ---------------------------------------------------------------------------

class TestIntegration:
    @pytest.fixture
    def rig(self):
        state = ScaleState(config=ScaleConfig(capacity=150.0, graduation=0.05))
        engine = MTSICSEngine(state)
        sim = noiseless_sim(state)
        return state, engine, sim

    def test_si_reads_settled_weight(self, rig):
        state, engine, sim = rig
        sim.set_target(10.0)
        sim.advance_to_stable()
        r = engine.handle("SI")
        assert r.startswith("SI S")
        assert "10.00" in r

    def test_s_stable_flag_set_correctly(self, rig):
        state, engine, sim = rig
        sim.set_target(10.0)
        sim.tick(0.05)
        sim.tick(0.05)          # unstable mid-settle
        r = engine.handle("S")
        assert r.startswith("S D")

    def test_tare_workflow(self, rig):
        state, engine, sim = rig
        # Place a 2 kg container
        sim.set_target(2.0)
        sim.advance_to_stable()
        engine.handle("T")              # tare the container
        # Add 5 kg contents
        sim.set_target(7.0)
        sim.advance_to_stable()
        r = engine.handle("SI")
        assert "5.00" in r              # net = 5.0 kg

    def test_zero_then_tare_then_weigh(self, rig):
        state, engine, sim = rig
        # Scale has a small drift
        sim.snap_to(0.1)
        sim.advance_to_stable()
        engine.handle("Z")              # zero out the drift
        # Put a 1 kg container
        sim.snap_to(1.1)
        sim.advance_to_stable()
        engine.handle("T")              # tare it
        # Add 3 kg product
        sim.snap_to(4.1)
        sim.advance_to_stable()
        r = engine.handle("SI")
        assert "3.00" in r              # net = 3.0 kg

    def test_overrange_during_settle(self, rig):
        state, engine, sim = rig
        sim.snap_to(200.0)             # over capacity of 150 kg
        r = engine.handle("SI")
        assert r.startswith("SI +")

    def test_sir_flag_set_via_engine(self, rig):
        state, engine, sim = rig
        sim.snap_to(5.0)
        engine.handle("SIR")
        assert engine.sir_active is True
        # After reset, SIR flag cleared and weight still readable
        engine.handle("@")
        assert engine.sir_active is False
        r = engine.handle("SI")
        assert "5.00" in r