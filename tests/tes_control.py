"""
Tests for the HTTP control API.

Each test starts a ControlAPI on port 0, makes raw HTTP requests against it
using asyncio streams (no external HTTP client dependency), and verifies
the JSON responses and the resulting simulator/state changes.

The final class proves the headline feature: weight set via HTTP is
immediately readable by an MT-SICS client on the TCP transport.

Run with:  pytest tests/test_control.py -v
"""
from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from mtsics.core.simulator import SimConfig, WeightSimulator
from mtsics.core.state import ScaleConfig, ScaleState
from mtsics.protocol.engine import MTSICSEngine
from mtsics.transport.control import ControlAPI, ControlConfig
from mtsics.transport.tcp import TCPConfig, TCPTransport


# ---------------------------------------------------------------------------
# Fixtures and HTTP helper
# ---------------------------------------------------------------------------

@pytest.fixture
async def rig():
    """Yields (control, state, sim, host, port) with a noiseless simulator."""
    state = ScaleState(config=ScaleConfig(capacity=150.0, graduation=0.05))
    sim = WeightSimulator(state, cfg=SimConfig(noise_sigma=0.0), seed=0)
    control = ControlAPI(state, sim, cfg=ControlConfig(port=0))
    host, port = await control.start()

    yield control, state, sim, host, port

    with contextlib.suppress(Exception):
        await asyncio.wait_for(control.close(), timeout=2.0)


async def http(
    host: str,
    port: int,
    method: str,
    path: str,
    body: dict | str | None = None,
    timeout: float = 3.0,
) -> tuple[int, dict]:
    """
    Minimal HTTP/1.1 client built on asyncio streams.
    Returns (status_code, parsed_json_body).
    """
    if isinstance(body, dict):
        payload = json.dumps(body).encode()
    elif isinstance(body, str):
        payload = body.encode()
    else:
        payload = b""

    request = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode() + payload

    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(request)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), timeout=timeout)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    head, _, body_bytes = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode()
    status = int(status_line.split()[1])
    parsed = json.loads(body_bytes) if body_bytes else {}
    return status, parsed


# ---------------------------------------------------------------------------
# GET /state
# ---------------------------------------------------------------------------

class TestGetState:
    async def test_returns_200(self, rig):
        _, _, _, host, port = rig
        status, _ = await http(host, port, "GET", "/state")
        assert status == 200

    async def test_initial_state_fields(self, rig):
        _, _, _, host, port = rig
        _, data = await http(host, port, "GET", "/state")
        assert data["gross"] == 0.0
        assert data["net"] == 0.0
        assert data["tare"] == 0.0
        assert data["stable"] is True
        assert data["overrange"] is False
        assert data["underrange"] is False
        assert data["unit"] == "kg"
        assert data["capacity"] == 150.0
        assert data["graduation"] == 0.05

    async def test_reflects_simulator_weight(self, rig):
        _, _, sim, host, port = rig
        sim.snap_to(12.5)
        _, data = await http(host, port, "GET", "/state")
        assert data["gross"] == 12.5
        assert data["target"] == 12.5

    async def test_trailing_slash_accepted(self, rig):
        _, _, _, host, port = rig
        status, _ = await http(host, port, "GET", "/state/")
        assert status == 200

    async def test_post_to_state_is_405(self, rig):
        _, _, _, host, port = rig
        status, data = await http(host, port, "POST", "/state")
        assert status == 405
        assert "error" in data


# ---------------------------------------------------------------------------
# POST /weight
# ---------------------------------------------------------------------------

