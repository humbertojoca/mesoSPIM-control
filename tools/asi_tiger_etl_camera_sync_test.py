#!/usr/bin/env python3
"""
asi_tiger_etl_camera_sync_test.py
====================================
Step 2 of the staged full-loop test plan (see PATCHNOTES_ASI_TIGER.md).
Confirms ETL sweeps in sync with the REAL camera's Expose-Out rising
edge -- with the camera FREE-RUNNING (not yet controlled by the
trigger chain), so this only tests "does ETL correctly respond to a
real, externally-timed edge" in isolation, before wiring it into the
full self-sustaining loop.

Both halves of this have already been individually validated:
  - ETL/SAM=2 auto-rearms correctly given ANY genuine rising edge
    (confirmed earlier with PLC-cell-toggled edges).
  - The PLC reliably detects Expose-Out's rising edge (5/5 cycles,
    asi_tiger_camera_expose_test.py).
This test confirms they compose correctly when Expose-Out's real,
SUSTAINED level (not a brief PLC-computed edge pulse) is the actual
trigger source -- routing the raw level directly and letting the ETL
axis's own SAM=2 hardware do the edge detection, matching every
previously confirmed-working ETL trigger in this project.

Confirmation here is visual (a scope on the ETL axis, ideally a second
channel on Expose-Out for direct comparison) -- there's no clean
software-only way to confirm a waveform actually swept the way there is
for Z's simple position readback (see asi_tiger_z_camera_sync_test.py
for that one, which IS software-confirmable).

REQUIRED: camera already free-running (live/continuous acquisition) in
PVCAMTest before starting. REQUIRED WIRING: camera Expose-Out -> PLC
BNC (--camera-expose-bnc, default 3, matching earlier testing).

Usage:
    python tools/asi_tiger_etl_camera_sync_test.py --port COM4
    python tools/asi_tiger_etl_camera_sync_test.py --port COM4 --etl-axis I --watch-s 15
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import (
    TigerController, PLCCard, SingleAxisWaveform, PATTERN_SAWTOOTH,
    bnc_addr, IO_TYPE_INPUT, IO_TYPE_PUSH_PULL_OUTPUT,
    enable_backplane_trigger_mode, trigger_in_backplane_addr, axis_slot_index,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--plc-card-addr", type=int, default=36)
    parser.add_argument("--plc-axis", default="E")
    parser.add_argument("--camera-expose-bnc", type=int, default=3)
    parser.add_argument("--etl-card-addr", type=int, default=34)
    parser.add_argument("--etl-axis", default="H",
                         help="ETL-L per the design doc's hardware table (card 34: 'ETL-L (H), "
                              "ETL-R (J)'). Separately confirmed as a working trigger axis via the "
                              "jumper-swap diagnostic during the K-axis fault investigation.")
    parser.add_argument("--etl-card-first-axis", default="H")
    parser.add_argument("--etl-amplitude", type=float, default=0.38, help="Vpp")
    parser.add_argument("--etl-offset", type=float, default=2.32, help="V")
    parser.add_argument("--etl-period-ms", type=float, default=20.0,
                         help="Should be set shorter than your camera's REAL Expose-Out pulse "
                              "duration -- CONFIRMED ON REAL HARDWARE this is NOT the same as the "
                              "configured exposure time in Rolling Shutter mode (one confirmed data "
                              "point: 150ms exposure setting -> ~120ms real Expose-Out pulse, "
                              "~30ms shorter). Measure your own camera's real pulse duration (e.g. "
                              "on a scope) rather than assuming the configured exposure time "
                              "directly, then apply the usual 2-3ms margin below that.")
    parser.add_argument("--etl-max-volts", type=float, default=4.096)
    parser.add_argument("--watch-s", type=float, default=15.0,
                         help="How long to leave ETL armed while you watch a scope")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    etl_lo = args.etl_offset - args.etl_amplitude / 2
    etl_hi = args.etl_offset + args.etl_amplitude / 2
    print(f"ETL: card {args.etl_card_addr}, axis {args.etl_axis}, swings {etl_lo:+.3f}V to "
          f"{etl_hi:+.3f}V, period {args.etl_period_ms:.2f}ms")
    if etl_lo < 0 or etl_hi > args.etl_max_volts:
        print(f"\nREFUSING: ETL swing [{etl_lo:.3f}, {etl_hi:.3f}]V outside [0, {args.etl_max_volts:.3f}]V.")
        return

    print(f"\nWiring: camera Expose-Out -> PLC BNC{args.camera_expose_bnc}.")
    print("REQUIRED: camera must already be free-running (live/continuous acquisition) in PVCAMTest.")
    print("Put a scope on the ETL axis (ideally a 2nd channel on Expose-Out too) to confirm each "
          "ramp starts right at Expose-Out's rising edge, across several camera cycles.")

    if not args.yes:
        resp = input("\nConfirm camera is free-running and scope is ready. Continue? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    etl = plc = None
    try:
        tiger.connect()
        plc = PLCCard(tiger, card_addr=args.plc_card_addr, axis=args.plc_axis)

        expose_addr = bnc_addr(args.camera_expose_bnc)
        plc.configure_io(expose_addr, IO_TYPE_INPUT)

        print(f"\nConfiguring ETL (axis {args.etl_axis})...")
        etl = SingleAxisWaveform(tiger, card_addr=args.etl_card_addr, axis=args.etl_axis)
        etl.stop_and_zero()
        enable_backplane_trigger_mode(tiger, card_addr=args.etl_card_addr)
        etl.configure(pattern=PATTERN_SAWTOOTH, amplitude_v=args.etl_amplitude,
                      offset_v=args.etl_offset, period_ms=args.etl_period_ms, external_trigger=True)

        slot = axis_slot_index(args.etl_axis, args.etl_card_first_axis)
        etl_trigger_addr = trigger_in_backplane_addr(slot)
        print(f"Routing Expose-Out's RAW LEVEL -> ETL trigger-in (backplane addr {etl_trigger_addr})...")
        # NOT rising_edge(expose_addr) -- that's a brief (~250us) PLC-internal pulse, likely too
        # short for the ETL axis's own SAM=2 edge detection to reliably catch. Every previously
        # confirmed-working ETL trigger in this project used a genuine, SUSTAINED level change on
        # the backplane line, letting the axis card do its own edge detection -- not a PLC-computed
        # brief pulse. Expose-Out is already high for the whole exposure, a real sustained
        # transition the ETL card's hardware can actually catch.
        plc.configure_io(etl_trigger_addr, IO_TYPE_PUSH_PULL_OUTPUT, source_addr=expose_addr)

        print("Arming ETL (SAM=2, confirmed auto-rearming)...")
        etl.arm_triggered(free_running=False)

        print(f"\nWatching for {args.watch_s:.0f}s -- observe the scope now...")
        time.sleep(args.watch_s)

        resp = input("\nDid the ETL ramp start in sync with Expose-Out's rising edge, consistently "
                      "across multiple camera cycles? [y/N] ").strip().lower()
        ok = resp.startswith("y")
        print(f"\n=== Result ===")
        if ok:
            print("PASS: ETL correctly triggers from the camera's real Expose-Out rising edge. "
                  "Step 2 of the staged test plan complete.")
        else:
            print("Did not confirm sync -- worth checking: is the camera actually free-running "
                  "(generating repeated exposures) during the watch window? Is --etl-period-ms set "
                  "with enough margin below the camera's real exposure interval (see the earlier "
                  "confirmed margin requirement)? Describe exactly what you saw for next steps.")

        etl.stop_and_zero()

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if etl is not None:
            try:
                etl.stop_and_zero()
            except Exception as exc:
                print(f"WARNING: could not stop/zero ETL: {exc}")
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
