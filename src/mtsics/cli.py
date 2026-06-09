"""
Simple stdin/stdout REPL for exercising the MT-SICS engine interactively.

Usage:
    mtsics [--profile <name>]
    echo "SI" | mtsics --profile mt-xs204

Special REPL commands (not part of MT-SICS):
    WEIGHT <value>           — set platform weight (stable)
    WEIGHT <value> UNSTABLE  — set platform weight (not stable)
    CONFIG                   — print current scale config
    PROFILES                 — list available scale profiles
"""
from __future__ import annotations

import sys

from mtsics.core.state import ScaleState
from mtsics.protocol.engine import MTSICSEngine


def main() -> None:
    import argparse
    from mtsics.profiles import DEFAULT_PROFILE, load, summary_table

    p = argparse.ArgumentParser(description="MT-SICS scale emulator — interactive REPL")
    p.add_argument(
        "--profile", default=DEFAULT_PROFILE,
        help=f"Scale profile (default: {DEFAULT_PROFILE})",
    )
    p.add_argument(
        "--list-profiles", action="store_true",
        help="Print available scale profiles and exit.",
    )
    args = p.parse_args()

    if args.list_profiles:
        print(summary_table())
        return

    state = ScaleState(config=load(args.profile))
    engine = MTSICSEngine(state)

    stderr = sys.stderr
    print(f"MT-SICS emulator — REPL  (profile: {args.profile})", file=stderr)
    print(f"Model: {state.config.model}  S/N: {state.config.serial_number}", file=stderr)
    print(f"Capacity: {state.config.capacity} {state.config.unit}  "
          f"Graduation: {state.config.graduation} {state.config.unit}", file=stderr)
    print("─" * 50, file=stderr)
    print("MT-SICS commands: SI  S  SIR  Z  ZI  T  TI  TAR <n>  TAC  I1  @", file=stderr)
    print("REPL extras:      WEIGHT <n> [UNSTABLE]  CONFIG  PROFILES", file=stderr)
    print("─" * 50, file=stderr)

    try:
        for line in sys.stdin:
            line = line.rstrip("\r\n")
            if not line:
                continue

            upper = line.upper()

            # -- REPL-only: set weight -----------------------------------
            if upper.startswith("WEIGHT"):
                parts = line.split()
                if len(parts) < 2:
                    print("[usage: WEIGHT <value> [UNSTABLE]]", file=stderr)
                    continue
                try:
                    weight = float(parts[1])
                except ValueError:
                    print(f"[bad value: {parts[1]}]", file=stderr)
                    continue
                stable = "UNSTABLE" not in upper
                state.set_weight(weight, stable=stable)
                flag = "stable" if stable else "unstable"
                print(
                    f"[weight → {weight} {state.config.unit}, {flag}  "
                    f"gross={state.gross}  net={state.net}  tare={state.tare}]",
                    file=stderr,
                )
                continue

            # -- REPL-only: list profiles --------------------------------
            if upper == "PROFILES":
                from mtsics.profiles import summary_table
                print(summary_table(), file=stderr)
                continue

            # -- REPL-only: print config ---------------------------------
            if upper == "CONFIG":
                cfg = state.config
                print(
                    f"[model={cfg.model}  s/n={cfg.serial_number}  "
                    f"cap={cfg.capacity} {cfg.unit}  grad={cfg.graduation} {cfg.unit}  "
                    f"dp={cfg.decimal_places}]",
                    file=stderr,
                )
                continue

            # -- MT-SICS command ----------------------------------------
            response = engine.handle(line)
            sys.stdout.write(response)
            sys.stdout.flush()

    except (KeyboardInterrupt, EOFError):
        print("\n[exit]", file=stderr)


if __name__ == "__main__":
    main()