#!/usr/bin/env python3
"""
asi_tiger_galvo_per_arm_bench_test.py
========================================
Bench test for the galvo + per-arm ETL logic added to
mesoSPIM_ASITigerWaveFormGenerator.write_waveforms_to_tasks() -- the one
piece of that adapter that had NOT yet had its own real-hardware bench
pass (see PATCHNOTES_ASI_TIGER.md, "Added galvo driving + per-arm ETL").

Unlike asi_tiger_galvo_etl_demo.py (which drives ONE galvo/ETL pair in
isolation to confirm the underlying free-running-galvo +
triggered-ETL mechanism), THIS script exercises the adapter's actual
ROW-REFRESH logic against real hardware:

  - TWO separate galvo axes and TWO separate ETL axes (one pair per
    illumination arm), matching the confirmed real topology.
  - A sequence of simulated "rows" that can alternate sides (L/R/L/R by
    default) -- on each row, the INACTIVE side's galvo+ETL are
    explicitly quiesced (stop_and_zero()) and the ACTIVE side's are
    configured+armed/started, exactly matching
    write_waveforms_to_tasks()'s real sequence.
  - A real hardware READBACK after each row (SAM <axis>? on all four
    axes) confirming that exactly one galvo axis reads SAM=1 (running)
    and one ETL axis reads SAM=2 (armed for external trigger), while
    the other two read SAM=0 (idle) -- not just "no exception was
    raised", an actual query of the card's own state.
  - Optionally (--test-safety-refusal), one final row with a
    deliberately out-of-range galvo amplitude, confirming via the same
    SAM readback that the galvo axis was correctly left at SAM=0 (never
    started) rather than driven out of range.
  - Fires --n-frames-per-row real camera trigger pulses per row (the
    same manual-cell mechanism as asi_tiger_per_frame_trigger_test.py),
    so the ETL's sweeps and the galvo's continuous scan can both be
    watched on a scope, in sync, for the currently active side.

REQUIRED WIRING: same as the adapter -- PLC BNC{camera-expose-bnc}<-
camera Expose-Out, PLC BNC{camera-trigger-bnc}->camera trigger input.
REQUIRED: camera in EXTERNAL TRIGGER mode in PVCAMTest.

SAFETY: fires real camera triggers, real ETL sweeps, and runs BOTH
galvo axes (one at a time) at real voltages. Start with small
amplitudes and --n-frames-per-row 2-3 on a first run, and watch the
first row closely before trusting the rest.

ETL PERIOD: by default, the ETL's SAM=2 sweep period is DERIVED from
--camera-exposure-time-s (minus --etl-period-margin-ms), matching
mesoSPIM_ASITigerWaveFormGenerator._etl_period_ms() exactly -- pass
--etl-period-ms explicitly to override this. This follows the
user's real-hardware finding that after switching the camera's
readout mode to "All Rows", Expose-Out now spans the full configured
exposure duration (previously shorter, under rolling-shutter mode),
so the ETL sweep should track the real exposure time rather than use
a flat guessed value.

Usage:
    python tools/asi_tiger_galvo_per_arm_bench_test.py --port COM4 \\
        --galvo-amplitude 1.0 --galvo-offset 0.0 --galvo-frequency 100 \\
        --etl-amplitude 0.3 --etl-offset 2.3 \\
        --rows L,R,L --n-frames-per-row 3 --test-safety-refusal
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import (
    TigerController, TigerError, PLCCard, SingleAxisWaveform,
    PATTERN_SAWTOOTH, PATTERN_TRIANGLE,
    enable_backplane_trigger_mode, axis_slot_index, trigger_in_backplane_addr,
    IO_TYPE_INPUT, IO_TYPE_PUSH_PULL_OUTPUT, cell_addr, bnc_addr,
)

SAM_LABELS = {0: "idle (0)", 1: "running/free-run (1)", 2: "armed-for-trigger (2)",
              3: "running+sync (3)", 4: "armed-free-run (4)"}


def read_sam_mode(tiger, card_addr, axis):
    """
    Best-effort readback of an axis's current SAM mode via 'SAM <axis>?'.
    Not independently confirmed elsewhere in this project that this
    specific query is supported by this firmware (every other SAM usage
    in this project only ever WRITES it) -- wrapped defensively so a
    real failure here degrades to "readback unavailable" rather than
    crashing the whole bench test, matching the established pattern for
    any read this project hasn't specifically confirmed yet.
    """
    try:
        reply = tiger.send_command(f"SAM {axis}?", card_addr=card_addr)
        return int(float(reply.split("=")[-1].split()[0]))
    except (TigerError, TimeoutError, ValueError, IndexError):
        return None


def resolve_etl_period_ms(args):
    """
    Mirrors mesoSPIM_ASITigerWaveFormGenerator._etl_period_ms() exactly,
    so this bench test doesn't silently diverge from the adapter's real
    behavior: an explicit --etl-period-ms always wins unchanged; otherwise
    the period is derived from --camera-exposure-time-s minus
    --etl-period-margin-ms, clamped to a 2.0ms minimum (with a printed
    warning) if that comes out too short to be meaningful.
    """
    if args.etl_period_ms is not None:
        return args.etl_period_ms
    period_ms = args.camera_exposure_time_s * 1000.0 - args.etl_period_margin_ms
    if period_ms < 2.0:
        print(f"WARNING: derived ETL period ({period_ms:.1f}ms, from --camera-exposure-time-s="
              f"{args.camera_exposure_time_s}s minus --etl-period-margin-ms={args.etl_period_margin_ms}ms) "
              f"is too short to be meaningful (1ms is documented as undefined behavior for this firmware) -- "
              f"clamping to 2ms. Check --camera-exposure-time-s and/or --etl-period-margin-ms.")
        period_ms = 2.0
    return period_ms


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)

    parser.add_argument("--plc-card-addr", type=int, default=36)
    parser.add_argument("--plc-axis", default="E")
    parser.add_argument("--camera-expose-bnc", type=int, default=3)
    parser.add_argument("--camera-trigger-bnc", type=int, default=4)
    parser.add_argument("--camera-trigger-cell", type=int, default=10)
    parser.add_argument("--camera-trigger-pulse-ms", type=float, default=10.0)
    parser.add_argument("--camera-expose-start-timeout-s", type=float, default=2.0)
    parser.add_argument("--camera-exposure-time-s", type=float, default=0.15,
                         help="Expected exposure duration -- sizes the 'wait for Expose-Out low' timeout.")

    parser.add_argument("--etl-card-addr", type=int, default=34)
    parser.add_argument("--etl-card-first-axis", default="H")
    parser.add_argument("--etl-l-axis", default="H")
    parser.add_argument("--etl-r-axis", default="J")
    parser.add_argument("--etl-amplitude", type=float, default=0.3, help="Vpp, same value used for both sides")
    parser.add_argument("--etl-offset", type=float, default=2.3, help="V, same value used for both sides")
    parser.add_argument("--etl-max-volts", type=float, default=4.096)
    parser.add_argument("--etl-period-ms", type=float, default=None,
                         help="Explicit override for the ETL's SAM=2 sweep period. If omitted (the default), "
                              "this is DERIVED from --camera-exposure-time-s minus --etl-period-margin-ms, "
                              "matching mesoSPIM_ASITigerWaveFormGenerator._etl_period_ms() exactly -- see "
                              "that method's docstring for why (the camera's real Expose-Out now spans the "
                              "full configured exposure once the camera is in 'All Rows' readout mode, so "
                              "the ETL sweep should track it rather than use a flat guessed value).")
    parser.add_argument("--etl-period-margin-ms", type=float, default=0.0,
                         help="Only used when --etl-period-ms is omitted. Default 0.0ms -- per the user, "
                              "directly: 'in reality, margin period is negligible if the expose out is in "
                              "all rows', since under that readout mode Expose-Out spans the real exposure "
                              "window exactly with no known extra jitter to margin against. Set this above "
                              "0 only if you find your rack/readout mode still needs some margin (the ETL "
                              "period must stay SHORTER than the camera's real trigger interval, or the "
                              "axis misses every other trigger edge). Matches the adapter's default.")

    parser.add_argument("--galvo-card-addr", type=int, default=37)
    parser.add_argument("--galvo-l-axis", default="A")
    parser.add_argument("--galvo-r-axis", default="C")
    parser.add_argument("--galvo-units-per-volt", type=float, default=4000 / 10.24,
                         help="Default matches ASI's new TGGALVO firmware -- pass 1000.0 for the original firmware.")
    parser.add_argument("--galvo-amplitude", type=float, default=1.0, help="Vpp, same value used for both sides")
    parser.add_argument("--galvo-offset", type=float, default=0.0, help="V, same value used for both sides")
    parser.add_argument("--galvo-frequency", type=float, default=100.0, help="Hz")
    parser.add_argument("--galvo-duty-cycle", type=float, default=0.5,
                         help="0.4-0.6 maps to PATTERN_TRIANGLE (this adapter's approximation), else PATTERN_SAWTOOTH")
    parser.add_argument("--galvo-max-volts", type=float, default=10.0,
                         help="ASI's own amplifier safety limit -- see write_waveforms_to_tasks()'s own check")

    parser.add_argument("--rows", default="L,R,L,R",
                         help="Comma-separated side sequence to simulate, e.g. 'L,R,L'. Each letter is one row.")
    parser.add_argument("--n-frames-per-row", type=int, default=3)
    parser.add_argument("--inter-frame-pause-s", type=float, default=0.3)
    parser.add_argument("--test-safety-refusal", action="store_true",
                         help="Append one extra row with a deliberately out-of-range galvo amplitude, "
                              "confirming (via real SAM readback) that it's refused rather than driven.")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    etl_period_ms = resolve_etl_period_ms(args)

    rows = [r.strip().upper() for r in args.rows.split(",") if r.strip()]
    for r in rows:
        if r not in ("L", "R"):
            print(f"REFUSING: --rows entries must be 'L' or 'R', got {r!r}.")
            return
    if not rows:
        print("REFUSING: --rows produced an empty sequence.")
        return

    etl_lo = args.etl_offset - args.etl_amplitude / 2
    etl_hi = args.etl_offset + args.etl_amplitude / 2
    if etl_lo < 0 or etl_hi > args.etl_max_volts:
        print(f"REFUSING: ETL swing [{etl_lo:.3f}, {etl_hi:.3f}]V outside [0, {args.etl_max_volts:.3f}]V.")
        return
    galvo_peak = abs(args.galvo_amplitude) / 2 + abs(args.galvo_offset)
    if galvo_peak > args.galvo_max_volts:
        print(f"REFUSING: galvo peak {galvo_peak:.3f}V exceeds --galvo-max-volts {args.galvo_max_volts:.3f}V.")
        return

    print(f"Galvo+per-arm-ETL bench test: {len(rows)} row(s) ({','.join(rows)}), "
          f"{args.n_frames_per_row} camera trigger(s) per row.")
    print(f"ETL: card {args.etl_card_addr}, axes L={args.etl_l_axis}/R={args.etl_r_axis}, "
          f"swings {etl_lo:+.3f}V to {etl_hi:+.3f}V, period {etl_period_ms:.1f}ms"
          f"{' (explicit --etl-period-ms)' if args.etl_period_ms is not None else f' (derived from --camera-exposure-time-s={args.camera_exposure_time_s}s)'}.")
    print(f"Galvo: card {args.galvo_card_addr}, axes L={args.galvo_l_axis}/R={args.galvo_r_axis}, "
          f"{args.galvo_amplitude:.3f} Vpp, offset {args.galvo_offset:+.3f}V, {args.galvo_frequency:.1f} Hz, "
          f"units_per_volt={args.galvo_units_per_volt:.4f}.")
    print("REQUIRED: camera in EXTERNAL TRIGGER mode in PVCAMTest.")
    if args.test_safety_refusal:
        print("Will append one extra row with a deliberately out-of-range galvo amplitude (30Vpp) at the end.")

    if not args.yes:
        resp = input(f"\nThis drives real galvo/ETL voltages and fires real camera triggers across "
                      f"{len(rows)} row(s). Continue? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    plc = etl_l = etl_r = galvo_l = galvo_r = None
    n_row_failures = 0
    try:
        tiger.connect()

        # --- Setup, mirroring create_tasks() exactly ---
        plc = PLCCard(tiger, card_addr=args.plc_card_addr, axis=args.plc_axis)
        plc.clear_state()

        etl_l = SingleAxisWaveform(tiger, card_addr=args.etl_card_addr, axis=args.etl_l_axis)
        etl_r = SingleAxisWaveform(tiger, card_addr=args.etl_card_addr, axis=args.etl_r_axis)
        etl_l.stop_and_zero()
        etl_r.stop_and_zero()
        enable_backplane_trigger_mode(tiger, card_addr=args.etl_card_addr)

        galvo_l = SingleAxisWaveform(tiger, card_addr=args.galvo_card_addr, axis=args.galvo_l_axis,
                                      units_per_volt=args.galvo_units_per_volt)
        galvo_r = SingleAxisWaveform(tiger, card_addr=args.galvo_card_addr, axis=args.galvo_r_axis,
                                      units_per_volt=args.galvo_units_per_volt)
        galvo_l.stop_and_zero()
        galvo_r.stop_and_zero()

        camera_trigger_cell = args.camera_trigger_cell
        plc.configure_cell(camera_trigger_cell, "d_flop", inputs={"a": 0, "b": 0, "c": 0})
        plc.set_cell_state(camera_trigger_cell, False)
        plc.configure_io(bnc_addr(args.camera_trigger_bnc), IO_TYPE_PUSH_PULL_OUTPUT,
                          source_addr=cell_addr(camera_trigger_cell))

        expose_addr = bnc_addr(args.camera_expose_bnc)
        plc.configure_io(expose_addr, IO_TYPE_INPUT)
        for etl_axis_letter in (args.etl_l_axis, args.etl_r_axis):
            etl_slot = axis_slot_index(etl_axis_letter, args.etl_card_first_axis)
            etl_trigger_addr = trigger_in_backplane_addr(etl_slot)
            plc.configure_io(etl_trigger_addr, IO_TYPE_PUSH_PULL_OUTPUT, source_addr=expose_addr)

        expose_bit = 1 << (args.camera_expose_bnc - 1)
        etl_by_letter = {"L": etl_l, "R": etl_r}
        galvo_by_letter = {"L": galvo_l, "R": galvo_r}
        axis_by_letter = {"L": args.galvo_l_axis, "R": args.galvo_r_axis}

        row_plan = list(rows)
        if args.test_safety_refusal:
            # Use the OPPOSITE side from the last real row, so the refused axis starts
            # from a genuinely clean idle (SAM=0) state -- otherwise, if the same side
            # was already legitimately running from a previous row, a refused row
            # correctly leaves it running (write_waveforms_to_tasks() only skips THIS
            # row's configure()/start() on refusal, it doesn't stop what's already
            # active from before), which would make a naive "expect SAM=0" check here
            # fail for a reason that has nothing to do with the refusal logic itself.
            refusal_side = "R" if rows[-1] == "L" else "L"
            row_plan.append(f"{refusal_side}-REFUSE")

        for row_num, row in enumerate(row_plan, start=1):
            is_refusal_test = row.endswith("-REFUSE")
            letter = row[0]
            other_letter = "R" if letter == "L" else "L"
            print(f"\n--- Row {row_num}/{len(row_plan)}: side={letter}"
                  f"{' (SAFETY REFUSAL TEST -- deliberately out-of-range galvo amplitude)' if is_refusal_test else ''} ---")

            # --- Quiesce the inactive side first, exactly matching write_waveforms_to_tasks() ---
            etl_by_letter[other_letter].stop_and_zero()
            galvo_by_letter[other_letter].stop_and_zero()

            # --- ETL: configure + arm the active side ---
            active_etl = etl_by_letter[letter]
            active_etl.configure(pattern=PATTERN_SAWTOOTH, amplitude_v=args.etl_amplitude,
                                  offset_v=args.etl_offset, period_ms=etl_period_ms, external_trigger=True)
            active_etl.arm_triggered(free_running=False)

            # --- Galvo: safety check, then configure + start the active side (unless testing refusal) ---
            galvo_amp = 30.0 if is_refusal_test else args.galvo_amplitude
            galvo_off = args.galvo_offset
            peak = abs(galvo_amp) / 2 + abs(galvo_off)
            refused = peak > args.galvo_max_volts
            active_galvo = galvo_by_letter[letter]
            if refused:
                print(f"  Galvo peak {peak:.3f}V exceeds --galvo-max-volts {args.galvo_max_volts:.3f}V -- "
                      f"REFUSING to drive it (matches write_waveforms_to_tasks()'s own safety check). "
                      f"Leaving axis {axis_by_letter[letter]} stopped/zeroed.")
            else:
                period_ms = 1000.0 / args.galvo_frequency if args.galvo_frequency > 0 else etl_period_ms
                pattern = PATTERN_TRIANGLE if 0.4 <= args.galvo_duty_cycle <= 0.6 else PATTERN_SAWTOOTH
                if pattern == PATTERN_TRIANGLE:
                    period_ms = 2 * round(period_ms / 2)
                active_galvo.configure(pattern=pattern, amplitude_v=galvo_amp, offset_v=galvo_off, period_ms=period_ms)
                active_galvo.start()

            # --- Real hardware readback: query all 4 axes' SAM mode ---
            time.sleep(0.05)
            modes = {
                ("ETL", "L", args.etl_l_axis): read_sam_mode(tiger, args.etl_card_addr, args.etl_l_axis),
                ("ETL", "R", args.etl_r_axis): read_sam_mode(tiger, args.etl_card_addr, args.etl_r_axis),
                ("galvo", "L", args.galvo_l_axis): read_sam_mode(tiger, args.galvo_card_addr, args.galvo_l_axis),
                ("galvo", "R", args.galvo_r_axis): read_sam_mode(tiger, args.galvo_card_addr, args.galvo_r_axis),
            }
            any_readback = any(v is not None for v in modes.values())
            if not any_readback:
                print("  Readback: 'SAM <axis>?' does not appear to be supported by this firmware -- "
                      "skipping the hardware-state check for this row (commands were still sent as above).")
            else:
                print("  Readback (SAM mode per axis):")
                row_ok = True
                for (kind, side_letter, axis), mode in modes.items():
                    label = SAM_LABELS.get(mode, f"unknown ({mode})") if mode is not None else "read failed"
                    print(f"    {kind} {side_letter} (axis {axis}): {label}")
                    expect_active = (side_letter == letter)
                    if kind == "ETL":
                        expected_mode = 2 if expect_active else 0
                    else:  # galvo
                        expected_mode = (0 if refused else 1) if expect_active else 0
                    if mode is not None and mode != expected_mode:
                        print(f"      MISMATCH: expected SAM={expected_mode}, read {mode}.")
                        row_ok = False
                if row_ok:
                    print("  Readback matches expected state for this row.")
                else:
                    n_row_failures += 1

            if is_refusal_test:
                # No frames fired for the refusal-test row -- its point is the readback above, not triggering.
                continue

            for i in range(1, args.n_frames_per_row + 1):
                print(f"  Frame {i}/{args.n_frames_per_row}...", end=" ")
                t0 = time.perf_counter()
                plc.set_cell_state(camera_trigger_cell, True)
                time.sleep(args.camera_trigger_pulse_ms / 1000.0)
                plc.set_cell_state(camera_trigger_cell, False)

                start = time.perf_counter()
                went_high = False
                while time.perf_counter() - start < args.camera_expose_start_timeout_s:
                    if plc.read_bnc_inputs() & expose_bit:
                        went_high = True
                        break
                    time.sleep(0.001)
                if not went_high:
                    print(f"Expose-Out never went HIGH within {args.camera_expose_start_timeout_s:.1f}s.")
                    continue

                end_timeout = args.camera_exposure_time_s + 2.0
                start = time.perf_counter()
                went_low = False
                while time.perf_counter() - start < end_timeout:
                    if not (plc.read_bnc_inputs() & expose_bit):
                        went_low = True
                        break
                    time.sleep(0.001)
                elapsed = time.perf_counter() - t0
                if went_low:
                    print(f"OK, {elapsed*1000:.0f}ms.")
                else:
                    print(f"Expose-Out never went LOW within {end_timeout:.1f}s.")

                time.sleep(args.inter_frame_pause_s)

        print("\n=== Result ===")
        if n_row_failures:
            print(f"{n_row_failures} row(s) had a SAM-readback mismatch (see above) -- the adapter's "
                  f"row-refresh/side-switch/safety-refusal logic may not be behaving as intended on this "
                  f"hardware/firmware.")
        else:
            print("All rows' SAM readback matched the expected state (correct axis armed/running, "
                  "correct axes quiesced, safety refusal left the galvo un-driven when tested). "
                  "Also watch the scope: the active galvo axis should show continuous scanning, "
                  "and the active ETL axis should show a sweep in sync with each camera trigger printed "
                  "above -- the inactive side's axes should show nothing.")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        for name, axis in (("ETL L", etl_l), ("ETL R", etl_r), ("galvo L", galvo_l), ("galvo R", galvo_r)):
            if axis is not None:
                try:
                    axis.stop_and_zero()
                except Exception as exc:
                    print(f"WARNING: could not stop/zero {name}: {exc}")
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
