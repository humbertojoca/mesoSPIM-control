#!/usr/bin/env python3
"""
asi_tiger_lr_switch_test.py
==============================
Small bench test for the L/R illumination-arm switch
(row_setup.configure_lr_switch()) -- cycles between LEFT and RIGHT
several times with pauses, so you can watch the transitions on a scope
(and/or confirm the physical switch actually moves, if it's connected).

Put a scope on the DAC channel's output (card 34, axis I by default --
same physical connector as any other DAC channel on that card) before
running this.

SAFETY: only touches this one DAC channel (a low-current control
voltage) -- no stage motion, no camera, no ETL/galvo involvement.

Usage:
    python tools/asi_tiger_lr_switch_test.py --port COM4
    python tools/asi_tiger_lr_switch_test.py --port COM4 --left-v 0 --right-v 4.0 --n-cycles 5
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import TigerController, ASITigerDAC, configure_lr_switch


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--card-addr", type=int, default=34)
    parser.add_argument("--axis", default="I")
    parser.add_argument("--left-v", type=float, default=0.0)
    parser.add_argument("--right-v", type=float, default=4.0)
    parser.add_argument("--range-code", type=int, default=1)
    parser.add_argument("--n-cycles", type=int, default=5,
                         help="How many full LEFT->RIGHT->LEFT cycles to run")
    parser.add_argument("--hold-s", type=float, default=2.0,
                         help="How long to hold each position -- give yourself time to look "
                              "at the scope (and watch/listen for physical movement, if the "
                              "switch is connected) before it moves again")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    print(f"L/R switch test: card {args.card_addr}, axis {args.axis}, "
          f"LEFT={args.left_v}V / RIGHT={args.right_v}V, {args.n_cycles} cycles, "
          f"{args.hold_s:.1f}s hold each.")
    print("Put a scope on this DAC channel's output before continuing.")

    if not args.yes:
        resp = input("\nReady? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    try:
        tiger.connect()
        dac = ASITigerDAC(tiger=tiger)
        switch = configure_lr_switch(
            dac, card_addr=args.card_addr, axis=args.axis,
            left_v=args.left_v, right_v=args.right_v, range_code=args.range_code,
        )

        for i in range(1, args.n_cycles + 1):
            print(f"\n--- Cycle {i}/{args.n_cycles} ---")
            print(f"LEFT  ({args.left_v}V)...")
            switch.select_left()
            time.sleep(args.hold_s)

            print(f"RIGHT ({args.right_v}V)...")
            switch.select_right()
            time.sleep(args.hold_s)

        print("\nReturning to LEFT (a defined resting state) before disconnecting...")
        switch.select_left()

        resp = input("\nDid the scope show clean transitions at every cycle, holding steady at "
                      "each level (and did the physical switch move correctly, if connected)? "
                      "[y/N] ").strip().lower()
        if resp.startswith("y"):
            print("PASS: L/R switch confirmed working on real hardware.")
        else:
            print("Not confirmed -- describe what you saw (wrong voltage, no transition, "
                  "switch didn't move, bounced/chattered, etc.) for next steps.")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if tiger.is_connected:
            tiger.disconnect()
            print("Disconnected.")


if __name__ == "__main__":
    main()
