#!/usr/bin/env python3
"""
asi_tiger_shared_trigger_test.py
===================================
Tests the "shared trigger source" architecture: the PLC generates ONE
pulse per simulated frame and fans it out to BOTH the camera's external
trigger input and the ETL's backplane trigger-in line simultaneously --
removing the independently-quantized camera->PLC relay stage from the
camera-vs-ETL timing relationship (see PLCCard.fan_out_trigger()'s
docstring).

CONFIRMED on real hardware: ETL single-axis "once" mode (SAM=2) DOES
auto-rearm -- it responds to every genuine rising edge while armed, no
host intervention needed between triggers (see
SingleAxisWaveform.arm_triggered()'s docstring; an earlier version of
this script assumed the opposite and re-armed every frame from the
host, which turned out to be unnecessary complexity built on a
testing-artifact finding).

So this script is simple: arm ETL ONCE, then fire N pulses at your real
frame interval, with NO per-frame host commands in the trigger path at
all. Watch the ETL on a scope across all N pulses to confirm:
  1. Every pulse produces a fresh, correctly-shaped ramp (auto-rearm
     holds up over many cycles, not just the 3 manually tested by hand).
  2. Timing stays consistent frame-to-frame (no drift, since each cycle
     is freshly triggered rather than free-running).

This does NOT actually trigger your camera unless --camera-trigger-bnc
is wired to it -- if omitted, only the ETL side is driven.

Usage:
    python tools/asi_tiger_shared_trigger_test.py --port COM4 \\
        --etl-card-addr 34 --etl-axis I \\
        --plc-card-addr 36 --camera-trigger-bnc 2 \\
        --etl-period-ms 20 --n-frames 100 --frame-interval-s 0.1
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import (
    TigerController, TigerError, PLCCard,
    SingleAxisWaveform, PATTERN_SAWTOOTH,
    trigger_in_backplane_addr, axis_slot_index, enable_backplane_trigger_mode,
    bnc_addr,
)
from asi_tiger.plc import cell_addr


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)

    parser.add_argument("--etl-card-addr", type=int, default=34)
    parser.add_argument("--etl-axis", default="I")
    parser.add_argument("--etl-card-first-axis", default="H")
    parser.add_argument("--etl-amplitude", type=float, default=1.0, help="Vpp")
    parser.add_argument("--etl-offset", type=float, default=2.0, help="V")
    parser.add_argument("--etl-period-ms", type=float, default=20.0,
                         help="CONFIRMED ON REAL HARDWARE: must be set at least a couple ms SHORTER "
                              "than --frame-interval-s*1000, not equal to it -- SAM=2 needs to finish "
                              "its cycle before the next trigger arrives, or it misses every OTHER "
                              "trigger (exactly this pattern was observed with zero margin). This "
                              "script warns if the margin looks thin.")
    parser.add_argument("--etl-max-volts", type=float, default=4.096)

    parser.add_argument("--plc-card-addr", type=int, default=36)
    parser.add_argument("--plc-axis", default="E")
    parser.add_argument("--trigger-source-cell", type=int, default=8,
                         help="PLC cell used as the shared pulse source (default 8, pick one "
                              "not used by other PLC config, e.g. the laser toggle's cells 1-3)")
    parser.add_argument("--camera-trigger-bnc", type=int, default=None,
                         help="PLC BNC wired to your camera's external trigger input. If omitted, "
                              "only the ETL side is driven -- useful for testing without a camera connected.")
    parser.add_argument("--pulse-width-s", type=float, default=0.002,
                         help="How long to hold the shared trigger high -- must be a genuine pulse "
                              "(low->high->low), not a sustained level, for auto-rearm to see distinct "
                              "edges on each frame.")

    parser.add_argument("--n-frames", type=int, default=100, help="Number of simulated frames to run")
    parser.add_argument("--frame-interval-s", type=float, default=0.1, help="Delay between simulated frames")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    etl_lo = args.etl_offset - args.etl_amplitude / 2
    etl_hi = args.etl_offset + args.etl_amplitude / 2
    print(f"ETL: card {args.etl_card_addr}, axis {args.etl_axis}, swings {etl_lo:+.3f}V to {etl_hi:+.3f}V, "
          f"period {args.etl_period_ms:.2f}ms")
    if etl_lo < 0 or etl_hi > args.etl_max_volts:
        print(f"\nREFUSING: ETL swing [{etl_lo:.3f}, {etl_hi:.3f}]V outside [0, {args.etl_max_volts:.3f}]V.")
        return

    frame_period_ms = args.frame_interval_s * 1000.0
    margin_ms = frame_period_ms - args.etl_period_ms
    MIN_SAFE_MARGIN_MS = 2.0  # empirical from real hardware -- see --etl-period-ms help text
    if margin_ms < MIN_SAFE_MARGIN_MS:
        print(f"\nWARNING: --etl-period-ms ({args.etl_period_ms:.2f}ms) leaves only {margin_ms:.2f}ms "
              f"of margin below --frame-interval-s ({frame_period_ms:.2f}ms). CONFIRMED ON REAL "
              f"HARDWARE: too little margin here means the axis is still finishing its current cycle "
              f"when the next trigger arrives, and MISSES EVERY OTHER TRIGGER. {MIN_SAFE_MARGIN_MS:.1f}ms "
              f"was sufficient in testing, but verify on a scope at your real rate.")
        if not args.yes:
            resp = input("Continue anyway? [y/N] ").strip().lower()
            if not resp.startswith("y"):
                print("Cancelled.")
                return
    else:
        print(f"ETL period margin below frame interval: {margin_ms:.2f}ms (>= {MIN_SAFE_MARGIN_MS:.1f}ms, OK).")

    if args.camera_trigger_bnc is not None:
        print(f"Camera: PLC BNC{args.camera_trigger_bnc} will be pulsed alongside every ETL trigger.")
    else:
        print("Camera: --camera-trigger-bnc not set -- only driving the ETL side this run.")

    if not args.yes:
        resp = input(f"\nAbout to run {args.n_frames} simulated frames, ETL armed ONCE at the start "
                      f"(no host commands in the per-frame trigger path). Continue? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    etl = plc = None
    try:
        tiger.connect()
        print(f"\nConnected. Defensively resetting ETL axis {args.etl_axis}...")
        etl = SingleAxisWaveform(tiger, card_addr=args.etl_card_addr, axis=args.etl_axis)
        etl.stop_and_zero()

        enable_backplane_trigger_mode(tiger, card_addr=args.etl_card_addr)
        try:
            etl.configure(pattern=PATTERN_SAWTOOTH, amplitude_v=args.etl_amplitude,
                          offset_v=args.etl_offset, period_ms=args.etl_period_ms, external_trigger=True)
        except TigerError as exc:
            print(f"\nERROR configuring ETL: {exc}")
            etl = None
            return

        plc = PLCCard(tiger, card_addr=args.plc_card_addr, axis=args.plc_axis)
        plc.set_cell_state(args.trigger_source_cell, False)  # start low -- defines a clean baseline

        slot = axis_slot_index(args.etl_axis, args.etl_card_first_axis)
        trig_addr = trigger_in_backplane_addr(slot)
        fanned_out_addrs = [trig_addr]
        if args.camera_trigger_bnc is not None:
            fanned_out_addrs.append(bnc_addr(args.camera_trigger_bnc))
        plc.fan_out_trigger(source_addr=cell_addr(args.trigger_source_cell), output_addrs=fanned_out_addrs)
        print(f"PLC cell {args.trigger_source_cell} fans out to: {fanned_out_addrs} "
              f"(ETL trigger-in{' + camera BNC' if args.camera_trigger_bnc else ''}).")

        print("Arming ETL ONCE (SAM=2) -- no further SAM commands during the frame loop below...")
        etl.arm_triggered(free_running=False)

        print(f"\nFiring {args.n_frames} shared trigger pulses at {args.frame_interval_s*1000:.1f}ms "
              f"intervals. Watch the ETL on a scope -- every pulse should produce a fresh, "
              f"consistently-timed ramp.")
        t_start = time.perf_counter()
        for i in range(args.n_frames):
            plc.set_cell_state(args.trigger_source_cell, True)
            time.sleep(args.pulse_width_s)
            plc.set_cell_state(args.trigger_source_cell, False)

            elapsed = time.perf_counter() - t_start
            target = (i + 1) * args.frame_interval_s
            remaining = target - elapsed
            if remaining > 0:
                time.sleep(remaining)

        total = time.perf_counter() - t_start
        expected = args.n_frames * args.frame_interval_s
        note = "host-loop overhead only -- expected" if abs(total - expected) < 0.05 else \
               "larger than expected drift in the HOST pacing loop, not the ETL trigger itself"
        print(f"\nDone: {args.n_frames} pulses over {total:.3f}s (target: {expected:.3f}s, {note}).")
        print("Check on the scope: did every one of the pulses produce a correctly-shaped, "
              "consistently-timed ramp? Any missed or malformed cycles would point to a real "
              "hardware-side auto-rearm limit at this rate, worth reporting back precisely (which "
              "pulse number, if any pattern).")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if etl is not None:
            print("\nStopping and zeroing ETL...")
            try:
                etl.stop_and_zero()
            except Exception as exc:
                print(f"WARNING: could not stop/zero ETL: {exc}")
        if plc is not None:
            print("Safing PLC outputs...")
            try:
                plc.safe_all_outputs()
            except Exception as exc:
                print(f"WARNING: could not safe PLC outputs: {exc}")
        if tiger.is_connected:
            tiger.disconnect()
            print("Disconnected.")


if __name__ == "__main__":
    main()
