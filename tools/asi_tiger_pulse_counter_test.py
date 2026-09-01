#!/usr/bin/env python3
"""
asi_tiger_pulse_counter_test.py
==================================
Bench-tests PLCCard.configure_pulse_pass_through_counter() -- "pass an
incoming pulse through exactly N times, then block until reset" --
a faithful port of ASI's own documented, customer-tested example
("Pass through pulse N*M times",
https://www.asiimaging.com/docs/tiger_programmable_logic_card#pass_through_pulse_nm_times).

STAGE 1 (this script): pure software validation, zero external wiring.
Uses a PLC cell (manually toggled via CCA F, the same confirmed-working
mechanism used elsewhere in this project -- e.g. the laser-toggle and
shared-trigger tests) as the "incoming pulse" source instead of a real
external signal, and reads the output AND gate's computed value
directly via RDADC Z? instead of a scope -- this validates the
COUNTING LOGIC itself completely independent of any physical wiring,
mirroring this whole project's "software self-test before electrical
test" methodology (same idea as the Z-stage ring buffer's bare-RM test).

STAGE 2 (not this script, do this AFTER Stage 1 passes): a real
electrical test with an actual external pulse source jumpered to a
real BNC input and a scope on the real BNC output, confirming the
physical path too -- this script only proves the LOGIC is correct, not
that real signals will reach it correctly.

SAFETY: this only touches PLC logic -- no stage motion, no DAC output.
Nothing physical moves. Safe to run repeatedly.

Usage:
    python tools/asi_tiger_pulse_counter_test.py --port COM4 --n-inner 2 --n-outer 2
    python tools/asi_tiger_pulse_counter_test.py --port COM4 --n-inner 3 --n-outer 4 --extra-pulses 5
"""

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import TigerController, PLCCard, cell_addr


