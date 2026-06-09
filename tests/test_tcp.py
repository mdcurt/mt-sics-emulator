"""
Integration tests for the TCP transport.

Each test starts a server on port 0 (OS-assigned), connects one or more
TCP clients, exercises the MT-SICS protocol over the wire, and tears down.

The rig fixture uses sir_rate=20 Hz (50 ms intervals) so SIR streaming
tests complete in under a second.

Run with:  pytest tests/test_tcp.py -v
"""
import asyncio
import contextlib

import pytest

from mtsics.core.simulator import SimConfig, WeightSimulator
from mtsics.core.state import ScaleConfig, ScaleState
from mtsics.protocol.engine import MTSICSEngine
from mtsics.transport.tcp import TCPConfig, TCPTransport


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def rig():
    """
    Yields (transport, state, engine, sim, host, port).

    Server binds to port 0 so tests never conflict. Uses sir_rate=20 Hz
    (50 ms) and noise_sigma=0 for fast, deterministic SIR tests.
    The teardown has a 2 s timeout so a leaked connection never hangs CI.
    """
    state = ScaleState(config=ScaleConfig(capacity=150.0, graduation=0.05))
    engine = MTSICSEngine(state)
    sim = WeightSimulator(state, cfg=SimConfig(noise_sigma=0.0), seed=0)
    cfg = TCPConfig(port=0, sir_rate=20.0, sim_tick_rate=20.0)
    transport = TCPTransport(engine, simulator=sim, cfg=cfg)
    host, port = await transport.start()

    yield transport, state, engine, sim, host, port

    with contextlib.suppress(asyncio.TimeoutError, Exception):
        await asyncio.wait_for(transport.close(), timeout=2.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def open_conn(host: str, port: int):
    return await asyncio.open_connection(host, port)


async def close_conn(writer: asyncio.StreamWriter) -> None:
    """Close a connection, ignoring errors and waiting at most 1 s."""
    writer.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)


async def send_recv(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    cmd: str,
    timeout: float = 2.0,
) -> str:
    """Send one command, return one stripped response line."""
    writer.write(f"{cmd}\r\n".encode())
    await writer.drain()
    task = asyncio.create_task(reader.readline())
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if task not in done:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        pytest.fail(f"No response to {cmd!r} within {timeout}s")
    return task.result().decode().strip()


async def collect_for(reader: asyncio.StreamReader, duration: float) -> list[str]:
    """
    Collect all lines that arrive within ``duration`` seconds.

    Uses a background task (not repeated wait_for cancellations) so the
    reader is cancelled exactly once at the end, which is safe.
    """
    lines: list[str] = []

    async def _run():
        try:
            while True:
                line = await reader.readline()
                if line:
                    lines.append(line.decode().strip())
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    task = asyncio.create_task(_run())
    await asyncio.sleep(duration)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return lines


# ---------------------------------------------------------------------------
# Connectivity
# ---------------------------------------------------------------------------

class TestConnectivity:
    async def test_server_accepts_connection(self, rig):
        _, _, _, _, host, port = rig
        _, w = await open_conn(host, port)
        await close_conn(w)

    async def test_address_is_nonzero_port(self, rig):
        transport, _, _, _, host, port = rig
        assert port > 0
        assert transport.address == (host, port)

    async def test_multiple_clients_connect(self, rig):
        _, _, _, _, host, port = rig
        conns = [await open_conn(host, port) for _ in range(3)]
        for _, w in conns:
            await close_conn(w)

    async def test_close_stops_server(self, rig):
        transport, _, _, _, host, port = rig
        await transport.close()
        with pytest.raises(OSError):
            await asyncio.open_connection(host, port)


# ---------------------------------------------------------------------------
# Command framing
# ---------------------------------------------------------------------------

