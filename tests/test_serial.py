"""
Tests for the serial transport.

Because serial I/O requires hardware or a virtual port pair (socat / com0com),
all tests mock ``serial.Serial``.  The tests exercise:

  * Command dispatch through ``_handle_line()``
  * SIR continuous output (start, stop, restart)
  * Simulator ticking while serving
  * Port disconnect (reader thread sentinel)
  * ``close()`` idempotency

Run with:  pytest tests/test_serial.py -v
"""
from __future__ import annotations

import asyncio
import contextlib
import queue
from unittest.mock import MagicMock, patch

import pytest

from mtsics.core.simulator import SimConfig, WeightSimulator
from mtsics.core.state import ScaleConfig, ScaleState
from mtsics.protocol.engine import MTSICSEngine
from mtsics.transport.serial import SerialConfig, SerialTransport


# ---------------------------------------------------------------------------
# Mock serial.Serial factory
# ---------------------------------------------------------------------------

def make_serial_mock(
    rx_lines: list[bytes] | None = None,
) -> tuple[MagicMock, list[bytes]]:
    """
    Build a mock ``serial.Serial`` instance.

    ``rx_lines`` is a list of byte strings the mock will return from
    ``read()`` in order, one per call.  After all lines are exhausted the
    mock blocks (returns b"") so the reader thread stays alive until
    ``_running`` is cleared.

    Returns ``(mock, written)`` where ``written`` accumulates every
    ``write()`` call.
    """
    rx_lines = rx_lines or []
    rx_q: queue.Queue[bytes] = queue.Queue()
    for line in rx_lines:
        rx_q.put(line)

    written: list[bytes] = []

    def _read(n: int) -> bytes:
        try:
            return rx_q.get(timeout=0.05)
        except queue.Empty:
            return b""

    mock = MagicMock()
    mock.is_open = True
    mock.read = _read
    mock.write = lambda data: written.append(data)
    return mock, written


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state() -> ScaleState:
    return ScaleState(config=ScaleConfig(capacity=150.0, graduation=0.05))


@pytest.fixture
def engine(state: ScaleState) -> MTSICSEngine:
    return MTSICSEngine(state)


@pytest.fixture
def sim(state: ScaleState) -> WeightSimulator:
    return WeightSimulator(state, cfg=SimConfig(noise_sigma=0.0), seed=0)


def _make_transport(engine, sim, written_out, mock_serial):
    """Create a SerialTransport with a pre-configured mock serial object."""
    cfg = SerialConfig(port="/dev/ttyMOCK", baudrate=9600, sir_rate=20.0)
    with patch("serial.Serial", return_value=mock_serial):
        transport = SerialTransport(engine, simulator=sim, cfg=cfg)
    transport._serial = mock_serial      # inject directly (bypass open())
    transport._loop = asyncio.get_event_loop()
    transport._running = True
    return transport


# ---------------------------------------------------------------------------
# _handle_line — unit tests (no threading needed)
# ---------------------------------------------------------------------------

