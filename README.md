# MT-SICS-Emulator

[![PyPI version](https://img.shields.io/pypi/v/mt-sics-emulator)](https://pypi.org/project/mt-sics-emulator/)
[![Python 3.11+](https://img.shields.io/pypi/pyversions/mt-sics-emulator)](https://pypi.org/project/mt-sics-emulator/)

The first open-source server-side implementation of the **MT-SICS** (Mettler Toledo Standard Interface Command Set) protocol. Emulates major industrial bench scales over TCP and RS-232 so client applications (WMS integrations, label printers, custom scripts, etc) can be developed and tested without physical hardware.

---

## Why this exists

MT-SICS is the dominant protocol for industrial scales. Dozens of client libraries exist for it in every language. As far as I could find, no one had written a server, something that actually responds as a scale, until this project. The gap meant developers either borrowed bench time on a physical unit or built one-off stubs from scratch.

---

## Features

- **MT-SICS Level 0 + 1** — `S`, `SI`, `SIR`, `Z`, `ZI`, `T`, `TI`, `TAR`, `TAC`, `@`, and `I0`–`I4`
- **8 built-in scale profiles** — Ohaus, Mettler Toledo, and Sartorius scales from 0.1 mg analytical balances to 300 kg industrial terminals
- **TCP transport** — raw socket server matching each scale's Ethernet / WiFi mode; multiple concurrent clients supported
- **Serial transport** — RS-232 / USB via `pyserial`; works with real or virtual COM ports
- **Realistic weight physics** — exponential settle curve, Gaussian noise, stability detection
- **Programmatic control** — drive the simulated weight from test code with a single call

---

## Scale profiles

| Profile | Scale | Capacity | Graduation | Segment |
|---|---|---|---|---|
| `ohaus-defender5000` | Ohaus Defender 5000 (TD52P) | 150 kg | 0.05 kg | Warehouse / receiving |
| `ohaus-ranger7000` | Ohaus Ranger 7000 (R71MHD35) | 35 kg | 0.005 kg | Parts counting / bench |
| `ohaus-scout` | Ohaus Scout (SPX4201) | 4200 g | 0.1 g | Portable / general purpose |
| `mt-xs204` | Mettler Toledo XS204 | 220 g | 0.0001 g | Analytical lab |
| `mt-ms3002s` | Mettler Toledo MS3002S | 3100 g | 0.01 g | Precision / formulation |
| `mt-ind570` | Mettler Toledo IND570 | 300 kg | 0.1 kg | Heavy industrial |
| `sartorius-quintix224` | Sartorius Quintix 224-1S | 220 g | 0.0001 g | Pharma / research |
| `sartorius-practum6100` | Sartorius Practum 6100-1S | 6100 g | 0.1 g | Teaching / production |

Default profile when no `--profile` flag is given: `ohaus-defender5000`.

---

## Quick start

```bash
pip install mt-sics-emulator           # TCP only
pip install "mt-sics-emulator[serial]" # + serial support
```

Or from source:

```bash
git clone https://github.com/mdcurt/mt-sics-emulator.git
cd mt-sics-emulator
pip install -e ".[dev]"
```

### TCP server

```bash
mtsics-tcp --port 8000
mtsics-tcp --port 8000 --profile mt-xs204
mtsics-tcp --list-profiles
```

```
$ nc localhost 8000
SI
SI S       0.00 kg
T
T S       0.00 kg
SIR
S S       0.00 kg
S S       0.00 kg
...
```

### Serial port

Linux / macOS (virtual port pair via socat):

```bash
socat PTY,link=/tmp/scale-emu,rawer PTY,link=/tmp/scale-client,rawer &
mtsics-serial --port /tmp/scale-emu --profile ohaus-ranger7000
```

Windows (com0com or HHD Virtual Serial Port):

```bash
mtsics-serial --port COM3 --profile sartorius-quintix224
```

### Interactive REPL

```bash
mtsics                        # default profile
mtsics --profile mt-xs204    # analytical balance
```

```
WEIGHT 0.1234        → sets platform to 0.1234 g (stable)
SI                   → SI S      0.1234 g
T                    → T S      0.1234 g
WEIGHT 0.2500        → add more load
SI                   → SI S      0.1266 g  (net)
PROFILES             → list all profiles
CONFIG               → show current scale config
```

### Python API

```python
import asyncio
from mtsics.core.state import ScaleState
from mtsics.core.simulator import WeightSimulator, SimConfig
from mtsics.protocol.engine import MTSICSEngine
from mtsics.transport.tcp import TCPTransport, TCPConfig
from mtsics.profiles import load

async def main():
    state = ScaleState(config=load("mt-xs204"))
    engine = MTSICSEngine(state)
    sim = WeightSimulator(state)

    transport = TCPTransport(engine, simulator=sim)
    host, port = await transport.start()
    print(f"Emulator on {host}:{port}")

    # Drive the scale from your tests:
    sim.set_target(0.1234)      # place 0.1234 g on the platform
    sim.advance_to_stable()     # skip the settle curve

    await transport.serve_forever()

asyncio.run(main())
```

---

---
 
## HTTP control API
 
The control API is a second, optional HTTP port that exposes the simulator: the "physical world" interface for tests, scripts, and GUIs.
 
```bash
mtsics-tcp --port 8000 --control-port 8001
```
 
| Endpoint | Description |
|---|---|
| `GET /state` | Full scale state: gross, net, tare, stability, ranges, target, profile info |
| `POST /weight` | `{"target": 12.5}` settles naturally; `{"value": 12.5, "snap": true}` jumps immediately |
| `POST /reset` | Clears tare and zero offset, returns platform to 0 |
 
```bash
# Place 12.5 kg on the platform (settles over ~2 s)
curl -X POST localhost:8001/weight -d '{"target": 12.5}'
 
# Jump straight to 5 kg, no settling
curl -X POST localhost:8001/weight -d '{"value": 5.0, "snap": true}'
 
# Read everything
curl localhost:8001/state
```
 
The control API binds to loopback (`127.0.0.1`) only and is disabled unless `--control-port` is given. It has zero dependencies — implemented directly on asyncio.
 
A typical integration test: start the emulator, `POST /weight` to simulate a parcel landing on the scale, then assert your application under test reads the correct weight through its normal MT-SICS connection.
 
---

## Supported MT-SICS commands

| Command | Description | Response |
|---|---|---|
| `S` | Send stable weight | `S S <weight> <unit>` |
| `SI` | Send immediately | `SI S/D <weight> <unit>` |
| `SIR` / `SNR` | Start continuous output | stream of `S S/D ...` lines |
| `Z` | Zero (within ±2 % capacity) | `Z A` or `Z I` / `Z +` / `Z -` |
| `ZI` | Zero immediately | `ZI A` or `ZI I` |
| `T` | Tare (stable) | `T S <tare> <unit>` or `T I` |
| `TI` | Tare immediately | `TI S/D <tare> <unit>` |
| `TAR <value>` | Set explicit tare | `TAR A` or `TAR I` |
| `TAC` | Clear tare | `TAC A` |
| `@` | Full reset | `I4 A "<serial>"` |
| `I0` | Scale data summary | `I0 A "..." ...` |
| `I1` | Model name | `I1 A "<model>"` |
| `I2` | Serial number | `I2 A "<serial>"` |
| `I3` | Software version | `I3 A "<version>"` |
| `I4` | SW ID | `I4 A "<serial>"` |

Unknown commands return `ES`. Condition failures return `<CMD> I`.

---

## Adding a custom profile

```python
from mtsics.core.state import ScaleConfig
from mtsics.core.state import ScaleState
from mtsics.protocol.engine import MTSICSEngine

my_scale = ScaleConfig(
    capacity=500.0,
    graduation=0.2,
    unit="kg",
    model="MyScale 500",
    serial_number="MS0000001",
    sw_version="1.0",
)

state = ScaleState(config=my_scale)
engine = MTSICSEngine(state)
```

To contribute a profile, open a PR adding an entry to `src/mtsics/profiles.py` with a source citation for the capacity and graduation values.

---

## Connecting BarTender

**Important:** BarTender's built-in "Ohaus" driver does not speak MT-SICS and will not work with current Defender 5000 firmware or this emulator. Use the **Mettler Toledo MT-SICS Level 1** driver instead — it works with any MT-SICS compatible scale regardless of manufacturer.

In BarTender's Scale/Device settings, select manufacturer → **Mettler Toledo**, protocol → **MT-SICS Level 1**, then enter the host/port for TCP or the COM port for serial.

---

## Development

```bash
git clone https://github.com/mdcurt/mt-sics-emulator.git
cd mt-sics-emulator
pip install -e ".[dev]"
pytest tests/ -v
```

### Project structure

```
src/mtsics/
├── profiles.py          # Named scale configs (Ohaus, MT, Sartorius)
├── core/
│   ├── state.py         # ScaleState — weight, tare, zero, stability
│   └── simulator.py     # WeightSimulator — settle curve, noise, stability detection
├── protocol/
│   └── engine.py        # MTSICSEngine — pure text-in / text-out, no I/O
├── transport/
│   ├── tcp.py           # TCPTransport — asyncio server, multi-client, SIR streaming
│   └── serial.py        # SerialTransport — pyserial thread bridge
│   └── control.py       # ControlAPI — HTTP control plane (dependency-free)
└── cli.py               # Interactive REPL

tests/
├── test_profiles.py     # 120 profile + engine integration tests
├── test_engine.py       # MT-SICS protocol command tests
├── test_simulator.py    # Physics / stability tests
├── test_tcp.py          # Network integration tests
└── test_serial.py       # Serial transport tests (mock-based)
└── test_control.py      # HTTP control API tests
```

---

## Contributing

Issues and pull requests are welcome.

**Good first issues** — areas where contributions would have immediate impact:

- Additional scale profiles (open a PR with a source citation for capacity and graduation values)
- MT-SICS Level 2 commands (`C`, `CA`, `CDP`, `D`, `DW`, `K`, `SA`, `SR`)
- Checkweighing mode — above/within/below threshold bands
- Parts counting mode

**Long term goals** - areas that will require far more work but will greatly enhance the value of this tool:

- Emulating scale model specific functionality
- Scale face GUI — a visual representation of a scale display that connects to the emulator over TCP and shows live weight, stability indicator, and unit; useful as a demo and as a manual testing tool, ant framework (Tkinter, PyQt, web-based) welcome
- Platform simulator GUI — a control panel for setting the target weight and watching the settle curve in real time, as an alternative to driving the simulator programmatically

**To contribute code:**

1. Open an issue first if the change is non-trivial — helps avoid duplicate effort
2. Fork the repo and create a branch from `main`
3. Add tests alongside your code — every new command or feature should have test coverage
4. Run `pytest tests/` before opening a PR
5. Open a pull request with a short description of what the change does and why

**To report a behavior difference from a real scale:**

Include the exact command you sent, the response you received from the emulator, and the response from the real unit. That is the most useful possible bug report and will get a fast response.

## Attribution and disclaimer

This project is an independent implementation of a publicly documented communication protocol and is not affiliated with, endorsed by, or sponsored by any scale manufacturer.

**MT-SICS** (Mettler Toledo Standard Interface Command Set) is a protocol specification published by **Mettler Toledo International Inc.** Referenced here under fair use for interoperability purposes.

**Ohaus Defender 5000**, **Ranger 7000**, and **Scout** are products of **Ohaus Corporation**. **XS204**, **MS3002S**, and **IND570** are products of **Mettler Toledo International Inc.** **Quintix** and **Practum** are products of **Sartorius AG**. Capacity and graduation specifications are taken from the respective manufacturers' published product documentation.

No proprietary firmware, binary code, or trade secrets are used or reproduced in this project.

---

## License

[MIT](LICENSE)
