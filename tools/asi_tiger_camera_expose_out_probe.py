#!/usr/bin/env python
"""
asi_tiger_camera_expose_out_probe.py
====================================
Bench probe for a Photometrics (pyvcam) camera's EXPOSE-OUT behaviour -- independent of the
ASI Tiger, the PLC and mesoSPIM. Close mesoSPIM first (only one program can open the camera).

Why this exists
---------------
On the user's bench, camera_parameters['exp_out_mode'] = 1 (All Rows) was set, but a scope on the
camera's Expose-Out showed only a short pulse, not a level as long as the exposure (the ASI backend's
own log pointed the same way: run_tasks ended ~0.35 s after the trigger with a 200 ms exposure, i.e.
the 'high' was far shorter than the exposure). PVCAM documents All Rows as high for exactly the
exposure time, so either the mode is not what the camera really runs, or something else shapes the
signal. This script answers WHICH, without any trigger wiring:

  * It applies the SAME setup mesoSPIM's mesoSPIM_PhotometricsCamera.open_camera() applies
    (speed table, exposure mode, readout port, gain, exp_out_mode, exp_res, scan mode/direction/
    line delay), then READS BACK what the camera reports for PARAM_EXPOSE_OUT_MODE, both right after
    setting it and again while it is running -- so you can see whether the value you set survives.
  * It free-runs the camera ('Internal Trigger' by default, no external trigger needed) at the
    exposure you choose, so you can look at the Expose-Out BNC on a scope for each combination.
  * --sweep walks Expose-Out modes 0..4 (First Row, All Rows, Any Row, Rolling Shutter, Line Output)
    for one scan mode, or both scan modes with --sweep-scan, pausing for you to look at the scope.

What to look at on the scope (ch1 = Expose-Out; set the trigger on its rising edge):
  All Rows (1)       : ONE level, high for ~= the exposure time (PVCAM docs).
  First Row (0)      : one level, ~= the first row's exposure.
  Any Row (2)        : one level, longer than the exposure (adds the rolling-shutter sweep).
  Line Output (4)    : a train of short pulses, one per line readout.
If a mode you set reads back as a DIFFERENT number while running, the camera/pyvcam is overriding it.
Also compare Auto (0) against Line Delay (1) scan mode: the stock mesoSPIM configs ship
scan_mode=1 together with exp_out_mode=4, and this script shows whether scan mode changes the
Expose-Out mode the camera reports.

Not run against a camera by the author (none available) -- every pyvcam call mirrors one already used
in mesoSPIM_Camera.py, and each read-back is wrapped so one unsupported parameter only prints 'n/a'.

Examples
--------
  python asi_tiger_camera_expose_out_probe.py --exp-out-mode 1 --scan-mode 1 --scan-line-delay 0 --exp-ms 200
  python asi_tiger_camera_expose_out_probe.py --sweep --scan-mode 0 --exp-ms 200
  python asi_tiger_camera_expose_out_probe.py --sweep --sweep-scan --exp-ms 200
Use the speed table / readout port / gain index from your mesoSPIM config's camera_parameters.
"""
import argparse
import sys
import time

MODE_NAMES = {0: "First Row", 1: "All Rows", 2: "Any Row", 3: "Rolling Shutter", 4: "Line Output"}
SCAN_NAMES = {0: "Auto", 1: "Line Delay", 2: "Scan Width"}


def read_param(cam, const, name):
    """cam.get_param(const.<name>) or 'n/a' (never raises)."""
    try:
        return cam.get_param(getattr(const, name))
    except Exception as exc:  # unsupported parameter / not available in this state
        return f"n/a ({type(exc).__name__})"


def report(cam, const, label):
    eo = read_param(cam, const, "PARAM_EXPOSE_OUT_MODE")
    sm = read_param(cam, const, "PARAM_SCAN_MODE")
    print(f"  [{label}] ExposeOutMode={eo} ({MODE_NAMES.get(eo, '?') if isinstance(eo, int) else '?'}), "
          f"ScanMode={sm} ({SCAN_NAMES.get(sm, '?') if isinstance(sm, int) else '?'}), "
          f"LineDelay={read_param(cam, const, 'PARAM_SCAN_LINE_DELAY')}, "
          f"LineTime={read_param(cam, const, 'PARAM_SCAN_LINE_TIME')}, "
          f"ReadoutTime={read_param(cam, const, 'PARAM_READOUT_TIME')}, "
          f"ExposureMode={read_param(cam, const, 'PARAM_EXPOSURE_MODE')}")
    return eo


def try_set(label, fn):
    """Run one setting; a refused/unsupported one prints a note instead of aborting the probe."""
    try:
        fn()
        return True
    except Exception as exc:
        print(f"  (could not set {label}: {exc})")
        return False


def apply_setup(cam, const, a, exp_out_mode, scan_mode):
    """Same order/calls as mesoSPIM_PhotometricsCamera.open_camera(), except that settings the camera
    refuses are reported, not fatal. PARAM_SCAN_LINE_DELAY is only writable in Line Delay scan mode
    (1); in Auto (0) the camera answers PL_ERR_ACCESS_DENIED, so it is skipped there."""
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # pyvcam: speed_table_index -> 'speed'
        try_set("speed_table_index", lambda: setattr(cam, "speed_table_index", a.speed_table_index))
    try_set("exp_mode", lambda: setattr(cam, "exp_mode", a.exp_mode))
    try_set("readout port", lambda: cam.set_param(param_id=const.PARAM_READOUT_PORT, value=a.readout_port))
    try_set("gain index", lambda: cam.set_param(const.PARAM_GAIN_INDEX, a.gain_index))
    try_set("exp_out_mode", lambda: setattr(cam, "exp_out_mode", exp_out_mode))
    try_set("exp_res", lambda: setattr(cam, "exp_res", 0))  # 0 = ms, as mesoSPIM does
    try_set("scan mode", lambda: cam.set_param(param_id=const.PARAM_SCAN_MODE, value=scan_mode))
    try_set("scan direction", lambda: cam.set_param(param_id=const.PARAM_SCAN_DIRECTION, value=a.scan_direction))
    if scan_mode == 1:
        try_set("scan line delay", lambda: cam.set_param(param_id=const.PARAM_SCAN_LINE_DELAY, value=a.scan_line_delay))
    else:
        print(f"  (scan line delay not set: only writable in Line Delay scan mode, requested scan mode is {scan_mode})")


