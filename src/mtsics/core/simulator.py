"""
Weight simulator for the MT-SICS emulator.

Models the physical behavior of the scale platform:

  Settle curve
    When the load changes, the displayed weight approaches the new target
    via an exponential decay: Δ current = (target - current) * (1 - e^(-dt/τ)).
    τ (settle_tau) defaults to 0.4 s, giving ~98% convergence in 1.6 s and
    ~99.9% in 2.8 s — typical for MT-SICS compatible bench scales (1–3 s
    is typical).
    

  Gaussian noise
    Each tick adds a zero-mean Gaussian sample with σ = noise_sigma to the
    raw reading before it is written to ScaleState. This simulates the
    residual load-cell noise after the indicator's internal averaging filter.
    Default σ = graduation / 10, well below the minimum division.

  Stability detection
    The simulator maintains a rolling window of displayed readings. A reading
    is considered stable when the peak-to-peak variation within the window
    stays at or below one graduation. Once the settle curve has converged and
    the window fills with near-identical readings, stable → True.

The simulator drives ScaleState by calling set_weight() on every tick. The
MT-SICS engine reads from ScaleState, so no extra plumbing is needed.

Usage::

    from mtsics.core.state import ScaleState
    from mtsics.core.simulator import WeightSimulator, SimConfig

    state = ScaleState()
    sim = WeightSimulator(state)

    sim.set_target(12.5)          # place 12.5 kg on the platform
    sim.advance_to_stable()       # fast-forward past the settle period

    print(state.stable)           # True
    print(state.net)              # ≈ 12.50

The simulator is tick-based (not wall-clock-based) so tests can drive it
deterministically without sleeping.
"""
from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass

from mtsics.core.state import ScaleState


@dataclass
class SimConfig:
    """
    Tunable parameters for WeightSimulator.

    All time values are in seconds; weight values in whatever unit the
    ScaleState uses (typically kg).
    """

    # ── Settle curve ─────────────────────────────────────────────────
    # Exponential time constant.  After N × τ seconds:
    #   1τ → 63%   2τ → 86%   3τ → 95%   4τ → 98%   5τ → 99.3%
    settle_tau: float = 0.4

    # ── Noise ────────────────────────────────────────────────────────
    # Standard deviation of the Gaussian noise added each tick.
    # None → graduation / 10 (resolved in WeightSimulator.__init__).
    noise_sigma: float | None = None

    # ── Stability detection ───────────────────────────────────────────
    # Length of the rolling window (seconds).
    stability_window: float = 1.0

    # Max peak-to-peak variation within the window for stable = True.
    # None → 1 × graduation (resolved in WeightSimulator.__init__).
    stability_threshold: float | None = None


