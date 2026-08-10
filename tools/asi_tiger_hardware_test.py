#!/usr/bin/env python3
"""
asi_tiger_hardware_test.py
============================
Standalone smoke test for ASI Tiger DAC + PLC hardware. Run this BEFORE
plugging asi_tiger into mesoSPIM or any acquisition software, to confirm
the serial link, DAC channels, and PLC logic actually work on your rack.

No GUI, no mesoSPIM dependency -- just this script + the asi_tiger/
package sitting next to it.

Usage (from the repo root, or anywhere -- paths are resolved relative to
this script's own location):
    python tools/asi_tiger_hardware_test.py --port COM5
    python tools/asi_tiger_hardware_test.py --port COM5 --config mesoSPIM/config/examples/asi_dac_channels_example.json
    python asi_tiger_hardware_test.py --port COM5 --skip-dac
    python asi_tiger_hardware_test.py --port COM5 --skip-plc
    python asi_tiger_hardware_test.py --port COM5 --test-voltage 0.5
    python asi_tiger_hardware_test.py --port COM5 --plc-camera-bnc 1
    python asi_tiger_hardware_test.py --port COM5 --test-laser-toggle --plc-camera-bnc 1 --plc-laser-bncs 5,6

What it does, in order:
  1. Connects and asks the controller who it is (raw 'N' command) so you
     can confirm you're talking to the rack you think you are.
  2. DAC test: zeros every configured channel, then for each one does a
     set-voltage / read-back round trip at a small test voltage and
     reports PASS/FAIL, pausing so you can confirm with a multimeter or
     scope if you want to. Always ends each channel back at 0V.
  3. PLC test:
       a) An internal logic self-test (AND gate on two known constants)
          that needs no external wiring -- proves cell programming works.
       b) An optional live BNC-input read loop so you can manually pulse
          a wire (e.g. your camera trigger) and watch the PLC see it.
       c) An OPT-IN, explicitly-confirmed dry run of the two-laser-toggle
          logic from the earlier design -- only runs if you pass
          --test-laser-toggle AND type the confirmation phrase, since it
          drives real BNC outputs.
  4. Zeros all DAC channels and clears PLC state on exit -- including on
     Ctrl-C or any error, via try/finally.

Nothing here is destructive by default: without --test-laser-toggle, the
script only ever touches DAC voltages (always ending at 0V) and a scratch
PLC cell/self-test that doesn't drive any physical output.
"""

import argparse
import json
import sys
import time
from pathlib import Path

# This script lives in tools/, the library lives in mesoSPIM/src/devices/asi_tiger/.
# Add mesoSPIM/src/devices to the path so `from asi_tiger import ...` resolves
# without needing PyQt5/nidaqmx/the rest of mesoSPIM installed.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import (
    TigerController, TigerError, ASITigerDAC, PLCCard,
    bnc_addr, backplane_addr, inverted,
)


def prompt_yes_no(question: str, default: bool = False) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    resp = input(question + suffix).strip().lower()
    if not resp:
        return default
    return resp.startswith("y")


def pause(message: str = "Press Enter to continue..."):
    input(message)


def section(title: str):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


# ---------------------------------------------------------------------
def test_connection(port: str, baudrate: int) -> TigerController:
    section("1. Connection")
    tiger = TigerController(port, baudrate=baudrate)
    print(f"Connecting to {port} @ {baudrate} baud...")
    tiger.connect()
    print("Connected. Querying controller identity ('N' command)...")
    try:
        reply = tiger.send_command("N")
        print("Controller reports:")
        for line in reply.replace("\r", "\n").splitlines():
            if line.strip():
                print(f"  {line.strip()}")
    except Exception as exc:
        print(f"WARNING: identity query failed ({exc}). Connection is open, "
              f"but double-check the port/baud rate are actually correct for this rack.")
    return tiger


# ---------------------------------------------------------------------
def load_dac_channels(config_path: str):
    with open(config_path) as f:
        data = json.load(f)
    if "asi_dac" in data:
        return data["asi_dac"]["channels"]
    if "channels" in data:
        return data["channels"]
    raise ValueError(f"Could not find a channel list in {config_path} "
                      f"(expected top-level 'asi_dac.channels' or 'channels')")


