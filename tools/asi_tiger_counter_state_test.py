#!/usr/bin/env python3
"""
asi_tiger_counter_state_test.py
==================================
Maps PLCCard.read_cell_state() (CCA F?) against a KNOWN, exact pulse
count on the pulse-pass-through counter -- confirmed from ASI's own
tiger_programmable_logic_card docs that a one-shot's "state" is its
internal countdown value ("the current clock counter value... counter
decreases with each clock"), separate from its binary output
(read_cell_outputs()/RDADC Z?). This is a REAL, useful mechanism if it
behaves as documented -- but the exact starting value, direction, and
value at exhaustion for THIS specific configuration
(configure_pulse_pass_through_counter_single()'s cell wiring) haven't
been empirically confirmed before this script.

Pure software test, same methodology as asi_tiger_pulse_counter_test.py
-- a manually-toggled PLC cell simulates the incoming pulse, no real Z/
camera/ETL hardware involved, nothing physical moves. Fires pulses ONE
AT A TIME up to n_pulses (plus a few extra), reading BOTH the count
cell's state and its binary output after each one, so the exact
relationship is read off real hardware rather than assumed from a
theoretical derivation.

ALSO checks whether polling read_cell_state() repeatedly BETWEEN
pulses gives stable, consistent readings -- the same category of
concern that made WHERE unreliable during active Z triggering. If
read_cell_state() is ALSO unreliable under similar conditions, that
would matter a lot for using it to track progress during the real,
autonomous acquisition loop.

SAFETY: pure PLC logic, nothing physical moves. Safe to run repeatedly.

Usage:
    python tools/asi_tiger_counter_state_test.py --port COM4 --n-pulses 5
"""

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import TigerController, PLCCard, cell_addr


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--plc-card-addr", type=int, default=36)
    parser.add_argument("--plc-axis", default="E")
    parser.add_argument("--pulse-source-cell", type=int, default=10,
                         help="Manual 'incoming pulse' source cell -- avoids the counter's own "
                              "cells 1-5")
    parser.add_argument("--pulse-out-bnc", type=int, default=8)
    parser.add_argument("--n-pulses", type=int, default=5)
    parser.add_argument("--extra-pulses", type=int, default=3,
                         help="How many pulses past n_pulses to also observe")
    parser.add_argument("--repeat-poll-count", type=int, default=5,
                         help="How many times to re-read state between pulses, to check "
                              "for jitter/instability in the readback itself")
    args = parser.parse_args()

    count_cell = 2  # 2nd of 5 cells in configure_pulse_pass_through_counter_single's default allocation
    and_cell = 5    # 5th (last) cell -- the gated output

    print(f"Mapping counter state (CCA F?) against a known pulse count (n_pulses={args.n_pulses}).")
    print("Pure software test -- nothing physical moves.")

    tiger = TigerController(args.port, baudrate=args.baudrate)
    plc = None
    try:
        tiger.connect()
        plc = PLCCard(tiger, card_addr=args.plc_card_addr, axis=args.plc_axis)

        src = args.pulse_source_cell
        plc.configure_cell(src, "d_flop", inputs={"a": 0, "b": 0, "c": 0})
        plc.set_cell_state(src, False)

        plc.configure_pulse_pass_through_counter_single(
            pulse_in_addr=cell_addr(src),
            pulse_out_bnc=args.pulse_out_bnc,
            n_pulses=args.n_pulses,
        )
        plc.reset_pulse_pass_through_counter()

        print(f"\nState immediately after reset (before any pulses): "
              f"{plc.read_cell_state(count_cell)}")

        print(f"\n--- Checking read stability: reading state {args.repeat_poll_count} times "
              f"in a row with nothing happening in between ---")
        readings = [plc.read_cell_state(count_cell) for _ in range(args.repeat_poll_count)]
        print(f"  Readings: {readings}")
        if len(set(readings)) == 1:
            print("  STABLE: identical every time -- no jitter observed at rest.")
        else:
            print("  UNSTABLE: readings varied with nothing happening -- worth understanding "
                  "why before trusting this for progress tracking.")

        print(f"\n--- Firing pulses one at a time, reading state + output after each ---")
        n_total = args.n_pulses + args.extra_pulses
        for i in range(1, n_total + 1):
            plc.set_cell_state(src, True)
            state = plc.read_cell_state(count_cell)
            bitmask = plc.read_cell_outputs()
            passed = bool(bitmask & (1 << (and_cell - 1)))
            plc.set_cell_state(src, False)
            expected_pass = i <= args.n_pulses
            marker = "" if passed == expected_pass else "  **UNEXPECTED**"
            print(f"  Pulse {i:2d}: state={state:>8.1f}  output={'PASS' if passed else 'BLOCKED':8s}"
                  f"  (expected {'PASS' if expected_pass else 'BLOCKED'}){marker}")

        print(f"\n--- Checking read stability again, now that the counter is blocked ---")
        readings = [plc.read_cell_state(count_cell) for _ in range(args.repeat_poll_count)]
        print(f"  Readings: {readings}")
        if len(set(readings)) == 1:
            print("  STABLE: identical every time.")
        else:
            print("  UNSTABLE: readings varied at rest -- worth understanding why.")

        print(f"\n--- Reset test: confirming state returns to its starting value ---")
        plc.reset_pulse_pass_through_counter()
        print(f"  State after reset: {plc.read_cell_state(count_cell)}")

        print(f"\n=== Summary ===")
        print("Review the pulse-by-pulse table above: does state count DOWN toward some fixed "
              "value (e.g. 0) as pulses arrive, reaching that value exactly when the output "
              "switches from PASS to BLOCKED? That relationship (starting value, direction, "
              "value-at-block) is what a real completion-tracking method should be built on -- "
              "read it off this table rather than assume it.")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if plc is not None:
            try:
                plc.safe_all_outputs()
            except Exception as exc:
                print(f"WARNING: could not safe PLC outputs: {exc}")
        if tiger.is_connected:
            tiger.disconnect()
            print("Disconnected.")


if __name__ == "__main__":
    main()
