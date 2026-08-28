"""
LabJack T4 -> Thorlabs QS15X-AG galvo benchmark
================================================

Generates a hardware-timed triangle (or sawtooth) wave on DAC0 using the
T4's Stream-Out feature, which loops a pre-loaded buffer at a fixed hardware
clock rate -- this avoids the jitter of software/command-response writes.

Because your target amplitude is small (~1 V) with a positive offset, this
stays within the T4's native 0-5 V DAC range, so no external amplifier/
level-shifter (e.g. LJTick-DAC) is needed for this test.

Requirements:
    pip install labjack-ljm
    LJM driver installed from labjack.com/ljm (required, not just the pip package)

Hardware:
    DAC0 -> galvo command input (BNC), GND -> galvo GND
    Optional loopback: short DAC0 to AIN0 with a wire to self-verify the
    waveform actually being produced (see VERIFY_WITH_AIN0 below).
"""

import time
import numpy as np
from labjack import ljm

# ----------------------------------------------------------------------
# Test parameters -- edit these
# ----------------------------------------------------------------------
FREQ_HZ = 100.0          # waveform frequency (set to 100.0 for the target test)
AMPLITUDE_PP_V = 1.2    # peak-to-peak amplitude in volts
OFFSET_V = 0.6          # DC offset (center of waveform), keeps it positive
WAVEFORM = "triangle"   # "triangle" or "sawtooth"
POINTS_PER_CYCLE = 200  # waveform resolution -> scan_rate = FREQ_HZ * POINTS_PER_CYCLE
RUN_SECONDS = 30.0      # how long to run the test
VERIFY_WITH_AIN0 = False  # set True if you've wired DAC0 -> AIN0 for self-check
CSV_LOG = "t4_benchmark_log.csv"

DAC0_TARGET_ADDR = 1000  # Modbus address for DAC0 (DAC1 would be 1002)
STREAM_OUT_INDEX = 0


def make_waveform(shape, amplitude_pp, offset, n_points):
    t = np.linspace(0, 1, n_points, endpoint=False)
    if shape == "triangle":
        # symmetric triangle, 0 -> 1 -> 0 normalized shape
        tri = 2 * np.abs(2 * (t - np.floor(t + 0.5))) - 1  # -1..1 triangle
        wave = offset + (amplitude_pp / 2.0) * tri
    elif shape == "sawtooth":
        saw = 2 * (t - np.floor(t + 0.5))  # -1..1 ramp
        wave = offset + (amplitude_pp / 2.0) * saw
    else:
        raise ValueError("WAVEFORM must be 'triangle' or 'sawtooth'")
    # Clip to the T4's usable DAC range as a safety net
    return np.clip(wave, 0.0, 5.0)


def main():
    scan_rate = FREQ_HZ * POINTS_PER_CYCLE
    waveform = make_waveform(WAVEFORM, AMPLITUDE_PP_V, OFFSET_V, POINTS_PER_CYCLE)

    print(f"Opening LabJack T4 ...")
    handle = ljm.openS("T4", "ANY", "ANY")
    info = ljm.getHandleInfo(handle)
    print(f"Connected: DeviceType={info[0]} SN={info[2]}")

    try:
        # Configure and start periodic Stream-Out on DAC0
        ljm.periodicStreamOut(
            handle,
            STREAM_OUT_INDEX,
            DAC0_TARGET_ADDR,
            scan_rate,
            len(waveform),
            waveform.tolist(),
        )

        scan_list_names = ["STREAM_OUT0"]
        if VERIFY_WITH_AIN0:
            scan_list_names = ["AIN0"] + scan_list_names
        scan_list = ljm.namesToAddresses(len(scan_list_names), scan_list_names)[0]

        scans_per_read = max(1, int(scan_rate / 10))
        actual_scan_rate = ljm.eStreamStart(
            handle, scans_per_read, len(scan_list), scan_list, scan_rate
        )
        print(f"Stream started. Requested {scan_rate:.1f} Hz, actual {actual_scan_rate:.1f} Hz")
        print(f"Waveform: {WAVEFORM}, {FREQ_HZ} Hz, {AMPLITUDE_PP_V} Vpp, offset {OFFSET_V} V")

        log_rows = []
        t_start = time.time()

        while time.time() - t_start < RUN_SECONDS:
            if VERIFY_WITH_AIN0:
                ret = ljm.eStreamRead(handle)
                data, dev_backlog, ljm_backlog = ret
                t_now = time.time() - t_start
                # data is interleaved [AIN0, STREAM_OUT0, AIN0, STREAM_OUT0, ...]
                ain0_samples = data[0::2]
                log_rows.append((t_now, float(np.mean(ain0_samples)), dev_backlog, ljm_backlog))
                if dev_backlog > scans_per_read * 2:
                    print(f"WARNING: device backlog growing ({dev_backlog} scans) "
                          f"-- host may not be keeping up")
            else:
                time.sleep(0.5)

        ljm.eStreamStop(handle)

        if VERIFY_WITH_AIN0 and log_rows:
            import csv
            with open(CSV_LOG, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["t_s", "ain0_mean_v", "device_backlog", "ljm_backlog"])
                writer.writerows(log_rows)
            print(f"Logged {len(log_rows)} rows to {CSV_LOG}")

    finally:
        ljm.close(handle)
        print("Device closed.")


if __name__ == "__main__":
    main()
