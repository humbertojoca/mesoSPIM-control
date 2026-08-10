# Patch note: ASI Tiger DAC + PLC waveform generation backend

**Fork:** humbertojoca/mesoSPIM-control
**Adds:** an alternative to `mesoSPIM_WaveFormGenerator` (NI-DAQmx) backed
by ASI Tiger `SIGNAL_DAC_4CH` and Programmable Logic (PLC) cards, for
setups that want to drive analog outputs and trigger/gating logic
through an existing ASI Tiger rack instead of an NI-DAQmx card.

---

## Summary

mesoSPIM-control's waveform generator cleanly separates waveform
**math** (numpy, in `mesoSPIM_WaveFormGenerator.create_waveforms()` and
friends) from waveform **I/O** (6 nidaqmx-touching methods:
`create_tasks` / `write_waveforms_to_tasks` / `start_tasks` / `run_tasks`
/ `stop_tasks` / `close_tasks`) -- the same split their own
`mesoSPIM_DemoWaveFormGenerator` already uses for demo mode. This patch
adds a third implementation, `mesoSPIM_ASITigerWaveFormGenerator`,
following that exact pattern: it subclasses the real
`mesoSPIM_WaveFormGenerator`, inherits all the waveform math unchanged,
and overrides only the 6 I/O methods to drive an ASI Tiger DAC card
(`asi_tiger.ASITigerDAC`) and a PLC card (`asi_tiger.PLCCard`) instead
of an NI card.

Verified against the actual upstream `mesoSPIM_WaveFormGenerator.py`
(not a reimplementation guess): imported it, confirmed the subclass MRO
resolves correctly and all 6 lifecycle methods are overridden as
intended. Also exercised the DAC/PLC logic itself against a scripted
fake Tiger serial device (tracks per-axis voltage and PLC cell state,
not just canned replies) to catch integration bugs before this ever
touches real hardware.

## Read this first: the bandwidth gap