def run_pulses(plc: PLCCard, src_cell: int, and_cell: int, n_pulses: int, n_total: int, label: str) -> bool:
    """Sends n_pulses manual pulses, checks the AND gate's output after each
    (while the pulse is still asserted high), reports PASS/BLOCKED vs.
    expected, and returns whether every pulse matched expectation."""
    print(f"\n--- {label}: sending {n_pulses} pulses (expect first {n_total} to PASS) ---")
    all_correct = True
    for i in range(1, n_pulses + 1):
        plc.set_cell_state(src_cell, True)
        cells_bitmask = plc.read_cell_outputs()
        passed = bool(cells_bitmask & (1 << (and_cell - 1)))
        plc.set_cell_state(src_cell, False)

        expected = i <= n_total
        correct = passed == expected
        all_correct = all_correct and correct
        marker = "" if correct else "  **MISMATCH**"
        print(f"  Pulse {i:3d}: {'PASS' if passed else 'BLOCKED':8s} "
              f"(expected {'PASS' if expected else 'BLOCKED'}){marker}")
    return all_correct


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--plc-card-addr", type=int, default=36)
    parser.add_argument("--plc-axis", default="E")
    parser.add_argument("--pulse-source-cell", type=int, default=10,
                         help="PLC cell used as the manual 'incoming pulse' source for this "
                              "software-only test (default 10 -- avoids the counter's own cells 1-6)")
    parser.add_argument("--pulse-out-bnc", type=int, default=8,
                         help="BNC to route the gated output to (not read back by this script, "
                              "just configured -- optionally watch it on a scope too)")
    parser.add_argument("--single", action="store_true",
                         help="Use the single-counter circuit (configure_pulse_pass_through_counter_single) "
                              "instead of the two-counter n_inner*n_outer version -- recommended whenever "
                              "the target count fits in one 16-bit one-shot (up to 65535), since it has no "
                              "prime-N gap (only N=1 is excluded, vs. any prime N for the two-counter form).")
    parser.add_argument("--n-pulses", type=int, default=200,
                         help="Target pass-through count when --single is used")
    parser.add_argument("--n-inner", type=int, default=2, help="Only used without --single")
    parser.add_argument("--n-outer", type=int, default=2, help="Only used without --single")
    parser.add_argument("--extra-pulses", type=int, default=4,
                         help="How many pulses PAST the target count to send, to confirm blocking")
    parser.add_argument("--skip-reset-test", action="store_true")
    args = parser.parse_args()

    if args.single:
        n_total = args.n_pulses
        and_cell = 5  # 5th (last) cell in configure_pulse_pass_through_counter_single's default cells=(1,2,3,4,5)
        print(f"Testing configure_pulse_pass_through_counter_single(n_pulses={n_total}) -- single counter")
    else:
        n_total = args.n_inner * args.n_outer
        and_cell = 6  # 6th (last) cell in configure_pulse_pass_through_counter's default cells=(1,2,3,4,5,6)
        print(f"Testing configure_pulse_pass_through_counter(n_inner={args.n_inner}, n_outer={args.n_outer}) "
              f"-> target count = {n_total}")
    n_pulses = n_total + args.extra_pulses

    print("Pure software test -- a PLC cell simulates the incoming pulse, RDADC reads the result. "
          "No wiring, nothing physical moves.")

    tiger = TigerController(args.port, baudrate=args.baudrate)
    plc = None
    try:
        tiger.connect()
        plc = PLCCard(tiger, card_addr=args.plc_card_addr, axis=args.plc_axis)

        # Manual pulse source: a D-flop whose D and clock are tied low, so its
        # own D-flop mechanics never fire on their own -- the ONLY way its
        # state changes is via direct CCA F writes (set_cell_state()).
        src = args.pulse_source_cell
        plc.configure_cell(src, "d_flop", inputs={"a": 0, "b": 0, "c": 0})
        plc.set_cell_state(src, False)

        if args.single:
            plc.configure_pulse_pass_through_counter_single(
                pulse_in_addr=cell_addr(src),
                pulse_out_bnc=args.pulse_out_bnc,
                n_pulses=args.n_pulses,
            )
        else:
            plc.configure_pulse_pass_through_counter(
                pulse_in_addr=cell_addr(src),
                pulse_out_bnc=args.pulse_out_bnc,
                n_inner=args.n_inner,
                n_outer=args.n_outer,
            )
        plc.reset_pulse_pass_through_counter()

        main_ok = run_pulses(plc, src, and_cell, n_pulses, n_total, "Main test")

        print(f"\n=== Main test result ===")
        if main_ok:
            print(f"PASS: counter passed exactly the first {n_total} pulses and blocked all "
                  f"{args.extra_pulses} beyond that -- matches expected behavior exactly.")
        else:
            print("FAIL: see MISMATCH markers above for exactly which pulse(s) misbehaved -- "
                  "that detail (which pulse, pass vs block) is what's needed to diagnose the "
                  "actual issue, not just 'it didn't work'.")

        if not args.skip_reset_test:
            plc.reset_pulse_pass_through_counter()
            n_reset_test = min(n_total + 2, n_pulses)
            reset_ok = run_pulses(plc, src, and_cell, n_reset_test, n_total, "Reset test")
            print(f"\n=== Reset test result ===")
            print(f"{'PASS' if reset_ok else 'FAIL'}: reset_pulse_pass_through_counter() "
                  f"{'correctly re-armed' if reset_ok else 'did NOT correctly re-arm'} the counter "
                  f"for a fresh count.")

        print(f"\n=== Overall ===")
        overall_ok = main_ok and (args.skip_reset_test or reset_ok)
        if overall_ok:
            print("Logic validated in software. Next: Stage 2 -- a real electrical test with an "
                  "actual external pulse source and a scope on the real output BNC, before trusting "
                  "this in the full acquisition loop.")
        else:
            print("Logic did NOT validate -- do not proceed to a real electrical test or wire this "
                  "into the acquisition loop until the mismatch above is understood and fixed.")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
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
