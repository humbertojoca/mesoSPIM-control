# Galvo DAC Benchmark Scripts

Three isolated scripts, one per device, for testing a small-amplitude
(~1 Vpp, positive-offset) triangle/sawtooth wave at 50 Hz (with 100 Hz as
the target) into a Thorlabs QS15X-AG galvo.

| Script | Device | Method | Install |
|---|---|---|---|
| `t4_galvo_benchmark.py` | LabJack T4 | Hardware Stream-Out on DAC0 (0-5V native range -- fits your amplitude with no extra hardware) | `pip install labjack-ljm` + [LJM driver](https://labjack.com/support/software/installers/ljm) |
| `ni_usb6001_galvo_benchmark.py` | NI USB-6001 | Hardware-timed regenerating AO buffer on ao0 | `pip install nidaqmx` + NI-DAQmx driver |
| `asi_tggalvo_benchmark.py` | ASI TGGALVO | Onboard single-axis waveform generator (SAF/SAA/SAO/SAM serial commands) | `pip install pyserial` |

## Why they're separate
Each uses a different vendor SDK/driver. Keeping them independent means a
missing or misbehaving driver for one device doesn't block testing the
others, and you can run them side-by-side (e.g. two terminals) if you
want to compare two devices driving two different galvo axes at once.

## Before running

- **T4**: DAC0 -> galvo command BNC, DAC GND -> galvo GND. No amplifier
  needed for ~1 Vpp around a positive offset since it fits inside the
  T4's native 0-5 V DAC range.
- **NI USB-6001**: edit `AO_CHANNEL` to match your device name in NI MAX
  (e.g. `Dev1/ao0`).
- **ASI TGGALVO**: edit `SERIAL_PORT` and `AXIS` (the axis letter
  assigned to your galvo channel) before running. Since your card runs
  `SIGNAL_DAC` firmware, `SAA`/`SAO` take the amplitude/offset directly
  in millivolts -- no counts-to-voltage scaling needed, the script just
  converts your volts to mV. (Overall output range on this firmware is
  set via `PR` rather than `PM`, worth a quick check that it covers your
  ~2 V swing.) Waveform shape (triangle/sawtooth) is set with `SAP`,
  which I've left as a placeholder (`SAP_COMMAND`) since the exact
  bit-field values depend on your firmware version -- check
  [Command:SAP](https://asiimaging.com/docs/commands/sap) and fill it in.

## Verifying the actual output

None of these scripts prove the galvo mirror moved correctly -- they
only prove what was commanded. For real benchmarking:

1. **Best**: scope both channels (or use a second, faster DAQ's AI) on
   the BNC output of each DAC and compare rise time, overshoot, and
   phase jitter against the commanded waveform.
2. **Quick self-check**: the T4 and NI scripts both have a
   `VERIFY_WITH_AI` / `VERIFY_WITH_AIN0` flag -- wire the AO output back
   to an AI input on the *same* device and it'll log the readback to a
   CSV so you can eyeball basic sanity (mean level, noise) without a
   scope. This does not verify frequency-domain fidelity since the
   device's own AI is slower/noisier than a scope.
3. For the ASI card, since the waveform is generated onboard rather than
   streamed from the PC, host-side timing tests are less meaningful --
   focus your scope work there on DAC settling behavior and the effect
   of the card's Bessel filter (tunable via `BACKLASH`) at your target
   100 Hz.

## Sample-rate math already baked in

- T4: `POINTS_PER_CYCLE=200` at 100 Hz -> 20 kS/s stream-out rate, well
  under its ~40 kHz single-channel stream-out ceiling.
- NI USB-6001: `POINTS_PER_CYCLE=40` at 100 Hz -> 4 kS/s, safely under
  its 5 kS/s/ch hardware-timed limit (the script raises an error if you
  push it over).
- ASI TGGALVO: frequency is set directly via `SAF` (period in ms); the
  card's own 4 kHz (or 4 kHz / #axes) internal clock handles the rest,
  so there's no equivalent "points per cycle" choice to make.