def test_dac(tiger: TigerController, channels: list, test_voltage: float, interactive: bool):
    section("2. DAC channels")
    dac = ASITigerDAC(tiger=tiger)
    for ch in channels:
        dac.add_channel(
            name=ch["name"], card_addr=ch["card_addr"], axis=ch["axis"],
            range_code=ch.get("range_code", 6),
        )

    print(f"Registered {len(dac.channels)} channel(s): {list(dac.channels)}")
    print("Zeroing all channels before starting (mandatory safety step)...")
    dac.zero_all()
    print("All channels at 0V.")

    results = {}
    for name in dac.channels:
        print(f"\n--- Channel {name!r} ---")
        if interactive and not prompt_yes_no(
            f"About to set {name} to {test_voltage:+.3f} V, read it back, then return to 0V. Proceed?",
            default=True,
        ):
            print("Skipped.")
            continue
        try:
            dac.set_voltage(name, test_voltage)
            time.sleep(0.05)
            readback = dac.get_voltage(name)
            error_mv = abs(readback - test_voltage) * 1000
            passed = error_mv < 20  # 20 mV tolerance
            print(f"  Commanded: {test_voltage:+.3f} V | Read back: {readback:+.3f} V "
                  f"| Error: {error_mv:.1f} mV | {'PASS' if passed else 'FAIL'}")
            if interactive:
                pause(f"  Check {name}'s BNC with a multimeter/scope now if you want, "
                      f"then press Enter to zero it and continue...")
            results[name] = passed
        except (TigerError, ValueError, RuntimeError, TimeoutError) as exc:
            print(f"  ERROR: {exc}")
            results[name] = False
        finally:
            dac.set_voltage(name, 0.0)

    print("\nDAC summary:")
    for name, passed in results.items():
        print(f"  {name}: {'PASS' if passed else 'FAIL'}")
    if not results:
        print("  (no channels tested)")
    return dac


# ---------------------------------------------------------------------
def test_plc_self_test(plc: PLCCard):
    print("\n--- Internal logic self-test (no external wiring needed) ---")
    print("Programming scratch cell 16 as AND(constant_1, constant_1) -> expect output 1")
    plc.configure_cell(16, "and2", inputs={"a": 64, "b": 64})  # 64 = inverted(0) = constant 1
    time.sleep(0.05)
    out = plc.read_cell_outputs()
    bit15_high = bool(out & (1 << 15))
    print(f"  RDADC Z? = {out} (binary {out:016b}), cell 16 bit = {'1' if bit15_high else '0'} "
          f"| {'PASS' if bit15_high else 'FAIL'}")

    print("Reprogramming scratch cell 16 as AND(constant_1, constant_0) -> expect output 0")
    plc.configure_cell(16, "and2", inputs={"a": 64, "b": 0})
    time.sleep(0.05)
    out = plc.read_cell_outputs()
    bit15_low = not bool(out & (1 << 15))
    print(f"  RDADC Z? = {out} (binary {out:016b}), cell 16 bit = {'0' if bit15_low else '1'} "
          f"| {'PASS' if bit15_low else 'FAIL'}")

    # Leave the scratch cell harmless (constant 0)
    plc.configure_cell(16, "constant", config=0)
    return bit15_high and bit15_low


def test_plc_bnc_live_read(plc: PLCCard, bnc_num: int, seconds: float = 8.0):
    print(f"\n--- Live BNC{bnc_num} input read ({seconds:.0f}s) ---")
    print(f"Manually toggle whatever is wired into BNC{bnc_num} now (e.g. pulse your camera "
          f"trigger by hand) and watch the value change below.")
    plc.configure_io(bnc_addr(bnc_num), 0)  # ensure it's configured as an input
    end = time.time() + seconds
    last = None
    while time.time() < end:
        bits = plc.read_bnc_inputs()
        state = bool(bits & (1 << (bnc_num - 1)))
        if state != last:
            print(f"  BNC{bnc_num}: {'HIGH' if state else 'low '}  (RDADC X? = {bits:#010b})")
            last = state
        time.sleep(0.1)
    print("Done watching.")


def test_plc_laser_toggle(plc: PLCCard, camera_bnc: int, laser_bncs: tuple, seconds: float = 15.0):
    section("PLC 2-laser toggle dry run (drives real BNC outputs)")
    print(f"This will configure BNC{camera_bnc} as the trigger input and drive BNC{laser_bncs[0]}/"
          f"BNC{laser_bncs[1]} as alternating outputs, exactly as mesoSPIM integration would.")
    print("DISCONNECT real laser hardware from these BNCs before continuing -- this is a wiring/logic "
          "test, verify with a scope or LEDs first.")
    typed = input("Type EXACTLY 'lasers disconnected' to proceed, anything else cancels: ")
    if typed.strip() != "lasers disconnected":
        print("Cancelled -- laser toggle test not run.")
        return

    plc.configure_two_laser_toggle(
        trigger_source_addr=bnc_addr(camera_bnc),
        laser0_bnc=laser_bncs[0],
        laser1_bnc=laser_bncs[1],
    )
    print(f"Configured. Manually pulse BNC{camera_bnc} a few times over the next {seconds:.0f}s "
          f"and confirm BNC{laser_bncs[0]}/BNC{laser_bncs[1]} alternate (scope, LED, or multimeter).")
    end = time.time() + seconds
    last = None
    while time.time() < end:
        cells = plc.read_cell_outputs()
        toggle_state = bool(cells & (1 << 0))  # cell 1 = toggle flop, LSB
        if toggle_state != last:
            print(f"  toggle flop (cell 1): {'laser1 side' if toggle_state else 'laser0 side'}")
            last = toggle_state
        time.sleep(0.1)
    print("Done. Clearing PLC state.")


