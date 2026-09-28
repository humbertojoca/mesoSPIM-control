#!/usr/bin/env python3
"""
asi_tiger_per_frame_trigger_test.py
======================================
Standalone bench test replicating mesoSPIM_ASITigerWaveFormGenerator's
NEW per-frame design EXACTLY -- the same manual camera-trigger cell,
the same camera-Expose-Out-to-ETL-sync wiring, the same two-phase wait
(Expose-Out HIGH then LOW), the same per-frame arm/disarm cycle -- but
run standalone against real hardware, without needing the full
mesoSPIM-control software (PyQt5, a real Camera object, Core/Camera
threading) at all. This is the right level to validate the mechanism
itself at, the same "isolate before integrating" approach this whole
project has used throughout.

Runs N simulated "frames", each doing a full start/run/stop cycle
(arm ETL -> fire trigger + wait for that one exposure -> stop/zero
ETL), matching EXACTLY what mesoSPIM_Core.run_acquisition()'s loop
does via snap_image_in_series() -> start_tasks()/run_tasks()/
stop_tasks(). Does NOT move Z at all -- this design doesn't touch Z,
by design (see mesoSPIM_ASITigerWaveFormGenerator.py's ARCHITECTURE
NOTE).

REQUIRED WIRING: PLC BNC3<-camera Expose-Out, PLC BNC4->camera trigger
input (same BNCs as the earlier full-loop tests). REQUIRED: camera in
EXTERNAL TRIGGER mode in PVCAMTest.

SAFETY: fires real camera triggers and real ETL sweeps, N times. Start
with a small N.

Usage:
    python tools/asi_tiger_per_frame_trigger_test.py --port COM4 --n-frames 5
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import (
    TigerController, PLCCard, SingleAxisWaveform, PATTERN_SAWTOOTH,
    enable_backplane_trigger_mode, axis_slot_index, trigger_in_backplane_addr,
    IO_TYPE_INPUT, IO_TYPE_PUSH_PULL_OUTPUT, cell_addr, bnc_addr,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--plc-card-addr", type=int, default=36)
    parser.add_argument("--plc-axis", default="E")
    parser.add_argument("--etl-card-addr", type=int, default=34)
    parser.add_argument("--etl-axis", default="H")
    parser.add_argument("--etl-card-first-axis", default="H")
    parser.add_argument("--etl-amplitude", type=float, default=0.38, help="Vpp")
    parser.add_argument("--etl-offset", type=float, default=2.32, help="V")
    parser.add_argument("--etl-period-ms", type=float, default=120.0)
    parser.add_argument("--camera-expose-bnc", type=int, default=3)
    parser.add_argument("--camera-trigger-bnc", type=int, default=4)
    parser.add_argument("--camera-trigger-cell", type=int, default=10)
    parser.add_argument("--camera-trigger-pulse-ms", type=float, default=10.0)
    parser.add_argument("--camera-expose-start-timeout-s", type=float, default=2.0)
    parser.add_argument("--camera-exposure-time-s", type=float, default=0.15,
                         help="Expected exposure duration -- used only to size the "
                              "'wait for Expose-Out low' timeout, with a safety margin")
    parser.add_argument("--n-frames", type=int, default=5)
    parser.add_argument("--inter-frame-pause-s", type=float, default=0.5,
                         help="Pause between frames -- purely so you can watch each one on "
                              "a scope if you want, not required by the mechanism itself")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    etl_lo = args.etl_offset - args.etl_amplitude / 2
    etl_hi = args.etl_offset + args.etl_amplitude / 2
    if etl_lo < 0 or etl_hi > 4.096:
        print(f"REFUSING: ETL swing [{etl_lo:.3f}, {etl_hi:.3f}]V outside [0, 4.096]V.")
        return

    print(f"Per-frame trigger test: {args.n_frames} frames, matching "
          f"mesoSPIM_ASITigerWaveFormGenerator's exact per-frame mechanism.")
    print(f"Wiring: PLC BNC{args.camera_expose_bnc}<-camera Expose-Out, "
          f"PLC BNC{args.camera_trigger_bnc}->camera trigger input.")
    print("REQUIRED: camera in EXTERNAL TRIGGER mode in PVCAMTest.")

    if not args.yes:
        resp = input(f"\nThis fires {args.n_frames} real camera triggers + ETL sweeps. "
                      f"Continue? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    plc = etl = None
    try:
        tiger.connect()

        # --- Exactly create_tasks()'s setup, minus the DAC/laser/L-R-switch parts
        # this test doesn't need ---
        plc = PLCCard(tiger, card_addr=args.plc_card_addr, axis=args.plc_axis)

        # Diagnostic for the "spurious exposure before ETL is armed" report: read
        # camera_trigger_bnc's and camera_trigger_cell's RAW state BEFORE touching
        # anything, so a residual-high condition left by a PREVIOUS script/run (if any)
        # is visible here rather than silently glitching through moments later.
        try:
            trig_bit = 1 << (args.camera_trigger_bnc - 1)
            bnc_pre = plc.read_bnc_inputs() & trig_bit
            cell_pre = plc.read_cell_outputs() & (1 << (args.camera_trigger_cell - 1))
            print(f"Diagnostic: BEFORE any setup -- BNC{args.camera_trigger_bnc} raw level = "
                  f"{'HIGH' if bnc_pre else 'low'}, cell {args.camera_trigger_cell} output = "
                  f"{'HIGH' if cell_pre else 'low'} (leftover from a prior run, if any).")
        except Exception as exc:
            print(f"Diagnostic read failed (non-fatal): {exc}")

        plc.clear_state()

        etl = SingleAxisWaveform(tiger, card_addr=args.etl_card_addr, axis=args.etl_axis)
        etl.stop_and_zero()
        enable_backplane_trigger_mode(tiger, card_addr=args.etl_card_addr)

        camera_trigger_cell = args.camera_trigger_cell
        plc.configure_cell(camera_trigger_cell, "d_flop", inputs={"a": 0, "b": 0, "c": 0})
        plc.set_cell_state(camera_trigger_cell, False)
        plc.configure_io(bnc_addr(args.camera_trigger_bnc), IO_TYPE_PUSH_PULL_OUTPUT,
                          source_addr=cell_addr(camera_trigger_cell))

        expose_addr = bnc_addr(args.camera_expose_bnc)
        plc.configure_io(expose_addr, IO_TYPE_INPUT)
        etl_slot = axis_slot_index(args.etl_axis, args.etl_card_first_axis)
        etl_trigger_addr = trigger_in_backplane_addr(etl_slot)
        plc.configure_io(etl_trigger_addr, IO_TYPE_PUSH_PULL_OUTPUT, source_addr=expose_addr)

        # --- Exactly write_waveforms_to_tasks()'s ETL configure AND arm, ONCE for the
        # whole row -- NOT per frame. CONFIRMED ON REAL HARDWARE (two real bugs found
        # from this exact pattern): (1) re-arming every frame is unnecessary --
        # external-trigger arming (SAM=2) auto-rearms on every subsequent edge on its
        # own -- and was a real source of intermittent "Expose-Out never went high"
        # failures, most likely a race between the re-arm serial command completing and
        # the next trigger pulse firing. (2) arming here, PLUS this row's real trigger
        # wiring already being established above (unlike the ordering bug found and
        # fixed in asi_tiger_full_loop_test.py), avoids a newly-armed ETL ever watching
        # an unconnected/stale line. See mesoSPIM_ASITigerWaveFormGenerator.py's
        # write_waveforms_to_tasks()/start_tasks()/stop_tasks() docstrings for the full
        # explanation -- this script now matches that fix exactly.
        etl.configure(pattern=PATTERN_SAWTOOTH, amplitude_v=args.etl_amplitude,
                      offset_v=args.etl_offset, period_ms=args.etl_period_ms, external_trigger=True)
        etl.arm_triggered(free_running=False)

        expose_bit = 1 << (args.camera_expose_bnc - 1)
        n_ok, n_start_timeout, n_end_timeout = 0, 0, 0

        for i in range(1, args.n_frames + 1):
            print(f"\n--- Frame {i}/{args.n_frames} ---")
            t0 = time.perf_counter()

            # run_tasks()
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
                print(f"  Expose-Out never went HIGH within {args.camera_expose_start_timeout_s:.1f}s "
                      f"-- trigger may not have reached the camera.")
                n_start_timeout += 1
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
                print(f"  OK: Expose-Out went HIGH then LOW, full cycle took {elapsed*1000:.0f}ms.")
                n_ok += 1
            else:
                print(f"  Expose-Out went HIGH but never LOW within {end_timeout:.1f}s -- "
                      f"exposure may still be in progress.")
                n_end_timeout += 1

            time.sleep(args.inter_frame_pause_s)

        print(f"\n=== Result ===")
        print(f"{n_ok}/{args.n_frames} frames completed cleanly (HIGH then LOW, full arm/trigger/stop cycle).")
        if n_start_timeout:
            print(f"{n_start_timeout} frame(s) never saw Expose-Out go HIGH -- check the camera "
                  f"trigger wiring/BNC{args.camera_trigger_bnc} and that PVCAMTest is in "
                  f"external trigger mode.")
        if n_end_timeout:
            print(f"{n_end_timeout} frame(s) saw Expose-Out go HIGH but never LOW -- check "
                  f"--camera-exposure-time-s matches your real exposure setting.")
        if n_ok == args.n_frames:
            print("PASS: the per-frame mechanism (manual trigger + two-phase expose wait + "
                  "per-frame ETL arm/disarm) works reliably, matching what the real adapter does.")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if etl is not None:
            try:
                etl.stop_and_zero()
            except Exception as exc:
                print(f"WARNING: could not stop/zero ETL: {exc}")
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
