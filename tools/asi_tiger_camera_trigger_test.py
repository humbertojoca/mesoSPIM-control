#!/usr/bin/env python3
"""
asi_tiger_camera_trigger_test.py
===================================
Tests driving the camera's PHYSICAL TRIGGER INPUT from the PLC --
genuinely new, untested territory in this project. Everything touching
the camera so far has only ever READ Expose-Out; this is the first
time anything drives a signal INTO the camera. Required before trusting
configure_zstack_trigger_chain()'s camera_trigger_bnc.

The camera needs to be configured for EXTERNAL TRIGGER mode in
PVCAMTest (or whatever software you're using) BEFORE running this --
this script cannot configure the camera itself, only drive its trigger
BNC. Watch PVCAMTest's live view / frame counter to confirm each pulse
actually captures a frame -- this script cannot verify that itself,
you have to.

Two stages:
  1. Single pulse -- confirms ANY response at all.
  2. Several well-spaced repeated pulses -- confirms reliable
     per-trigger response, not just the first (same caution applied to
     every other trigger destination in this project).

REQUIRED PHYSICAL WIRING: PLC BNC (--camera-trigger-bnc) -> camera's
physical trigger input.

Pulse width/polarity are NOT confirmed for this specific camera model
-- --pulse-width-ms starts at a generous default; narrow down once you
know the camera actually responds, don't assume this default is
correct or optimal for production use.

Usage:
    python tools/asi_tiger_camera_trigger_test.py --port COM4
    python tools/asi_tiger_camera_trigger_test.py --port COM4 --camera-trigger-bnc 4 --n-pulses 5
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import TigerController, PLCCard, bnc_addr, cell_addr, IO_TYPE_PUSH_PULL_OUTPUT


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--plc-card-addr", type=int, default=36)
    parser.add_argument("--plc-axis", default="E")
    parser.add_argument("--camera-trigger-bnc", type=int, default=4,
                         help="PLC BNC jumpered to the camera's physical trigger input")
    parser.add_argument("--trigger-source-cell", type=int, default=6)
    parser.add_argument("--pulse-width-ms", type=float, default=10.0,
                         help="How long to hold the trigger pulse high. NOT confirmed for this "
                              "camera model -- a generous starting point, narrow down once you "
                              "confirm the camera actually responds.")
    parser.add_argument("--n-pulses", type=int, default=5,
                         help="How many repeated, well-spaced pulses for stage 2")
    parser.add_argument("--spacing-s", type=float, default=1.0)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    print(f"Testing camera trigger via PLC BNC{args.camera_trigger_bnc}.")
    print("BEFORE continuing: confirm the camera is configured for EXTERNAL TRIGGER mode in "
          "PVCAMTest (or your camera software), and that you're watching its live view/frame "
          "counter to see whether each pulse actually captures a frame -- this script cannot "
          "verify that itself.")

    if not args.yes:
        resp = input("\nConfirm wiring and camera trigger mode are ready. Continue? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    plc = None
    try:
        tiger.connect()
        plc = PLCCard(tiger, card_addr=args.plc_card_addr, axis=args.plc_axis)

        src = args.trigger_source_cell
        plc.configure_cell(src, "d_flop", inputs={"a": 0, "b": 0, "c": 0})
        plc.set_cell_state(src, False)
        plc.configure_io(bnc_addr(args.camera_trigger_bnc), IO_TYPE_PUSH_PULL_OUTPUT,
                          source_addr=cell_addr(src))

        print(f"\n--- Stage 1: single pulse ({args.pulse_width_ms:.1f}ms) ---")
        input("Press Enter to fire one pulse, then check PVCAMTest for a captured frame... ")
        plc.set_cell_state(src, True)
        time.sleep(args.pulse_width_ms / 1000.0)
        plc.set_cell_state(src, False)
        resp = input("Did a frame get captured? [y/N] ").strip().lower()
        stage1_ok = resp.startswith("y")
        print(f"Stage 1: {'PASS' if stage1_ok else 'FAIL'}")

        if not stage1_ok:
            print("\nNo response to a single pulse -- before assuming the PLC side is wrong, check: "
                  "is the camera actually in external trigger mode? Is the wiring the correct "
                  "polarity (this drives the BNC high then low -- some triggers expect the "
                  "opposite, active-low)? Try a longer --pulse-width-ms in case this camera needs "
                  "more time to recognize it.")
            return

        print(f"\n--- Stage 2: {args.n_pulses} repeated pulses, {args.spacing_s:.1f}s apart ---")
        print("Watch PVCAMTest's frame counter -- it should increase by exactly 1 per pulse.")
        input("Press Enter to begin... ")
        for i in range(1, args.n_pulses + 1):
            plc.set_cell_state(src, True)
            time.sleep(args.pulse_width_ms / 1000.0)
            plc.set_cell_state(src, False)
            print(f"  Pulse {i} sent.")
            time.sleep(args.spacing_s)

        resp = input(f"\nDid the frame counter increase by exactly {args.n_pulses}? [y/N] ").strip().lower()
        stage2_ok = resp.startswith("y")
        print(f"Stage 2: {'PASS' if stage2_ok else 'FAIL'}")

        print(f"\n=== Result ===")
        if stage1_ok and stage2_ok:
            print(f"PASS: camera reliably triggers on PLC BNC{args.camera_trigger_bnc}, pulse width "
                  f"{args.pulse_width_ms:.1f}ms. Safe to use this BNC as camera_trigger_bnc in "
                  f"configure_zstack_trigger_chain().")
        else:
            print("Did not fully pass -- do not wire this into configure_zstack_trigger_chain() yet. "
                  "Narrow down pulse width/polarity/wiring before retrying.")

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
