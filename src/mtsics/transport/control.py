"""
HTTP control API for the MT-SICS emulator.

A minimal, dependency-free HTTP/1.1 server that exposes the weight simulator
and scale state for programmatic control. This is the emulator's "physical
world" interface — the MT-SICS port speaks only what a real scale speaks,
while this port lets tests, scripts, and GUIs place weights on the platform.

Endpoints
---------
GET  /state
    Current scale state::

        {
          "gross": 12.5, "net": 12.5, "tare": 0.0,
          "stable": true, "overrange": false, "underrange": false,
          "unit": "kg", "target": 12.5,
          "model": "Defender 5000", "serial_number": "B123456789",
          "capacity": 150.0, "graduation": 0.05
        }

POST /weight
    Set the platform weight. Body (JSON)::

        {"target": 12.5}                  # approach via settle curve
        {"value": 12.5, "snap": true}     # jump immediately, no settling

    Responds with the same payload as GET /state.

POST /reset
    Clear tare and zero offset, snap the platform to 0, and stop settling.
    Responds with the same payload as GET /state.

All responses are JSON. Errors return ``{"error": "<message>"}`` with an
appropriate 4xx status code.

Usage::

    control = ControlAPI(state, sim, cfg=ControlConfig(port=8001))
    await control.start()
    ...
    await control.close()

Or from the command line (alongside the MT-SICS TCP port)::

    mtsics-tcp --port 8000 --control-port 8001

Example session::

    curl localhost:8001/state
    curl -X POST localhost:8001/weight -d '{"target": 12.5}'
    curl -X POST localhost:8001/weight -d '{"value": 5.0, "snap": true}'
    curl -X POST localhost:8001/reset
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Optional

from mtsics.core.simulator import WeightSimulator
from mtsics.core.state import ScaleState

logger = logging.getLogger(__name__)

_MAX_BODY = 64 * 1024          # 64 KiB — far larger than any valid request
_MAX_HEADER_LINES = 64


@dataclass
class ControlConfig:
    """Listen address for the control API. Use port=0 for OS-assigned."""
    host: str = "127.0.0.1"    # control plane defaults to loopback only
    port: int = 8001


class ControlAPI:
    """
    Dependency-free HTTP control server for the emulator.

    Wraps a ScaleState and WeightSimulator. Runs on its own port,
    independent of the MT-SICS transports.
    """

    def __init__(
        self,
        state: ScaleState,
        simulator: WeightSimulator,
        cfg: Optional[ControlConfig] = None,
    ) -> None:
        self._state = state
        self._sim = simulator
        self._cfg = cfg or ControlConfig()
        self._server: asyncio.Server | None = None
        self._address: tuple[str, int] | None = None

    @property
    def address(self) -> tuple[str, int] | None:
        """Bound ``(host, port)`` after ``start()``, else None."""
        return self._address

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def start(self) -> tuple[str, int]:
        """Bind and begin serving. Returns the actual (host, port)."""
        self._server = await asyncio.start_server(
            self._handle_connection,
            self._cfg.host,
            self._cfg.port,
        )
        sock = self._server.sockets[0]
        self._address = sock.getsockname()[:2]
        logger.info("Control API listening on %s:%d", *self._address)
        return self._address

    async def close(self) -> None:
        """Stop accepting connections."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            self._address = None

    # ------------------------------------------------------------------ #
    # HTTP plumbing                                                        #
    # ------------------------------------------------------------------ #

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request = await self._read_request(reader)
            if request is None:
                await self._respond(writer, 400, {"error": "malformed request"})
                return
            method, path, body = request
            status, payload = self._route(method, path, body)
            await self._respond(writer, status, payload)
        except Exception as exc:                      # pragma: no cover
            logger.warning("Control API error: %s", exc)
            try:
                await self._respond(writer, 500, {"error": "internal error"})
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _read_request(
        self,
        reader: asyncio.StreamReader,
    ) -> tuple[str, str, bytes] | None:
        """
        Parse one HTTP/1.1 request. Returns (method, path, body) or None
        if the request is malformed.
        """
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        except asyncio.TimeoutError:
            return None
        parts = request_line.decode("ascii", errors="replace").split()
        if len(parts) != 3:
            return None
        method, path, _version = parts

        # Headers — we only care about Content-Length
        content_length = 0
        for _ in range(_MAX_HEADER_LINES):
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode("ascii", errors="replace").partition(":")
            if name.strip().lower() == "content-length":
                try:
                    content_length = int(value.strip())
                except ValueError:
                    return None
        else:
            return None        # too many header lines

        if content_length < 0 or content_length > _MAX_BODY:
            return None

        body = b""
        if content_length:
            body = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=5.0
            )
        return method.upper(), path, body

    async def _respond(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        payload: dict,
    ) -> None:
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found",
                  405: "Method Not Allowed", 500: "Internal Server Error"}
        body = json.dumps(payload).encode()
        head = (
            f"HTTP/1.1 {status} {reason.get(status, 'Unknown')}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode()
        writer.write(head + body)
        await writer.drain()

    # ------------------------------------------------------------------ #
    # Routing                                                              #
    # ------------------------------------------------------------------ #

    def _route(self, method: str, path: str, body: bytes) -> tuple[int, dict]:
        path = path.rstrip("/") or "/"

        if path == "/state":
            if method != "GET":
                return 405, {"error": "use GET for /state"}
            return 200, self._state_payload()

        if path == "/weight":
            if method != "POST":
                return 405, {"error": "use POST for /weight"}
            return self._handle_weight(body)

        if path == "/reset":
            if method != "POST":
                return 405, {"error": "use POST for /reset"}
            return self._handle_reset()

        return 404, {"error": f"unknown path {path!r}",
                     "paths": ["GET /state", "POST /weight", "POST /reset"]}

    # ------------------------------------------------------------------ #
    # Handlers                                                             #
    # ------------------------------------------------------------------ #

    def _state_payload(self) -> dict:
        cfg = self._state.config
        return {
            "gross": self._state.gross,
            "net": self._state.net,
            "tare": self._state.tare,
            "stable": self._state.stable,
            "overrange": self._state.overrange,
            "underrange": self._state.underrange,
            "unit": cfg.unit,
            "target": self._sim.target,
            "model": cfg.model,
            "serial_number": cfg.serial_number,
            "capacity": cfg.capacity,
            "graduation": cfg.graduation,
        }

    def _handle_weight(self, body: bytes) -> tuple[int, dict]:
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return 400, {"error": "body must be valid JSON"}
        if not isinstance(data, dict):
            return 400, {"error": "body must be a JSON object"}

        snap = bool(data.get("snap", False))
        raw = data.get("value", data.get("target"))
        if raw is None:
            return 400, {"error": 'provide "target" (settle) or "value" with "snap": true'}
        try:
            weight = float(raw)
        except (TypeError, ValueError):
            return 400, {"error": f"weight must be a number, got {raw!r}"}
        if not (weight == weight):                    # NaN check
            return 400, {"error": "weight must not be NaN"}
        if weight in (float("inf"), float("-inf")):
            return 400, {"error": "weight must be finite"}

        if snap:
            self._sim.snap_to(weight)
        else:
            self._sim.set_target(weight)
        return 200, self._state_payload()

    def _handle_reset(self) -> tuple[int, dict]:
        self._state.do_reset()
        self._sim.snap_to(0.0)
        return 200, self._state_payload()