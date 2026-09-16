#!/usr/bin/env python3
"""
asi_tiger_zstack_loop_isolated_test.py
=========================================
Isolated bench test of the FULL self-sustaining Z-stepping loop from
the design doc, BEFORE committing it to the library as
configure_zstack_trigger_chain(). This is the first time every
individually-validated piece gets combined into one genuinely new
thing (a real closed hardware loop, not just a sequence) -- worth its
own dedicated check, same reasoning as every other new mechanism in
this project.

Loop topology (matches the design doc, using everything confirmed so far):
  1. A one-time "kick" (manual PLC cell) OR'd with the counter's output
     drives a BNC that's physically jumpered to the Z card's IN0.
  2. Z executes one relative step (TTL X=12), then its OUT0 (confirmed
     on the physical connector, NOT backplane) pulses.
  3. That OUT0 pulse is jumpered into a PLC BNC input, feeding the
     pulse-pass-through counter's pulse_in.
  4. If the counter hasn't reached N yet, its output pulses too --
     through the same OR gate -- driving Z's IN0 again. Repeat.
  5. Once N pulses have gone through, the counter blocks permanently
     (until reset) -- the loop stops entirely in hardware, no host
     software involved after the initial kick.

The counter's own AND-gate cell ALSO drives a spare monitoring BNC
(--counter-monitor-bnc, default 8) purely as an optional scope-visible
"counter fired" indicator -- not required for the loop to function.

REQUIRED PHYSICAL WIRING (two jumpers):
  - Z card's OUT0 connector -> a PLC BNC input (--z-out-bnc, default 2,
    matching the discovery test's wiring you likely already have).
  - A PLC BNC output (--loop-out-bnc, default 1) -> Z card's IN0
    connector.

This script only WATCHES the loop via WHERE polling from the host --
it does not participate in the loop itself once the kick fires. If the
loop doesn't stop as expected, --max-wait-s bounds how long this script
waits before warning you to manually intervene.

SAFETY: starts a real, autonomous, hardware-driven sequence of small
real Z moves (default N=5, step=1 micron). Confirm nothing is in the
way. Watch it, don't walk away, especially on a first run.

Usage:
    python tools/asi_tiger_zstack_loop_isolated_test.py --port COM4
    python tools/asi_tiger_zstack_loop_isolated_test.py --port COM4 --n-planes 10 --step 20
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import (
    TigerController, StageRingBuffer, PLCCard,
    bnc_addr, cell_addr, IO_TYPE_PUSH_PULL_OUTPUT, OUT0_MODE_MOVE_COMPLETE,
)


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
                         help="Relative step per plane, tenths of a micron (default 10 = 1 micron)")
    parser.add_argument("--n-planes", type=int, default=5,
                         help="Target plane count -- START SMALL on a first run")
    parser.add_argument("--pulse-duration-ms", type=int, default=200,
                         help="RT Y=<value> for Z's OUT0 -- confirmed on real hardware that RT Y=1000 "
                              "gives a 1-second pulse, so this parameter is assumed to be milliseconds")
    parser.add_argument("--plc-card-addr", type=int, default=36)
    parser.add_argument("--plc-axis", default="E")
    parser.add_argument("--z-out-bnc", type=int, default=2,
                         help="PLC BNC jumpered FROM the Z card's OUT0 connector")
    parser.add_argument("--loop-out-bnc", type=int, default=1,
                         help="PLC BNC jumpered TO the Z card's IN0 connector")
    parser.add_argument("--counter-monitor-bnc", type=int, default=8,
                         help="Spare BNC the counter's own output also drives, purely for an "
                              "optional scope check -- not required for the loop to function")
    parser.add_argument("--kick-cell", type=int, default=6)
    parser.add_argument("--or-gate-cell", type=int, default=7)
    parser.add_argument("--max-wait-s", type=float, default=30.0,
                         help="How long to watch for the loop to finish before warning you to "
                              "manually intervene")
    parser.add_argument("--poll-interval-s", type=float, default=0.2)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    print(f"Isolated Z-stepping loop test: target {args.n_planes} planes, "
          f"{args.step/10:.2f} micron per step.")
    print(f"Required wiring: Z OUT0 -> PLC BNC{args.z_out_bnc}, PLC BNC{args.loop_out_bnc} -> Z IN0.")
    print("This will run AUTONOMOUSLY in hardware once kicked off -- this script only watches, "
          "it does not control each step.")

    if not args.yes:
        resp = input(f"\nConfirm both jumpers are connected and nothing is in the way of "
                      f"{args.n_planes} small Z moves. Continue? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    z = plc = None
    try:
        tiger.connect()
        z = StageRingBuffer(tiger, card_addr=args.z_card_addr, axis=args.z_axis, axis_mask=args.z_axis_mask)
        plc = PLCCard(tiger, card_addr=args.plc_card_addr, axis=args.plc_axis)

        print("\nArming Z ring buffer (single relative step)...")
        z.clear()
        z.load_relative_step(args.step)
        z.arm_relative(settle_s=0.3)
        z.set_output_mode(OUT0_MODE_MOVE_COMPLETE)
        z.set_output_pulse_duration(args.pulse_duration_ms)

        print(f"Configuring pulse-pass-through counter (N={args.n_planes}), "
              f"pulse_in=BNC{args.z_out_bnc}, monitor output=BNC{args.counter_monitor_bnc}...")
        plc.configure_pulse_pass_through_counter_single(
            pulse_in_addr=bnc_addr(args.z_out_bnc),
            pulse_out_bnc=args.counter_monitor_bnc,
            n_pulses=args.n_planes,
        )
        counter_and_cell = 5  # default 5th (last) cell in configure_pulse_pass_through_counter_single

        print(f"Configuring manual kick (cell {args.kick_cell}) and OR gate "
              f"(cell {args.or_gate_cell})...")
        plc.configure_cell(args.kick_cell, "d_flop", inputs={"a": 0, "b": 0, "c": 0})
        plc.set_cell_state(args.kick_cell, False)
        plc.configure_cell(
            args.or_gate_cell, "or2",
            inputs={"a": cell_addr(args.kick_cell), "b": cell_addr(counter_and_cell)},
        )
        plc.configure_io(bnc_addr(args.loop_out_bnc), IO_TYPE_PUSH_PULL_OUTPUT,
                          source_addr=cell_addr(args.or_gate_cell))

        print("Resetting counter...")
        plc.reset_pulse_pass_through_counter()

        start_pos = parse_position(z.where())
        print(f"\nStarting position: {start_pos}")
        print("Firing kick pulse -- loop is now autonomous...")
        plc.set_cell_state(args.kick_cell, True)
        time.sleep(0.05)
        plc.set_cell_state(args.kick_cell, False)

        print(f"Watching position for up to {args.max_wait_s:.0f}s (polling every "
              f"{args.poll_interval_s:.1f}s, host does NOT control the loop)...")
        last_pos = start_pos
        moves_seen = 0
        stable_since = time.perf_counter()
        t_start = time.perf_counter()
        STABLE_REQUIRED_S = 1.0  # position must hold steady this long to call it "stopped"

        while time.perf_counter() - t_start < args.max_wait_s:
            time.sleep(args.poll_interval_s)
            pos = parse_position(z.where())
            if pos != last_pos:
                moves_seen += 1
                print(f"  Move {moves_seen}: position {last_pos} -> {pos}")
                last_pos = pos
                stable_since = time.perf_counter()
            elif time.perf_counter() - stable_since >= STABLE_REQUIRED_S and moves_seen > 0:
                print(f"  Position stable for {STABLE_REQUIRED_S:.0f}s -- loop appears to have stopped.")
                break
        else:
            print(f"\n**WARNING**: no stable stop detected within {args.max_wait_s:.0f}s. "
                  f"The loop may still be running. Sending TTL X=0 to disarm Z now as a safety measure.")

        z.disarm()  # always disarm at the end regardless of outcome, defensive

        print(f"\n=== Result ===")
        print(f"Moves observed: {moves_seen} (target: {args.n_planes})")
        if moves_seen == args.n_planes:
            print("PASS: the self-sustaining loop moved exactly the target number of planes, then "
                  "stopped on its own in hardware -- no host software involved after the initial kick. "
                  "This validates the full loop topology before it becomes configure_zstack_trigger_chain().")
        else:
            print("Did NOT match the target plane count -- worth understanding why before trusting "
                  "this topology for real acquisitions. Check: did it stop too early (counter/wiring "
                  "issue) or not stop at all (counter not actually gating, or a wiring short)?")

    except KeyboardInterrupt:
        print("\nInterrupted -- disarming Z immediately.")
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
