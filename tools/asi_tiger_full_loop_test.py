#!/usr/bin/env python3
"""
asi_tiger_full_loop_test.py
==============================
Step 4 (final step) of the staged full-loop test plan (see
PATCHNOTES_ASI_TIGER.md). Tests the actual self-sustaining loop via
configure_zstack_trigger_chain() -- camera, ETL, and Z all
participating together, camera no longer free-running but actually
gated by the plane counter. Every individual signal in this chain has
already been confirmed precisely (steps 1-3): camera trigger (exact
5/5), ETL syncs to Expose-Out's rising edge, Z syncs to Expose-Out's
falling edge with an EXACT trigger-count match (not estimated).

Learned from earlier testing: WHERE polling during active triggering
can be unreliable (empty replies, possible stale reads). This script
does NOT poll during the loop -- it waits a generous fixed duration for
the autonomous loop to finish on its own (gated by the counter, no
host involvement needed), THEN takes one clean, disarmed, settled
position reading and compares against EXACTLY n_planes x z_step --
matching the precise, ambiguity-free methodology that resolved step 3.

REQUIRED: camera in EXTERNAL TRIGGER mode in PVCAMTest (this chain
controls the camera itself, it does not free-run).

REQUIRED WIRING (four jumpers, all already used/confirmed in earlier
steps -- no new wiring needed if you've been following the staged plan):
  - PLC BNC1 -> Z card's IN0
  - PLC BNC2 <- Z card's OUT0
  - PLC BNC3 <- camera's Expose-Out
  - PLC BNC4 -> camera's trigger input

SAFETY: starts a real, fully autonomous, hardware-driven sequence of
small Z moves + ETL sweeps + camera exposures. Start with a SMALL
n_planes. Watch it -- don't walk away, especially on a first run.

Usage:
    python tools/asi_tiger_full_loop_test.py --port COM4 --n-planes 3
"""

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import (
    TigerController, SingleAxisWaveform, PATTERN_SAWTOOTH,
    enable_backplane_trigger_mode, configure_zstack_trigger_chain,
)


def parse_position(where_reply: str) -> float:
    return float(where_reply.split("=")[-1])


