"""
Scale state machine for the MT-SICS emulator.

Tracks the physical state of the scale: raw weight, zero offset, tare,
stability, and configuration. All weight values are in the scale's
configured unit (default: kg).

The simulator (or a test) drives the physical weight by calling set_weight().
Command methods (do_zero, do_tare, etc.) are called by the MT-SICS engine
in response to incoming protocol commands.

Scale identity (model name, serial number, capacity, graduation) comes from
a ScaleConfig — typically loaded via ``mtsics.profiles.load(name)`` rather
than constructed directly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class ScaleConfig:
    """
    Immutable scale configuration.

    Describes the identity and physical limits of one scale. Typically
    loaded through ``mtsics.profiles.load(name)`` rather than constructed
    directly — the profile provides manufacturer-correct values for capacity,
    graduation, model name, and serial number format.

    If constructed directly without arguments, the defaults produce a
    generic MT-SICS scale useful for quick experiments.
    """
    capacity: float = 150.0       # Maximum weighing capacity (in unit)
    graduation: float = 0.05      # Minimum division / readability (in unit)
    unit: str = "kg"
    model: str = "MT-SICS Scale"
    serial_number: str = "SN000000001"
    sw_version: str = "1.0.0"

    @property
    def decimal_places(self) -> int:
        """Decimal places implied by graduation (e.g. 0.05 → 2, 0.1 → 1)."""
        if self.graduation >= 1:
            return 0
        return -int(math.floor(math.log10(self.graduation)))

    @property
    def zero_range(self) -> float:
        """Maximum gross weight that can be zeroed (2% of capacity is standard)."""
        return 0.02 * self.capacity


@dataclass
class ScaleState:
    """
    Mutable runtime state of the emulated scale.

    All weights are stored in the scale's configured unit. Values exposed
    through properties are always rounded to the nearest graduation.
    """
    config: ScaleConfig = field(default_factory=ScaleConfig)

    _raw_weight: float = 0.0      # Physical load on the platform
    _zero_offset: float = 0.0     # Offset applied by the Z / ZI command
    _tare: float = 0.0            # Tare set by T / TI / TAR
    _stable: bool = True          # Set by the weight simulator

    # ------------------------------------------------------------------
    # Read-only properties
    # ------------------------------------------------------------------

    @property
    def gross(self) -> float:
        """Gross weight (physical load minus zero offset), rounded to graduation."""
        return self._round(self._raw_weight - self._zero_offset)

    @property
    def net(self) -> float:
        """Net weight (gross minus tare), rounded to graduation."""
        return self._round(self.gross - self._tare)

    @property
    def tare(self) -> float:
        """Current tare value, rounded to graduation."""
        return self._round(self._tare)

    @property
    def stable(self) -> bool:
        return self._stable

    @property
    def overrange(self) -> bool:
        return self.gross > self.config.capacity

    @property
    def underrange(self) -> bool:
        return self.gross < -self.config.graduation

    # ------------------------------------------------------------------
    # Command methods — called by the MT-SICS engine
    # ------------------------------------------------------------------

    def do_zero(self) -> bool:
        """
        Zero the scale (Z command). Requires stability and that the
        gross weight is within the zeroing range (±2% of capacity).
        Returns True on success.
        """
        if not self._stable:
            return False
        if abs(self.gross) > self.config.zero_range:
            return False
        self._zero_offset = self._raw_weight
        return True

    def do_zero_immediate(self) -> bool:
        """
        Zero immediately (ZI command). No stability requirement.
        Still refuses if overrange or underrange.
        Returns True on success.
        """
        if self.overrange or self.underrange:
            return False
        self._zero_offset = self._raw_weight
        return True

    def do_tare(self, immediate: bool = False) -> bool:
        """
        Tare the scale.
        T (immediate=False) requires stability.
        TI (immediate=True) does not.
        Returns True on success.
        """
        if not immediate and not self._stable:
            return False
        self._tare = self.gross
        return True

    def do_set_tare(self, value: float) -> bool:
        """
        Set an explicit tare value (TAR <value> command).
        Returns True if value is within the valid range.
        """
        if value < 0 or value > self.config.capacity:
            return False
        self._tare = value
        return True

    def do_clear_tare(self) -> None:
        """Clear tare (TAC command)."""
        self._tare = 0.0

    def do_reset(self) -> None:
        """Full scale reset (@ command). Clears tare and zero offset."""
        self._tare = 0.0
        self._zero_offset = 0.0

    # ------------------------------------------------------------------
    # Simulator interface
    # ------------------------------------------------------------------

    def set_weight(self, weight: float, stable: bool = True) -> None:
        """
        Set the current physical weight on the platform.
        Called by the weight simulator or directly in tests.
        """
        self._raw_weight = weight
        self._stable = stable

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _round(self, value: float) -> float:
        """Round a weight value to the nearest graduation."""
        d = self.config.graduation
        return round(round(value / d) * d, self.config.decimal_places)