class TestPostWeight:
    async def test_snap_sets_weight_immediately(self, rig):
        _, state, sim, host, port = rig
        status, data = await http(
            host, port, "POST", "/weight", {"value": 25.0, "snap": True}
        )
        assert status == 200
        assert data["gross"] == 25.0
        assert sim.current == 25.0

    async def test_target_starts_settle(self, rig):
        _, _, sim, host, port = rig
        status, data = await http(host, port, "POST", "/weight", {"target": 50.0})
        assert status == 200
        assert sim.target == 50.0
        # No ticking has happened — current is still where it was
        assert sim.current == 0.0

    async def test_target_settles_with_ticks(self, rig):
        _, state, sim, host, port = rig
        await http(host, port, "POST", "/weight", {"target": 30.0})
        sim.advance_to_stable()
        _, data = await http(host, port, "GET", "/state")
        assert abs(data["gross"] - 30.0) <= 0.05

    async def test_value_without_snap_acts_as_target(self, rig):
        """'value' with snap omitted falls back to settle semantics."""
        _, _, sim, host, port = rig
        status, _ = await http(host, port, "POST", "/weight", {"value": 10.0})
        assert status == 200
        assert sim.target == 10.0

    async def test_response_includes_full_state(self, rig):
        _, _, _, host, port = rig
        _, data = await http(
            host, port, "POST", "/weight", {"value": 5.0, "snap": True}
        )
        for key in ("gross", "net", "tare", "stable", "unit", "target"):
            assert key in data

    async def test_get_weight_is_405(self, rig):
        _, _, _, host, port = rig
        status, _ = await http(host, port, "GET", "/weight")
        assert status == 405

    # ── Validation ──────────────────────────────────────────────────────

    async def test_missing_weight_field_is_400(self, rig):
        _, _, _, host, port = rig
        status, data = await http(host, port, "POST", "/weight", {})
        assert status == 400
        assert "error" in data

    async def test_invalid_json_is_400(self, rig):
        _, _, _, host, port = rig
        status, data = await http(host, port, "POST", "/weight", "not json{")
        assert status == 400

    async def test_non_object_json_is_400(self, rig):
        _, _, _, host, port = rig
        status, _ = await http(host, port, "POST", "/weight", "[1,2,3]")
        assert status == 400

    async def test_non_numeric_weight_is_400(self, rig):
        _, _, _, host, port = rig
        status, _ = await http(host, port, "POST", "/weight", {"target": "heavy"})
        assert status == 400

    async def test_nan_weight_is_400(self, rig):
        _, _, _, host, port = rig
        status, _ = await http(host, port, "POST", "/weight", '{"target": NaN}')
        assert status == 400

    async def test_negative_weight_accepted(self, rig):
        """Negative weights are physically valid (below-zero drift)."""
        _, _, sim, host, port = rig
        status, _ = await http(
            host, port, "POST", "/weight", {"value": -0.5, "snap": True}
        )
        assert status == 200
        assert sim.current == -0.5

    async def test_overrange_weight_accepted_and_flagged(self, rig):
        """Setting weight above capacity is allowed; state flags overrange."""
        _, _, _, host, port = rig
        _, data = await http(
            host, port, "POST", "/weight", {"value": 500.0, "snap": True}
        )
        assert data["overrange"] is True


# ---------------------------------------------------------------------------
# POST /reset
# ---------------------------------------------------------------------------

class TestPostReset:
    async def test_reset_returns_200(self, rig):
        _, _, _, host, port = rig
        status, _ = await http(host, port, "POST", "/reset")
        assert status == 200

    async def test_reset_clears_everything(self, rig):
        _, state, sim, host, port = rig
        # Build up some state: weight, tare
        sim.snap_to(10.0)
        state.do_tare(immediate=True)
        assert state.tare > 0

        _, data = await http(host, port, "POST", "/reset")
        assert data["gross"] == 0.0
        assert data["net"] == 0.0
        assert data["tare"] == 0.0
        assert data["target"] == 0.0

    async def test_get_reset_is_405(self, rig):
        _, _, _, host, port = rig
        status, _ = await http(host, port, "GET", "/reset")
        assert status == 405


# ---------------------------------------------------------------------------
# Routing and protocol edge cases
# ---------------------------------------------------------------------------