def read_position_robust(z, retries: int = 5, retry_delay_s: float = 0.05):
    for attempt in range(retries):
        try:
            return parse_position(z.where())
        except Exception:
            if attempt < retries - 1:
                time.sleep(retry_delay_s)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--plc-card-addr", type=int, default=36)
    parser.add_argument("--plc-axis", default="E")
    parser.add_argument("--z-card-addr", type=int, default=32)
    parser.add_argument("--z-axis", default="Z")
    parser.add_argument("--z-axis-mask", type=int, default=1)
    parser.add_argument("--z-step", type=float, default=10.0,
                         help="Relative step per plane, tenths of a micron (default 10 = 1 micron)")
    parser.add_argument("--etl-card-addr", type=int, default=34)
    parser.add_argument("--etl-axis", default="H")
    parser.add_argument("--etl-card-first-axis", default="H")
    parser.add_argument("--etl-amplitude", type=float, default=0.38, help="Vpp")
    parser.add_argument("--etl-offset", type=float, default=2.32, help="V")
    parser.add_argument("--etl-period-ms", type=float, default=147.0,
                         help="CONFIRMED ON REAL HARDWARE: the camera's real Expose-Out pulse "
                              "duration in Rolling Shutter mode is NOT the same as the configured "
                              "exposure time -- one confirmed data point showed a 150ms exposure "
                              "setting producing a ~120ms Expose-Out pulse (~30ms shorter). Set "
                              "this relative to the REAL pulse duration (with the usual 2-3ms "
                              "safety margin below it, see the earlier ETL-period-margin finding), "
                              "not the raw exposure setting -- verify with your own camera/exposure "
                              "combination, since whether this exact ~30ms offset holds at other "
                              "exposure settings is not yet confirmed.")
    parser.add_argument("--camera-expose-bnc", type=int, default=3)
    parser.add_argument("--camera-trigger-bnc", type=int, default=4)
    parser.add_argument("--z-in0-bnc", type=int, default=1)
    parser.add_argument("--z-out0-bnc", type=int, default=2)
    parser.add_argument("--counter-monitor-bnc", type=int, default=8)
    parser.add_argument("--n-planes", type=int, default=3,
                         help="Target plane count -- START SMALL on a first run")
    parser.add_argument("--seconds-per-plane-estimate", type=float, default=3.0,
                         help="Generous per-plane wait estimate -- total wait is n_planes x this, "
                              "plus a fixed safety margin. Not polled during the run (WHERE "
                              "polling during active triggering is unreliable), so err generous.")
    parser.add_argument("--safety-margin-s", type=float, default=5.0)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    total_wait = args.n_planes * args.seconds_per_plane_estimate + args.safety_margin_s
    print(f"Full closed-loop test: target {args.n_planes} planes, {args.z_step/10:.2f} micron/step.")
    print(f"Will wait {total_wait:.0f}s (fixed, not polled) for the autonomous loop to finish, "
          f"then take one clean final reading.")
    print(f"Wiring: PLC BNC{args.z_in0_bnc}->Z IN0, PLC BNC{args.z_out0_bnc}<-Z OUT0, "
          f"PLC BNC{args.camera_expose_bnc}<-camera Expose-Out, PLC BNC{args.camera_trigger_bnc}->camera trigger.")
    print("REQUIRED: camera in EXTERNAL TRIGGER mode in PVCAMTest -- this chain controls it directly.")

    if not args.yes:
        resp = input(f"\nThis starts a fully autonomous sequence of {args.n_planes} Z moves + ETL "
                      f"sweeps + camera exposures. Confirm everything is ready. Continue? [y/N] ").strip().lower()
        if not resp.startswith("y"):
            print("Cancelled.")
            return

    tiger = TigerController(args.port, baudrate=args.baudrate)
    etl = chain = None
    try:
        tiger.connect()

        etl_lo = args.etl_offset - args.etl_amplitude / 2
        etl_hi = args.etl_offset + args.etl_amplitude / 2
        if etl_lo < 0 or etl_hi > 4.096:
            print(f"\nREFUSING: ETL swing [{etl_lo:.3f}, {etl_hi:.3f}]V outside [0, 4.096]V.")
            return

        print(f"\nConfiguring and arming ETL (axis {args.etl_axis})...")
        etl = SingleAxisWaveform(tiger, card_addr=args.etl_card_addr, axis=args.etl_axis)
        etl.stop_and_zero()
        enable_backplane_trigger_mode(tiger, card_addr=args.etl_card_addr)
        etl.configure(pattern=PATTERN_SAWTOOTH, amplitude_v=args.etl_amplitude,
                      offset_v=args.etl_offset, period_ms=args.etl_period_ms, external_trigger=True)
        etl.arm_triggered(free_running=False)

        print(f"Configuring the full trigger chain (n_planes={args.n_planes})...")
        chain = configure_zstack_trigger_chain(
            tiger,
            plc_card_addr=args.plc_card_addr, plc_axis=args.plc_axis,
            z_card_addr=args.z_card_addr, z_axis=args.z_axis, z_axis_mask=args.z_axis_mask,
            etl_card_addr=args.etl_card_addr, etl_axis=args.etl_axis,
            etl_card_first_axis=args.etl_card_first_axis,
            n_planes=args.n_planes, z_step=args.z_step,
            camera_expose_bnc=args.camera_expose_bnc, camera_trigger_bnc=args.camera_trigger_bnc,
            z_in0_bnc=args.z_in0_bnc, z_out0_bnc=args.z_out0_bnc,
            counter_monitor_bnc=args.counter_monitor_bnc,
        )

        start_pos = read_position_robust(chain.z)
        if start_pos is None:
            print("ERROR: could not read starting Z position -- aborting before starting the loop.")
            return
        print(f"\nStart position: {start_pos}.")

        chain.reset()
        print("Firing kick -- the loop is now FULLY AUTONOMOUS (camera, ETL, and Z all "
              "participating, no host involvement until it finishes)...")
        chain.kick()

        print(f"Waiting {total_wait:.0f}s for the loop to complete on its own...")
        time.sleep(total_wait)

        # Disarm FIRST (stops Z's trigger response), THEN take a settled clean reading --
        # same reasoning as the precise trigger-count test: don't trust a read taken while
        # anything might still be actively triggering.
        chain.disarm()
        etl.stop_and_zero()
        time.sleep(0.5)
        final_pos = read_position_robust(chain.z)

        print(f"\n=== Result ===")
        if final_pos is None:
            print("Could not get a clean final reading -- something may be wrong, worth "
                  "investigating before trusting this loop for real acquisitions.")
            return

        total_displacement = final_pos - start_pos
        expected = args.n_planes * args.z_step
        diff = total_displacement - expected

        print(f"Start: {start_pos}, Final: {final_pos}, total displacement: {total_displacement:+.2f}")
        print(f"Expected ({args.n_planes} planes x {args.z_step} step): {expected:+.2f}")
        if abs(diff) < 1e-6:
            print(f"MATCH: the full self-sustaining loop -- camera, ETL, and Z together, gated "
                  f"autonomously by the plane counter -- moved Z by exactly the expected amount "
                  f"for {args.n_planes} planes. Step 4 (final step) of the staged test plan is DONE.")
        else:
            print(f"MISMATCH: {diff:+.2f} unaccounted for ({diff/args.z_step:+.1f} steps' worth). "
                  f"Worth checking: did the loop actually stop on its own (watch the camera / a "
                  f"scope on --counter-monitor-bnc next time to see when it blocks), or could it "
                  f"still be running past this script's fixed wait? Try a longer "
                  f"--seconds-per-plane-estimate if so, and re-run to isolate whether this is a "
                  f"timing issue with THIS SCRIPT's wait, or a genuine loop problem.")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if chain is not None:
            try:
                chain.disarm()
            except Exception as exc:
                print(f"WARNING: could not disarm chain: {exc}")
        if etl is not None:
            try:
                etl.stop_and_zero()
            except Exception as exc:
                print(f"WARNING: could not stop/zero ETL: {exc}")
        if tiger.is_connected:
            tiger.disconnect()
            print("Disconnected.")


if __name__ == "__main__":
    main()
