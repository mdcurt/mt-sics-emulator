"""
Built-in scale profiles for the MT-SICS emulator.

Each profile is a ScaleConfig whose values are taken from the manufacturer's
published specifications.  The profile drives:

  * Capacity and graduation (and therefore decimal places and response format)
  * Unit string used in weight responses
  * Model name returned by I1
  * Serial number format (fictional placeholder matching manufacturer conventions)
  * Software version string returned by I3

Profiles are grouped by manufacturer prefix:
  ohaus-*         Ohaus Corporation
  mt-*            Mettler Toledo International Inc.
  sartorius-*     Sartorius AG

Usage::

    from mtsics.profiles import load, list_profiles

    cfg = load("mt-xs204")
    print(list_profiles())
"""
from __future__ import annotations

from mtsics.core.state import ScaleConfig

# ---------------------------------------------------------------------------
# Profile definitions
# ---------------------------------------------------------------------------
#
# Sources: manufacturer product pages and indicator instruction manuals.
# Serial numbers are fictional placeholders in the manufacturer's format.
#

_PROFILES: dict[str, ScaleConfig] = {

    # ── Ohaus ─────────────────────────────────────────────────────────────

    "ohaus-defender5000": ScaleConfig(
        # TD52P — the most common warehouse / receiving bench scale.
        # 150 kg at 0.05 kg readability is the flagship configuration.
        capacity=150.0,
        graduation=0.05,
        unit="kg",
        model="Defender 5000",
        serial_number="B123456789",
        sw_version="1.00",
    ),

    "ohaus-ranger7000": ScaleConfig(
        # R71MHD35 — compact counting and parts-weighing bench scale.
        # Graduated at 0.005 kg; popular in electronics and retail.
        capacity=35.0,
        graduation=0.005,
        unit="kg",
        model="Ranger 7000",
        serial_number="R712345678",
        sw_version="1.02",
    ),

    "ohaus-scout": ScaleConfig(
        # SPX4201 — portable general-purpose bench scale.
        # Works in grams; common in labs, classrooms, and small retail.
        capacity=4200.0,
        graduation=0.1,
        unit="g",
        model="Scout SPX4201",
        serial_number="S123456789",
        sw_version="1.00",
    ),

    # ── Mettler Toledo ────────────────────────────────────────────────────

    "mt-xs204": ScaleConfig(
        # XS204 Excellence analytical balance.
        # 220 g at 0.1 mg (0.0001 g) — standard analytical lab balance.
        capacity=220.0,
        graduation=0.0001,
        unit="g",
        model="XS204",
        serial_number="B246813579",
        sw_version="2.30",
    ),

    "mt-ms3002s": ScaleConfig(
        # MS3002S NewClassic precision balance.
        # 3100 g at 0.01 g — mid-range precision for formulation and QC.
        capacity=3100.0,
        graduation=0.01,
        unit="g",
        model="MS3002S",
        serial_number="B135792468",
        sw_version="3.10",
    ),

    "mt-ind570": ScaleConfig(
        # IND570 industrial weighing terminal paired with a 300 kg load cell.
        # 0.1 kg graduation — truck scales, floor scales, silo weighing.
        capacity=300.0,
        graduation=0.1,
        unit="kg",
        model="IND570",
        serial_number="C987654321",
        sw_version="3.05",
    ),

    # ── Sartorius ─────────────────────────────────────────────────────────

    "sartorius-quintix224": ScaleConfig(
        # Quintix 224-1S — semi-micro analytical balance.
        # 220 g at 0.0001 g; popular in pharma and chemical research.
        capacity=220.0,
        graduation=0.0001,
        unit="g",
        model="Quintix 224-1S",
        serial_number="26200901",
        sw_version="V04.00.0039",
    ),

    "sartorius-practum6100": ScaleConfig(
        # Practum 6100-1S — high-capacity precision balance.
        # 6100 g at 0.1 g; formulation, teaching labs, small production.
        capacity=6100.0,
        graduation=0.1,
        unit="g",
        model="Practum 6100-1S",
        serial_number="26400203",
        sw_version="V04.00.0039",
    ),
}

# The profile used when no --profile flag is given.
DEFAULT_PROFILE = "ohaus-defender5000"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load(name: str) -> ScaleConfig:
    """
    Return the ScaleConfig for a named profile.

    Raises ``KeyError`` with a helpful message if the name is unknown.
    """
    try:
        return _PROFILES[name]
    except KeyError:
        available = ", ".join(sorted(_PROFILES))
        raise KeyError(
            f"Unknown profile {name!r}. Available profiles: {available}"
        ) from None


def list_profiles() -> list[str]:
    """Return a sorted list of all profile names."""
    return sorted(_PROFILES)


def summary_table() -> str:
    """
    Return a human-readable table of all profiles — used by --list-profiles.
    """
    rows = []
    rows.append(f"{'Profile':<30} {'Model':<26} {'Capacity':>10}  {'Grad':>10}  Unit")
    rows.append("-" * 85)
    for name in sorted(_PROFILES):
        c = _PROFILES[name]
        rows.append(
            f"{name:<30} {c.model:<26} "
            f"{c.capacity:>9.4g} {c.unit}  "
            f"{c.graduation:>9.4g} {c.unit}  "
            f"{c.unit}"
        )
    return "\n".join(rows)