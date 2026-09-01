#!/usr/bin/env python3
"""
asi_tiger_stage_ring_buffer_test.py
======================================
Tests the Z-stage ring-buffer trigger mechanism (asi_tiger.stage_trigger,
TTL X=12 relative mode). Confirmed on real hardware in prior testing via
a separate diagnostic script: both a software-simulated trigger (bare
RM) and a real electrical pulse (PLC BNC jumpered to the Z card's
physical TRIG IN) moved the stage correctly. This script re-implements
that same confirmed sequence through the library, with the same
safety/confirmation conventions used elsewhere in this project.

Two-stage test, matching the diagnostic script's own recommended order:
  1. Software self-test (bare RM) -- confirms RM/LOAD/TTL config is
     correct, independent of any wiring.
  2. Electrical test -- requires a physical jumper from a PLC BNC to
     the Z card's TRIG IN. Confirms the physical/electrical path.

SAFETY: this moves the Z stage by a real, small amount (default 1
micron) on each trigger. Confirm nothing is in the way before running.

Usage:
    python tools/asi_tiger_stage_ring_buffer_test.py --port COM4
    python tools/asi_tiger_stage_ring_buffer_test.py --port COM4 --skip-electrical
    python tools/asi_tiger_stage_ring_buffer_test.py --port COM4 --electrical-trigger-bnc 1
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import TigerController, StageRingBuffer, PLCCard, bnc_addr, cell_addr, IO_TYPE_PUSH_PULL_OUTPUT


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--z-card-addr", type=int, default=32)
    parser.add_argument("--z-axis", default="Z")
    parser.add_argument("--z-axis-mask", type=int, default=1,
                         help="Bit0 = first axis listed on this card (default 1 = Z on 'Z,T')")
    parser.add_argument("--step", type=float, default=10.0,
                         help="Relative step per trigger, in tenths of a micron (default 10 = 1 micron)")
    parser.add_argument("--skip-electrical", action="store_true",
                         help="Only run the software self-test, skip the jumpered electrical test")
    parser.add_argument("--plc-card-addr", type=int, default=36)
    parser.add_argument("--plc-axis", default="E")
    parser.add_argument("--trigger-source-cell", type=int, default=8,
                         help="PLC cell used to manually pulse the electrical test")
    parser.add_argument("--electrical-trigger-bnc", type=int, default=1,
                         help="PLC BNC to jumper to the Z card's TRIG IN for the electrical test")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    print(f"Z ring buffer: card {args.z_card_addr}, axis {args.z_axis}, mask {args.z_axis_mask}, "
          f"step {args.step} (tenths-of-micron, i.e. {args.step/10:.2f} micron per trigger)")

    if not args.yes:
        resp = input("\nThis will move the Z stage by a small real amount on each trigger. "
                      "Confirm nothing is in the way. Continue? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    z = plc = None
    try:
        tiger.connect()
        z = StageRingBuffer(tiger, card_addr=args.z_card_addr, axis=args.z_axis, axis_mask=args.z_axis_mask)

        print("\n--- Software self-test (bare RM, no wiring involved) ---")
        z.clear()
        z.load_relative_step(args.step)
        z.arm_relative()
        before = z.where()
        z.software_trigger()
        time.sleep(0.3)
        after = z.where()
        print(f"  Before: {before}  After: {after}")
        sw_ok = before != after
        print(f"  {'PASS' if sw_ok else 'FAIL'}: software self-test {'moved' if sw_ok else 'did NOT move'} the axis.")
        z.disarm()

        if not sw_ok:
            print("\nConfig problem -- fix RM/LOAD/TTL setup before testing wiring. Stopping here.")
            return

        if args.skip_electrical:
            print("\n--skip-electrical passed -- stopping after the software self-test.")
            return

        print(f"\n--- Electrical test (PLC BNC{args.electrical_trigger_bnc} -> jumper -> Z card TRIG IN) ---")
        input(f"  Confirm the jumper is connected, then press Enter... ")

        plc = PLCCard(tiger, card_addr=args.plc_card_addr, axis=args.plc_axis)
        z.clear()
        z.load_relative_step(args.step)
        z.arm_relative()
        before = z.where()

        # Route the trigger-source cell onto the BNC, then pulse the cell high->low
        plc.set_cell_state(args.trigger_source_cell, False)
        plc.configure_io(bnc_addr(args.electrical_trigger_bnc), IO_TYPE_PUSH_PULL_OUTPUT,
                          source_addr=cell_addr(args.trigger_source_cell))
        plc.set_cell_state(args.trigger_source_cell, True)
        time.sleep(0.2)
        plc.set_cell_state(args.trigger_source_cell, False)
        time.sleep(0.3)

        after = z.where()
        print(f"  Before: {before}  After: {after}")
        elec_ok = before != after
        print(f"  {'PASS' if elec_ok else 'FAIL'}: electrical test {'moved' if elec_ok else 'did NOT move'} the axis.")
        z.disarm()
        plc.safe_all_outputs()

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if z is not None:
            try:
                z.disarm()
            except Exception as exc:
                print(f"WARNING: could not disarm Z ring buffer: {exc}")
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
