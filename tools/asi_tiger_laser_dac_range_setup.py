#!/usr/bin/env python3
"""
asi_tiger_laser_dac_range_setup.py
=====================================
One-time hardware commissioning script for the laser-intensity DAC
card's output RANGE -- NOT a regular bench test to re-run casually.

REAL-HARDWARE FINDING THAT MOTIVATED THIS (from the user, after
receiving real Oxxius L4Cc lasers -- 638/561/488/405 -- and running
asi_tiger_laser_intensity_test.py): commanding up to ~4V (close to
range_code=1's 4.096V ceiling) only reached ~70mW of the Oxxius
module's rated 100mW. CONFIRMED from Oxxius's own real L4Cc/L6Cc user
manual (fetched directly, not assumed -- see
https://www.oxxius.com/wp-content/uploads/2023/05/UserManual_LnCc_v177aa2.pdf):
"The analog modulation functions allow the user to deliver an output
power proportionally to the input voltage: 0V for a nil power, 5V for
the maximal power" -- linear 0-5V = 0-100%. 4V is 80% of that range,
so ~70% measured output is in the right ballpark (some calibration
slop) but genuinely short of 100%, because the DAC card has never
actually been able to reach 5V.

THE REAL ROOT CAUSE (found by reading asi_tiger/dac.py's own docstrings
closely, NOT a new bug introduced by this script -- a pre-existing gap
this project never closed): `ASITigerDAC.add_channel(range_code=...)`
and every place that's called it so far (including this project's own
asi_tiger_laser_intensity_test.py, and config_asi_tiger_example.py's
laser_dac_channels) ONLY set a CLIENT-SIDE label used for software
safety-limit math -- it does NOT send anything to the hardware. The
actual electrical output range is a CARD-WIDE hardware setting changed
only by the `PR` command (ASITigerDAC.set_range()), and per ASI's own
command:pr docs, quoted directly in set_range()'s docstring:
"Controller reset or restart is needed for setting to take effect."
`set_range()`/`PR` has never been called anywhere in this project
before this script. So the card's REAL, currently-active range has
simply always been whatever it already was (factory default or
whatever was last set outside this project) -- NOT range_code=1's
0-4.096V by deliberate configuration. The ~4.096V ceiling the user hit
is consistent with that real range happening to already be code 1 (or
something close to it) -- this script's first job is to tell you for
certain, via PR <axis>?, rather than continue guessing.

CORRECTION, confirmed on real hardware by the user, after an earlier
version of this script's --set-range alone turned out NOT to be
enough: PR's new range reverted across a real power cycle until the
user manually sent `SS Z` (card-addressed -- "35SS Z" on card 35)
AFTER PR. This is NOT documented in anything this project has managed
to fetch from ASI (several attempts to fetch ASI's own SS/PR doc pages
returned fetch errors -- provenance/rate-limit issues, not a lookup
that came back empty) -- treat this as real-hardware-confirmed only.
`--set-range` below now sends both PR and SS Z.

WHAT THIS SCRIPT DOES, in order:
  1. Queries the REAL, currently-active range code for the laser DAC
     card via `PR <axis>?` (read-only, safe, no hardware change) and
     reports it in plain terms.
  2. With --set-range, sends `PR <axis>=<new-range-code>` (default:
     range_code 2, 0-10.24V -- comfortably covers the Oxxius's 0-5V
     with headroom) for EVERY axis on the card (redundant but
     harmless -- PR is card-wide, confirmed in dac.py's own docstring),
     THEN sends `SS Z` once for the card (ASITigerDAC.save_settings())
     -- the step confirmed necessary above to make the new range
     survive a real power cycle, not just PR alone.
  3. STOPS and tells you to power-cycle or reset the Tiger controller
     -- this script cannot do that itself (there's no documented
     software "reset controller" command this project has confirmed;
     a physical power cycle is the safe, known way). Does NOT
     continue automatically past this point in the same run.
  4. On a SEPARATE run with --verify, re-queries PR <axis>? and
     confirms the new range is genuinely active, then (only if
     confirmed) does a real voltage sweep up to --max-volts (default
     5.5V, comfortably above the Oxxius's 5V=100% point with a little
     headroom to re-confirm linearity near the top) so you can watch
     the real laser power meter and confirm 100% is actually reached
     around 5V, not before or long after.

SAFETY: step 2 changes real hardware state intended to persist across
power cycles (PR + SS Z, confirmed on real hardware -- see above).
Step 4 commands real voltages into whatever is plugged into this DAC
card's outputs -- if real laser
driver hardware is connected, these are real intensity commands.
Default --max-volts (5.5V) is chosen to stay safely inside range_code
2's 10.24V ceiling while slightly exceeding the Oxxius's documented
5V=100% point, not to approach the new range's own limit.

Usage:
    # Step 1: just look, change nothing
    python tools/asi_tiger_laser_dac_range_setup.py --port COM4

    # Step 2: commit the new range (then POWER-CYCLE the Tiger controller)
    python tools/asi_tiger_laser_dac_range_setup.py --port COM4 --set-range

    # Step 3 (after the power cycle): confirm and sweep
    python tools/asi_tiger_laser_dac_range_setup.py --port COM4 --verify
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import TigerController, ASITigerDAC, DAC_RANGE_LIMITS_MV


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--card-addr", type=int, default=35)
    parser.add_argument("--axes", nargs="+", default=["P", "Q", "R", "S"],
                         help="All axes on the laser DAC card -- PR is card-wide, so every axis "
                              "here gets the same new range, but each axis is queried/set "
                              "individually to catch any card that (unexpectedly) doesn't apply "
                              "PR uniformly.")
    parser.add_argument("--new-range-code", type=int, default=2,
                         help="Default 2 = 0-10.24V (see asi_tiger.dac.DAC_RANGE_CODES). "
                              "Comfortably covers the Oxxius L4Cc/L6Cc's documented 0-5V=100%% "
                              "modulation range with headroom. Only used with --set-range.")
    parser.add_argument("--set-range", action="store_true",
                         help="Actually send PR <axis>=<new-range-code> for every axis. "
                              "Requires a controller power-cycle/reset afterward before the new "
                              "range is real (per ASI's own docs) -- this script will tell you "
                              "and stop; it will NOT sweep voltages in the same run as --set-range.")
    parser.add_argument("--verify", action="store_true",
                         help="Re-query PR <axis>? and, if it now matches --new-range-code, run a "
                             "real voltage sweep up to --max-volts so you can confirm against a "
                             "real power meter. Use this on a run AFTER you've power-cycled "
                             "following --set-range -- never combine --set-range and --verify in "
                             "the same run, since the range isn't real yet in that same run.")
    parser.add_argument("--max-volts", type=float, default=5.5,
                         help="Top of the verification sweep -- default 5.5V, chosen to sit just "
                              "above the Oxxius's documented 5V=100%% point (so you can confirm "
                              "power actually plateaus there) while staying well inside "
                              "range_code 2's 10.24V ceiling.")
    parser.add_argument("--n-levels", type=int, default=6)
    parser.add_argument("--hold-s", type=float, default=2.0,
                         help="Longer default than asi_tiger_laser_intensity_test.py's -- give "
                              "yourself time to read the power meter at each level.")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    if args.set_range and args.verify:
        print("REFUSING: --set-range and --verify together in one run -- the new range is NOT "
              "real until you power-cycle the controller in between. Run --set-range, power-cycle, "
              "then run --verify separately.")
        return

    lo_mv, hi_mv = DAC_RANGE_LIMITS_MV.get(args.new_range_code, (None, None))
    if lo_mv is None:
        print(f"REFUSING: unknown --new-range-code {args.new_range_code}; valid: "
              f"{sorted(DAC_RANGE_LIMITS_MV)}")
        return
    new_range_v = (lo_mv / 1000.0, hi_mv / 1000.0)

    tiger = TigerController(args.port, baudrate=args.baudrate)
    dac = None
    try:
        tiger.connect()
        dac = ASITigerDAC(tiger=tiger)
        for axis in args.axes:
            dac.add_channel(f"laser_{axis}", card_addr=args.card_addr, axis=axis, range_code=1)
            # ^ range_code here is just this object's own starting label for display purposes
            # below -- irrelevant to this script's actual logic, which only ever trusts the
            # real PR <axis>? query, never this cached value.

        print(f"=== Querying REAL, currently-active range on card {args.card_addr} ===")
        real_codes = {}
        for axis in args.axes:
            reply = dac.query_range(f"laser_{axis}")
            real_codes[axis] = reply
            print(f"  axis {axis}: PR {axis}? -> {reply!r}")
        print("(compare these across axes -- if they don't all agree, that's itself a real "
              "finding worth investigating before trusting PR to be genuinely card-wide here.)")

        if args.set_range:
            print(f"\n=== Setting range_code={args.new_range_code} "
                  f"({new_range_v[0]:.3f}V to {new_range_v[1]:.3f}V) on card {args.card_addr} ===")
            print("This changes real hardware state. Per ASI's own command:pr docs, this does "
                  "NOT take effect until the controller is reset or restarted -- and, CONFIRMED "
                  "ON REAL HARDWARE by the user, a real power cycle alone was not enough either: "
                  "PR's new range reverted until an explicit 'SS Z' save-to-flash command was "
                  "also sent. This script now sends both.")
            if not args.yes:
                resp = input("Send PR + SS Z now? [y/N] ").strip().lower()
                if not resp.startswith("y"):
                    print("Cancelled.")
                    return
            for axis in args.axes:
                dac.set_range(f"laser_{axis}", args.new_range_code)
                print(f"  Sent PR {axis}={args.new_range_code}.")
            dac.save_settings(args.card_addr)
            print(f"  Sent SS Z (card {args.card_addr}) to commit the new range to flash -- "
                  f"confirmed on real hardware as the step that actually makes it survive a "
                  f"power cycle, not PR alone.")
            print("\n*** NOW POWER-CYCLE (or otherwise reset/restart) THE TIGER CONTROLLER. ***")
            print("This script will NOT verify or sweep in this same run -- the new range is not "
                  "confirmed active yet. After the power cycle, re-run this script with --verify.")
            return

        if args.verify:
            print(f"\n=== Verifying range_code={args.new_range_code} is now genuinely active ===")
            all_match = True
            for axis in args.axes:
                reply = dac.query_range(f"laser_{axis}")
                matched = str(args.new_range_code) in reply
                all_match = all_match and matched
                print(f"  axis {axis}: PR {axis}? -> {reply!r} "
                      f"({'MATCHES' if matched else 'does NOT match'} requested range_code={args.new_range_code})")
            if not all_match:
                print("\nNOT CONFIRMED -- at least one axis does not report the new range yet. "
                      "Did you actually power-cycle/reset the controller after --set-range? "
                      "Refusing to sweep voltages against an unconfirmed range.")
                return

            # NOTE: deliberately NOT using ASITigerDAC.confirm_range_change() here --
            # that method only updates an in-memory pending-change record set by a prior
            # set_range() call on the SAME ASITigerDAC instance, which doesn't exist across
            # two separate script invocations (--set-range and --verify are intentionally
            # run as separate processes, with a real power cycle in between). Since this
            # run just confirmed the real hardware range directly via query_range() above,
            # it's simplest and equally correct to just re-register each channel with the
            # NOW-CONFIRMED range_code from scratch, rather than route through that pending-
            # change bookkeeping (which is meant for a single long-running session).
            for axis in args.axes:
                dac.add_channel(f"laser_{axis}", card_addr=args.card_addr, axis=axis,
                                 range_code=args.new_range_code)

            print(f"\nCONFIRMED: card {args.card_addr} is genuinely at range_code={args.new_range_code} "
                  f"({new_range_v[0]:.3f}V to {new_range_v[1]:.3f}V). Software safety limits updated "
                  f"to match.")

            levels = [args.max_volts * i / (args.n_levels - 1) for i in range(args.n_levels)]
            print(f"\n=== Voltage sweep, 0 to {args.max_volts:.2f}V, {args.n_levels} levels ===")
            print("Watch your real laser power meter. Per Oxxius's L4Cc/L6Cc manual, power should "
                  "rise LINEARLY and reach 100% (rated power) at 5.000V, then stay flat above that.")
            for axis in args.axes:
                name = f"laser_{axis}"
                print(f"\n--- axis {axis} ---")
                for v in levels:
                    print(f"  Setting {v:.3f}V...")
                    dac.set_voltage(name, v)
                    time.sleep(args.hold_s)
                print("  Returning to 0V...")
                dac.set_voltage(name, 0.0)
                time.sleep(args.hold_s)

            print("\nIf 100% power was reached at/near 5.000V (not before, not needing more than "
                  "5V): this card's range is correctly configured -- update "
                  "config_asi_tiger_example.py's 'laser_dac_channels' range_code to match "
                  f"({args.new_range_code}) if it doesn't already, and set cfg.startup"
                  "['max_laser_voltage'] = 5 in your real mesoSPIM config file.")
            print("If power plateaus BELOW 100% even at 5V+: the issue is not this DAC range -- "
                  "check the Oxxius module's own analog-modulation enable setting/mode and its "
                  "current-vs-power calibration before assuming it's a voltage problem.")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if dac is not None:
            for axis in args.axes:
                try:
                    dac.set_voltage(f"laser_{axis}", 0.0)
                except Exception:
                    pass
        if tiger.is_connected:
            tiger.disconnect()
            print("Disconnected.")


if __name__ == "__main__":
    main()
