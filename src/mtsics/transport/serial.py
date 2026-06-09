"""
Serial (RS-232) transport for the MT-SICS emulator.

Opens a real or virtual serial port and exchanges MT-SICS commands with a
connected client over RS-232 or USB-serial.

Because pyserial is synchronous, serial I/O runs in a background thread.
Incoming bytes are line-buffered and forwarded to the asyncio event loop
via a thread-safe Queue. Outgoing bytes are written synchronously from the
asyncio thread (serial.write is fast — it hands bytes to the OS buffer).

Virtual port setup
------------------
Linux / macOS (socat)::

    socat PTY,link=/tmp/scale-emu,rawer PTY,link=/tmp/scale-client,rawer
    mtsics-serial --port /tmp/scale-emu

Windows (com0com or HHD Virtual Serial Port)::

    mtsics-serial --port COM3

Requires ``pyserial``::

    pip install "mt-sics-emulator[serial]"
    # or:  pip install pyserial
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from mtsics.core.simulator import WeightSimulator
from mtsics.protocol.engine import MTSICSEngine

logger = logging.getLogger(__name__)


@dataclass
class SerialConfig:
    """
    Serial port settings.  Defaults (9600 8N1) match the factory settings

    port
        Device path (``/dev/ttyUSB0``, ``/tmp/scale-emu``) or Windows port
        name (``COM3``).
    baudrate
        9600 is the default for most MT-SICS scales; the indicator menu supports
        1200 – 115200.
    sim_tick_rate
        Hz at which the WeightSimulator is ticked while serving.
    sir_rate
        Hz at which weight readings are sent during SIR continuous mode.
    """
    port: str = "/dev/ttyUSB0"
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"          # N=None, E=Even, O=Odd
    stopbits: float = 1.0
    read_timeout: float = 0.1  # seconds; keep small for responsiveness
    sim_tick_rate: float = 20.0
    sir_rate: float = 5.0


class SerialTransport:
    """
    Serial transport for the MT-SICS emulator.

    Lifecycle::

        transport = SerialTransport(engine, simulator=sim,
                                    cfg=SerialConfig(port="/tmp/scale-emu"))
        await transport.open()
        print(f"Listening on {transport.port}")
        await transport.serve_forever()   # blocks until port closes or cancelled

    The serial port can also be driven by a test by injecting lines directly
    into ``_rx_queue`` and reading captured writes from a mock serial object.
    """

    def __init__(
        self,
        engine: MTSICSEngine,
        simulator: Optional[WeightSimulator] = None,
        cfg: Optional[SerialConfig] = None,
    ) -> None:
        try:
            import serial as _serial  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "pyserial is required for SerialTransport.\n"
                "Install it with:  pip install pyserial\n"
                "or:               pip install 'mt-sics-emulator[serial]'"
            ) from exc

        self._engine = engine
        self._simulator = simulator
        self._cfg = cfg or SerialConfig()

        self._serial = None          # serial.Serial instance
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._rx_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        self._read_thread: Optional[threading.Thread] = None
        self._sir_task: Optional[asyncio.Task] = None
        self._tick_task: Optional[asyncio.Task] = None
        self._running = False

    @property
    def port(self) -> Optional[str]:
        """The configured port path, or None if not yet opened."""
        return self._cfg.port if self._serial is not None else None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def open(self) -> str:
        """
        Open the serial port and start the background reader thread.
        Returns the port path.
        Call ``serve_forever()`` to begin processing commands.
        """
        import serial as _serial

        self._loop = asyncio.get_event_loop()
        self._serial = _serial.Serial(
            port=self._cfg.port,
            baudrate=self._cfg.baudrate,
            bytesize=self._cfg.bytesize,
            parity=self._cfg.parity,
            stopbits=self._cfg.stopbits,
            timeout=self._cfg.read_timeout,
        )
        self._running = True

        self._read_thread = threading.Thread(
            target=self._reader_thread,
            name="mtsics-serial-rx",
            daemon=True,
        )
        self._read_thread.start()

        if self._simulator is not None:
            self._tick_task = asyncio.create_task(
                self._tick_simulator(),
                name="mtsics-sim-tick",
            )

        logger.info(
            "Serial port open: %s @ %d %d%s%s",
            self._cfg.port,
            self._cfg.baudrate,
            self._cfg.bytesize,
            self._cfg.parity,
            self._cfg.stopbits,
        )
        return self._cfg.port

    async def serve_forever(self) -> None:
        """
        Dispatch incoming commands until cancelled or the port disconnects.
        Call ``open()`` first.
        """
        if self._serial is None:
            raise RuntimeError("Call open() before serve_forever()")
        try:
            while self._running:
                line = await self._rx_queue.get()
                if line is None:
                    # Reader thread posted sentinel — port gone
                    logger.info("Serial port disconnected")
                    break
                await self._handle_line(line)
        except asyncio.CancelledError:
            pass
        finally:
            await self.close()

    async def close(self) -> None:
        """Stop the simulator tick, cancel SIR, and close the serial port."""
        if not self._running and self._serial is None:
            return
        self._running = False

        await self._stop_sir()

        if self._tick_task is not None and not self._tick_task.done():
            self._tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tick_task
            self._tick_task = None

        # Wake serve_forever if it is blocked on _rx_queue.get()
        with contextlib.suppress(Exception):
            self._rx_queue.put_nowait(None)

        if self._serial is not None:
            with contextlib.suppress(Exception):
                self._serial.close()
            self._serial = None

        if self._read_thread is not None and self._read_thread.is_alive():
            self._read_thread.join(timeout=2.0)
            self._read_thread = None

    # ------------------------------------------------------------------ #
    # Command handling                                                     #
    # ------------------------------------------------------------------ #

    async def _handle_line(self, line: str) -> None:
        tokens = line.strip().upper().split()
        cmd = tokens[0] if tokens else ""

        if self._sir_task is not None and cmd not in ("SIR", "SNR"):
            await self._stop_sir()

        response = self._engine.handle(line)
        self._write_bytes(response.encode("ascii"))

        if cmd in ("SIR", "SNR"):
            await self._start_sir()

    # ------------------------------------------------------------------ #
    # SIR continuous output                                                #
    # ------------------------------------------------------------------ #

    async def _start_sir(self) -> None:
        if self._sir_task is not None:
            return
        self._sir_task = asyncio.create_task(self._sir_loop())

    async def _stop_sir(self) -> None:
        if self._sir_task is None:
            return
        task, self._sir_task = self._sir_task, None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _sir_loop(self) -> None:
        interval = 1.0 / self._cfg.sir_rate
        try:
            while True:
                await asyncio.sleep(interval)
                response = self._engine.handle("S")
                self._write_bytes(response.encode("ascii"))
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ #
    # Serial I/O helpers                                                   #
    # ------------------------------------------------------------------ #

    def _write_bytes(self, data: bytes) -> None:
        """Write bytes to the serial port (called from asyncio thread)."""
        try:
            if self._serial is not None and self._serial.is_open:
                self._serial.write(data)
        except Exception as exc:
            logger.warning("Serial write error: %s", exc)

    def _reader_thread(self) -> None:
        """
        Background thread: read bytes from the serial port, assemble CR/LF-
        terminated lines, and hand them to the asyncio event loop.
        """
        buf = b""
        while self._running:
            try:
                chunk = self._serial.read(256)
            except Exception:
                break

            if chunk:
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    text = raw.strip(b"\r").decode("ascii", errors="replace").strip()
                    if text:
                        asyncio.run_coroutine_threadsafe(
                            self._rx_queue.put(text), self._loop
                        )

        # Sentinel: tell serve_forever the port is gone
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(
                self._rx_queue.put(None), self._loop
            )

    # ------------------------------------------------------------------ #
    # Simulator tick                                                       #
    # ------------------------------------------------------------------ #

    async def _tick_simulator(self) -> None:
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

async def _async_main(port: str, baudrate: int, profile: str) -> None:
    from mtsics.core.simulator import SimConfig
    from mtsics.core.state import ScaleState
    from mtsics.profiles import load

    scale_cfg = load(profile)
    state = ScaleState(config=scale_cfg)
    engine = MTSICSEngine(state)
    sim = WeightSimulator(state, cfg=SimConfig())
    cfg = SerialConfig(port=port, baudrate=baudrate)
    transport = SerialTransport(engine, simulator=sim, cfg=cfg)

    opened = await transport.open()
    print(f"MT-SICS emulator — serial port {opened} @ {baudrate} baud")
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

    p = argparse.ArgumentParser(description="MT-SICS scale emulator — serial transport")
    p.add_argument("--port", default="/dev/ttyUSB0",
                   help="Serial port path (default: /dev/ttyUSB0)")
    p.add_argument("--baudrate", type=int, default=9600,
                   help="Baud rate (default: 9600)")
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

    asyncio.run(_async_main(args.port, args.baudrate, args.profile))


if __name__ == "__main__":
    main()