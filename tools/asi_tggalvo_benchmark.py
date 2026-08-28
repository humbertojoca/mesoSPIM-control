"""
ASI Tiger TGGALVO card -> Thorlabs QS15X-AG galvo benchmark
=============================================================

Unlike the T4/USB-6001 scripts, this one does NOT stream samples from the
PC. The TGGALVO/TGDAC4 card has an onboard "single-axis function" module
that generates triangle/sawtooth/square waveforms in firmware, clocked
internally at 4 kHz (divided by the number of axes on the card). The host
just sets a few parameters (period, amplitude, offset) over serial and
then arms it -- so host-side jitter is irrelevant to the waveform quality
here; what you're benchmarking is really the card's own DAC + filter
response, not USB/PC timing.

Serial commands used (Tiger syntax, confirmed against ASI's docs):
    SAF <axis>=<period_ms>      period of the pattern, in milliseconds
    SAA <axis>=<amplitude_mV>   peak-to-peak amplitude, in millivolts
                                 (SIGNAL_DAC firmware sets voltage directly --
                                 no PM-range scaling/counts involved, unlike
                                 GALVO_SPIM/PIEZO_DAC firmware on this same card)
    SAO <axis>=<offset_mV>      offset / center point, in millivolts
    SAM <axis>=<mode>           0=idle, 1=start, 3=start+sync w/ other axes
    SAM <axis>=0                stop

Note: with SIGNAL_DAC firmware the overall output range is set with the
PR command (not PM) -- worth double-checking PR covers your +/-, but you
still don't need to convert volts to counts; SAA/SAO take raw millivolts.

IMPORTANT: The exact waveform shape (triangle vs sawtooth vs square) and
any extra flags are set with the SAP command, whose bit-field values
aren't reproduced here reliably -- check `Command:SAP` in the ASI docs
(https://asiimaging.com/docs/commands/sap) for your firmware version and
adjust SAP_COMMAND below. The module's default pattern is triangle.

Requirements:
    pip install pyserial

Hardware:
    Confirm your COM port and the axis letter assigned to the TGGALVO
    card (check with Micro-Manager, ASI Console, or by sending "BU X"
    / axis query commands).
"""

import time
import serial

# ----------------------------------------------------------------------
# Test parameters -- edit these
# ----------------------------------------------------------------------
SERIAL_PORT = "COM4"     # e.g. "COM4" on Windows, "/dev/ttyUSB0" on Linux
BAUD_RATE = 115200
AXIS = "B"                # axis letter for the galvo channel you're testing
FREQ_HZ = 50.0             # waveform frequency (set to 100.0 for the target test)
AMPLITUDE_PP_V = 1.0       # peak-to-peak amplitude in volts
OFFSET_V = 1.0             # DC offset (center of waveform)
SAP_COMMAND = None         # e.g. "SAP A=0" for triangle -- set per your firmware;
                           # leave None to use the module's power-on default
RUN_SECONDS = 15.0


def volts_to_mv(volts):
    """SIGNAL_DAC firmware takes SAA/SAO directly in millivolts -- no
    counts/PM-range conversion needed, just V -> mV."""
    return int(round(volts * 1000.0))


def send(ser, cmd, expect_reply=True):
    ser.write((cmd + "\r").encode("ascii"))
    if expect_reply:
        time.sleep(0.02)
        reply = ser.read(ser.in_waiting or 1).decode("ascii", errors="replace")
        return reply
    return None


def main():
    period_ms = round(1000.0 / FREQ_HZ)
    if period_ms % 2 != 0:
        period_ms += 1  # SAF forces even ms for triangle/square patterns
        print(f"Note: rounding period to {period_ms} ms (even) as required by SAF")

    amp_mv = volts_to_mv(AMPLITUDE_PP_V)
    offset_mv = volts_to_mv(OFFSET_V)

    print(f"Opening {SERIAL_PORT} @ {BAUD_RATE} baud ...")
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.5)
    time.sleep(0.5)  # let the port settle

    try:
        # Make sure the pattern is idle before reconfiguring
        print(send(ser, f"SAM {AXIS}=0"))

        if SAP_COMMAND:
            print(send(ser, SAP_COMMAND))

        print(send(ser, f"SAF {AXIS}={period_ms}"))
        print(send(ser, f"SAA {AXIS}={amp_mv}"))
        print(send(ser, f"SAO {AXIS}={offset_mv}"))

        print(f"Starting single-axis pattern on axis {AXIS}: "
              f"{1000.0/period_ms:.2f} Hz nominal, "
              f"{AMPLITUDE_PP_V} Vpp, offset {OFFSET_V} V "
              f"(mV: amp={amp_mv}, offset={offset_mv})")

        print(send(ser, f"SAM {AXIS}=1"))  # start the pattern (mode 3 instead to sync axes)

        t_start = time.time()
        while time.time() - t_start < RUN_SECONDS:
            time.sleep(0.5)

    finally:
        print(send(ser, f"SAM {AXIS}=0"))  # stop the pattern before closing
        ser.close()
        print("Serial port closed.")


if __name__ == "__main__":
    main()
