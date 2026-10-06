#!/usr/bin/env python3
"""
asi_tiger_singleaxis_test.py
==============================
Tests ASI Tiger's ON-CARD single-axis waveform generator (SAA/SAF/SAO/
SAP/SAM) -- genuine hardware-timed sawtooth/triangle/square/sine output.
No per-sample serial commands, no software-timing ceiling: configure
once, start once, the card generates the waveform autonomously. This is
the intended path for galvo scanning (see PATCHNOTES_ASI_TIGER.md's
bandwidth discussion -- this sidesteps that problem entirely, IF your
card's firmware implements this module).

CONFIRMED WORKING on this rack's firmware -- single-axis function does
NOT require GALVO_SPIM firmware, contrary to an earlier note here (see
PATCHNOTES_ASI_TIGER.md's "CORRECTION" section for the full story). The
galvo has been tested successfully with this script's approach
(triangle wave confirmed on a scope). Use this script to test/tune a
single axis in isolation; use tools/asi_tiger_galvo_etl_demo.py for the
full galvo+ETL+PLC recipe.

UPDATE -- new firmware on card 37: ASI provided new TGGALVO-derived
firmware for the galvo card specifically, bench-confirmed working (100Hz
triangle on A/C is visibly smooth). Two things changed from the
original SIGNAL_DAC_4CH firmware: (1) A and C are now the 40kHz-fast
axes, B/D the normal ~1kHz pair -- the OPPOSITE of the original
firmware; (2) SAA/SAO's units changed to a control range of -4000..4000
for the same +/-10.24V the original represents as -10240..10240 (mV) --
use --units-per-volt (see below) to select the right encoding for your
card's actual firmware.

Usage (per ASI's confirmed card assignments for this rack):
    # Galvo card (7 = A/B/C/D), NEW TGGALVO firmware, A/C are fast:
    python tools/asi_tiger_singleaxis_test.py --port COM4 --card-addr 37 --axis A \\
        --pattern triangle --amplitude 2.0 --offset 0 --frequency 100 --duration 15

    # Galvo card, ORIGINAL SIGNAL_DAC_4CH firmware (if not yet reflashed), B/D fast:
    python tools/asi_tiger_singleaxis_test.py --port COM4 --card-addr 37 --axis B \\
        --pattern sawtooth --amplitude 2.0 --offset 0 --frequency 50 --duration 15 \\
        --units-per-volt 1000

    # ETL card (4 = H/I/J/K, 0-4.096V range ONLY, I/K are the fast axes --
    # note the positive offset, this card can't go negative; unaffected by
    # the card-37 firmware update, still the original SIGNAL_DAC_4CH):
    python tools/asi_tiger_singleaxis_test.py --port COM4 --card-addr 34 --axis I \\
        --pattern sawtooth --amplitude 1.0 --offset 2.0 --frequency 50 --duration 15 --max-volts 4.096

What it does:
  1. Zeros the axis with a plain M command first (baseline safety).
  2. Sends SAA/SAO/SAF/SAP to configure the pattern -- if this errors,
     stops immediately and reports that this firmware module likely
     isn't available on your card.
  3. Starts it (SAM=1) and holds for --duration seconds so you can watch
     it on a scope connected to this axis's BNC output.
  4. Stops it (SAM=0) and zeros the output again -- runs even on Ctrl-C
     or an error, via try/finally.

Amplitude/offset safety: refuses to start if amplitude/2 + |offset|
exceeds --max-volts (default 10.0V, a conservative bound under the
card's default +/-10.24V range) unless --yes is passed.
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import TigerController, TigerError
from asi_tiger.singleaxis import (
    SingleAxisWaveform, PATTERN_SAWTOOTH, PATTERN_TRIANGLE, PATTERN_SQUARE, PATTERN_SINE,
)

PATTERNS = {
    "sawtooth": PATTERN_SAWTOOTH,
    "triangle": PATTERN_TRIANGLE,
    "square": PATTERN_SQUARE,
    "sine": PATTERN_SINE,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--card-addr", type=int, required=True, help="e.g. 37")
    parser.add_argument("--axis", required=True, help="e.g. A")
    parser.add_argument("--units-per-volt", type=float, default=4000 / 10.24,
                         help="Raw device units per volt for SAA/SAO. Default (~390.625) matches "
                              "ASI's new TGGALVO firmware on card 37. Pass 1000.0 for the original "
                              "SIGNAL_DAC_4CH firmware (still on the ETL/laser cards, and on card 37 "
                              "if not yet reflashed).")
    parser.add_argument("--pattern", choices=list(PATTERNS), default="sawtooth")
    parser.add_argument("--amplitude", type=float, default=1.0, help="Peak-to-peak amplitude, volts")
    parser.add_argument("--offset", type=float, default=0.0, help="Center offset, volts")
    parser.add_argument("--frequency", type=float, default=10.0, help="Waveform frequency, Hz")
    parser.add_argument("--duration", type=float, default=10.0, help="How long to hold the waveform, seconds")
    parser.add_argument("--max-volts", type=float, default=10.0,
                         help="Safety bound on amplitude/2 + |offset| (default 10.0V)")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    args = parser.parse_args()

    period_ms = 1000.0 / args.frequency
    # abs(): a NEGATIVE amplitude only reverses the ramp direction; the excursion is the same
    peak = abs(args.amplitude) / 2 + abs(args.offset)
    swing_lo = args.offset - abs(args.amplitude) / 2
    swing_hi = args.offset + abs(args.amplitude) / 2

    print(f"Pattern: {args.pattern}")
    print(f"units_per_volt: {args.units_per_volt:.4f} "
          f"({'new TGGALVO firmware' if abs(args.units_per_volt - 1000.0) > 1 else 'original SIGNAL_DAC_4CH firmware'})")
    print(f"Amplitude: {args.amplitude:.3f} Vpp, offset: {args.offset:+.3f} V "
          f"-> swings between {swing_lo:+.3f} V and {swing_hi:+.3f} V")
    print(f"Frequency: {args.frequency:.2f} Hz (period {period_ms:.2f} ms)")

    if swing_lo < 0:
        print(f"\nNOTE: this swings to {swing_lo:.3f} V, i.e. negative. Per ASI's card specs for this "
              f"rack, ONLY the galvo card (card 7 = axes A/B/C/D, range +/-10.24V) supports negative "
              f"voltage -- the ETL card (card 4 = H/I/J/K) and laser card (card 5 = P/Q/R/S) are both "
              f"0 to 4.096V only. If --card-addr/--axis below is NOT the galvo card, use a positive "
              f"--offset at least --amplitude/2 to stay in range.")
        if not args.yes:
            resp = input("Continue anyway? [y/N] ").strip().lower()
            if not resp.startswith("y"):
                print("Cancelled.")
                return

    if peak > args.max_volts:
        print(f"\nREFUSING: peak excursion {peak:.3f}V exceeds --max-volts {args.max_volts:.3f}V. "
              f"Pass a smaller --amplitude/--offset, or raise --max-volts if you've confirmed "
              f"your card's configured range and downstream hardware can handle it.")
        return

    if not args.yes:
        resp = input(f"\nAbout to drive card {args.card_addr} axis {args.axis} with this waveform "
                      f"for {args.duration:.0f}s. Continue? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    saw = None
    try:
        tiger.connect()
        print(f"\nConnected. Defensively stopping any leftover single-axis mode on {args.axis} "
              f"and zeroing (plain M=0 alone does NOT work if the axis is already in single-axis "
              f"mode from a previous run -- confirmed on real hardware, see "
              f"SingleAxisWaveform.stop()'s docstring)...")
        saw = SingleAxisWaveform(tiger, card_addr=args.card_addr, axis=args.axis,
                                  units_per_volt=args.units_per_volt)
        saw.stop_and_zero()

        print("Configuring single-axis waveform (SAA/SAO/SAF/SAP) -- this is the real test...")
        try:
            saw.configure(
                pattern=PATTERNS[args.pattern],
                amplitude_v=args.amplitude,
                offset_v=args.offset,
                period_ms=period_ms,
            )
        except TigerError as exc:
            print(f"\nERROR configuring single-axis mode: {exc}")
            print("Single-axis function is confirmed working on this rack's firmware (galvo tested "
                  "successfully) -- this error more likely means a wrong --card-addr/--axis, or an "
                  "axis-specific issue, rather than the module being unavailable. Double-check the "
                  "card address and axis letter.")
            saw = None  # nothing to stop/clean up -- configure() never succeeded
            return
        print("Configured successfully (':A' reply) -- this firmware module IS available on this card.")

        print(f"Starting waveform (SAM {args.axis}=1)...")
        saw.start()
        print(f"Running for {args.duration:.0f}s -- check with a scope on card {args.card_addr}, "
              f"axis {args.axis}'s BNC output now.")
        time.sleep(args.duration)
        print("Duration elapsed.")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if saw is not None:
            print("Stopping waveform (SAM=0) and zeroing output...")
            try:
                saw.stop_and_zero()
            except Exception as exc:
                print(f"WARNING: could not stop/zero single-axis mode: {exc}")
        if tiger.is_connected:
            tiger.disconnect()
            print("Disconnected.")


if __name__ == "__main__":
    main()
