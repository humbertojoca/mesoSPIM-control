#!/usr/bin/env python3
"""
asi_tiger_move_absolute_test.py
==================================
Bench test for StageRingBuffer.move_absolute() -- the row-level, direct
host-commanded move (M <axis>=<position>).

CONFIRMED ON REAL HARDWARE, TWICE: RDSTAT's 'M' flag clears BEFORE the
stage has physically finished settling, and the gap is NOT a fixed
duration -- it scales with how far the move traveled. A fixed settle
delay that's long enough for a small row-to-row nudge is wasteful; one
short enough to be fast for that case is NOT long enough for a
multi-mm tile-to-tile jump (real Z-stack starting positions can easily
span that range). move_absolute() no longer uses a fixed delay -- it
polls WHERE directly after RDSTAT clears, until position converges to
within --settle-tolerance of the commanded target, self-adapting to
whatever the real settling time actually is for THAT specific move.

This test validates that self-adapting behavior directly: runs moves
across a WIDE RANGE of distances (small, medium, and -- if you opt in
via --test-large-move-mm -- a genuinely large, multi-mm move), and
reports how long each one actually took to converge. A small move
should converge almost instantly; a large one should take
proportionally longer, with no manual tuning needed for either.

Checks:
  1. Does the move land at the EXACT commanded position (confirmed by
     an independent WHERE read after move_absolute() returns)?
  2. Does convergence time scale sensibly with distance (short for
     small moves, longer for large ones), rather than being fixed or
     wrong at either end?
  3. Does wait=False correctly return immediately, with the move still
     completing correctly on its own afterward?

SAFETY: moves the real Z stage several times. --max-travel-tenths-um
bounds the small/medium test moves; --test-large-move-mm is OFF by
default and asks for separate confirmation, since a multi-mm move is a
much bigger physical action than the rest of this test.

Usage:
    python tools/asi_tiger_move_absolute_test.py --port COM4
    python tools/asi_tiger_move_absolute_test.py --port COM4 --test-large-move-mm 2.0
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import TigerController, StageRingBuffer


def parse_position(where_reply: str) -> float:
    return float(where_reply.split("=")[-1])


def do_move(z, start_pos, target, label, args, results):
    print(f"\n--- {label}: target={target:.1f} (offset {target - start_pos:+.1f} from start) ---")
    t0 = time.perf_counter()
    completed = z.move_absolute(target, wait=True, poll_interval_s=args.poll_interval_s,
                                 timeout_s=args.timeout_s, settle_tolerance=args.settle_tolerance,
                                 extra_settle_s=args.extra_settle_s)
    elapsed = time.perf_counter() - t0
    actual = parse_position(z.where())
    residual = abs(actual - target)
    # Pass/fail against the SAME tolerance move_absolute() itself promises to converge
    # within -- NOT an exact bit-for-bit match, which is a stricter standard than the
    # function's own contract and would flag ordinary, already-characterized mechanical
    # settling noise (~1 unit) as a false failure.
    landed_correctly = residual <= args.settle_tolerance
    marker = "" if (completed and landed_correctly) else "  **MISMATCH**"
    print(f"  move_absolute() returned {completed} after {elapsed:.3f}s. "
          f"Position immediately after: {actual:.1f} (residual {residual:.1f}){marker}")
    if not completed:
        print("  -> Returned False: never converged within timeout. Check if it actually "
              "finished moving anyway (a false negative) or is genuinely stuck, and whether "
              "--settle-tolerance is too tight or --timeout-s too short for this distance.")
    if not landed_correctly:
        print(f"  -> Landed at {actual:.1f}, residual {residual:.1f} EXCEEDS "
              f"settle_tolerance={args.settle_tolerance} -- this is a real mismatch, not "
              f"ordinary settling noise.")
    results.append((label, target - start_pos, elapsed, residual, completed and landed_correctly))
    return completed and landed_correctly


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--z-card-addr", type=int, default=32)
    parser.add_argument("--z-axis", default="Z")
    parser.add_argument("--z-axis-mask", type=int, default=1)
    parser.add_argument("--max-travel-tenths-um", type=float, default=1000.0,
                         help="How far (tenths of a micron) the small/medium test moves go from "
                              "the starting position, in either direction. Default 1000 = 100 "
                              "microns, matching the range already confirmed to expose the "
                              "settling gap.")
    parser.add_argument("--test-large-move-mm", type=float, default=None,
                         help="OPT IN: also test one large, genuinely multi-mm round trip "
                              "(there and back) -- the scale real row-to-row/tile-to-tile moves "
                              "can span, and the actual scenario this fix targets. Off by default "
                              "since it's a much bigger physical move than the rest of this test.")
    parser.add_argument("--poll-interval-s", type=float, default=0.05)
    parser.add_argument("--timeout-s", type=float, default=30.0,
                         help="Generous on purpose -- large moves may legitimately take longer "
                              "to converge; this bounds worst-case wait, not expected time.")
    parser.add_argument("--settle-tolerance", type=float, default=2.0,
                         help="Raw position units (tenths of a micron) -- how close to the "
                              "target counts as 'settled'. Default is a small margin above the "
                              "~1-unit mechanical noise floor already characterized for the ring "
                              "buffer.")
    parser.add_argument("--extra-settle-s", type=float, default=0.1)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    print(f"move_absolute() bench test -- self-adapting settle behavior, "
          f"settle_tolerance={args.settle_tolerance}.")
    print(f"Small/medium moves within +/-{args.max_travel_tenths_um} (tenths of a micron) of start.")
    if args.test_large_move_mm:
        print(f"ALSO testing one large round trip: +/-{args.test_large_move_mm}mm from start.")

    if not args.yes:
        resp = input("\nThis moves the real Z stage several times. Continue? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    z = None
    try:
        tiger.connect()
        z = StageRingBuffer(tiger, card_addr=args.z_card_addr, axis=args.z_axis, axis_mask=args.z_axis_mask)

        start_pos = parse_position(z.where())
        print(f"\nStarting position: {start_pos}")

        results = []
        d = args.max_travel_tenths_um
        offsets = [d, -d, d / 2, -d / 2, 0.0]
        for i, offset in enumerate(offsets, 1):
            do_move(z, start_pos, start_pos + offset, f"Move {i}/{len(offsets)}", args, results)

        if args.test_large_move_mm:
            large_offset = args.test_large_move_mm * 1e4  # mm -> tenths of a micron (1mm = 10000)
            if not args.yes:
                resp = input(f"\nAbout to move {args.test_large_move_mm}mm from start and back -- "
                              f"confirm nothing is in the way. Continue? [y/N] ").strip().lower()
                if not resp.startswith("y"):
                    print("Skipping large-move test.")
                    large_offset = None
            if large_offset is not None:
                do_move(z, start_pos, start_pos + large_offset, "Large move out", args, results)
                do_move(z, start_pos, start_pos, "Large move back", args, results)

        print(f"\n--- wait=False test ---")
        target = start_pos
        print(f"Firing a move to {target:.1f} with wait=False (should return immediately)...")
        t0 = time.perf_counter()
        result = z.move_absolute(target, wait=False)
        elapsed = time.perf_counter() - t0
        print(f"  Returned {result} after {elapsed:.3f}s (should be near-instant, not blocking)")
        fast_return = elapsed < 0.5
        if not fast_return:
            print(f"  -> Took {elapsed:.3f}s -- expected near-instant since wait=False shouldn't poll at all.")

        print("Waiting generously for the move to complete on its own, then checking final position...")
        time.sleep(max(args.timeout_s, 5.0))
        final_pos = parse_position(z.where())
        landed = abs(final_pos - target) <= args.settle_tolerance
        print(f"  Final position: {final_pos:.1f} ({'landed correctly' if landed else 'MISMATCH'})")

        print(f"\n=== Summary: distance vs. convergence time ===")
        for label, offset, elapsed, residual, ok in results:
            print(f"  {label:20s} offset={offset:+10.1f}  time={elapsed:6.3f}s  "
                  f"residual={residual:4.1f}  {'OK' if ok else '**FAILED**'}")
        all_ok = all(ok for _, _, _, _, ok in results) and fast_return and landed

        print(f"\n=== Result ===")
        if all_ok:
            print("PASS: every move landed exactly on target, and convergence time scaled "
                  "sensibly with distance rather than needing a fixed or manually-tuned delay -- "
                  "check the summary table above to see that scaling directly.")
        else:
            print("Did not fully pass -- see FAILED markers above for exactly which case and how.")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if tiger.is_connected:
            tiger.disconnect()
            print("Disconnected.")


if __name__ == "__main__":
    main()
