#!/usr/bin/env python3
"""
asi_tiger_ring_buffer_wraparound_test.py
===========================================
Tests the ACTUAL documented ring buffer behavior: it WRAPS AROUND, it
does not exhaust and stop. From ASI's own ring buffer documentation
(https://asiimaging.com/docs/ring_buffer): "Each press of the @ button
causes the stage to advance to the next position. When you reach the
last position, the next press... will take you back to the first
position."

This corrects an earlier, wrong hypothesis tested by a previous version
of this script (that N loaded points would naturally stop responding
after N triggers, "N then stop" for free). That was untested and turned
out to be false -- the ring buffer is a genuine circular buffer, not a
queue that exhausts. This matters for the acquisition design: it means
the PLC pulse-pass-through counter IS the mechanism that gates "stop
after N frames" -- the ring buffer will happily repeat forever on its
own, by design, and that's not a bug to work around.

It also means, usefully: with exactly ONE relative step loaded, every
trigger wraps to that same single entry -- so uniform Z-stepping (the
real use case) never needs more than 1 loaded point regardless of how
many planes are in the stack, sidestepping the buffer's 50/250-position
capacity limit entirely. Recommended architecture:
  - Row-level (once, slow): absolute MOVE to Z_start.
  - Row-level (once, slow): LOAD exactly one relative step, arm TTL X=12.
  - The PLC pulse-pass-through counter is what stops it after N frames.

Two tests:
  1. Single-entry consistency: load ONE relative step, trigger it many
     times, confirm every trigger moves by exactly that same amount --
     no ceiling, no degradation, matching the recommended architecture
     above directly.
  2. Multi-entry wraparound proof: load several DISTINCT step values,
     trigger through and past the full set more than once, confirm the
     sequence of deltas repeats in the same order -- directly
     demonstrates wraparound rather than exhaustion, on the record.

SAFETY: moves the real Z stage a small real amount per trigger.

Usage:
    python tools/asi_tiger_ring_buffer_wraparound_test.py --port COM4
    python tools/asi_tiger_ring_buffer_wraparound_test.py --port COM4 --skip-multi-entry
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import TigerController, StageRingBuffer


def parse_position(where_reply: str) -> float:
    """WHERE replies look like 'Z=1234.0' -- extract the numeric value."""
    return float(where_reply.split("=")[-1])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--z-card-addr", type=int, default=32)
    parser.add_argument("--z-axis", default="Z")
    parser.add_argument("--z-axis-mask", type=int, default=1)
    parser.add_argument("--step", type=float, default=10.0,
                         help="Relative step, tenths of a micron (default 10 = 1 micron)")
    parser.add_argument("--n-triggers-single", type=int, default=20,
                         help="How many triggers for the single-entry consistency test")
    parser.add_argument("--multi-entry-steps", default="10,20,30",
                         help="Comma-separated DISTINCT relative steps for the wraparound proof")
    parser.add_argument("--multi-entry-cycles", type=int, default=3,
                         help="How many full cycles through the multi-entry set to trigger")
    parser.add_argument("--skip-multi-entry", action="store_true")
    parser.add_argument("--settle-s", type=float, default=0.3)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    print(f"Test 1: load ONE relative step ({args.step} tenths-of-micron), trigger "
          f"{args.n_triggers_single} times, confirm every trigger moves by exactly that amount.")
    if not args.skip_multi_entry:
        multi_steps = [float(s) for s in args.multi_entry_steps.split(",")]
        print(f"Test 2: load {len(multi_steps)} distinct steps {multi_steps}, trigger through "
              f"{args.multi_entry_cycles} full cycles, confirm the delta sequence repeats "
              f"(proves wraparound, not exhaustion).")

    if not args.yes:
        resp = input("\nThis moves the real Z stage. Confirm nothing is in the way. "
                      "Continue? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    z = None
    try:
        tiger.connect()
        z = StageRingBuffer(tiger, card_addr=args.z_card_addr, axis=args.z_axis, axis_mask=args.z_axis_mask)

        # --- Test 1: single-entry consistency ---
        print(f"\n--- Test 1: single-entry, {args.n_triggers_single} triggers ---")
        z.clear()
        z.load_relative_step(args.step)
        pos_after_load = parse_position(z.where())
        idx_after_load = z.query_read_index()
        z.arm_relative()
        pos_after_arm = parse_position(z.where())
        idx_after_arm = z.query_read_index()
        print(f"  After load: pos={pos_after_load}  read_index={idx_after_load}")
        print(f"  After arm (BEFORE any trigger): pos={pos_after_arm}  read_index={idx_after_arm}")
        if pos_after_arm != pos_after_load:
            print(f"  **arm_relative() itself moved the axis** ({pos_after_load} -> {pos_after_arm}) "
                  f"-- this alone, before any trigger, would explain a first-trigger offset.")

        last_pos = pos_after_arm
        deltas = []
        for i in range(1, args.n_triggers_single + 1):
            idx_before = z.query_read_index()
            z.software_trigger()
            time.sleep(args.settle_s)
            pos = parse_position(z.where())
            idx_after = z.query_read_index()
            delta = pos - last_pos
            deltas.append(delta)
            last_pos = pos
            marker = "" if abs(delta - args.step) < 1e-6 else "  **UNEXPECTED**"
            print(f"  Trigger {i:3d}: read_index {idx_before}->{idx_after}  delta={delta:+.2f}{marker}")
        z.disarm()

        test1_ok = all(abs(d - args.step) < 1e-6 for d in deltas)
        print(f"\nTest 1 result: {'PASS' if test1_ok else 'FAIL'} -- "
              f"{'every trigger moved by exactly the loaded step, no ceiling, no degradation' if test1_ok else 'see UNEXPECTED markers above'}")

        # --- Test 2: multi-entry wraparound proof ---
        test2_ok = True
        if not args.skip_multi_entry:
            multi_steps = [float(s) for s in args.multi_entry_steps.split(",")]
            n_total = len(multi_steps) * args.multi_entry_cycles
            print(f"\n--- Test 2: {len(multi_steps)} distinct entries, {args.multi_entry_cycles} "
                  f"full cycles ({n_total} triggers) ---")
            z.clear()
            for s in multi_steps:
                z.load_relative_step(s)
            idx_after_load = z.query_read_index()
            z.arm_relative()
            pos_after_arm = parse_position(z.where())
            idx_after_arm = z.query_read_index()
            print(f"  After load: read_index={idx_after_load}")
            print(f"  After arm (BEFORE any trigger): pos={pos_after_arm}  read_index={idx_after_arm} "
                  f"(expect 0 -- clear() now explicitly resets this)")

            last_pos = pos_after_arm
            deltas = []
            for i in range(1, n_total + 1):
                idx_before = z.query_read_index()
                z.software_trigger()
                time.sleep(args.settle_s)
                pos = parse_position(z.where())
                idx_after = z.query_read_index()
                delta = pos - last_pos
                deltas.append(delta)
                last_pos = pos
                expected = multi_steps[(i - 1) % len(multi_steps)]
                marker = "" if abs(delta - expected) < 1e-6 else "  **UNEXPECTED**"
                print(f"  Trigger {i:3d}: read_index {idx_before}->{idx_after}  delta={delta:+.2f} "
                      f"(expected {expected:+.2f}, entry {(i - 1) % len(multi_steps) + 1}/{len(multi_steps)}){marker}")
            z.disarm()

            test2_ok = all(
                abs(deltas[i] - multi_steps[i % len(multi_steps)]) < 1e-6 for i in range(n_total)
            )
            print(f"\nTest 2 result: {'PASS' if test2_ok else 'FAIL'} -- "
                  f"{'delta sequence repeated exactly as expected, confirming wraparound' if test2_ok else 'see UNEXPECTED markers above'}")

        print(f"\n=== Overall ===")
        if test1_ok and test2_ok:
            print("Confirmed: the ring buffer wraps around rather than exhausting. A single loaded "
                  "relative step, triggered repeatedly, is sufficient for uniform Z-stepping regardless "
                  "of stack size -- the buffer's 50/250-position capacity limit doesn't apply to this "
                  "use case. The PLC pulse-pass-through counter is what actually gates 'stop after N "
                  "frames' in this design; the ring buffer itself will not stop on its own.")
        else:
            print("Did NOT confirm the expected wraparound behavior -- see the FAIL result(s) above for "
                  "exactly what happened instead. Worth understanding before relying on the single-entry "
                  "architecture for real acquisitions.")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if z is not None:
            try:
                z.disarm()
            except Exception as exc:
                print(f"WARNING: could not disarm: {exc}")
        if tiger.is_connected:
            tiger.disconnect()
            print("Disconnected.")


if __name__ == "__main__":
    main()
