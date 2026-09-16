#!/usr/bin/env python3
"""
asi_tiger_z_camera_sync_test.py
==================================
Step 3 of the staged full-loop test plan (see PATCHNOTES_ASI_TIGER.md).
Confirms Z steps in sync with the camera's real Expose-Out falling
edge -- with a PRECISE, KNOWN, host-controlled trigger count, not a
free-running camera and a manually-typed frame counter.

This replaces an earlier version that ran the camera free-running and
asked the user to type PVCAMTest's frame counter before/after a watch
window -- correctly identified as imprecise: a human reading a counter
off a free-running camera is a race-prone, approximate measurement,
not a real ground truth. This version instead DRIVES the camera itself
with an exact number of pulses -- the same confirmed-working mechanism
from asi_tiger_camera_trigger_test.py (PLC BNC -> camera trigger
input) -- so "how many real exposures occurred" is known exactly by
construction, not estimated by a human. Combined with a clean,
disarmed, settled final position reading (unaffected by any WHERE
polling reliability issues during active triggering, confirmed as a
real issue in earlier testing), the comparison against N x step is
fully precise, with no ambiguity from either side.

REQUIRED: camera in EXTERNAL TRIGGER mode (not free-running) in
PVCAMTest before starting -- this script IS the trigger source.

REQUIRED WIRING (three jumpers):
  - PLC BNC (--camera-trigger-bnc, default 4) -> camera trigger input
    (same as asi_tiger_camera_trigger_test.py, confirmed working there)
  - camera Expose-Out -> PLC BNC (--camera-expose-bnc, default 3)
  - PLC BNC (--z-trigger-bnc, default 1) -> Z card's IN0 connector

CONFIRMED ON REAL HARDWARE (carried over from earlier testing): Z's
trigger needs a genuine brief PULSE (falling_edge()), not a sustained
level -- this script uses that confirmed-correct signal shape.

--trigger-spacing-s MUST be longer than your camera's real full
exposure+readout cycle, or triggers can queue/overlap -- default is a
generous 2.0s; narrow it down once you've confirmed correctness at a
safe spacing, not before.

Usage:
    python tools/asi_tiger_z_camera_sync_test.py --port COM4
    python tools/asi_tiger_z_camera_sync_test.py --port COM4 --n-triggers 10 --trigger-spacing-s 1.0
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import (
    TigerController, PLCCard, StageRingBuffer,
    bnc_addr, cell_addr, falling_edge, IO_TYPE_INPUT, IO_TYPE_PUSH_PULL_OUTPUT,
)


def parse_position(where_reply: str) -> float:
    return float(where_reply.split("=")[-1])


def read_position_robust(z, retries: int = 5, retry_delay_s: float = 0.05):
    """
    z.where() can return an empty reply (a bare ':A' with no position
    data attached) -- CONFIRMED ON REAL HARDWARE, during active
    high-rate electrical triggering. Retries briefly rather than
    crashing or misreading it; returns None if all retries exhaust.
    """
    for attempt in range(retries):
        try:
            reply = z.where()
            return parse_position(reply)
        except Exception:
            if attempt < retries - 1:
                time.sleep(retry_delay_s)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--plc-card-addr", type=int, default=36)
    parser.add_argument("--plc-axis", default="E")
    parser.add_argument("--camera-trigger-bnc", type=int, default=4,
                         help="PLC BNC jumpered to the camera's trigger input")
    parser.add_argument("--camera-expose-bnc", type=int, default=3,
                         help="PLC BNC jumpered from the camera's Expose-Out signal")
    parser.add_argument("--z-trigger-bnc", type=int, default=1,
                         help="PLC BNC jumpered to Z card's IN0 connector")
    parser.add_argument("--trigger-source-cell", type=int, default=6)
    parser.add_argument("--camera-pulse-width-ms", type=float, default=10.0,
                         help="Camera trigger pulse width -- matches the confirmed-working "
                              "default from asi_tiger_camera_trigger_test.py")
    parser.add_argument("--z-card-addr", type=int, default=32)
    parser.add_argument("--z-axis", default="Z")
    parser.add_argument("--z-axis-mask", type=int, default=1)
    parser.add_argument("--step", type=float, default=10.0,
                         help="Relative step, tenths of a micron (default 10 = 1 micron)")
    parser.add_argument("--n-triggers", type=int, default=5,
                         help="Exact number of camera triggers to fire -- start small")
    parser.add_argument("--trigger-spacing-s", type=float, default=2.0,
                         help="Delay between camera triggers -- MUST exceed your camera's real "
                              "full exposure+readout cycle or triggers can queue/overlap")
    parser.add_argument("--final-settle-s", type=float, default=1.0,
                         help="Extra wait after the last trigger before the final clean reading, "
                              "to make sure the last exposure's Expose-Out falling edge has "
                              "definitely already happened")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    print(f"Precise Z-camera sync test: firing exactly {args.n_triggers} camera triggers, "
          f"{args.trigger_spacing_s:.1f}s apart.")
    print(f"Wiring: PLC BNC{args.camera_trigger_bnc} -> camera trigger; camera Expose-Out -> "
          f"PLC BNC{args.camera_expose_bnc}; PLC BNC{args.z_trigger_bnc} -> Z's IN0.")
    print("REQUIRED: camera must be in EXTERNAL TRIGGER mode (not free-running) in PVCAMTest.")
    print(f"Expected: Z advances by exactly {args.n_triggers * args.step / 10:.2f} micron total "
          f"({args.n_triggers} x {args.step/10:.2f} micron).")

    if not args.yes:
        resp = input("\nConfirm camera is in external trigger mode, all three jumpers connected, "
                      "and nothing is in the way of Z moving. Continue? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    z = plc = None
    try:
        tiger.connect()
        plc = PLCCard(tiger, card_addr=args.plc_card_addr, axis=args.plc_axis)
        z = StageRingBuffer(tiger, card_addr=args.z_card_addr, axis=args.z_axis, axis_mask=args.z_axis_mask)

        expose_addr = bnc_addr(args.camera_expose_bnc)
        plc.configure_io(expose_addr, IO_TYPE_INPUT)

        print(f"\nArming Z (single relative step)...")
        z.clear()
        z.load_relative_step(args.step)
        z.arm_relative(settle_s=0.3)

        z_trigger_addr = bnc_addr(args.z_trigger_bnc)
        print(f"Routing Expose-Out's falling edge (brief pulse) -> Z trigger (BNC{args.z_trigger_bnc})...")
        plc.configure_io(z_trigger_addr, IO_TYPE_PUSH_PULL_OUTPUT, source_addr=falling_edge(expose_addr))

        src = args.trigger_source_cell
        plc.configure_cell(src, "d_flop", inputs={"a": 0, "b": 0, "c": 0})
        plc.set_cell_state(src, False)
        plc.configure_io(bnc_addr(args.camera_trigger_bnc), IO_TYPE_PUSH_PULL_OUTPUT,
                          source_addr=cell_addr(src))

        start_pos = read_position_robust(z)
        if start_pos is None:
            print("ERROR: could not read starting position even with retries -- aborting.")
            return
        print(f"\nStart position: {start_pos}. Firing {args.n_triggers} camera triggers...")

        for i in range(1, args.n_triggers + 1):
            plc.set_cell_state(src, True)
            time.sleep(args.camera_pulse_width_ms / 1000.0)
            plc.set_cell_state(src, False)
            print(f"  Trigger {i}/{args.n_triggers} sent.")
            if i < args.n_triggers:
                time.sleep(args.trigger_spacing_s)

        print(f"\nAll triggers sent. Waiting {args.final_settle_s:.1f}s for the last exposure to "
              f"fully complete...")
        time.sleep(args.trigger_spacing_s + args.final_settle_s)

        z.disarm()
        time.sleep(0.5)
        final_pos = read_position_robust(z)

        print(f"\n=== Result ===")
        if final_pos is None:
            print("Could not get a clean final reading even after retrying -- something else may "
                  "be wrong, worth investigating before trusting this axis for triggered acquisition.")
            return

        total_displacement = final_pos - start_pos
        expected = args.n_triggers * args.step
        diff = total_displacement - expected

        print(f"Start: {start_pos}, Final: {final_pos}, total displacement: {total_displacement:+.2f}")
        print(f"Expected ({args.n_triggers} triggers x {args.step} step): {expected:+.2f}")
        if abs(diff) < 1e-6:
            print(f"MATCH: Z moved by exactly the expected amount for {args.n_triggers} precisely-known "
                  f"camera triggers. No ambiguity -- the trigger count here is exact by construction, "
                  f"not estimated. Step 3 of the staged test plan is DONE.")
        else:
            print(f"MISMATCH: {diff:+.2f} unaccounted for ({diff/args.step:+.1f} steps' worth). This is "
                  f"a real, precise discrepancy -- the trigger count is known exactly, so this can't be "
                  f"explained by measurement/counting ambiguity the way the earlier free-running "
                  f"approach could. Worth checking: did --trigger-spacing-s definitely exceed your "
                  f"camera's real exposure+readout cycle (a too-short spacing could cause triggers to "
                  f"queue/overlap)?")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if z is not None:
            try:
                z.disarm()
            except Exception as exc:
                print(f"WARNING: could not disarm Z: {exc}")
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