def test_plc(tiger: TigerController, card_addr: int, axis: str, args):
    section("3. PLC (Programmable Logic Card)")
    plc = PLCCard(tiger, card_addr=card_addr, axis=axis)
    print(f"Using PLC at card address {card_addr}, axis {axis}.")
    print("Clearing all flip-flop/one-shot/delay state first (HOME)...")
    plc.clear_state()

    self_test_ok = test_plc_self_test(plc)

    if args.plc_camera_bnc is not None:
        if prompt_yes_no(f"\nRun a live read on BNC{args.plc_camera_bnc} so you can verify wiring?", default=True):
            test_plc_bnc_live_read(plc, args.plc_camera_bnc)

    if args.test_laser_toggle:
        if args.plc_camera_bnc is None or not args.plc_laser_bncs:
            print("\n--test-laser-toggle requires --plc-camera-bnc and --plc-laser-bncs -- skipping.")
        else:
            test_plc_laser_toggle(plc, args.plc_camera_bnc, args.plc_laser_bncs)

    print("\nResetting PLC to a clean/harmless state...")
    plc.clear_state()
    return self_test_ok


# ---------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", required=True, help="Serial port, e.g. COM5 or /dev/ttyUSB0")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--config", default=str(_REPO_ROOT / "mesoSPIM" / "config" / "examples" / "asi_dac_channels_example.json"),
                         help="JSON file with DAC channel definitions")
    parser.add_argument("--skip-dac", action="store_true", help="Skip the DAC channel test")
    parser.add_argument("--skip-plc", action="store_true", help="Skip the PLC test")
    parser.add_argument("--test-voltage", type=float, default=1.0,
                         help="Voltage used for the DAC set/read-back round trip (default 1.0V)")
    parser.add_argument("--non-interactive", action="store_true",
                         help="Don't pause for confirmation between DAC channel tests")
    parser.add_argument("--plc-card-addr", type=int, default=36, help="PLC card address (default 36)")
    parser.add_argument("--plc-axis", default="E", help="PLC axis letter (default E)")
    parser.add_argument("--plc-camera-bnc", type=int, default=None,
                         help="BNC number to live-read for wiring verification, e.g. 1")
    parser.add_argument("--test-laser-toggle", action="store_true",
                         help="Also dry-run the 2-laser toggle logic (drives real BNC outputs -- requires confirmation)")
    parser.add_argument("--plc-laser-bncs", default=None,
                         help="Comma-separated BNC pair for laser toggle test, e.g. 5,6")
    args = parser.parse_args()

    if args.plc_laser_bncs:
        args.plc_laser_bncs = tuple(int(x) for x in args.plc_laser_bncs.split(","))

    tiger = None
    dac = None
    try:
        tiger = test_connection(args.port, args.baudrate)

        if not args.skip_dac:
            try:
                channels = load_dac_channels(args.config)
            except (FileNotFoundError, ValueError) as exc:
                print(f"\nCould not load DAC channels from {args.config!r} ({exc}) -- skipping DAC test. "
                      f"Pass --config with a valid channel file, or --skip-dac to silence this.")
                channels = []
            if channels:
                dac = test_dac(tiger, channels, args.test_voltage, interactive=not args.non_interactive)
        else:
            print("\nSkipping DAC test (--skip-dac).")

        if not args.skip_plc:
            test_plc(tiger, args.plc_card_addr, args.plc_axis, args)
        else:
            print("\nSkipping PLC test (--skip-plc).")

        section("Done")
        print("All requested tests completed. See PASS/FAIL results above.")

    except KeyboardInterrupt:
        print("\n\nInterrupted -- cleaning up safely before exit.")
    finally:
        if dac is not None:
            try:
                print("\nFinal safety step: zeroing all DAC channels...")
                dac.zero_all()
            except Exception as exc:
                print(f"WARNING: could not zero DAC channels on exit: {exc}")
        if tiger is not None and tiger.is_connected:
            tiger.disconnect()
            print("Serial connection closed.")


if __name__ == "__main__":
    main()