class TestRouting:
    async def test_unknown_path_is_404(self, rig):
        _, _, _, host, port = rig
        status, data = await http(host, port, "GET", "/bogus")
        assert status == 404
        assert "paths" in data        # discoverability hint

    async def test_root_path_is_404_with_hint(self, rig):
        _, _, _, host, port = rig
        status, data = await http(host, port, "GET", "/")
        assert status == 404
        assert "paths" in data

    async def test_malformed_request_line(self, rig):
        """A non-HTTP request gets a 400, not a hang or crash."""
        _, _, _, host, port = rig
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(b"GARBAGE\r\n\r\n")
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(), timeout=3.0)
            assert b"400" in raw.split(b"\r\n", 1)[0]
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def test_concurrent_requests(self, rig):
        """Multiple simultaneous requests all succeed."""
        _, _, _, host, port = rig
        results = await asyncio.gather(
            *[http(host, port, "GET", "/state") for _ in range(10)]
        )
        assert all(status == 200 for status, _ in results)

    async def test_close_stops_server(self, rig):
        control, _, _, host, port = rig
        await control.close()
        with pytest.raises(OSError):
            await asyncio.open_connection(host, port)


# ---------------------------------------------------------------------------
# Integration: HTTP control + MT-SICS TCP port together
# ---------------------------------------------------------------------------

class TestControlPlusMTSICS:
    """The headline use case: set weight over HTTP, read it over MT-SICS."""

    @pytest.fixture
    async def full_rig(self):
        state = ScaleState(config=ScaleConfig(capacity=150.0, graduation=0.05))
        engine = MTSICSEngine(state)
        # Fast settle (τ=0.1 s) so the real-time stability test completes
        # quickly: converged < 1 s, stable after the 1 s window ≈ 1.6 s.
        sim = WeightSimulator(
            state, cfg=SimConfig(noise_sigma=0.0, settle_tau=0.1), seed=0
        )

        mtsics = TCPTransport(
            engine, simulator=sim, cfg=TCPConfig(port=0, sim_tick_rate=20.0)
        )
        m_host, m_port = await mtsics.start()

        control = ControlAPI(state, sim, cfg=ControlConfig(port=0))
        c_host, c_port = await control.start()

        yield (m_host, m_port), (c_host, c_port), sim

        with contextlib.suppress(Exception):
            await asyncio.wait_for(control.close(), timeout=2.0)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(mtsics.close(), timeout=2.0)

    async def _mtsics_cmd(self, host, port, cmd: str) -> str:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(f"{cmd}\r\n".encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=3.0)
            return line.decode().strip()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def test_http_snap_visible_via_mtsics(self, full_rig):
        (m_host, m_port), (c_host, c_port), sim = full_rig

        # Set weight over HTTP
        status, _ = await http(
            c_host, c_port, "POST", "/weight", {"value": 42.0, "snap": True}
        )
        assert status == 200

        # Read it over MT-SICS
        resp = await self._mtsics_cmd(m_host, m_port, "SI")
        assert "42.00" in resp

    async def test_http_target_settles_and_mtsics_reads_stable(self, full_rig):
        (m_host, m_port), (c_host, c_port), sim = full_rig

        # Set a target over HTTP — settle curve will run via the
        # transport's background tick task
        await http(c_host, c_port, "POST", "/weight", {"target": 20.0})

        # Wait for the real-time tick loop to settle the weight
        # (τ=0.1 s → converged ~0.6 s; + 1 s stability window ≈ 1.6 s)
        await asyncio.sleep(2.0)

        resp = await self._mtsics_cmd(m_host, m_port, "S")
        assert resp.startswith("S S"), f"Expected stable reading, got: {resp}"
        assert "20.00" in resp

    async def test_http_reset_clears_mtsics_tare(self, full_rig):
        (m_host, m_port), (c_host, c_port), sim = full_rig

        # Put weight on, tare it via MT-SICS
        await http(c_host, c_port, "POST", "/weight", {"value": 5.0, "snap": True})
        await self._mtsics_cmd(m_host, m_port, "TI")

        # Net should now be zero
        resp = await self._mtsics_cmd(m_host, m_port, "SI")
        assert "0.00" in resp

        # Reset over HTTP — tare cleared, platform back to 0
        await http(c_host, c_port, "POST", "/reset")
        await http(c_host, c_port, "POST", "/weight", {"value": 5.0, "snap": True})
        resp = await self._mtsics_cmd(m_host, m_port, "SI")
        assert "5.00" in resp      # tare gone → net = gross