mesoSPIM's default timing (`samplerate=100000 Hz`, `sweeptime=0.2s`) is
20,000 analog samples per frame on an NI card. ASI's `SIGNAL_DAC_4CH`
card has a hard **4 kHz** refresh ceiling per ASI's own documentation --
25x fewer samples even with a confirmed hardware-triggered ring-buffer
playback mode, which **this patch does not implement** (see "Known
limitations" below). What's implemented today is a software-timed
serial loop, realistically **~100 Hz** (~20 samples/sweep, 1000x fewer
than NI).

That's fine for confirming wiring and trigger logic. It is **not**
adequate for real galvo/ETL scanning as-is -- expect visible
stepping/banding if you point the galvo channels at this today.
Recommended near-term architecture is a **hybrid**:

| Signal | Recommended source | Why |
|---|---|---|
| Master trigger, camera trigger, stage trigger | ASI PLC | Hardware-deterministic, replaces 3 NI tasks with zero host latency -- solid, implemented, tested |
| Laser modulation (on/off gate) | ASI DAC or PLC | Low bandwidth need (`single_pulse` waveform), fine even at 4 kHz or via PLC gating |
| Galvo sweep, ETL ramp | Keep a small NI card for now, or confirm+implement ASI ring-buffer playback first | Needs real analog bandwidth; both the software loop and the DAC's 4 kHz ceiling are meaningfully below NI's 100 kHz |

The code does not hide this: `config_check()` logs a warning on every
startup, and the module docstring spells out the numbers.

---

## Files added

```
mesoSPIM/
├── src/
│   ├── mesoSPIM_ASITigerWaveFormGenerator.py     # the adapter (~300 lines)
│   └── devices/
│       └── asi_tiger/                            # standalone driver library, no mesoSPIM dependency
│           ├── __init__.py
│           ├── controller.py    # low-level pyserial wrapper for the Tiger command/reply protocol
│           ├── dac.py           # ASITigerDAC: volts-based set/get, card-wide range handling
│           ├── waveform.py      # waveform math + software-timed streaming (WaveformStreamer)
│           └── plc.py           # PLCCard: logic-cell programming, 2-laser toggle, shutter gate helpers
└── config/
    └── examples/
        ├── config_asi_tiger_example.py           # config additions to copy into your own config
        └── asi_dac_channels_example.json         # channel layout used by the standalone hardware test

tools/
└── asi_tiger_hardware_test.py    # standalone CLI smoke test, no mesoSPIM/PyQt5 dependency
```

`mesoSPIM/src/devices/asi_tiger/` has no dependency on mesoSPIM, PyQt5,
or nidaqmx -- only `pyserial` and `numpy`. It can be tested and used
completely standalone (that's what `tools/asi_tiger_hardware_test.py`
does).

## Files changed

Only one file in mesoSPIM-control itself needs a change, and it's
additive (an `elif` branch):

### `mesoSPIM/src/mesoSPIM_Core.py`

```diff
 from .mesoSPIM_WaveFormGenerator import mesoSPIM_WaveFormGenerator, mesoSPIM_DemoWaveFormGenerator
+from .mesoSPIM_ASITigerWaveFormGenerator import mesoSPIM_ASITigerWaveFormGenerator
```

```diff
 ''' Setting waveform generation up '''
 if self.cfg.waveformgeneration in ('NI', 'cDAQ'):
     self.waveformer = mesoSPIM_WaveFormGenerator(self)
 elif self.cfg.waveformgeneration == 'DemoWaveFormGeneration':
     self.waveformer = mesoSPIM_DemoWaveFormGenerator(self)
+elif self.cfg.waveformgeneration == 'ASI_Tiger':
+    self.waveformer = mesoSPIM_ASITigerWaveFormGenerator(self)
```

### `requirements-conda-mamba.txt` / `requirements-clean-python.txt`

Add `pyserial` if not already present (mesoSPIM's own ASI stage driver,
`StageControlASI`, already depends on it, so it's likely already there
-- check before adding a duplicate).

No other upstream files are touched. `mesoSPIM_Stages.py` and
`asicontrol.py` (the existing ASI stage driver) are read from, not
modified -- see "Connection sharing" below.

---

## Connection sharing (important if your stage is ALSO ASI)

A Tiger rack is one physical device behind one serial port, even though
it hosts multiple logical cards (stage, DAC, PLC). Only one process/
handle can hold a COM port open at a time on Windows.

If `stage_parameters['stage_type']` is an ASI type, mesoSPIM's existing
`StageControlASI` (in `mesoSPIM/src/devices/stages/asi/asicontrol.py`)
already opens a `serial.Serial` on that port. The adapter's
`_get_shared_tiger_connection()` looks for
`self.parent.serial_worker.stage.asi_stages.asi_connection` and reuses
it automatically via `TigerController.from_open_serial()` -- **no
change to `asicontrol.py` needed**, since Python doesn't enforce access
control and `asi_connection` is already a plain attribute. This only
works if `asi_dac_parameters['port']` matches the stage's
`asi_parameters['COMport']`; if they differ, the adapter opens its own
separate connection and logs why.

If your stage is NOT ASI (e.g. PI stages), this is moot -- `create_tasks()`
just opens its own connection to the Tiger rack.

---

## Testing performed

- **Unit-level, mocked serial**: every `asi_tiger` module (`controller`,
  `dac`, `plc`, `waveform`) exercised against a mocked/fake serial
  backend -- connect, zero, set/get voltage, range clamping, PLC cell
  programming (verified exact command sequences generated), waveform
  generation, and the software-timed streaming loop.
- **Real import against actual upstream source**: fetched the real
  `mesoSPIM_WaveFormGenerator.py` from `mesoSPIM/mesoSPIM-control@master`,
  imported it (with PyQt5/nidaqmx stubbed, since this environment can't
  install them), then imported the adapter against it and confirmed:
  - MRO is `mesoSPIM_ASITigerWaveFormGenerator -> mesoSPIM_WaveFormGenerator -> QObject -> object`
  - all 6 lifecycle methods (`create_tasks`, `write_waveforms_to_tasks`,
    `start_tasks`, `run_tasks`, `stop_tasks`, `close_tasks`) plus
    `config_check` resolve to the adapter's overrides, not the parent's.
- **Standalone hardware-test script**: run end-to-end against a
  stateful fake Tiger serial device (tracks per-axis voltage and PLC
  cell/IO state so responses are self-consistent, not just canned
  replies) -- confirmed DAC round-trip PASS/FAIL reporting, the "skip
  this channel" interactive path, the PLC internal self-test, and that
  a `KeyboardInterrupt` mid-run still zeros all channels and closes the
  connection cleanly via `finally`.
- **Real headless run against the exact pinned PyQt5 version**: installed
  `PyQt5==5.15.11` (matching `requirements-conda-mamba.txt` exactly) and
  ran `tools/asi_dac_standalone_gui.py` under the Qt offscreen platform
  plugin against the fake Tiger serial device -- created the real window,
  connected, moved a slider through the actual signal/slot chain, ran a
  full waveform stream through the real `QThread` worker to completion,
  and disconnected, all without errors.
  `tools/asi_tiger_hardware_test.py` against your actual rack before
  trusting any of it, and read the ring-buffer caveat below before
  running a real acquisition.

## Known limitations / open items

1. **Galvo/ETL bandwidth** -- see "Read this first" above. The
   software-timed path works but is ~1000x coarser than NI. Recommend
   the hybrid architecture until one of the items below lands.
2. **DAC ring-buffer / hardware-triggered playback is unimplemented.**
   ASI's docs mention `SIGNAL_DAC_4CH` supports a ring-buffer
   multi-point save/move mode for arbitrary waveforms, which would be
   the real fix for (1) -- hardware-clocked at up to 4 kHz instead of
   software-timed at ~100 Hz. I could not confirm the exact command
   sequence to trigger ring-buffer step advances specifically on this
   card variant and didn't want to ship guessed commands controlling
   real lasers/optics. Test empirically with Tiger Console or ask ASI
   support, then implement in `asi_tiger/dac.py` +
   `mesoSPIM_ASITigerWaveFormGenerator._stream_dac_waveforms()`.
3. **PLC laser-toggle wiring is a reference implementation, not
   hardware-verified.** `PLCCard.configure_two_laser_toggle()` reproduces
   the 2-laser toggle scheme described in ASI's PLC manual (also used by
   ASI's own diSPIM plugin), and its command sequence has been checked
   against a mocked serial port, but not against a scope on real BNC
   outputs. Verify before connecting real laser hardware --
   `tools/asi_tiger_hardware_test.py --test-laser-toggle` is built for
   exactly this and requires an explicit typed confirmation before it
   drives anything.
4. **No GUI for PLC configuration.** The standalone `tools/asi_dac_standalone_gui.py`
   GUI covers DAC sliders and waveform preview (PyQt5, matching mesoSPIM's
   own dependency, not PySide6 -- verified by actually running it headless
   against PyQt5==5.15.11, the exact version pinned in
   `requirements-conda-mamba.txt`, including the QThread-based waveform
   streaming path), but PLC setup is library/script-only for now.

## Rollback

This patch is purely additive at the mesoSPIM-control level -- the only
change to existing files is the 2-line `elif` branch in
`mesoSPIM_Core.py`. Setting `waveformgeneration = 'NI'` (or `'cDAQ'` /
`'DemoWaveFormGeneration'`) in your config continues to use the
untouched original code path.
