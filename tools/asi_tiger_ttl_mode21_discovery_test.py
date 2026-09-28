#!/usr/bin/env python3
"""
asi_tiger_ttl_mode21_discovery_test.py
=========================================
Tests ASI TTL OUT0_mode=21 ("TTL OUT0 set at the end of a ring buffer
move... requires MM_TARGET firmware module... as of firmware v3.36
outputs to backplane TTL1 (addr 42) instead of the physical OUT0
connector") on the Z/Theta stage card, to decide between two candidate
approaches for the L/R illumination-arm switch (all 8 PLC BNCs are
already allocated on this rack -- Z: 1-2, camera: 3-4, laser: 5-8):
  1. If mode 21 reliably routes to backplane TTL1, that could free up
     Z's current BNC2 (used for OUT0/mode=2 today) for the L/R switch
     instead, by switching Z's loop-closing signal to ride the
     backplane instead of a physical BNC.
  2. If not, fall back to a free DAC axis driving a binary voltage
     (lower risk, doesn't touch anything in the currently-working
     trigger chain).

UNLIKE the earlier OUT0_mode=2 discovery test, this one MUST use a
REAL RING BUFFER MOVE (TTL X=12 armed, triggered) -- ASI's own
documentation specifically describes mode 21 as tied to "a ring buffer
move," not commanded moves in general (mode 2 is documented as firing
for "MOVE, MOVREL, move via ring buffer, or via array module" -- mode
21's wording is narrower, so a plain M-command move would NOT be a
valid test of this mode).

This checks THREE things, not assumed from the docs:
  1. Does the card even accept TTL Y=21 without an error (a rejected
     command would suggest the MM_TARGET firmware module isn't present
     on this card)?
  2. Does anything appear on backplane TTL1 (addr 42) -- using the same
     latching pulse-catcher technique validated in the OUT0_mode=2
     discovery test, swept across all 8 backplane lines (not just
     TTL1) in case it lands somewhere other than documented, plus an
     optional check on the physical OUT0 connector too (already
     confirmed as mode=2's real location on this card -- worth ruling
     out mode 21 ALSO using it, despite the docs saying otherwise).
  3. Is it a brief PULSE or a SUSTAINED LEVEL ("set", per the docs' own
     wording -- different from mode 2's "pulse")? Polls RDADC Y? (the
     backplane's raw instantaneous state, not the latching catcher)
     right after triggering, then again after a longer wait, to see
     whether it clears on its own or stays "set" until something else
     resets it -- this matters for whether such a signal is safe to
     reuse for something like L/R switching without its own dedicated
     reset logic.

REQUIRED WIRING: none required for the backplane sweep. Optionally, if
you still have Z's physical OUT0 jumpered to a PLC BNC from the
mode=2 discovery test, pass --physical-out-bnc to also rule out mode
21 landing there instead.

SAFETY: moves the real Z stage a small amount once (a real ring-buffer
move is required to exercise this mode).

Usage:
    python tools/asi_tiger_ttl_mode21_discovery_test.py --port COM4
    python tools/asi_tiger_ttl_mode21_discovery_test.py --port COM4 --physical-out-bnc 2
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import (
    TigerController, StageRingBuffer, PLCCard, TigerError,
    bnc_addr, backplane_addr,
)

OUT0_MODE_21 = 21


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--z-card-addr", type=int, default=32)
    parser.add_argument("--z-axis", default="Z")
    parser.add_argument("--z-axis-mask", type=int, default=1)
    parser.add_argument("--step", type=float, default=10.0,
                         help="Relative step, tenths of a micron (default 10 = 1 micron)")
    parser.add_argument("--plc-card-addr", type=int, default=36)
    parser.add_argument("--plc-axis", default="E")
    parser.add_argument("--physical-out-bnc", type=int, default=None,
                         help="PLC BNC number, IF you still have Z's physical OUT connector "
                              "jumpered to it from the mode=2 discovery test. Omit to only "
                              "check the 8 backplane lines.")
    parser.add_argument("--settle-s", type=float, default=0.3)
    parser.add_argument("--level-check-wait-s", type=float, default=2.0,
                         help="How long to wait between the immediate and delayed raw-state "
                              "reads, to distinguish a brief pulse from a sustained 'set' level")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    print("Testing TTL OUT0_mode=21 on the Z/Theta card -- deciding the L/R switch approach.")
    print("Checking: does the card accept mode 21, does anything appear on backplane TTL1 "
          "(addr 42) or elsewhere, and is it a pulse or a sustained level?")
    if args.physical_out_bnc is not None:
        print(f"Also checking: PLC BNC{args.physical_out_bnc} -- confirm Z's physical OUT "
              f"connector is still jumpered to it.")

    if not args.yes:
        resp = input("\nThis moves the real Z stage once, a small amount. Continue? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    z = plc = None
    try:
        tiger.connect()
        z = StageRingBuffer(tiger, card_addr=args.z_card_addr, axis=args.z_axis, axis_mask=args.z_axis_mask)
        plc = PLCCard(tiger, card_addr=args.plc_card_addr, axis=args.plc_axis)

        print("\nArming Z ring buffer (confirmed-working relative stepping, same as mode=2 test)...")
        z.clear()
        z.load_relative_step(args.step)
        z.arm_relative(settle_s=args.settle_s)

        print(f"Setting TTL Y={OUT0_MODE_21}...")
        try:
            z.set_output_mode(OUT0_MODE_21)
        except TigerError as exc:
            print(f"\n=== Result ===")
            print(f"REJECTED: the card returned an error setting TTL Y=21 ({exc}). This strongly "
                  f"suggests the MM_TARGET firmware module isn't present on this card -- mode 21 "
                  f"is documented as requiring it. Recommend the free-DAC-axis approach instead; "
                  f"contact ASI if you want to pursue mode 21 further (they can confirm whether "
                  f"this firmware build supports it, and add the module if not).")
            return
        print("Accepted without error.")

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
        print("Firing one ring-buffer move (software_trigger)...")
        z.software_trigger()
        raw_immediate = plc.read_backplane()
        time.sleep(args.settle_s)
        after = z.where()
        print(f"Position after: {after} ({'moved' if before != after else 'DID NOT MOVE -- check ring buffer setup'})")

        print(f"Waiting {args.level_check_wait_s:.1f}s, then re-checking raw backplane state "
              f"(distinguishes a brief pulse from a sustained 'set' level)...")
        time.sleep(args.level_check_wait_s)
        raw_delayed = plc.read_backplane()

        time.sleep(0.05)
        bitmask = plc.read_cell_outputs()
        print(f"\nCatcher readback (RDADC Z? = {bitmask:016b}):")
        any_caught = False
        caught_backplane_bits = []
        for cell, addr, label in zip(catcher_cells, addresses, labels):
            caught = bool(bitmask & (1 << (cell - 1)))
            if caught:
                any_caught = True
                if 41 <= addr <= 48:
                    caught_backplane_bits.append(addr - 41)
            print(f"  {label:35s} (addr {addr}): {'*** PULSE/EDGE CAUGHT ***' if caught else 'nothing'}")

        print(f"\n=== Result ===")
        if not any_caught:
            print("No edge caught anywhere checked, despite the command being accepted without "
                  "error. Either this card's mode-21 behavior doesn't match the documented "
                  "backplane routing, or it needs something this test didn't provide (e.g. a "
                  "different trigger path than software_trigger()). Recommend the free-DAC-axis "
                  "approach -- this isn't confirmed reliable enough to build on.")
        else:
            print(f"Found it -- see the '*** CAUGHT ***' line(s) above for exactly where.")
            for bit in caught_backplane_bits:
                raw_imm_bit = bool(raw_immediate & (1 << bit))
                raw_del_bit = bool(raw_delayed & (1 << bit))
                print(f"\n  Backplane TTL{bit} raw state: immediately after trigger = "
                      f"{'HIGH' if raw_imm_bit else 'low'}, after {args.level_check_wait_s:.1f}s "
                      f"= {'HIGH' if raw_del_bit else 'low'}")
                if raw_del_bit:
                    print(f"  -> Looks like a SUSTAINED 'set' level (still high after "
                          f"{args.level_check_wait_s:.1f}s), matching the docs' wording ('TTL OUT0 "
                          f"SET', not 'pulse'). This is NOT the same signal shape as mode=2's timed "
                          f"pulse -- if you plan to use this, you'll need to understand what clears "
                          f"it (the next ring-buffer move? an explicit reset?) before relying on it, "
                          f"the same way mode=2's pulse duration needed RT Y understood first.")
                else:
                    print(f"  -> Cleared within {args.level_check_wait_s:.1f}s -- behaves more like "
                          f"a pulse than a persistent level.")
            print(f"\nIf this looks usable, next step would be confirming it reliably repeats "
                  f"across multiple triggers (same rigor as every other signal validated in this "
                  f"project) before committing the L/R switch design to it.")

        z.disarm()
        z.set_output_mode(0)

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if z is not None:
            try:
                z.disarm()
            except Exception as exc:
                print(f"WARNING: could not disarm: {exc}")
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