class TestFraming:
    async def test_crlf_terminator(self, rig):
        _, _, _, sim, host, port = rig
        sim.snap_to(1.0)
        r, w = await open_conn(host, port)
        try:
            w.write(b"SI\r\n")
            await w.drain()
            line = await asyncio.wait_for(r.readline(), timeout=2.0)
            assert b"SI" in line
        finally:
            await close_conn(w)

    async def test_lf_only_terminator(self, rig):
        """Some clients (e.g. netcat) send \\n without \\r."""
        _, _, _, sim, host, port = rig
        sim.snap_to(2.0)
        r, w = await open_conn(host, port)
        try:
            w.write(b"SI\n")
            await w.drain()
            line = await asyncio.wait_for(r.readline(), timeout=2.0)
            assert b"SI" in line
        finally:
            await close_conn(w)

    async def test_lowercase_command(self, rig):
        _, _, _, sim, host, port = rig
        sim.snap_to(1.0)
        r, w = await open_conn(host, port)
        try:
            resp = await send_recv(r, w, "si")
            assert resp.startswith("SI")
        finally:
            await close_conn(w)

    async def test_extra_whitespace(self, rig):
        _, _, _, sim, host, port = rig
        sim.snap_to(1.0)
        r, w = await open_conn(host, port)
        try:
            resp = await send_recv(r, w, "  SI  ")
            assert resp.startswith("SI")
        finally:
            await close_conn(w)


# ---------------------------------------------------------------------------
# Weight queries
# ---------------------------------------------------------------------------

class TestWeightQueries:
    async def test_si_stable_weight(self, rig):
        _, _, _, sim, host, port = rig
        sim.snap_to(12.5)
        sim.advance_to_stable()
        r, w = await open_conn(host, port)
        try:
            resp = await send_recv(r, w, "SI")
            assert resp.startswith("SI S")
            assert "12.50" in resp
        finally:
            await close_conn(w)

    async def test_si_dynamic_when_unstable(self, rig):
        _, state, _, _, host, port = rig
        state.set_weight(8.0, stable=False)
        r, w = await open_conn(host, port)
        try:
            resp = await send_recv(r, w, "SI")
            assert resp.startswith("SI D")
        finally:
            await close_conn(w)

    async def test_si_overrange(self, rig):
        _, _, _, sim, host, port = rig
        sim.snap_to(200.0)
        r, w = await open_conn(host, port)
        try:
            assert await send_recv(r, w, "SI") == "SI +"
        finally:
            await close_conn(w)

    async def test_s_stable(self, rig):
        _, _, _, sim, host, port = rig
        sim.snap_to(7.5)
        sim.advance_to_stable()
        r, w = await open_conn(host, port)
        try:
            resp = await send_recv(r, w, "S")
            assert resp.startswith("S S") and "7.50" in resp
        finally:
            await close_conn(w)

    async def test_sequential_si(self, rig):
        _, _, _, sim, host, port = rig
        sim.snap_to(5.0)
        r, w = await open_conn(host, port)
        try:
            for _ in range(5):
                assert "5.00" in await send_recv(r, w, "SI")
        finally:
            await close_conn(w)


# ---------------------------------------------------------------------------
# Zero and tare
# ---------------------------------------------------------------------------

class TestZeroTare:
    async def test_zero(self, rig):
        _, _, _, sim, host, port = rig
        sim.snap_to(0.5)
        sim.advance_to_stable()
        r, w = await open_conn(host, port)
        try:
            assert await send_recv(r, w, "Z") == "Z A"
        finally:
            await close_conn(w)

    async def test_zero_then_si_shows_zero(self, rig):
        _, _, _, sim, host, port = rig
        sim.snap_to(0.5)
        sim.advance_to_stable()
        r, w = await open_conn(host, port)
        try:
            await send_recv(r, w, "Z")
            assert "0.00" in await send_recv(r, w, "SI")
        finally:
            await close_conn(w)

    async def test_tare_net_zero(self, rig):
        _, _, _, sim, host, port = rig
        sim.snap_to(5.0)
        sim.advance_to_stable()
        r, w = await open_conn(host, port)
        try:
            tare = await send_recv(r, w, "T")
            assert tare.startswith("T S") and "5.00" in tare
            assert "0.00" in await send_recv(r, w, "SI")
        finally:
            await close_conn(w)

    async def test_tare_then_add_load(self, rig):
        _, _, _, sim, host, port = rig
        sim.snap_to(2.0)
        sim.advance_to_stable()
        r, w = await open_conn(host, port)
        try:
            await send_recv(r, w, "T")
            sim.snap_to(7.0)
            assert "5.00" in await send_recv(r, w, "SI")
        finally:
            await close_conn(w)


