"""
TCP transport for the MT-SICS emulator.

Exposes the MT-SICS engine over a raw TCP socket, matching how a scale behaves
when configured for Ethernet or WiFi connectivity. Multiple
clients can connect simultaneously; each gets its own SIR streaming state
while sharing the same scale state.

The optional WeightSimulator is ticked in a background task at
sim_tick_rate Hz so the reading evolves continuously even between client
commands.

Quick start::

    import asyncio
    from mtsics.core.state import ScaleState
    from mtsics.core.simulator import WeightSimulator
    from mtsics.protocol.engine import MTSICSEngine
    from mtsics.transport.tcp import TCPTransport, TCPConfig

    async def main():
        state = ScaleState()
        engine = MTSICSEngine(state)
        sim = WeightSimulator(state)
        transport = TCPTransport(engine, simulator=sim)
        await transport.start()
        print(f"Listening on {transport.address}")
        await transport.serve_forever()

    asyncio.run(main())

From the command line::

    python -m mtsics.transport.tcp [--host 0.0.0.0] [--port 8000]

To connect manually and test::

    nc localhost 8000
    SI
    T
    SIR
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from mtsics.core.simulator import WeightSimulator
from mtsics.protocol.engine import MTSICSEngine

logger = logging.getLogger(__name__)


@dataclass
class TCPConfig:
    """
    Configuration for the TCP transport.

    host / port
        Listen address. Use ``port=0`` to let the OS pick a free port
        (handy in tests).

    sim_tick_rate
        How often the WeightSimulator is ticked, in Hz. 20 Hz (50 ms)
        gives smooth settle curves without busy-looping.

    sir_rate
        Continuous-output rate for SIR mode, in Hz. Most MT-SICS compatible
        scales send approximately 5 readings per second in continuous mode.
    """
    host: str = "0.0.0.0"
    port: int = 8000
    sim_tick_rate: float = 20.0   # Hz — simulator update cadence
    sir_rate: float = 5.0         # Hz — SIR streaming cadence


# ---------------------------------------------------------------------------
# Per-client session
# ---------------------------------------------------------------------------

class _ClientSession:
    """
    Manages one TCP client connection.

    Reads CR/LF-terminated MT-SICS command lines, dispatches them through
    the shared engine, and writes responses back. SIR streaming is tracked
    per session; the engine (and therefore the scale state) is shared across
    all sessions.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        engine: MTSICSEngine,
        sir_interval: float,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._engine = engine
        self._sir_interval = sir_interval
        self._sir_task: asyncio.Task | None = None
        # Serialise writes so the SIR loop and the command handler
        # never interleave bytes on the socket.
        self._write_lock = asyncio.Lock()
        self._addr = writer.get_extra_info("peername", default="?")

    async def run(self) -> None:
        """
        Main read-dispatch loop. Returns when the client disconnects or
        sends a half-close (FIN).
        """
        logger.info("Client connected:    %s", self._addr)
        try:
            while True:
                try:
                    raw = await self._reader.readuntil(b"\n")
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    break
                line = raw.decode("ascii", errors="replace").strip()
                if line:
                    await self._handle_line(line)
        finally:
            await self._stop_sir()
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            logger.info("Client disconnected: %s", self._addr)

    # ------------------------------------------------------------------ #
    # Command dispatch                                                     #
    # ------------------------------------------------------------------ #

    async def _handle_line(self, line: str) -> None:
        tokens = line.strip().upper().split()
        cmd = tokens[0] if tokens else ""

        # Any command except SIR/SNR stops the continuous stream.
        if self._sir_task is not None and cmd not in ("SIR", "SNR"):
            await self._stop_sir()

        response = self._engine.handle(line)
        await self._write(response.encode("ascii"))

        if cmd in ("SIR", "SNR"):
            await self._start_sir()

    # ------------------------------------------------------------------ #
    # SIR streaming                                                        #
    # ------------------------------------------------------------------ #

    async def _start_sir(self) -> None:
        """Start the continuous-output task (idempotent)."""
        if self._sir_task is not None:
            return
        self._sir_task = asyncio.create_task(self._sir_loop())

    async def _stop_sir(self) -> None:
        """Cancel the continuous-output task and wait for it to finish."""
        if self._sir_task is None:
            return
        task, self._sir_task = self._sir_task, None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _sir_loop(self) -> None:
        """
        Periodically send a weight reading to this client.
        Uses 'S' format (stable/dynamic prefix) per the MT-SICS specification:
        continuous-output mode.
        """
        try:
            while True:
                await asyncio.sleep(self._sir_interval)
                response = self._engine.handle("S")
                await self._write(response.encode("ascii"))
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ #
    # Write helper                                                         #
    # ------------------------------------------------------------------ #

    async def _write(self, data: bytes) -> None:
        """Write ``data`` to the socket, serialised via the write lock."""
        async with self._write_lock:
            self._writer.write(data)
            await self._writer.drain()


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class TCPTransport:
    """
    Asyncio TCP server that exposes the MT-SICS engine over a raw socket.

    Lifecycle::

        transport = TCPTransport(engine, simulator=sim)

        # Bind and start accepting (non-blocking):
        host, port = await transport.start()

        # Block until cancelled (production use):
        await transport.serve_forever()

        # Or run your own event loop and close manually (tests):
        await transport.close()
    """

    def __init__(
        self,
        engine: MTSICSEngine,
        simulator: Optional[WeightSimulator] = None,
        cfg: Optional[TCPConfig] = None,
    ) -> None:
        self._engine = engine
        self._simulator = simulator
        self._cfg = cfg or TCPConfig()
        self._server: asyncio.Server | None = None
        self._address: tuple[str, int] | None = None
        self._tick_task: asyncio.Task | None = None

    @property
    def address(self) -> tuple[str, int] | None:
        """Bound ``(host, port)`` after ``start()``, ``None`` otherwise."""
        return self._address

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def start(self) -> tuple[str, int]:
        """
        Bind the TCP socket and begin accepting connections.

        Returns the actual ``(host, port)`` that was bound. Connections are
        accepted as soon as the event loop runs; you do not need to call
        ``serve_forever()`` for the server to be usable.
        """
        self._server = await asyncio.start_server(
            self._handle_client,
            self._cfg.host,
            self._cfg.port,
        )
        sock = self._server.sockets[0]
        self._address = sock.getsockname()[:2]

        if self._simulator is not None:
            self._tick_task = asyncio.create_task(
                self._tick_simulator(),
                name="mtsics-sim-tick",
            )

        logger.info("Listening on %s:%d", *self._address)
        return self._address

    async def serve_forever(self) -> None:
        """
        Block until cancelled. Calls ``close()`` automatically on exit.
        Use this in a ``asyncio.run()`` entry point.
        """
        if self._server is None:
            raise RuntimeError("Call start() before serve_forever()")
        try:
            await self._server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            await self.close()

    async def close(self) -> None:
        """
        Stop accepting new connections and cancel the simulator tick task.
        Existing client sessions are allowed to finish their current write
        before their socket is closed.
        """
        if self._tick_task is not None and not self._tick_task.done():
            self._tick_task.cancel()
            await asyncio.gather(self._tick_task, return_exceptions=True)
            self._tick_task = None

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            self._address = None

    # ------------------------------------------------------------------ #
    # Internal callbacks                                                   #
    # ------------------------------------------------------------------ #

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        sir_interval = 1.0 / self._cfg.sir_rate
        session = _ClientSession(reader, writer, self._engine, sir_interval)
        await session.run()

    async def _tick_simulator(self) -> None:
        """
        Tick the simulator at ``sim_tick_rate`` Hz using real elapsed time.

        Tracks actual wall-clock dt so the simulation stays accurate even if
        the event loop is occasionally delayed.
        """
        interval = 1.0 / self._cfg.sim_tick_rate
        last = time.monotonic()
        try:
            while True:
                await asyncio.sleep(interval)
                now = time.monotonic()
                self._simulator.tick(now - last)
                last = now
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def _async_main(host: str, port: int, profile: str) -> None:
    from mtsics.core.simulator import SimConfig
    from mtsics.core.state import ScaleState
    from mtsics.profiles import load

    scale_cfg = load(profile)
    state = ScaleState(config=scale_cfg)
    engine = MTSICSEngine(state)
    sim = WeightSimulator(state, cfg=SimConfig())
    cfg = TCPConfig(host=host, port=port)
    transport = TCPTransport(engine, simulator=sim, cfg=cfg)

    h, p = await transport.start()
    print(f"MT-SICS emulator — listening on {h}:{p}")
    print(f"  Profile:    {profile}")
    print(f"  Model:      {state.config.model}")
    print(f"  S/N:        {state.config.serial_number}")
    print(f"  Capacity:   {state.config.capacity} {state.config.unit}")
    print(f"  Graduation: {state.config.graduation} {state.config.unit}")
    print("Press Ctrl+C to stop.\n")

    try:
        await transport.serve_forever()
    except KeyboardInterrupt:
        pass


def main() -> None:
    import argparse
    from mtsics.profiles import DEFAULT_PROFILE, summary_table

    p = argparse.ArgumentParser(description="MT-SICS scale emulator — TCP transport")
    p.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    p.add_argument("--port", type=int, default=8000, help="TCP port (default: 8000)")
    p.add_argument(
        "--profile", default=DEFAULT_PROFILE,
        help=f"Scale profile (default: {DEFAULT_PROFILE}). "
             f"Use --list-profiles to see all options.",
    )
    p.add_argument(
        "--list-profiles", action="store_true",
        help="Print available scale profiles and exit.",
    )
    args = p.parse_args()

    if args.list_profiles:
        print(summary_table())
        return

    asyncio.run(_async_main(args.host, args.port, args.profile))


if __name__ == "__main__":
    main()