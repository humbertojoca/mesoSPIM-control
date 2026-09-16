#!/usr/bin/env python3
"""
asi_tiger_z_output_discovery_test.py
=======================================
Discovers WHERE the Z/Theta stage card's move-complete pulse (TTL Y=2,
OUT0_mode=2) actually appears -- the ONE piece of the design doc's
self-sustaining acquisition loop that has never been tested. Everything
else in the loop (galvo free-run, ETL triggered sweep, Z ring-buffer
relative stepping, the PLC pulse-pass-through counter) has been
bench-validated; this is the missing link that closes it (Z-move-
complete -> next frame's trigger).

Per ASI's command:ttl docs: OUT0_mode=2 "generates TTL pulse at end of
a commanded move (MOVE, MOVREL, move via ring buffer, or via array
module)". Where that pulse physically appears is NOT assumed here --
per the same docs, OUT0 is normally the card's own PHYSICAL OUT
connector (paired with the IN0 connector the ring-buffer electrical
test already jumpers), not automatically a numbered backplane line
(backplane routing for a completion-style pulse is only documented as
a special case for a DIFFERENT mode, 21, not the one used here).

This tests BOTH possibilities using "pulse catchers" (latching D-flops
that catch any rising edge and hold it, so a brief pulse can't be
missed by slow serial polling or bad timing):
  1. All 8 backplane lines (41-48), automatically, no wiring needed.
  2. Optionally, a specific PLC BNC input -- IF you jumper the Z card's
     physical OUT connector to it first (--physical-out-bnc).

Sequence: arm the Z ring buffer (the same confirmed-working relative
stepping from earlier testing), set TTL Y=2, reset all catchers, fire
ONE real move (software_trigger -- moves the real stage a small
amount), then read back which catcher(s) latched.

SAFETY: moves the real Z stage a small real amount once.

Usage:
    python tools/asi_tiger_z_output_discovery_test.py --port COM4
    python tools/asi_tiger_z_output_discovery_test.py --port COM4 --physical-out-bnc 2
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import (
    TigerController, StageRingBuffer, PLCCard,
    bnc_addr, backplane_addr, OUT0_MODE_MOVE_COMPLETE,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--z-card-addr", type=int, default=32)
    parser.add_argument("--z-axis", default="Z")
    parser.add_argument("--z-axis-mask", type=int, default=1)
    parser.add_argument("--step", type=float, default=10.0,
                         help="Relative step, tenths of a micron (default 10 = 1 micron)")
    parser.add_argument("--pulse-duration", type=int, default=100,
                         help="RT Y=<value> -- OUT0 pulse duration. Units NOT independently "
                              "confirmed here; doesn't affect this test's correctness either way, "
                              "since the pulse catchers latch any rising edge regardless of duration.")
    parser.add_argument("--plc-card-addr", type=int, default=36)
    parser.add_argument("--plc-axis", default="E")
    parser.add_argument("--physical-out-bnc", type=int, default=None,
                         help="PLC BNC number, IF you've jumpered the Z card's physical OUT "
                              "connector to it. Omit to only check the 8 backplane lines.")
    parser.add_argument("--settle-s", type=float, default=0.3)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    print("Discovering where the Z card's TTL Y=2 (move-complete) pulse appears.")
    print("Checking: all 8 backplane lines (41-48), automatically.")
    if args.physical_out_bnc is not None:
        print(f"Also checking: PLC BNC{args.physical_out_bnc} -- confirm you've jumpered the Z "
              f"card's physical OUT connector to it before continuing.")
    else:
        print("Not checking any physical BNC (--physical-out-bnc not set) -- if backplane check "
              "comes back empty, that's the next thing to try, with a jumper cable.")

    if not args.yes:
        resp = input("\nThis moves the real Z stage once, a small amount. Continue? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    z = None
    try:
        tiger.connect()
        z = StageRingBuffer(tiger, card_addr=args.z_card_addr, axis=args.z_axis, axis_mask=args.z_axis_mask)
        plc = PLCCard(tiger, card_addr=args.plc_card_addr, axis=args.plc_axis)

        print("\nArming Z ring buffer (confirmed-working relative stepping)...")
        z.clear()
        z.load_relative_step(args.step)
        z.arm_relative(settle_s=args.settle_s)

        print(f"Setting TTL Y={OUT0_MODE_MOVE_COMPLETE} (move-complete pulse) and RT Y={args.pulse_duration}...")
        z.set_output_mode(OUT0_MODE_MOVE_COMPLETE)
        z.set_output_pulse_duration(args.pulse_duration)

        addresses = [backplane_addr(n) for n in range(8)]
        labels = [f"backplane TTL{n}" for n in range(8)]
        if args.physical_out_bnc is not None:
            addresses.append(bnc_addr(args.physical_out_bnc))
            labels.append(f"BNC{args.physical_out_bnc} (physical OUT jumper)")

        print(f"Configuring {len(addresses)} pulse catchers...")
        catcher_cells = plc.configure_pulse_catchers(addresses)
        plc.reset_pulse_catchers()

        before = z.where()
        print(f"\nPosition before: {before}")
        print("Firing one move (software_trigger)...")
        z.software_trigger()
        time.sleep(args.settle_s)
        after = z.where()
        print(f"Position after: {after} ({'moved' if before != after else 'DID NOT MOVE -- check ring buffer setup'})")

        time.sleep(0.05)  # let a few evaluation cycles pass before reading
        bitmask = plc.read_cell_outputs()
        print(f"\nCatcher readback (RDADC Z? = {bitmask:016b}):")
        any_caught = False
        for cell, addr, label in zip(catcher_cells, addresses, labels):
            caught = bool(bitmask & (1 << (cell - 1)))
            if caught:
                any_caught = True
            print(f"  {label:35s} (addr {addr}): {'*** PULSE CAUGHT ***' if caught else 'nothing'}")

        print(f"\n=== Result ===")
        if any_caught:
            print("Found it -- see the '*** PULSE CAUGHT ***' line(s) above for exactly which "
                  "address the Z card's move-complete pulse appears on. Use that address as the "
                  "input to the PLC pulse-pass-through counter when wiring the full self-sustaining "
                  "loop.")
        else:
            print("No pulse caught anywhere checked. If --physical-out-bnc wasn't set, try it with "
                  "a real jumper from the Z card's OUT connector next -- per ASI's docs this is the "
                  "more likely location for a plain OUT0_mode=2 pulse (backplane routing is "
                  "documented for a different, unrelated mode). If BOTH checks come back empty, "
                  "worth confirming with ASI directly whether this card's firmware build exposes "
                  "OUT0 at all, and if so where.")

        z.disarm()
        z.set_output_mode(0)  # OUT0_MODE_LOW -- leave the card in a quiet state

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