# ---------------------------------------------------------------------------
# Inquiry and reset
# ---------------------------------------------------------------------------

class TestInquiry:
    async def test_i1_model(self, rig):
        _, state, _, _, host, port = rig
        r, w = await open_conn(host, port)
        try:
            resp = await send_recv(r, w, "I1")
            assert resp.startswith("I1 A")
            assert state.config.model in resp
        finally:
            await close_conn(w)

    async def test_i2_serial(self, rig):
        _, _, _, _, host, port = rig
        r, w = await open_conn(host, port)
        try:
            assert (await send_recv(r, w, "I2")).startswith("I2 A")
        finally:
            await close_conn(w)

    async def test_reset_clears_tare(self, rig):
        _, _, _, sim, host, port = rig
        sim.snap_to(5.0)
        sim.advance_to_stable()
        r, w = await open_conn(host, port)
        try:
            await send_recv(r, w, "T")
            await send_recv(r, w, "@")
            assert "5.00" in await send_recv(r, w, "SI")
        finally:
            await close_conn(w)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrors:
    async def test_unknown_command(self, rig):
        _, _, _, _, host, port = rig
        r, w = await open_conn(host, port)
        try:
            assert await send_recv(r, w, "BOGUS") == "ES"
        finally:
            await close_conn(w)

    async def test_bad_command_keeps_session_alive(self, rig):
        _, _, _, sim, host, port = rig
        sim.snap_to(3.0)
        r, w = await open_conn(host, port)
        try:
            assert await send_recv(r, w, "BOGUS") == "ES"
            assert "3.00" in await send_recv(r, w, "SI")
        finally:
            await close_conn(w)

    async def test_tar_missing_arg(self, rig):
        _, _, _, _, host, port = rig
        r, w = await open_conn(host, port)
        try:
            assert await send_recv(r, w, "TAR") == "ES"
        finally:
            await close_conn(w)


# ---------------------------------------------------------------------------
# SIR continuous output
# ---------------------------------------------------------------------------

class TestSIR:
    async def test_sir_immediate_response(self, rig):
        _, _, _, sim, host, port = rig
        sim.snap_to(10.0)
        r, w = await open_conn(host, port)
        try:
            w.write(b"SIR\r\n")
            await w.drain()
            first = await asyncio.wait_for(r.readline(), timeout=2.0)
            assert first.decode().strip().startswith("S ")
        finally:
            await close_conn(w)

    async def test_sir_streams_multiple_responses(self, rig):
        """
        With sir_rate=20 Hz the server sends one reading every 50 ms.
        Collecting for 400 ms should yield ≥ 4 responses (immediate + stream).
        """
        _, _, _, sim, host, port = rig
        sim.snap_to(5.0)
        sim.advance_to_stable()
        r, w = await open_conn(host, port)
        try:
            w.write(b"SIR\r\n")
            await w.drain()
            lines = await collect_for(r, 0.4)   # 400 ms → ~8 frames at 20 Hz
            assert len(lines) >= 4, f"Expected ≥4 SIR lines, got {len(lines)}: {lines}"
            for line in lines:
                assert line.startswith("S "), f"Unexpected line: {line!r}"
        finally:
            await close_conn(w)

    async def test_sir_values_contain_weight(self, rig):
        _, _, _, sim, host, port = rig
        sim.snap_to(20.0)
        sim.advance_to_stable()
        r, w = await open_conn(host, port)
        try:
            w.write(b"SIR\r\n")
            await w.drain()
            lines = await collect_for(r, 0.2)
            assert any("20.00" in line for line in lines), f"Weight missing: {lines}"
        finally:
            await close_conn(w)

    async def test_command_stops_sir(self, rig):
        """
        Sending any command while SIR is active must stop the stream.
        Uses TAC (clear tare) as the stopping command — it is unconditional
        (always returns 'TAC A', no weight or stability conditions), so the
        test is not coupled to the zero-range or stability logic.
        """
        _, _, _, sim, host, port = rig
        sim.snap_to(5.0)
        sim.advance_to_stable()
        r, w = await open_conn(host, port)
        try:
            w.write(b"SIR\r\n")
            await w.drain()

            # Let SIR stream for 150 ms
            await collect_for(r, 0.15)

            # Send TAC — unconditional, stops SIR on the server side
            w.write(b"TAC\r\n")
            await w.drain()

            # Collect for 300 ms; must see "TAC A" then silence
            post = await collect_for(r, 0.3)

            assert any(line == "TAC A" for line in post), (
                f"'TAC A' not found in post-TAC responses: {post}"
            )

            # Nothing after "TAC A" should be an SIR frame
            tac_idx = next(i for i, line in enumerate(post) if line == "TAC A")
            sir_after = [line for line in post[tac_idx + 1:] if line.startswith("S ")]
            assert sir_after == [], f"SIR kept running after TAC: {sir_after}"
        finally:
            await close_conn(w)

    async def test_sir_restart(self, rig):
        """Sending SIR a second time should resume streaming."""
        _, _, _, sim, host, port = rig
        sim.snap_to(5.0)
        sim.advance_to_stable()
        r, w = await open_conn(host, port)
        try:
            # First run
            w.write(b"SIR\r\n")
            await w.drain()
            await collect_for(r, 0.15)

            # Stop with Z
            w.write(b"Z\r\n")
            await w.drain()
            await collect_for(r, 0.15)     # drain Z A + any overlap

            # Second run
            w.write(b"SIR\r\n")
            await w.drain()
            second = await collect_for(r, 0.2)

            assert len(second) >= 1, "SIR did not restart"
        finally:
            await close_conn(w)