class TestHandleLine:
    """Drive _handle_line() directly; no reader thread required."""

    @pytest.fixture
    async def rig(self, state, engine, sim):
        mock, written = make_serial_mock()
        transport = _make_transport(engine, sim, written, mock)
        yield transport, state, engine, sim, written
        await transport.close()

    async def test_si_stable(self, rig):
        transport, state, engine, sim, written = rig
        sim.snap_to(12.5)
        await transport._handle_line("SI")
        resp = b"".join(written)
        assert b"SI S" in resp
        assert b"12.50" in resp

    async def test_si_dynamic(self, rig):
        transport, state, _, _, written = rig
        state.set_weight(5.0, stable=False)
        await transport._handle_line("SI")
        assert b"SI D" in b"".join(written)

    async def test_si_overrange(self, rig):
        transport, _, _, sim, written = rig
        sim.snap_to(200.0)
        await transport._handle_line("SI")
        assert b"SI +" in b"".join(written)

    async def test_s_stable(self, rig):
        transport, _, _, sim, written = rig
        sim.snap_to(7.0)
        sim.advance_to_stable()
        await transport._handle_line("S")
        assert b"S S" in b"".join(written)

    async def test_zero(self, rig):
        transport, _, _, sim, written = rig
        sim.snap_to(0.5)
        sim.advance_to_stable()
        await transport._handle_line("Z")
        assert b"Z A" in b"".join(written)

    async def test_zero_refused_out_of_range(self, rig):
        transport, _, _, sim, written = rig
        sim.snap_to(50.0)           # way beyond ±2% capacity
        sim.advance_to_stable()
        await transport._handle_line("Z")
        assert b"Z I" in b"".join(written)

    async def test_tare(self, rig):
        transport, _, _, sim, written = rig
        sim.snap_to(5.0)
        sim.advance_to_stable()
        await transport._handle_line("T")
        assert b"T S" in b"".join(written)

    async def test_tare_then_net_zero(self, rig):
        transport, _, _, sim, written = rig
        sim.snap_to(5.0)
        sim.advance_to_stable()
        await transport._handle_line("T")
        written.clear()
        await transport._handle_line("SI")
        assert b"0.00" in b"".join(written)

    async def test_reset(self, rig):
        transport, _, _, _, written = rig
        await transport._handle_line("@")
        assert b"I4 A" in b"".join(written)

    async def test_i1_model(self, rig):
        transport, state, _, _, written = rig
        await transport._handle_line("I1")
        resp = b"".join(written)
        assert b"I1 A" in resp
        assert state.config.model.encode() in resp

    async def test_unknown_command(self, rig):
        transport, _, _, _, written = rig
        await transport._handle_line("BOGUS")
        assert b"ES" in b"".join(written)

    async def test_case_insensitive(self, rig):
        transport, _, _, sim, written = rig
        sim.snap_to(3.0)
        await transport._handle_line("si")
        assert b"SI" in b"".join(written)

    async def test_sequential_commands(self, rig):
        transport, _, _, sim, written = rig
        sim.snap_to(10.0)
        for _ in range(5):
            written.clear()
            await transport._handle_line("SI")
            assert b"10.00" in b"".join(written)


# ---------------------------------------------------------------------------
# SIR continuous output
# ---------------------------------------------------------------------------

class TestSIR:
    @pytest.fixture
    async def rig(self, state, engine, sim):
        mock, written = make_serial_mock()
        transport = _make_transport(engine, sim, written, mock)
        yield transport, state, engine, sim, written
        await transport.close()

    async def test_sir_starts_streaming(self, rig):
        transport, _, _, sim, written = rig
        sim.snap_to(5.0)
        sim.advance_to_stable()
        await transport._handle_line("SIR")

        # Give the SIR task (20 Hz) time to fire a few times
        await asyncio.sleep(0.25)

        all_data = b"".join(written)
        # Should have the immediate SIR response + at least 2 more from the loop
        assert all_data.count(b"S S") >= 3

        await transport._stop_sir()

    async def test_command_stops_sir(self, rig):
        transport, _, _, sim, written = rig
        sim.snap_to(5.0)
        sim.advance_to_stable()

        await transport._handle_line("SIR")
        await asyncio.sleep(0.1)          # let it stream briefly

        written.clear()
        await transport._handle_line("TAC")    # any non-SIR command stops it
        await asyncio.sleep(0.15)         # if SIR were still running, we'd see more S lines

        after = b"".join(written)
        assert b"TAC A" in after
        # No new S-prefix lines should appear after TAC A
        lines_after_tac = [
            line for line in after.split(b"\r\n")
            if line.startswith(b"S ")
        ]
        assert lines_after_tac == [], f"SIR continued after TAC: {lines_after_tac}"

    async def test_sir_restart(self, rig):
        transport, _, _, sim, written = rig
        sim.snap_to(5.0)
        sim.advance_to_stable()

        await transport._handle_line("SIR")
        await asyncio.sleep(0.1)
        await transport._handle_line("TAC")     # stop
        await asyncio.sleep(0.1)

        written.clear()
        await transport._handle_line("SIR")     # restart
        await asyncio.sleep(0.15)

        assert b"S S" in b"".join(written), "SIR did not restart"
        await transport._stop_sir()

    async def test_snr_alias(self, rig):
        """SNR is an alias for SIR (stable-continuous)."""
        transport, _, _, sim, written = rig
        sim.snap_to(5.0)
        sim.advance_to_stable()
        await transport._handle_line("SNR")
        await asyncio.sleep(0.1)
        assert transport._sir_task is not None
        await transport._stop_sir()


