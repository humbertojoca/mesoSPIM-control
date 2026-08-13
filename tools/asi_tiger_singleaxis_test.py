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

NOT YET CONFIRMED on SIGNAL_DAC_4CH specifically -- ASI's docs describe
this in terms of "MicroMirror cards". This script's first configure()
call is the actual test: a ':N-#' error reply means your card doesn't
implement this module. UPDATE: ASI has since confirmed these specific
DAC cards shipped WITHOUT the GALVO_SPIM firmware this module needs --
so this test is currently EXPECTED to fail until that firmware is
installed. Run it anyway to get the real, confirmed error rather than
assuming; re-run once ASI has updated the firmware.

Usage (per ASI's confirmed card assignments for this rack):
    # Galvo card (7 = A/B/C/D, +/-10.24V range, B/D are the fast axes):
    python tools/asi_tiger_singleaxis_test.py --port COM4 --card-addr 37 --axis B \\
        --pattern sawtooth --amplitude 2.0 --offset 0 --frequency 50 --duration 15

    # ETL card (4 = H/I/J/K, 0-4.096V range ONLY, I/K are the fast axes --
    # note the positive offset, this card can't go negative):
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
    peak = args.amplitude / 2 + abs(args.offset)
    swing_lo = args.offset - args.amplitude / 2
    swing_hi = args.offset + args.amplitude / 2

    print(f"Pattern: {args.pattern}")
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
        print(f"\nConnected. Zeroing axis {args.axis} on card {args.card_addr} first...")
        tiger.send_command(f"M {args.axis}=0", card_addr=args.card_addr)

        saw = SingleAxisWaveform(tiger, card_addr=args.card_addr, axis=args.axis)

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
            print("This most likely means your card's firmware does NOT implement the "
                  "SINGLEAXIS_FUNCTION module (ASI's docs describe it for 'MicroMirror' "
                  "cards specifically, and this hasn't been confirmed for SIGNAL_DAC_4CH). "
                  "Fall back to the software-timed WaveformStreamer approach instead.")
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
                saw.stop()
            except Exception as exc:
                print(f"WARNING: could not stop single-axis mode: {exc}")
            try:
                tiger.send_command(f"M {args.axis}=0", card_addr=args.card_addr)
            except Exception as exc:
                print(f"WARNING: could not zero axis: {exc}")
        if tiger.is_connected:
            tiger.disconnect()
            print("Disconnected.")


if __name__ == "__main__":
    main()
