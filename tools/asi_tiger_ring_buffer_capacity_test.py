#!/usr/bin/env python3
"""
asi_tiger_ring_buffer_capacity_test.py
=========================================
Tests whether the Z-stage ring buffer NATURALLY provides "N triggers
then stop" behavior once its loaded points are exhausted -- BEFORE
attempting to design any custom PLC counter-cell logic for this.

Why this matters: the acquisition design (see design doc) calls for
gating a self-sustaining Z-move-complete -> next-frame-trigger hardware
loop so it runs exactly N times (N = that row's plane count) and then
stops. The "obvious" way to build this is custom PLC flip-flop/one-shot
counter logic -- but that's exactly the kind of sequential-logic design
that's easy to get subtly wrong, and a mistake here means a REAL
Z-stage overrun, not just a software bug. I could not find ASI's own
documented "N pulses then stop" example to build from with confidence.

Before building anything that complex, this script tests a much
simpler hypothesis: maybe the ring buffer itself already does this for
free. If you LOAD exactly N points and then trigger it N+2 times, does
it:
  (a) move N times and then simply stop responding to further triggers
      (nothing queued -- exactly the "N then stop" behavior we want,
      for free, no counter cells needed), or
  (b) do something else (error, wrap and repeat the last point
      indefinitely, or something unexpected)?

This uses ONLY the software self-test (bare RM, no wiring/electrical
signal involved) -- safe to run repeatedly, no jumpers needed. It DOES
move the real Z stage a small real amount per successful trigger.

Usage:
    python tools/asi_tiger_ring_buffer_capacity_test.py --port COM4 --n-points 3 --n-triggers 6
    python tools/asi_tiger_ring_buffer_capacity_test.py --port COM4 --n-points 5 --n-triggers 8
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import TigerController, StageRingBuffer


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--z-card-addr", type=int, default=32)
    parser.add_argument("--z-axis", default="Z")
    parser.add_argument("--z-axis-mask", type=int, default=1)
    parser.add_argument("--step", type=float, default=10.0,
                         help="Relative step per point, tenths of a micron (default 10 = 1 micron)")
    parser.add_argument("--n-points", type=int, default=3,
                         help="How many ring-buffer points to load (this is the 'N' we're testing)")
    parser.add_argument("--n-triggers", type=int, default=6,
                         help="How many software triggers to send -- should be MORE than --n-points, "
                              "to see what happens once the buffer is exhausted")
    parser.add_argument("--settle-s", type=float, default=0.3)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    if args.n_triggers <= args.n_points:
        print(f"WARNING: --n-triggers ({args.n_triggers}) should be MORE than --n-points "
              f"({args.n_points}) to actually test what happens after exhaustion -- "
              f"otherwise this just confirms normal operation, not the stop behavior.")

    print(f"Loading {args.n_points} points ({args.step} tenths-of-micron each) into the Z ring buffer, "
          f"then sending {args.n_triggers} software triggers.")
    print("Expected IF the ring buffer provides free 'N then stop' behavior: position advances for "
          f"the first {args.n_points} triggers, then holds steady for the remaining "
          f"{args.n_triggers - args.n_points}.")

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

        z.clear()
        for _ in range(args.n_points):
            z.load_relative_step(args.step)
        z.arm_relative()

        print(f"\n{args.n_points} points loaded. Triggering {args.n_triggers} times...\n")
        positions = [z.where()]
        print(f"  Start:      {positions[0]}")

        for i in range(1, args.n_triggers + 1):
            z.software_trigger()
            time.sleep(args.settle_s)
            pos = z.where()
            positions.append(pos)
            moved = positions[-1] != positions[-2]
            marker = ""
            if i == args.n_points:
                marker = "  <-- last expected move"
            elif i == args.n_points + 1:
                marker = "  <-- first trigger PAST the loaded count -- did it move or hold?"
            print(f"  Trigger {i}: {pos}  ({'moved' if moved else 'held'}){marker}")

        z.disarm()

        moved_count = sum(1 for i in range(1, len(positions)) if positions[i] != positions[i - 1])
        print(f"\n=== Result ===")
        print(f"Total moves observed: {moved_count} (expected if hypothesis holds: exactly {args.n_points})")
        if moved_count == args.n_points:
            print("MATCHES hypothesis: the ring buffer naturally stopped once its loaded points were "
                  "exhausted -- no custom PLC counter-cell logic needed for 'N triggers then stop'. "
                  "Worth confirming this is consistent across a couple of different N values before "
                  "relying on it for real acquisitions.")
        else:
            print("Does NOT match the simple hypothesis -- something else happened (wraparound, error, "
                  "or an off-by-one). Custom PLC counter logic will likely be needed after all -- look "
                  "closely at exactly which trigger(s) behaved unexpectedly above, that detail matters "
                  "for designing the real fix.")

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
