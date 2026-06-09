"""
MT-SICS protocol engine for the MT-SICS emulator.

Accepts a single command line (whitespace-stripped, no CR/LF), returns a
complete response string including the trailing CR+LF.

The engine is deliberately stateless with respect to I/O — it has no
knowledge of sockets, serial ports, or threads. The transport layer
(added later) feeds lines in and writes responses out.

Reference commands implemented:
  Level 0:  @  S  SI  SIR  Z  ZI
  Level 1:  T  TI  TAR  TAC
  Inquiry:  I0  I1  I2  I3  I4

See: MT-SICS Reference Manual (Mettler Toledo)
     MT-SICS Reference Manual (Mettler Toledo)
"""
from __future__ import annotations

from typing import Callable

from mtsics.core.state import ScaleState

CRLF = "\r\n"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fmt_weight(state: ScaleState, value: float) -> str:
    """
    Format a weight value as MT-SICS expects: a right-justified number
    in a 10-character field, followed by a space and the unit string.

    Example:  "      0.00 kg"  or  "    150.00 kg"
    """
    dp = state.config.decimal_places
    return f"{value:10.{dp}f} {state.config.unit}"


def _weight_response(cmd: str, state: ScaleState, value: float) -> str:
    """
    Build the standard weight query response, handling range conditions.

      overrange  → "<CMD> +<CRLF>"
      underrange → "<CMD> -<CRLF>"
      stable     → "<CMD> S <weight><CRLF>"
      dynamic    → "<CMD> D <weight><CRLF>"
    """
    if state.overrange:
        return f"{cmd} +{CRLF}"
    if state.underrange:
        return f"{cmd} -{CRLF}"
    status = "S" if state.stable else "D"
    return f"{cmd} {status} {_fmt_weight(state, value)}{CRLF}"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class MTSICSEngine:
    """
    Pure MT-SICS protocol engine.

    Usage::

        state = ScaleState()
        engine = MTSICSEngine(state)

        state.set_weight(12.5)
        print(engine.handle("SI"))   # "SI S      12.50 kg\\r\\n"
        print(engine.handle("T"))    # "T S      12.50 kg\\r\\n"
        print(engine.handle("SI"))   # "SI S       0.00 kg\\r\\n"

    The ``sir_active`` flag is set by the SIR command. The transport layer
    (once added) should check this flag and send weight updates continuously.
    """

    sir_active: bool = False

    def __init__(self, state: ScaleState) -> None:
        self.state = state

        self._dispatch: dict[str, Callable[[list[str]], str]] = {
            "@":   self._cmd_reset,
            "S":   self._cmd_s,
            "SI":  self._cmd_si,
            "SIR": self._cmd_sir,
            "SNR": self._cmd_sir,     # SNR is the stable-continuous alias
            "Z":   self._cmd_z,
            "ZI":  self._cmd_zi,
            "T":   self._cmd_t,
            "TI":  self._cmd_ti,
            "TAR": self._cmd_tar,
            "TAC": self._cmd_tac,
            "I0":  self._cmd_i0,
            "I1":  self._cmd_i1,
            "I2":  self._cmd_i2,
            "I3":  self._cmd_i3,
            "I4":  self._cmd_i4,
        }

    def handle(self, raw: str) -> str:
        """
        Process one MT-SICS input line.

        ``raw`` must be a single command with surrounding whitespace stripped;
        the caller is responsible for framing (splitting on CR/LF).

        Returns a complete response string including trailing CRLF.
        Unknown commands and parse failures return the "ES" (syntax error)
        response, matching real MT-SICS scale behaviour.
        """
        line = raw.strip()
        if not line:
            return f"ES{CRLF}"

        tokens = line.split()
        cmd = tokens[0].upper()
        args = tokens[1:]

        handler = self._dispatch.get(cmd)
        if handler is None:
            return f"ES{CRLF}"

        return handler(args)

    # ------------------------------------------------------------------
    # Weight queries
    # ------------------------------------------------------------------

    def _cmd_s(self, args: list[str]) -> str:
        """
        S — send stable weight value.
        On a real scale this blocks until the reading is stable. Here we
        return the current reading with its stability status so tests can
        exercise both the stable and dynamic branches.
        """
        return _weight_response("S", self.state, self.state.net)

    def _cmd_si(self, args: list[str]) -> str:
        """SI — send weight value immediately (regardless of stability)."""
        return _weight_response("SI", self.state, self.state.net)

    def _cmd_sir(self, args: list[str]) -> str:
        """
        SIR — start continuous immediate weight output.
        Sets sir_active; the transport layer should begin streaming.
        Returns one immediate reading so the client gets an instant response.
        """
        self.sir_active = True
        return _weight_response("S", self.state, self.state.net)

    # ------------------------------------------------------------------
    # Zero
    # ------------------------------------------------------------------

    def _cmd_z(self, args: list[str]) -> str:
        """Z — zero the scale (requires stability and within zero range)."""
        if self.state.overrange:
            return f"Z +{CRLF}"
        if self.state.underrange:
            return f"Z -{CRLF}"
        return f"Z A{CRLF}" if self.state.do_zero() else f"Z I{CRLF}"

    def _cmd_zi(self, args: list[str]) -> str:
        """ZI — zero immediately (no stability requirement)."""
        return f"ZI A{CRLF}" if self.state.do_zero_immediate() else f"ZI I{CRLF}"

    # ------------------------------------------------------------------
    # Tare
    # ------------------------------------------------------------------

    def _cmd_t(self, args: list[str]) -> str:
        """T — tare (requires stability)."""
        if not self.state.do_tare(immediate=False):
            return f"T I{CRLF}"
        return f"T S {_fmt_weight(self.state, self.state.tare)}{CRLF}"

    def _cmd_ti(self, args: list[str]) -> str:
        """TI — tare immediately."""
        self.state.do_tare(immediate=True)
        status = "S" if self.state.stable else "D"
        return f"TI {status} {_fmt_weight(self.state, self.state.tare)}{CRLF}"

    def _cmd_tar(self, args: list[str]) -> str:
        """TAR <value> — set an explicit tare value."""
        if not args:
            return f"ES{CRLF}"
        try:
            value = float(args[0])
        except ValueError:
            return f"ES{CRLF}"
        return f"TAR A{CRLF}" if self.state.do_set_tare(value) else f"TAR I{CRLF}"

    def _cmd_tac(self, args: list[str]) -> str:
        """TAC — clear tare."""
        self.state.do_clear_tare()
        return f"TAC A{CRLF}"

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def _cmd_reset(self, args: list[str]) -> str:
        """@ — full reset. Clears tare and zero offset, stops SIR."""
        self.sir_active = False
        self.state.do_reset()
        # After reset the scale broadcasts its SW ID, just as a real unit does
        return f'I4 A "{self.state.config.serial_number}"{CRLF}'

    # ------------------------------------------------------------------
    # Inquiry
    # ------------------------------------------------------------------

    def _cmd_i0(self, args: list[str]) -> str:
        """I0 — scale data summary."""
        cfg = self.state.config
        return (
            f'I0 A "{cfg.model}" "{cfg.serial_number}" "{cfg.sw_version}" '
            f'"{cfg.capacity} {cfg.unit}" "{cfg.graduation} {cfg.unit}"{CRLF}'
        )

    def _cmd_i1(self, args: list[str]) -> str:
        """I1 — model name."""
        return f'I1 A "{self.state.config.model}"{CRLF}'

    def _cmd_i2(self, args: list[str]) -> str:
        """I2 — serial number."""
        return f'I2 A "{self.state.config.serial_number}"{CRLF}'

    def _cmd_i3(self, args: list[str]) -> str:
        """I3 — software version."""
        return f'I3 A "{self.state.config.sw_version}"{CRLF}'

    def _cmd_i4(self, args: list[str]) -> str:
        """I4 — SW ID number (same as serial number on most MT-SICS scales)."""
        return f'I4 A "{self.state.config.serial_number}"{CRLF}'