class WeightSimulator:
    """
    Simulates the physical weight sensor and indicator averaging filter
    of an MT-SICS compatible scale.  Drives a ScaleState via set_weight() on each
    tick.

    Parameters
    ----------
    state:
        The ScaleState that the MT-SICS engine reads from.
    cfg:
        Simulator tuning parameters.  Defaults suit a typical MT-SICS bench scale
        in a stable environment.
    seed:
        Optional RNG seed for reproducible noise in tests.
    """

    def __init__(
        self,
        state: ScaleState,
        cfg: SimConfig | None = None,
        seed: int | None = None,
    ) -> None:
        self._state = state
        self._cfg = cfg or SimConfig()
        self._rng = random.Random(seed)

        # Resolve None → scale-relative defaults
        grad = state.config.graduation
        self._noise_sigma: float = (
            self._cfg.noise_sigma
            if self._cfg.noise_sigma is not None
            else grad / 10.0
        )
        self._stability_threshold: float = (
            self._cfg.stability_threshold
            if self._cfg.stability_threshold is not None
            else grad
        )

        self._target: float = 0.0
        self._current: float = 0.0   # smoothed internal weight (no noise)
        self._elapsed: float = 0.0   # total simulated time (seconds)

        # Rolling history of (elapsed_time, displayed_weight)
        self._history: deque[tuple[float, float]] = deque()

        # Push the initial zero reading into state
        self._state.set_weight(0.0, stable=True)

    # ------------------------------------------------------------------ #
    # Properties                                                           #
    # ------------------------------------------------------------------ #

    @property
    def target(self) -> float:
        """Target weight currently being approached."""
        return self._target

    @property
    def current(self) -> float:
        """Smoothed internal weight (before noise is added)."""
        return self._current

    @property
    def elapsed(self) -> float:
        """Total simulated time in seconds."""
        return self._elapsed

    # ------------------------------------------------------------------ #
    # Control interface                                                    #
    # ------------------------------------------------------------------ #

    def set_target(self, weight: float) -> None:
        """
        Change the target weight.  The simulator will approach it
        exponentially from wherever it currently is.

        This is the primary way to simulate placing or removing a load.
        """
        self._target = weight

    def snap_to(self, weight: float) -> None:
        """
        Immediately jump the internal weight to ``weight`` with no settle
        delay.  Clears the history so stability is re-evaluated from this
        point forward.

        Useful in tests and for simulating instantaneous load removal.
        """
        self._target = weight
        self._current = weight
        self._history.clear()
        # A single fresh reading; stability will be True on the next tick
        # (not enough history to say otherwise)
        self._state.set_weight(weight, stable=True)

    # ------------------------------------------------------------------ #
    # Simulation tick                                                      #
    # ------------------------------------------------------------------ #

    def tick(self, dt: float) -> None:
        """
        Advance the simulation by ``dt`` seconds.

        Sequence per tick:
          1. Advance ``current`` toward ``target`` via exponential decay.
          2. Sample Gaussian noise and add it to produce ``displayed``.
          3. Append ``displayed`` to the rolling history.
          4. Prune history entries older than 2 x stability_window.
          5. Evaluate stability (peak-to-peak within window ≤ threshold).
          6. Call state.set_weight(displayed, stable).
        """
        if dt <= 0:
            return

        self._elapsed += dt

        # Step 1 — exponential approach
        alpha = 1.0 - math.exp(-dt / self._cfg.settle_tau)
        self._current += (self._target - self._current) * alpha

        # Step 2 — add noise
        noise = self._rng.gauss(0.0, self._noise_sigma)
        displayed = self._current + noise

        # Steps 3 & 4 — update history
        self._history.append((self._elapsed, displayed))
        self._prune_history()

        # Step 5 — stability
        stable = self._check_stable()

        # Step 6 — push to state
        self._state.set_weight(displayed, stable=stable)

    def advance_to_stable(
        self,
        convergence_tol: float = 1e-4,
        step: float = 0.05,
        max_sim_time: float = 10.0,
    ) -> float:
        """
        Tick in ``step``-second increments until the simulator is both
        converged (``|current - target| < convergence_tol``) and stable,
        or until ``max_sim_time`` seconds have been simulated.

        Returns the simulated time elapsed during the call.

        This is primarily a testing convenience so tests can skip the
        settle period without hard-coding a specific number of ticks.
        """
        elapsed = 0.0
        while elapsed < max_sim_time:
            self.tick(step)
            elapsed += step
            converged = abs(self._current - self._target) < convergence_tol
            if converged and self._state.stable:
                break
        return elapsed

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _prune_history(self) -> None:
        """Remove entries older than 2 × stability_window from the deque."""
        cutoff = self._elapsed - self._cfg.stability_window * 2.0
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

    def _check_stable(self) -> bool:
        """
        True if the peak-to-peak spread of readings within the stability
        window is at or below the stability threshold.

        Returns True immediately if fewer than 2 readings are in the window
        (not enough data to declare instability).
        """
        cutoff = self._elapsed - self._cfg.stability_window
        recent = [w for t, w in self._history if t >= cutoff]
        if len(recent) < 2:
            return True
        return (max(recent) - min(recent)) <= self._stability_threshold