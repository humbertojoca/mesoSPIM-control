#!/usr/bin/env python3
"""
asi_tiger_z_spacing_test.py
==============================
Isolates whether TTL Y=2 (OUT0 move-complete pulse) itself interferes
with Z's triggering, independent of retrigger SPEED -- using only
primitives already fully validated in this project (software_trigger(),
well-spaced host pacing), no new PLC logic at all.

Why this test: the isolated full-loop test showed Z stopping/glitching
after only 1 of 5 expected moves, with unexpected short pulses seen on
a scope. But that test fed Z's own OUT0 directly back into IN0 with
essentially ZERO delay (limited only by the PLC's ~250us evaluation
cycle) -- a MUCH tighter retrigger cadence than the real acquisition
architecture would ever produce (where at least one full camera
exposure period separates consecutive Z moves). We ALREADY validated Z
handles repeated triggers cleanly with 200-300ms of host-paced spacing
(the ring-buffer wraparound tests, weeks earlier) -- but those tests
never had TTL Y=2 configured at the same time.

This test: arm Z exactly as before, ALSO configure TTL Y=2 + RT Y=2000
(so OUT0 fires every move, matching the failing test's config), but
trigger it via well-spaced host software_trigger() calls instead of a
zero-delay hardware loop. Two possible outcomes:
  - Clean 5-for-5 moves -> confirms TTL Y=2 itself is NOT the problem;
    the issue is specifically the zero-delay autonomous retriggering,
    and the fix is inserting real dead-time into the hardware loop
    (matching what camera exposure would naturally provide anyway).
  - Same glitching even with generous spacing -> TTL Y=2 configuration
    itself interferes with normal triggering, a different and more
    fundamental problem worth raising with ASI directly.

SAFETY: moves the real Z stage 5 small amounts, well-spaced.

Usage:
    python tools/asi_tiger_z_spacing_test.py --port COM4
    python tools/asi_tiger_z_spacing_test.py --port COM4 --n-triggers 5 --spacing-s 0.5
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import TigerController, StageRingBuffer, OUT0_MODE_MOVE_COMPLETE, OUT0_MODE_LOW


def parse_position(where_reply: str) -> float:
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
    parser.add_argument("--n-triggers", type=int, default=5)
    parser.add_argument("--spacing-s", type=float, default=0.3,
                         help="Delay between host-paced triggers -- matches the earlier "
                              "confirmed-working wraparound test spacing")
    parser.add_argument("--pulse-duration-ms", type=int, default=2000,
                         help="RT Y=<value> -- matches the failing full-loop test's setting")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    print(f"Testing {args.n_triggers} HOST-PACED triggers, {args.spacing_s:.1f}s apart, "
          f"with TTL Y=2 (RT Y={args.pulse_duration_ms}) configured throughout.")
    print("Isolating: does TTL Y=2 itself interfere with normal triggering, independent of speed?")

    if not args.yes:
        resp = input(f"\nThis moves the real Z stage {args.n_triggers} times. Continue? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    z = None
    try:
        tiger.connect()
        z = StageRingBuffer(tiger, card_addr=args.z_card_addr, axis=args.z_axis, axis_mask=args.z_axis_mask)

        print("\nArming Z (single relative step, TTL Y=2 configured)...")
        z.clear()
        z.load_relative_step(args.step)
        z.arm_relative(settle_s=0.3)
        z.set_output_mode(OUT0_MODE_MOVE_COMPLETE)
        z.set_output_pulse_duration(args.pulse_duration_ms)

        last_pos = parse_position(z.where())
        print(f"Start position: {last_pos}")
        deltas = []
        for i in range(1, args.n_triggers + 1):
            z.software_trigger()
            time.sleep(args.spacing_s)
            pos = parse_position(z.where())
            delta = pos - last_pos
            deltas.append(delta)
            last_pos = pos
            marker = "" if abs(delta - args.step) < 1e-6 else "  **UNEXPECTED**"
            print(f"  Trigger {i}: delta={delta:+.2f}{marker}")

        z.disarm()
        z.set_output_mode(OUT0_MODE_LOW)

        all_ok = all(abs(d - args.step) < 1e-6 for d in deltas)
        print(f"\n=== Result ===")
        if all_ok:
            print("PASS: all triggers moved by exactly the expected step, well-spaced, with TTL Y=2 "
                  "active throughout. This means TTL Y=2 itself is NOT the problem -- the earlier "
                  "full-loop test's glitching is specific to zero-delay autonomous retriggering. Fix: "
                  "insert real dead-time into the hardware loop (a PLC delay, or accept that camera "
                  "exposure naturally provides this in the real architecture) rather than direct "
                  "OUT0->IN0 feedback with no gap.")
        else:
            print("FAIL even with generous spacing -- TTL Y=2 configuration itself appears to "
                  "interfere with normal triggering, not just speed. This is a more fundamental "
                  "question worth raising with ASI directly, with this exact result as evidence.")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if z is not None:
            try:
                z.disarm()
                z.set_output_mode(OUT0_MODE_LOW)
            except Exception as exc:
                print(f"WARNING: could not fully clean up Z: {exc}")
        if tiger.is_connected:
            tiger.disconnect()
            print("Disconnected.")


if __name__ == "__main__":
    main()
