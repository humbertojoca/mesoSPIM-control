#!/usr/bin/env python3
"""
asi_tiger_laser_enable_test.py
=================================
Bench test for row_setup.configure_laser_enable_lines() -- cycles each
configured laser BNC ON/OFF individually, plus disable_all(), so you
can watch the transitions on a scope (and/or confirm real laser
hardware responds, if connected).

Never tested on real hardware before this -- the underlying mechanism
(a manually-toggled cell driving a BNC) is the same one proven
repeatedly elsewhere in this project (camera trigger, kick cells, the
L/R switch), just not on this specific BNC range (5-8) or with
multiple independent lines active at once.

Put a scope on whichever laser BNC(s) you want to check before running
this -- default tests all 4 (BNC5-8), pass --n-lasers to test fewer if
you only have some wired right now.

SAFETY: only touches PLC digital I/O (BNC5-8 by default) -- no stage
motion, no camera, no ETL/galvo involvement. If real laser hardware IS
connected to any of these lines, this WILL turn it on/off -- treat
these transitions as real, not just scope traces, if so.

Usage:
    python tools/asi_tiger_laser_enable_test.py --port COM4
    python tools/asi_tiger_laser_enable_test.py --port COM4 --n-lasers 2 --n-cycles 5
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import TigerController, PLCCard, configure_laser_enable_lines


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--plc-card-addr", type=int, default=36)
    parser.add_argument("--plc-axis", default="E")
    parser.add_argument("--laser-bncs", type=int, nargs="+", default=[5, 6, 7, 8],
                         help="PLC BNC numbers, in laser-index order")
    parser.add_argument("--n-lasers", type=int, default=None,
                         help="Only test the first N of --laser-bncs -- use this if you only "
                              "have some wired right now (default: test all of --laser-bncs)")
    parser.add_argument("--n-cycles", type=int, default=3,
                         help="How many ON/OFF cycles per laser")
    parser.add_argument("--hold-s", type=float, default=1.5,
                         help="How long to hold each state -- time to look at the scope (and "
                              "watch/listen for real laser response, if connected)")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    bncs = args.laser_bncs[:args.n_lasers] if args.n_lasers else args.laser_bncs
    print(f"Laser enable test: {len(bncs)} line(s) on BNC{bncs}, {args.n_cycles} ON/OFF cycles "
          f"each, {args.hold_s:.1f}s hold.")
    print("Put a scope on these BNCs before continuing. If real laser hardware is connected to "
          "any of them, these ARE real on/off transitions, not just scope traces.")

    if not args.yes:
        resp = input("\nReady? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    plc = None
    try:
        tiger.connect()
        plc = PLCCard(tiger, card_addr=args.plc_card_addr, axis=args.plc_axis)
        toggle_cells = list(range(12, 12 + len(bncs)))  # match configure_laser_enable_lines()'s
                                                          # own default convention, sliced to len(bncs)
        lasers = configure_laser_enable_lines(plc, laser_bncs=bncs, toggle_cells=toggle_cells)

        for laser_idx, bnc in enumerate(bncs):
            print(f"\n=== Laser {laser_idx} (BNC{bnc}) ===")
            for i in range(1, args.n_cycles + 1):
                print(f"  Cycle {i}/{args.n_cycles}: ON...")
                lasers.enable(laser_idx)
                time.sleep(args.hold_s)

                print(f"  Cycle {i}/{args.n_cycles}: OFF...")
                lasers.disable(laser_idx)
                time.sleep(args.hold_s)

        if len(bncs) > 1:
            print(f"\n=== Testing independence: enabling ALL lasers together ===")
            for laser_idx in range(len(bncs)):
                lasers.enable(laser_idx)
            print(f"  All {len(bncs)} lines should now be ON simultaneously -- check each BNC.")
            time.sleep(args.hold_s * 2)

            print(f"\n=== disable_all() ===")
            lasers.disable_all()
            print(f"  All lines should now be OFF -- check each BNC.")
            time.sleep(args.hold_s)
        else:
            print(f"\nSkipping independence test (only one laser BNC configured).")
            lasers.disable_all()

        resp = input("\nDid every laser BNC show clean ON/OFF transitions, independently of the "
                      "others, at every cycle (and did real laser hardware respond correctly, if "
                      "connected)? [y/N] ").strip().lower()
        if resp.startswith("y"):
            print("PASS: laser enable lines confirmed working on real hardware.")
        else:
            print("Not confirmed -- describe what you saw (wrong BNC responding, no transition, "
                  "lines not independent, etc.) for next steps.")

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
