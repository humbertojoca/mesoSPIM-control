#!/usr/bin/env python3
"""
asi_tiger_laser_intensity_test.py
====================================
Bench test for laser intensity control via ASITigerDAC.set_voltage()
on card 35 (ASI numbering: card slot 5), axes P/Q/R/S -- one axis per
laser line. No new code needed for this mechanism (set_voltage() is
already thoroughly validated elsewhere -- ETL, galvo, the L/R switch),
but this specific card has never been touched before. Sets a small
sweep of voltage levels on each axis in turn, with pauses for a scope
check, so you can confirm: the card responds correctly to M <axis>=
commands, each axis is independent, and -- since P/Q/R/S's physical-
to-electrical mapping hasn't been independently confirmed the way the
ETL/galvo cards' axis layout was (via the N command, early in this
project) -- which physical output actually corresponds to which axis
letter.

REAL-HARDWARE FINDING, with real Oxxius L4Cc lasers (638/561/488/405):
an earlier default here (--range-code 1, --max-volts 4.0) only ever
reached ~70mW of these lasers' rated 100mW, confirmed from the real
Oxxius L4Cc/L6Cc user manual -- their analog modulation input is
LINEAR 0V=0% to 5V=100%, so a 4.096V ceiling can never reach 100%
regardless of what's requested. Defaults changed below to range_code 2
(0-10.24V) and --max-volts 5.5 (just above the real 100% point, to
confirm power plateaus there) -- but --range-code here is only ever a
CLIENT-SIDE safety-limit label, NOT a real hardware range change (the
real range needs the 'PR' command plus a controller power-cycle/reset
-- see tools/asi_tiger_laser_dac_range_setup.py, which you should run
ONCE against your real rack BEFORE trusting these new defaults to mean
what they say). This script does not know or check whether that's
been done; if it hasn't, set_voltage() will raise a clear ValueError
when this sweep tries to exceed whatever the hardware's REAL,
unconfirmed range actually is, rather than silently underpowering.

Put a scope on whichever laser intensity output(s) you want to check
before running this -- default tests all 4 axes (P,Q,R,S), pass
--n-lasers to test fewer if you only have some wired right now.

SAFETY: only sets DAC output voltages -- no stage motion, no camera,
no ETL/galvo involvement. If real laser driver hardware IS connected,
these ARE real intensity commands, not just scope traces.

Usage:
    python tools/asi_tiger_laser_intensity_test.py --port COM4
    python tools/asi_tiger_laser_intensity_test.py --port COM4 --n-lasers 1 --max-volts 2.0
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import TigerController, ASITigerDAC


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--card-addr", type=int, default=35)
    parser.add_argument("--axes", nargs="+", default=["P", "Q", "R", "S"])
    parser.add_argument("--n-lasers", type=int, default=None,
                         help="Only test the first N of --axes (default: test all of --axes)")
    parser.add_argument("--range-code", type=int, default=2,
                         help="DAC range code -- default 2 (0-10.24V). CHANGED from an earlier "
                              "default of 1 (0-4.096V) after a real-hardware finding: Oxxius "
                              "L4Cc/L6Cc lasers need up to 5V for 100%% power (confirmed from "
                              "their own manual), which range_code=1 can never reach. This is "
                              "only a CLIENT-SIDE label (see set_voltage()'s safety-limit check) "
                              "-- it does NOT itself change the real hardware range. Run "
                              "asi_tiger_laser_dac_range_setup.py ONCE first if you haven't "
                              "already confirmed this card's real range matches.")
    parser.add_argument("--max-volts", type=float, default=5.5,
                         help="Top of the test sweep -- default 5.5V, just above the Oxxius "
                              "L4Cc/L6Cc's documented 5V=100%% point (so you can confirm power "
                              "plateaus there), safely under range_code=2's 10.24V ceiling.")
    parser.add_argument("--n-levels", type=int, default=4,
                         help="How many evenly-spaced voltage levels to test per axis, from 0 "
                              "to --max-volts")
    parser.add_argument("--hold-s", type=float, default=1.5,
                         help="How long to hold each level -- time to look at the scope (and "
                              "watch/listen for real laser response, if connected)")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    axes = args.axes[:args.n_lasers] if args.n_lasers else args.axes
    levels = [args.max_volts * i / (args.n_levels - 1) for i in range(args.n_levels)] if args.n_levels > 1 else [args.max_volts]

    print(f"Laser intensity test: card {args.card_addr}, axes {axes}, "
          f"{args.n_levels} levels each (0 to {args.max_volts}V), {args.hold_s:.1f}s hold.")
    print("Put a scope on these laser intensity output(s) before continuing. If real laser "
          "driver hardware is connected, these ARE real intensity commands, not just scope traces.")

    if not args.yes:
        resp = input("\nReady? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    dac = None
    try:
        tiger.connect()
        dac = ASITigerDAC(tiger=tiger)
        for axis in axes:
            dac.add_channel(f"laser_{axis}", card_addr=args.card_addr, axis=axis,
                             range_code=args.range_code)

        for axis in axes:
            name = f"laser_{axis}"
            print(f"\n=== Axis {axis} ===")
            for v in levels:
                print(f"  Setting {v:.3f}V...")
                dac.set_voltage(name, v)
                time.sleep(args.hold_s)
            print("  Returning to 0V...")
            dac.set_voltage(name, 0.0)
            time.sleep(args.hold_s)

        resp = input(f"\nDid each axis ({axes}) correctly show the voltage sweep on its own "
                      f"output -- with the RIGHT axis letter corresponding to the physical "
                      f"connector you expected (and did real laser hardware respond correctly, "
                      f"if connected)? [y/N] ").strip().lower()
        if resp.startswith("y"):
            print(f"PASS: laser intensity control confirmed working on card {args.card_addr}.")
        else:
            print("Not confirmed -- describe what you saw (wrong axis responding, no output, "
                  "unexpected voltage, etc.) for next steps. If the axis letters didn't match "
                  "the physical connectors you expected, that's a real, useful finding -- note "
                  "which axis letter actually drove which physical output.")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if dac is not None:
            for axis in axes:
                try:
                    dac.set_voltage(f"laser_{axis}", 0.0)
                except Exception:
                    pass
        if tiger.is_connected:
            tiger.disconnect()
            print("Disconnected.")


if __name__ == "__main__":
    main()
