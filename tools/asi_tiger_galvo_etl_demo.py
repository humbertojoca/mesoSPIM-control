#!/usr/bin/env python3
"""
asi_tiger_galvo_etl_demo.py
==============================
Implements ASI's confirmed architecture for hardware-timed galvo + ETL
scanning, relayed directly from their support team:

    "I think the idea is to use single-axis function on both the galvo
    and ETL DAC cards, where the galvo is free-running, and the etl
    sawtooth wave is triggered across the backplane, from the TGPLC
    card."

What this script does:
  1. Configures the galvo (card 7 = A/B/C/D, using the fast axis B by
     default) as a free-running sawtooth -- internal clock, starts
     immediately, keeps running in hardware indefinitely.
  2. Configures the ETL (card 4 = H/I/J/K, using the fast axis I by
     default) as a sawtooth ARMED for external trigger -- configured but
     idle until a backplane TTL pulse arrives.
  3. Configures the PLC to route a chosen trigger source (default: BNC1,
     e.g. your camera trigger) onto the ETL axis's backplane trigger-in
     line, so each pulse advances/starts the ETL sawtooth.
  4. Holds for --duration seconds so you can watch both on a scope --
     the galvo running continuously, the ETL stepping/restarting each
     time BNC1 is pulsed.
  5. Cleans up: stops both single-axis patterns, zeros both outputs,
     and reconfigures the PLC's backplane output back to a harmless
     constant-0 -- runs even on Ctrl-C or an error.

PREREQUISITE (per ASI): a physical jumper on header SV9 on the ETL card
(the one receiving the backplane trigger) -- not needed on the galvo
card, which stays free-running. This script cannot verify the jumper is
present; if the ETL never responds to BNC1 pulses, check that first.

Usage:
    python tools/asi_tiger_galvo_etl_demo.py --port COM4 \\
        --galvo-card-addr 37 --galvo-axis A --galvo-pattern triangle \\
        --etl-card-addr 34 --etl-axis I --etl-pattern sawtooth \\
        --plc-card-addr 36 --trigger-bnc 1 \\
        --galvo-amplitude 2.0 --galvo-frequency 50 \\
        --etl-amplitude 1.0 --etl-offset 2.0 \\
        --duration 30

--galvo-pattern defaults to triangle (smoother, no flyback discontinuity
-- typically preferred for continuous galvo scanning); --etl-pattern
defaults to sawtooth (typical for a tunable-lens ramp synced to camera
exposure). Both accept sawtooth/triangle/square/sine. Triangle/square
periods are automatically rounded to an even number of ms, per SAF's
documented requirement -- watch for the printed note if your requested
frequency doesn't divide evenly.
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import (
    TigerController, TigerError, PLCCard,
    SingleAxisWaveform, PATTERN_SAWTOOTH, PATTERN_TRIANGLE, PATTERN_SQUARE, PATTERN_SINE,
    trigger_in_backplane_addr, axis_slot_index, enable_backplane_trigger_mode,
    bnc_addr, IO_TYPE_INPUT,
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

    parser.add_argument("--galvo-card-addr", type=int, default=37)
    parser.add_argument("--galvo-axis", default="A",
                         help="Fast axis on the galvo card. UPDATED: with ASI's new TGGALVO firmware "
                              "on this card, A and C are the 40kHz-fast axes (B/D are now the normal "
                              "~1kHz pair) -- the OPPOSITE of the original SIGNAL_DAC_4CH firmware, "
                              "where B/D were fastest. Confirm which firmware your card actually has "
                              "before trusting this default.")
    parser.add_argument("--galvo-units-per-volt", type=float, default=4000 / 10.24,
                         help="Raw device units per volt for SAA/SAO on the galvo axis. Default "
                              "(~390.625) matches ASI's new TGGALVO firmware, confirmed by them "
                              "directly: control range -4000..4000 for the same +/-10.24V the "
                              "original firmware represents as -10240..10240 (mV). Pass 1000.0 if "
                              "your card is still on the original SIGNAL_DAC_4CH firmware.")
    parser.add_argument("--galvo-pattern", choices=list(PATTERNS), default="triangle")
    parser.add_argument("--galvo-amplitude", type=float, default=1.3, help="Vpp")
    parser.add_argument("--galvo-offset", type=float, default=0.53, help="V")
    parser.add_argument("--galvo-frequency", type=float, default=100.0, help="Hz")
    parser.add_argument("--galvo-max-volts", type=float, default=5.0,
                         help="Safety bound -- ASI: limit galvo commands to +/-10.00V exactly")

    parser.add_argument("--etl-card-addr", type=int, default=34)
    parser.add_argument("--etl-axis", default="H", help="ASI-confirmed fast axis on the ETL card")
    parser.add_argument("--etl-pattern", choices=list(PATTERNS), default="sawtooth")
    parser.add_argument("--etl-card-first-axis", default="H", help="First axis letter on the ETL card (for slot math)")
    parser.add_argument("--etl-amplitude", type=float, default=0.38, help="Vpp")
    parser.add_argument("--etl-offset", type=float, default=2.32,
                         help="V -- must be >= amplitude/2 since the ETL card is 0-4.096V only (unipolar)")
    parser.add_argument("--etl-max-volts", type=float, default=4.096, help="ETL/laser card hardware ceiling")
    parser.add_argument("--etl-period-ms", type=float, default=None,
                         help="ETL waveform period in ms. CONFIRMED ON REAL HARDWARE: this must be "
                              "set SHORTER than your camera's actual trigger interval, not equal to "
                              "it -- SAM=2 runs one full cycle over this period before it's ready for "
                              "the next trigger; setting it exactly equal to the camera interval means "
                              "the axis is still finishing as the next edge arrives, missing every "
                              "other trigger. 2-3ms of margin was sufficient in testing, but pass "
                              "--camera-period-ms below to get an explicit warning if your margin looks "
                              "too thin, rather than discovering it as an every-other-pulse pattern on "
                              "a scope. Independent of --galvo-frequency; if omitted, defaults to "
                              "matching the galvo period (only correct if that happens to already have "
                              "enough margin below your camera's real interval).")
    parser.add_argument("--camera-period-ms", type=float, default=None,
                         help="Your camera's actual trigger interval, in ms -- used ONLY to check "
                              "--etl-period-ms has enough margin below it (see that flag's help). "
                              "Purely a safety check; doesn't change any command sent to the hardware.")
    parser.add_argument("--etl-trigger-mode", choices=["once", "free_run"], default="once",
                         help="'once' (SAM=2): one cycle per trigger. CONFIRMED on real hardware: DOES "
                              "auto-rearm -- responds to every genuine rising edge while armed, with no "
                              "host intervention between triggers (an earlier finding here claimed the "
                              "opposite; that was a testing artifact from holding the trigger line high "
                              "rather than producing distinct edges -- see arm_triggered()'s docstring). "
                              "This is the recommended default for real per-frame camera sync. "
                              "'free_run' (SAM=4): waits for the FIRST trigger, then free-runs on its "
                              "own internal clock -- does NOT re-sync to later camera triggers. "
                              "CONFIRMED on real hardware: since it and the trigger source are "
                              "independent clocks, their phase drifts continuously over an acquisition "
                              "(looks like jitter on a scope, is actually clock-drift beat frequency). "
                              "Little reason to prefer this now that 'once' is confirmed to auto-rearm.")

    parser.add_argument("--plc-card-addr", type=int, default=36)
    parser.add_argument("--plc-axis", default="E")
    parser.add_argument("--trigger-bnc", type=int, default=1, help="PLC BNC that pulses the ETL trigger")

    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    galvo_period_ms = 1000.0 / args.galvo_frequency
    if PATTERNS[args.galvo_pattern] in (PATTERN_TRIANGLE, PATTERN_SQUARE):
        rounded = 2 * round(galvo_period_ms / 2)
        if rounded != galvo_period_ms:
            print(f"Note: {args.galvo_pattern} pattern requires an even period in ms -- "
                  f"rounding galvo period from {galvo_period_ms:.2f}ms to {rounded}ms "
                  f"({1000/rounded:.2f} Hz instead of {args.galvo_frequency:.2f} Hz).")
        galvo_period_ms = rounded
    galvo_peak = args.galvo_amplitude / 2 + abs(args.galvo_offset)
    etl_lo = args.etl_offset - args.etl_amplitude / 2
    etl_hi = args.etl_offset + args.etl_amplitude / 2

    print("Galvo (free-running):")
    print(f"  card {args.galvo_card_addr}, axis {args.galvo_axis}, {args.galvo_pattern}, "
          f"{args.galvo_amplitude:.3f} Vpp, offset {args.galvo_offset:+.3f}V, {args.galvo_frequency:.1f} Hz "
          f"(period {galvo_period_ms:.1f}ms), units_per_volt={args.galvo_units_per_volt:.4f} "
          f"({'new TGGALVO firmware' if abs(args.galvo_units_per_volt - 1000.0) > 1 else 'original SIGNAL_DAC_4CH firmware'})")
    print("ETL (PLC-triggered):")
    etl_period_preview = args.etl_period_ms if args.etl_period_ms is not None else galvo_period_ms
    print(f"  card {args.etl_card_addr}, axis {args.etl_axis}, {args.etl_pattern}, "
          f"swings {etl_lo:+.3f}V to {etl_hi:+.3f}V, period {etl_period_preview:.2f}ms, "
          f"mode={args.etl_trigger_mode}, triggered from PLC BNC{args.trigger_bnc}")

    if galvo_peak > args.galvo_max_volts:
        print(f"\nREFUSING: galvo peak {galvo_peak:.3f}V exceeds --galvo-max-volts {args.galvo_max_volts:.3f}V "
              f"(ASI: limit galvo commands to +/-10.00V for amplifier safety).")
        return
    if etl_lo < 0 or etl_hi > args.etl_max_volts:
        print(f"\nREFUSING: ETL swing [{etl_lo:.3f}, {etl_hi:.3f}]V is outside [0, {args.etl_max_volts:.3f}]V -- "
              f"the ETL card is 0-4.096V only (unipolar), unlike the galvo card. Adjust --etl-offset/--etl-amplitude.")
        return

    if not args.yes:
        resp = input(f"\nAbout to run this for {args.duration:.0f}s. Continue? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    galvo = etl = plc = None
    trig_addr = None
    try:
        tiger.connect()
        print(f"\nConnected. Defensively stopping any leftover single-axis mode on "
              f"{args.galvo_axis}/{args.etl_axis}, then zeroing (a plain M=0 alone does NOT "
              f"work if either axis is already in single-axis mode from a previous run -- "
              f"see SingleAxisWaveform.stop()'s docstring)...")
        SingleAxisWaveform(tiger, card_addr=args.galvo_card_addr, axis=args.galvo_axis,
                           units_per_volt=args.galvo_units_per_volt).stop_and_zero()
        SingleAxisWaveform(tiger, card_addr=args.etl_card_addr, axis=args.etl_axis).stop_and_zero()

        # --- Galvo: free-running ---
        galvo = SingleAxisWaveform(tiger, card_addr=args.galvo_card_addr, axis=args.galvo_axis,
                                   units_per_volt=args.galvo_units_per_volt)
        try:
            galvo.configure(pattern=PATTERNS[args.galvo_pattern], amplitude_v=args.galvo_amplitude,
                             offset_v=args.galvo_offset, period_ms=galvo_period_ms)
        except TigerError as exc:
            print(f"\nERROR configuring galvo single-axis mode: {exc}")
            print("Single-axis function isn't responding on this card/axis -- confirm card address/axis, "
                  "or that this really is a card with SINGLEAXIS_FUNCTION firmware.")
            galvo = None
            return
        galvo.start()
        print("Galvo started (free-running).")

        # --- ETL: armed for backplane trigger ---
        etl_period_ms = args.etl_period_ms if args.etl_period_ms is not None else galvo_period_ms
        if args.etl_period_ms is None:
            print(f"Note: --etl-period-ms not set, defaulting to the galvo's period ({etl_period_ms:.2f}ms). "
                  f"Set --etl-period-ms explicitly to your camera's actual exposure time if that differs.")
        if PATTERNS[args.etl_pattern] in (PATTERN_TRIANGLE, PATTERN_SQUARE):
            rounded = 2 * round(etl_period_ms / 2)
            if rounded != etl_period_ms:
                print(f"Note: ETL {args.etl_pattern} pattern requires an even period in ms -- "
                      f"rounding from {etl_period_ms:.2f}ms to {rounded}ms.")
            etl_period_ms = rounded

        if args.camera_period_ms is not None:
            margin_ms = args.camera_period_ms - etl_period_ms
            MIN_SAFE_MARGIN_MS = 2.0  # empirical from real hardware -- see --etl-period-ms help text
            if margin_ms < MIN_SAFE_MARGIN_MS:
                print(f"\nWARNING: --etl-period-ms ({etl_period_ms:.2f}ms) leaves only {margin_ms:.2f}ms "
                      f"of margin below --camera-period-ms ({args.camera_period_ms:.2f}ms). CONFIRMED ON "
                      f"REAL HARDWARE: too little margin here means the axis is still finishing its "
                      f"current cycle when the next trigger arrives, and MISSES EVERY OTHER TRIGGER "
                      f"(exactly this pattern was observed with zero margin). {MIN_SAFE_MARGIN_MS:.1f}ms "
                      f"was sufficient in testing, but verify on a scope at your real trigger rate -- "
                      f"don't just trust this number.")
                if not args.yes:
                    resp = input("Continue anyway? [y/N] ").strip().lower()
                    if not resp.startswith("y"):
                        print("Cancelled.")
                        return
            else:
                print(f"ETL period margin below camera period: {margin_ms:.2f}ms (>= {MIN_SAFE_MARGIN_MS:.1f}ms, OK).")

        enable_backplane_trigger_mode(tiger, card_addr=args.etl_card_addr)
        etl = SingleAxisWaveform(tiger, card_addr=args.etl_card_addr, axis=args.etl_axis)
        try:
            etl.configure(pattern=PATTERNS[args.etl_pattern], amplitude_v=args.etl_amplitude,
                          offset_v=args.etl_offset, period_ms=etl_period_ms, external_trigger=True)
        except TigerError as exc:
            print(f"\nERROR configuring ETL single-axis mode: {exc}")
            etl = None
            return
        etl.arm_triggered(free_running=(args.etl_trigger_mode == "free_run"))
        print(f"ETL armed in {args.etl_trigger_mode!r} mode (waiting for backplane trigger).")
        if args.etl_trigger_mode == "once":
            print("  NOTE: confirmed on real hardware -- this DOES auto-rearm. A fresh ramp fires on "
                  "every genuine trigger edge while armed, no host intervention needed between frames. "
                  "Make sure your trigger source produces distinct edges, not a sustained high level.")

        # --- PLC: route trigger BNC onto the ETL's backplane trigger-in line ---
        plc = PLCCard(tiger, card_addr=args.plc_card_addr, axis=args.plc_axis)
        # Defensive reset, same reasoning as the DAC axis stop_and_zero() above: a previous
        # session (this script with a different --trigger-bnc, or shared_trigger_test.py with
        # --camera-trigger-bnc pointing at this same BNC) may have left this BNC configured as
        # an OUTPUT. That state persists in the controller's live config across script runs --
        # NOT reset by reconnecting the serial port, only by an actual power cycle (and possibly
        # not even then, if it was ever saved with SS). Force it back to input before trusting
        # it to read your camera's real trigger signal.
        plc.configure_io(bnc_addr(args.trigger_bnc), IO_TYPE_INPUT)
        slot = axis_slot_index(args.etl_axis, args.etl_card_first_axis)
        trig_addr = trigger_in_backplane_addr(slot)
        plc.route_to_dac_trigger(trig_addr, source_addr=bnc_addr(args.trigger_bnc))
        print(f"PLC routing BNC{args.trigger_bnc} -> ETL trigger-in (backplane addr {trig_addr}, slot {slot}).")

        print(f"\nRunning for {args.duration:.0f}s. Watch the galvo axis on a scope (continuous {args.galvo_pattern}) "
              f"and the ETL axis (should step/restart each time BNC{args.trigger_bnc} is pulsed -- pulse it "
              f"manually or via your camera if already wired).")
        print(f"If the ETL never responds: check the SV9 jumper is installed on card {args.etl_card_addr}.")
        time.sleep(args.duration)
        print("Duration elapsed.")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if galvo is not None:
            print("Stopping galvo and zeroing...")
            try:
                galvo.stop_and_zero()
            except Exception as exc:
                print(f"WARNING: could not stop/zero galvo: {exc}")
        if etl is not None:
            print("Stopping ETL and zeroing...")
            try:
                etl.stop_and_zero()
            except Exception as exc:
                print(f"WARNING: could not stop/zero ETL: {exc}")
        if plc is not None and trig_addr is not None:
            print("Safing PLC backplane routing...")
            try:
                plc.configure_io(trig_addr, IO_TYPE_INPUT)
            except Exception as exc:
                print(f"WARNING: could not safe PLC backplane output: {exc}")
        if tiger.is_connected:
            tiger.disconnect()
            print("Disconnected.")


if __name__ == "__main__":
    main()
