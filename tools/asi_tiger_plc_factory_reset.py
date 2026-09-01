#!/usr/bin/env python3
"""
asi_tiger_plc_factory_reset.py
=================================
Establishes a genuinely clean PLC baseline, then (optionally) saves it
as the permanent power-on default -- built after finding that SS Z
saves the card's ENTIRE current live configuration, not just whatever
you meant to fix. A BNC ended up defaulting to output after a real
controller reset, traced back to an earlier SS Z that saved whatever
was live at that moment, including config no one had verified was
clean.

What this does:
  1. Resets EVERY logic cell (1-16) to a harmless constant-0 type.
  2. Resets EVERY physical I/O (BNC 1-8, backplane 0-7) to input.
  3. Optionally (--save, with a separate explicit confirmation) saves
     this clean state as the new power-on default via SS Z.

This is DELIBERATE and COMPREHENSIVE: it will disrupt any currently
running acquisition/logic on this PLC card. Do not run this while
something else depends on the current PLC configuration.

Usage:
    python tools/asi_tiger_plc_factory_reset.py --port COM4 --plc-card-addr 36
    python tools/asi_tiger_plc_factory_reset.py --port COM4 --plc-card-addr 36 --save
"""

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import TigerController, PLCCard


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--plc-card-addr", type=int, required=True)
    parser.add_argument("--plc-axis", default="E")
    parser.add_argument("--save", action="store_true",
                         help="After resetting, also save this clean state as the permanent "
                              "power-on default (SS Z). Without this flag, the reset only affects "
                              "the card's current live state -- a real controller reset would "
                              "revert to whatever was saved before.")
    parser.add_argument("--yes", action="store_true", help="Skip the reset confirmation prompt "
                         "(the --save confirmation, if --save is used, is separate and always asked)")
    args = parser.parse_args()

    print(f"About to reset ALL 16 logic cells and ALL physical I/O (BNC 1-8, backplane 0-7) "
          f"on PLC card {args.plc_card_addr} to a harmless/idle state.")
    print("This WILL disrupt any currently running acquisition/logic on this card.")
    if not args.yes:
        resp = input("Continue with the reset? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    try:
        tiger.connect()
        plc = PLCCard(tiger, card_addr=args.plc_card_addr, axis=args.plc_axis)

        print("\nResetting all 16 cells to constant-0...")
        print("Resetting all physical I/O (BNC 1-8, backplane 0-7) to input...")
        plc.reset_all_cells_and_io()
        print("Done. PLC card is now in a clean, harmless live state.")

        if args.save:
            print(f"\nAbout to SAVE this clean state as the PERMANENT power-on default for card "
                  f"{args.plc_card_addr} (SS Z). This survives a real controller reset. There is no "
                  f"undo short of manually reconfiguring and saving again.")
            resp = input("Type EXACTLY 'save clean default' to proceed, anything else cancels: ")
            if resp.strip() == "save clean default":
                plc.save()
                print("Saved. This clean state is now the power-on default for this card.")
            else:
                print("Not saved -- live state was reset, but the OLD saved default is unchanged, "
                      "so a real controller reset would still revert to whatever was saved before.")
        else:
            print("\n--save not passed -- this reset only affects the current live state. A real "
                  "controller reset would revert to whatever was saved before. Re-run with --save "
                  "if you want this clean state to persist across resets.")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if tiger.is_connected:
            tiger.disconnect()
            print("Disconnected.")


if __name__ == "__main__":
    main()