# ---------------------------------------------------------------------------
# Simulator integration
# ---------------------------------------------------------------------------

class TestSimulatorIntegration:
    async def test_tick_task_advances_elapsed(self, state, engine, sim):
        mock, written = make_serial_mock()
        cfg = SerialConfig(port="/dev/ttyMOCK", sim_tick_rate=20.0)
        with patch("serial.Serial", return_value=mock):
            transport = SerialTransport(engine, simulator=sim, cfg=cfg)
            transport._serial = mock
            transport._loop = asyncio.get_event_loop()
            transport._running = True

        transport._tick_task = asyncio.create_task(transport._tick_simulator())
        before = sim.elapsed
        await asyncio.sleep(0.2)
        transport._tick_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await transport._tick_task

        assert sim.elapsed > before, "Simulator tick task did not run"

    async def test_settled_weight_via_handle_line(self, state, engine, sim):
        mock, written = make_serial_mock()
        transport = _make_transport(engine, sim, written, mock)
        sim.set_target(30.0)
        sim.advance_to_stable()
        await transport._handle_line("SI")
        assert b"30.00" in b"".join(written)
        await transport.close()


# ---------------------------------------------------------------------------
# Port disconnect
# ---------------------------------------------------------------------------

class TestPortDisconnect:
    async def test_sentinel_exits_serve_forever(self, state, engine, sim):
        """
        When the reader thread sends None (port gone), serve_forever()
        should return cleanly.
        """
        mock, written = make_serial_mock()
        cfg = SerialConfig(port="/dev/ttyMOCK", sim_tick_rate=20.0)
        with patch("serial.Serial", return_value=mock):
            transport = SerialTransport(engine, simulator=sim, cfg=cfg)
            transport._serial = mock
            transport._loop = asyncio.get_event_loop()
            transport._running = True

        # Inject sentinel directly — simulates port gone
        await transport._rx_queue.put(None)

        # serve_forever should return quickly
        try:
            await asyncio.wait_for(transport.serve_forever(), timeout=1.0)
        except asyncio.TimeoutError:
            pytest.fail("serve_forever() did not exit after sentinel")


# ---------------------------------------------------------------------------
# close() idempotency and safety
# ---------------------------------------------------------------------------

class TestClose:
    async def test_double_close_is_safe(self, state, engine, sim):
        mock, _ = make_serial_mock()
        transport = _make_transport(engine, sim, [], mock)
        await transport.close()
        await transport.close()   # must not raise

    async def test_close_cancels_sir(self, state, engine, sim):
        mock, written = make_serial_mock()
        transport = _make_transport(engine, sim, written, mock)
        sim.snap_to(5.0)
        await transport._handle_line("SIR")
        assert transport._sir_task is not None
        await transport.close()
        assert transport._sir_task is None


# ---------------------------------------------------------------------------
# SerialConfig defaults
# ---------------------------------------------------------------------------

class TestSerialConfig:
    def test_defaults_match_defender5000(self):
        cfg = SerialConfig()
        assert cfg.baudrate == 9600
        assert cfg.bytesize == 8
        assert cfg.parity == "N"
        assert cfg.stopbits == 1.0

    def test_custom_port(self):
        cfg = SerialConfig(port="COM5", baudrate=19200)
        assert cfg.port == "COM5"
        assert cfg.baudrate == 19200