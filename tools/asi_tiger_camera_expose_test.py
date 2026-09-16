#!/usr/bin/env python3
"""
asi_tiger_camera_expose_test.py
==================================
Confirms the PLC reliably detects BOTH edges of the Photometrics Iris
15's real Expose-Out signal (Rolling Shutter mode) before wiring
anything downstream (ETL start on rising edge, Z start on falling edge)
to depend on it. Same reasoning as the earlier OUT0 discovery test:
verify the raw input is correctly seen before trusting logic built on
top of it -- this is the first real external (non-Tiger-generated)
signal in this project, worth its own dedicated check.

Confirmed already (via scope + PVCAMTest): Expose-Out is a genuine TTL
(5V) signal -- directly compatible with a PLC BNC input, no signal
conditioning needed.

Uses the same latching-catcher technique as the OUT0 discovery test
(a D-flop that catches a rising/falling edge and holds it, readable via
a slow serial poll regardless of exact timing), but with TWO separate
catchers -- one for the rising edge (exposure start), one for the
falling edge (exposure end) -- since both matter for the final design.

REQUIRED PHYSICAL WIRING: camera Expose-Out -> a PLC BNC input
(--camera-bnc, default 3 -- chosen to avoid the Z-loop's BNC1/BNC2 from
earlier testing, in case both are wired at once later).

This runs several reset-wait-read cycles while your camera is
acquiring (free-run/live in PVCAMTest, or however you trigger
exposures) -- NOT just once -- to confirm detection is consistent
across multiple exposures, not a one-off fluke.

SAFETY: read-only on the PLC side, doesn't touch the Z stage or any
DAC output. Just make sure the camera is actually exposing during the
wait windows.

Usage:
    python tools/asi_tiger_camera_expose_test.py --port COM4
    python tools/asi_tiger_camera_expose_test.py --port COM4 --camera-bnc 3 --n-cycles 5
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import (
    TigerController, PLCCard,
    bnc_addr, cell_addr, rising_edge, falling_edge, inverted,
    IO_TYPE_INPUT,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--plc-card-addr", type=int, default=36)
    parser.add_argument("--plc-axis", default="E")
    parser.add_argument("--camera-bnc", type=int, default=3,
                         help="PLC BNC jumpered from the camera's Expose-Out signal")
    parser.add_argument("--reset-cell", type=int, default=1)
    parser.add_argument("--rising-catcher-cell", type=int, default=2)
    parser.add_argument("--falling-catcher-cell", type=int, default=3)
    parser.add_argument("--n-cycles", type=int, default=5,
                         help="How many reset-wait-read cycles to run -- confirms consistency, "
                              "not just a one-off catch")
    parser.add_argument("--wait-s", type=float, default=3.0,
                         help="How long to wait per cycle for at least one exposure to occur -- "
                              "make sure the camera is actively acquiring during this window")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    print(f"Confirming the PLC sees BOTH edges of the camera's Expose-Out signal "
          f"(wired to PLC BNC{args.camera_bnc}).")
    print(f"Running {args.n_cycles} cycles, {args.wait_s:.1f}s each -- make sure the camera is "
          f"actively acquiring (live/free-run in PVCAMTest, or however you trigger it) throughout.")

    if not args.yes:
        resp = input("\nConfirm the camera is acquiring and the BNC is wired. Continue? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    plc = None
    try:
        tiger.connect()
        plc = PLCCard(tiger, card_addr=args.plc_card_addr, axis=args.plc_axis)

        cam_addr = bnc_addr(args.camera_bnc)
        print(f"\nForcing BNC{args.camera_bnc} to input (learned the hard way earlier in this "
              f"project -- BNCs default to output)...")
        plc.configure_io(cam_addr, IO_TYPE_INPUT)

        print(f"Configuring reset cell ({args.reset_cell}), rising-edge catcher "
              f"({args.rising_catcher_cell}), falling-edge catcher ({args.falling_catcher_cell})...")
        plc.configure_cell(args.reset_cell, "d_flop", inputs={"a": 0, "b": 0, "c": 0})
        plc.set_cell_state(args.reset_cell, False)
        plc.configure_cell(
            args.rising_catcher_cell, "d_flop",
            inputs={"a": inverted(0), "b": rising_edge(cam_addr), "c": cell_addr(args.reset_cell)},
        )
        plc.configure_cell(
            args.falling_catcher_cell, "d_flop",
            inputs={"a": inverted(0), "b": falling_edge(cam_addr), "c": cell_addr(args.reset_cell)},
        )

        def reset():
            plc.set_cell_state(args.reset_cell, True)
            plc.set_cell_state(args.reset_cell, False)

        rising_bit = 1 << (args.rising_catcher_cell - 1)
        falling_bit = 1 << (args.falling_catcher_cell - 1)

        results = []
        for i in range(1, args.n_cycles + 1):
            reset()
            time.sleep(args.wait_s)
            bitmask = plc.read_cell_outputs()
            rising_caught = bool(bitmask & rising_bit)
            falling_caught = bool(bitmask & falling_bit)
            results.append((rising_caught, falling_caught))
            print(f"  Cycle {i}: rising={'CAUGHT' if rising_caught else 'nothing'}  "
                  f"falling={'CAUGHT' if falling_caught else 'nothing'}")

        plc.safe_all_outputs()

        print(f"\n=== Result ===")
        all_rising = all(r for r, f in results)
        all_falling = all(f for r, f in results)
        if all_rising and all_falling:
            print(f"PASS: both rising and falling edges detected in all {args.n_cycles} cycles. "
                  f"The PLC reliably sees this real external signal -- safe to wire ETL (rising "
                  f"edge) and Z (falling edge) triggers to it next.")
        else:
            missed_rising = sum(1 for r, f in results if not r)
            missed_falling = sum(1 for r, f in results if not f)
            print(f"Inconsistent: missed rising edge in {missed_rising}/{args.n_cycles} cycles, "
                  f"missed falling edge in {missed_falling}/{args.n_cycles} cycles. Worth checking "
                  f"whether the camera was actually acquiring during every wait window before "
                  f"assuming a real detection problem -- but if wiring/timing was solid throughout "
                  f"and this still misses edges, worth understanding why before trusting this signal "
                  f"for real triggering.")

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
