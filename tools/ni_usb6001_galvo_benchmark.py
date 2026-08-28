"""
NI USB-6001 -> Thorlabs QS15X-AG galvo benchmark
=================================================

Generates a hardware-timed triangle (or sawtooth) wave on ao0 using a
regenerating (looping) analog-output buffer, clocked by the device's
onboard sample clock. The USB-6001's AO is limited to 5 kS/s/channel
hardware-timed, so POINTS_PER_CYCLE is capped accordingly at high
frequencies -- the script checks this and warns you.

Requirements:
    pip install nidaqmx
    NI-DAQmx driver installed (required, not just the pip package)

Hardware:
    ao0 -> galvo command input (BNC), AO GND -> galvo GND
    Optional loopback: wire ao0 -> ai0 to self-verify the waveform with
    the device's own AI (also limited to this device's specs -- fine for
    a plausibility check, not a substitute for a scope).
"""

import time
import numpy as np
import nidaqmx
from nidaqmx.constants import AcquisitionType, RegenerationMode

# ----------------------------------------------------------------------
# Test parameters -- edit these
# ----------------------------------------------------------------------
FREQ_HZ = 100.0          # waveform frequency (set to 100.0 for the target test)
AMPLITUDE_PP_V = 1.150    # peak-to-peak amplitude in volts
OFFSET_V = 0.350          # DC offset (center of waveform)
WAVEFORM = "triangle"   # "triangle" or "sawtooth"
POINTS_PER_CYCLE = 50   # 50 pts * 100 Hz = 5000 S/s // 100 pts * 50 Hz = 5000 S/s
RUN_SECONDS = 20.0
AO_CHANNEL = "Dev1/ao0"       # adjust to your device name (check NI MAX)
AI_CHANNEL = "Dev1/ai0"       # only used if VERIFY_WITH_AI is True
VERIFY_WITH_AI = False
CSV_LOG = "ni6001_benchmark_log.csv"

MAX_HW_TIMED_RATE = 5000  # S/s/ch, hardware-timed simultaneous, per USB-6001 datasheet


def make_waveform(shape, amplitude_pp, offset, n_points):
    t = np.linspace(0, 1, n_points, endpoint=False)
    if shape == "triangle":
        tri = 2 * np.abs(2 * (t - np.floor(t + 0.5))) - 1
        wave = offset + (amplitude_pp / 2.0) * tri
    elif shape == "sawtooth":
        saw = 2 * (t - np.floor(t + 0.5))
        wave = offset + (amplitude_pp / 2.0) * saw
    else:
        raise ValueError("WAVEFORM must be 'triangle' or 'sawtooth'")
    return np.clip(wave, -10.0, 10.0)


def main():
    sample_rate = FREQ_HZ * POINTS_PER_CYCLE
    if sample_rate > MAX_HW_TIMED_RATE:
        raise ValueError(
            f"Requested sample rate {sample_rate:.0f} S/s exceeds the USB-6001's "
            f"{MAX_HW_TIMED_RATE} S/s/ch hardware-timed limit. Lower POINTS_PER_CYCLE."
        )

    waveform = make_waveform(WAVEFORM, AMPLITUDE_PP_V, OFFSET_V, POINTS_PER_CYCLE)

    log_rows = []

    with nidaqmx.Task() as ao_task:
        ao_task.ao_channels.add_ao_voltage_chan(AO_CHANNEL, min_val=-10.0, max_val=10.0)
        ao_task.timing.cfg_samp_clk_timing(
            rate=sample_rate,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=len(waveform),
        )
        # Regenerate the same buffer continuously instead of re-writing each cycle
        ao_task.out_stream.regen_mode = RegenerationMode.ALLOW_REGENERATION

        ai_task = None
        if VERIFY_WITH_AI:
            ai_task = nidaqmx.Task()
            ai_task.ai_channels.add_ai_voltage_chan(AI_CHANNEL, min_val=-10.0, max_val=10.0)
            ai_task.timing.cfg_samp_clk_timing(
                rate=sample_rate,
                sample_mode=AcquisitionType.CONTINUOUS,
            )

        ao_task.write(waveform.tolist(), auto_start=False)

        print(f"Starting AO on {AO_CHANNEL} at {sample_rate:.0f} S/s "
              f"({WAVEFORM}, {FREQ_HZ} Hz, {AMPLITUDE_PP_V} Vpp, offset {OFFSET_V} V)")
        ao_task.start()
        if ai_task:
            ai_task.start()

        t_start = time.time()
        try:
            while time.time() - t_start < RUN_SECONDS:
                if ai_task:
                    samples = ai_task.read(number_of_samples_per_channel=int(sample_rate // 10))
                    t_now = time.time() - t_start
                    log_rows.append((t_now, float(np.mean(samples)), float(np.std(samples))))
                else:
                    time.sleep(0.5)
        finally:
            ao_task.stop()
            if ai_task:
                ai_task.stop()
                ai_task.close()

    if VERIFY_WITH_AI and log_rows:
        import csv
        with open(CSV_LOG, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["t_s", "ai0_mean_v", "ai0_std_v"])
            writer.writerows(log_rows)
        print(f"Logged {len(log_rows)} rows to {CSV_LOG}")

    print("Done.")


if __name__ == "__main__":
    main()