# ---------------------------------------------------------------------------
# Multiple concurrent clients
# ---------------------------------------------------------------------------

class TestMultipleClients:
    async def test_two_clients_same_weight(self, rig):
        _, _, _, sim, host, port = rig
        sim.snap_to(10.0)
        sim.advance_to_stable()
        r1, w1 = await open_conn(host, port)
        r2, w2 = await open_conn(host, port)
        try:
            assert "10.00" in await send_recv(r1, w1, "SI")
            assert "10.00" in await send_recv(r2, w2, "SI")
        finally:
            await close_conn(w1)
            await close_conn(w2)

    async def test_tare_shared_across_clients(self, rig):
        """Scale state is shared — tare by client A is visible to client B."""
        _, _, _, sim, host, port = rig
        sim.snap_to(5.0)
        sim.advance_to_stable()
        r1, w1 = await open_conn(host, port)
        r2, w2 = await open_conn(host, port)
        try:
            await send_recv(r1, w1, "T")
            assert "0.00" in await send_recv(r2, w2, "SI")
        finally:
            await close_conn(w1)
            await close_conn(w2)

    async def test_sir_independent_per_client(self, rig):
        """SIR on client A must not corrupt client B's request/response cycle."""
        _, _, _, sim, host, port = rig
        sim.snap_to(8.0)
        sim.advance_to_stable()
        r1, w1 = await open_conn(host, port)
        r2, w2 = await open_conn(host, port)
        try:
            w1.write(b"SIR\r\n")
            await w1.drain()
            # While A is streaming, B should get a clean SI response
            resp2 = await send_recv(r2, w2, "SI")
            assert "8.00" in resp2
        finally:
            await close_conn(w1)
            await close_conn(w2)


# ---------------------------------------------------------------------------
# Simulator integration
# ---------------------------------------------------------------------------

class TestSimulatorIntegration:
    async def test_simulator_ticks_while_serving(self, rig):
        _, _, _, sim, host, port = rig
        before = sim.elapsed
        await asyncio.sleep(0.2)
        assert sim.elapsed > before, "Simulator did not tick"

    async def test_weight_evolves_toward_target(self, rig):
        _, _, _, sim, host, port = rig
        sim.snap_to(0.0)
        sim.set_target(100.0)
        await asyncio.sleep(0.3)
        assert sim.current > 10.0, f"Simulator barely moved: {sim.current}"

    async def test_settled_weight_readable_via_tcp(self, rig):
        _, _, _, sim, host, port = rig
        sim.set_target(30.0)
        sim.advance_to_stable()
        r, w = await open_conn(host, port)
        try:
            assert "30.00" in await send_recv(r, w, "SI")
        finally:
            await close_conn(w)