def run_one(cam, const, a, exp_out_mode, scan_mode, seconds):
    print(f"\n=== requested: Expose-Out mode {exp_out_mode} ({MODE_NAMES.get(exp_out_mode)}), "
          f"scan mode {scan_mode} ({SCAN_NAMES.get(scan_mode)}), exposure {a.exp_ms} ms, "
          f"exposure mode {a.exp_mode!r} ===")
    apply_setup(cam, const, a, exp_out_mode, scan_mode)
    report(cam, const, "after setting, before start")
    cam.exp_time = int(a.exp_ms)
    t_start = time.time()
    cam.start_live()
    try:
        time.sleep(0.3)
        got = report(cam, const, "while running     ")
        if isinstance(got, int) and got != exp_out_mode:
            print(f"  *** The camera reports Expose-Out mode {got} ({MODE_NAMES.get(got, '?')}) while running, "
                  f"NOT the {exp_out_mode} that was set -- something is overriding it. ***")
        print(f"  Free-running for {seconds:.0f} s -- look at the Expose-Out BNC on the scope now.")
        t_end = time.time() + seconds
        frames = 0
        first_err = None
        first_err_t = 0.0
        while time.time() < t_end:
            try:
                cam.poll_frame()  # same call as mesoSPIM_Camera.get_live_image()
                frames += 1
            except Exception as exc:
                if first_err is None:
                    first_err = exc
                    first_err_t = time.time() - t_start
                time.sleep(0.05)
        print(f"  (grabbed {frames} frames)")
        if first_err is not None:
            print(f"  first poll_frame error: {first_err!r} at {first_err_t:.2f} s after start_live")
    finally:
        try:
            cam.finish()
        except Exception as exc:
            print(f"  finish() raised {exc!r}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--exp-out-mode", type=int, default=1, choices=sorted(MODE_NAMES), help="PARAM_EXPOSE_OUT_MODE value to set (default 1 = All Rows)")
    p.add_argument("--scan-mode", type=int, default=0, choices=sorted(SCAN_NAMES), help="PARAM_SCAN_MODE (0 Auto, 1 Line Delay, 2 Scan Width)")
    p.add_argument("--scan-line-delay", type=int, default=0, help="PARAM_SCAN_LINE_DELAY (mesoSPIM camera_parameters['scan_line_delay'])")
    p.add_argument("--scan-direction", type=int, default=0, help="PARAM_SCAN_DIRECTION (0 Down, 1 Up, 2 Alternate)")
    p.add_argument("--exp-ms", type=float, default=200.0, help="Exposure in ms (default 200)")
    p.add_argument("--exp-mode", default="Internal Trigger",
                   help="pyvcam exposure mode. 'Internal Trigger' (default) free-runs with no wiring; use "
                        "'Edge Trigger' to mimic mesoSPIM's config (then you must supply triggers yourself)")
    p.add_argument("--speed-table-index", type=int, default=0)
    p.add_argument("--readout-port", type=int, default=0)
    p.add_argument("--gain-index", type=int, default=1)
    p.add_argument("--seconds", type=float, default=8.0, help="How long to free-run each setting (default 8)")
    p.add_argument("--sweep", action="store_true", help="Walk Expose-Out modes 0..4 (pauses between)")
    p.add_argument("--sweep-scan", action="store_true", help="With --sweep: also repeat for scan modes 0 and 1")
    a = p.parse_args()

    try:
        from pyvcam import pvc
        from pyvcam import constants as const
        from pyvcam.camera import Camera
    except Exception as exc:
        print(f"Could not import pyvcam ({exc}). Run this in the same Python environment mesoSPIM uses.")
        return 1

    pvc.init_pvcam()
    cam = None
    try:
        cams = list(Camera.detect_camera())
        if not cams:
            print("No camera found (is mesoSPIM still running and holding it?).")
            return 1
        cam = cams[0]
        cam.open()
        print(f"Camera: {read_param(cam, const, 'PARAM_PRODUCT_NAME')}  chip {read_param(cam, const, 'PARAM_CHIP_NAME')}")
        try:
            print(f"Expose-Out modes this camera offers (pyvcam enum): {cam.read_enum(const.PARAM_EXPOSE_OUT_MODE)}")
            print(f"Exposure modes: {cam.read_enum(const.PARAM_EXPOSURE_MODE)}")
        except Exception as exc:
            print(f"(could not read enums: {exc!r})")

        if a.sweep:
            scans = [0, 1] if a.sweep_scan else [a.scan_mode]
            for sm in scans:
                for mode in sorted(MODE_NAMES):
                    run_one(cam, const, a, mode, sm, a.seconds)
                    input("  Note what the scope showed, then press Enter for the next mode... ")
        else:
            run_one(cam, const, a, a.exp_out_mode, a.scan_mode, a.seconds)
        print("\nDone. Send back the '[while running]' lines above plus what the scope showed for each mode.")
        return 0
    finally:
        try:
            if cam is not None:
                cam.close()
        finally:
            pvc.uninit_pvcam()


if __name__ == "__main__":
    sys.exit(main())
