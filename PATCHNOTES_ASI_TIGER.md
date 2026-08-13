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

## Post-release fixes (from real hardware testing)

- **`PLCCard.clear_state()` sent `card_addr=None`** while every other
  method in the class addresses the specific card -- caused a real
  `TimeoutError` on actual hardware (`! E` went out with no card prefix,
  nothing responded). Fixed to match the rest of the class:
  `card_addr=self.card_addr`, plus added the space HOME's documented
  syntax uses (`! X Y Z`, not `!XYZ`). Re-confirmed working on real
  hardware after this fix.
- `tools/asi_tiger_hardware_test.py`: PLC calls that aren't fully
  hardware-confirmed now go through a `safe_call()` wrapper that prints
  a warning and continues instead of crashing the whole run on a wrong
  guess.
- Added `--raw`: drops into an interactive console that sends whatever
  you type straight to the controller and prints the raw reply -- for
  quickly finding correct syntax against your specific firmware without
  editing code and re-running the full test each time.

### Safety-critical fix: `clear_state()` does NOT stop live PLC logic

**Found via real hardware testing**: after running
`--test-laser-toggle`, the configured laser BNCs kept alternating on
every camera trigger pulse *after the test script had already exited*.

Root cause: the PLC is a real-time hardware logic device that runs its
programmed cells continuously, completely independent of the serial
connection. `clear_state()` (HOME) only resets a *stateful cell's
stored bit* (a flip-flop's current output, a one-shot's in-progress
timer) -- it does **not** remove a cell's type/wiring programming
(`CCA`/`CCB`), and does **not** touch physical I/O configuration. A BNC
still configured as a push-pull output, still sourced from a live cell,
keeps being driven no matter what HOME does or whether anything is
connected to the port at all. Calling `clear_state()` and assuming that
made things safe was simply wrong, and the code (in both the test
script and the mesoSPIM adapter) has been fixed accordingly:

- **`asi_tiger/plc.py`**: `PLCCard` now tracks every physical I/O
  address it configures as an output (`_output_addrs`, populated
  automatically by `configure_io()`). New `safe_all_outputs()`
  reconfigures all of them back to inputs -- the one thing that actually
  stops a line from being driven. New `disable_two_laser_toggle()` /
  `disable_shutter_gate()` reverse their respective `configure_*()`
  helpers explicitly (BNCs to inputs first, then cells to harmless
  constant-0). `clear_state()`'s docstring now says plainly what it does
  and does not do.
- **`tools/asi_tiger_hardware_test.py`**: `test_plc_laser_toggle()`'s
  wait loop is now wrapped in `try/finally` calling
  `disable_two_laser_toggle()` -- runs even on Ctrl-C mid-wait (verified:
  simulated an interrupt mid-loop, confirmed the BNCs still get
  reconfigured to inputs before the exception propagates). `test_plc()`
  itself also calls `safe_all_outputs()` in its own `finally`, and
  `main()` has a third, belt-and-suspenders call to it in the outermost
  `finally` as well.
- **`mesoSPIM_ASITigerWaveFormGenerator.close_tasks()`** had the exact
  same gap -- it zeroed the DAC but never touched the PLC, meaning a
  mesoSPIM session ending (normally or otherwise) would leave the
  camera-trigger/laser-toggle logic configured in `create_tasks()`
  running indefinitely on real hardware. Fixed to call
  `self._plc.safe_all_outputs()` before zeroing the DAC and
  disconnecting. Re-verified this still imports and overrides correctly
  against the actual upstream `mesoSPIM_WaveFormGenerator.py` after the
  change.

**If you already ran `--test-laser-toggle` before this fix and the
outputs are still toggling**: reconnect (`--raw` works) and run
`M E=<bnc_addr>` then `CCA Y=0` for each affected BNC (33+N for BNC N,
e.g. BNC5 -> `M E=37` then `CCA Y=0`), or just re-run the test script
with this fix -- `test_plc()`'s startup no longer assumes a clean slate,
but `safe_all_outputs()` will only safe outputs *this session* touches,
so a manual `--raw` fix is the fastest path for outputs left live from
a previous, unpatched run.



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

## Update: hardware-timed waveform generation found (`asi_tiger/singleaxis.py`)

ASI's `SINGLEAXIS_FUNCTION` firmware module (`SAA`/`SAF`/`SAO`/`SAP`/`SAM`
commands, see https://asiimaging.com/docs/singleaxis) generates
sawtooth/triangle/square/sine waveforms **entirely on-card**, off a 4kHz
internal clock (documented up to 40kHz on "fast DAC" axes on cards that
have them). This is categorically different from `WaveformStreamer`:
there are no per-sample serial commands at all -- configure once, start
once, the card runs the waveform autonomously. If confirmed working on
your `SIGNAL_DAC_4CH` cards, **this replaces the software-timed
bandwidth problem in the "Read this first" section above for
sawtooth/triangle/square/sine galvo/ETL waveforms specifically**
(arbitrary/non-periodic waveforms would still need the ring-buffer path
or NI).

**Not yet confirmed on your firmware** -- ASI's docs describe this
module in terms of "MicroMirror cards"; `SIGNAL_DAC_4CH` is not named
explicitly. `tools/asi_tiger_singleaxis_test.py` tests this directly:
its first `SAA` command either succeeds or comes back as a real Tiger
error, which is the actual answer, not a guess. The SAP bit-mapped
config code was cross-checked against ASI's own documented example
(`SAP R=161` = triangle + external-trigger + TTL-out) and matches
exactly.

Run before relying on this:
```
python tools/asi_tiger_singleaxis_test.py --port COM4 --card-addr 37 --axis A \
    --pattern sawtooth --amplitude 2.0 --offset 0 --frequency 50 --duration 15
```
Watch the axis's BNC output on a scope while it runs. `--amplitude`/
`--offset` are refused if their peak excursion exceeds `--max-volts`
(default 10.0V) before anything is sent.

Safety note consistent with the PLC finding above: `SingleAxisWaveform.stop()`
(SAM=0) halts the pattern but does not necessarily zero the output --
`tools/asi_tiger_singleaxis_test.py` always follows it with an explicit
zero, and any future integration should do the same.

## ASI-confirmed hardware facts (from ASI support directly)

ASI's team reviewed this build and sent back specifics that correct
several assumptions made earlier in this patch. Documenting the actual
email content here rather than paraphrasing loosely, since exact
numbers matter for safety:

> For the firmware that was included on the DAC cards (firmware v3.60
> SIGNAL_DAC_4CH), the digital update rate limit is 4KHz for all four
> channels. There are analog programmable Bessel lowpass filters on the
> DAC outputs to eliminate stairstep waveforms and create smooth
> signals despite a limited update rate. There is a difference in the
> DAC devices used for the four channels, however. If extremely fast
> updates are required, consider using the second and fourth DAC card
> channels as your critical signal sources; there may be a few hundred
> microsecond less delay on axes 2 and 4. None of the three DAC cards
> were shipped with GALVO_SPIM, though I am curious if this is desired?
>
> Card 4 (H/I/J/K): 0 to 4.096V range, 400Hz LPF, intended for Tunable
> Lens control (into EL-E-4). Axes I and K are guaranteed fastest.
>
> Card 5 (P/Q/R/S): 0 to 4.096V range, 400Hz LPF, intended for analog
> laser intensity control. LPF cutoff may be changed safely with the
> Backlash command.
>
> Card 7 (A/B/C/D): -10.24 to 10.24V, 1.6KHz LPF, intended for galvo
> control with axes B and D. Limit command voltage to + and - 10000
> (+-10v) to guarantee galvo amplifier safety.
>
> The Galvo and Tunable Lens control cards had the LPF frequency chosen
> carefully to prevent damage to the galvo motor, mirror or the tunable
> lens. Sudden jumps in command voltage that are faster then the
> inertial moment of the device can cause damage.
>
> We can certainly trigger single-axis function waveforms with external
> TTL, particularly from the backplane. I think the idea is to use
> single-axis function on both the galvo and ETL DAC cards, where the
> galvo is free-running, and the etl sawtooth wave is triggered across
> the backplane, from the TGPLC card. At least one jumper will need to
> be added to header SV9 on any DAC card that needs to be triggered by
> a TTL signal from the PLC card.

### What changed in the code because of this

1. **Corrected channel mapping** (`config_asi_tiger_example.py`): galvo
   now maps to card 37 axes **B/D** (not A/C), ETL to card 34 axes
   **I/K** (not H/J) -- these are ASI's confirmed fastest-response axes,
   not an arbitrary earlier guess.
2. **Corrected per-card voltage ranges**: ETL (card 34) and laser (card
   35) are `range_code=1` (0-4.096V, unipolar) not the `range_code=4`/`6`
   used in the earlier example. Galvo (card 37) stays `range_code=6`
   (+/-10.24V hardware range) but see next point.
3. **New: `DacChannel.safety_limit_mv`** (`asi_tiger/dac.py`) -- a hard
   clamp independent of the card's hardware range_code. Needed because
   ASI's galvo amplifier safety limit (+/-10.00V) is *tighter* than the
   card's electrical range (+/-10.24V); `range_code=6` alone would
   silently allow the extra 0.24V. `safety_limit_mv=10000` on the galvo
   channels in the example config enforces the actual documented limit.
4. **New: `DacChannel.max_step_v`** (`asi_tiger/dac.py`) -- slew-rate
   limiting. Directly motivated by ASI's warning about sudden voltage
   jumps damaging the galvo/lens: when set, `set_voltage()` reads the
   current output back first and breaks a large jump into intermediate
   `M` commands of at most `max_step_v`, instead of commanding it in one
   step. Conservative starting defaults (0.5V for galvo, 0.2V for ETL)
   are in the example config, explicitly flagged as not independently
   verified-safe numbers -- tune down if you have real slew-rate figures
   for your specific galvo/lens.
5. **`singleaxis.py` updated**: the earlier "not confirmed on
   SIGNAL_DAC_4CH, docs say MicroMirror" uncertainty is now a confirmed
   fact -- these cards do NOT have GALVO_SPIM firmware, which is what
   the single-axis module needs. `tools/asi_tiger_singleaxis_test.py`
   is EXPECTED to fail with a Tiger error until ASI reflashes it. ASI's
   own suggested architecture once that happens (galvo free-running,
   ETL triggered across the backplane from the PLC) is documented in
   the module docstring, including the SV9 jumper requirement.
6. **New safety check in `asi_tiger_singleaxis_test.py`**: warns and
   requires confirmation if a configured waveform would swing negative,
   since only the galvo card supports negative voltage -- the ETL/laser
   cards are unipolar (0-4.096V) and the script had no card-range
   awareness before this, only a flat `--max-volts` ceiling.

### CORRECTION: single-axis function works NOW, GALVO_SPIM was unrelated

Points 5-6 above and the "Open decision" that used to follow them were
based on a misreading of ASI's email on my part: I conflated "these
cards don't have GALVO_SPIM firmware" (ASI asking a separate, optional
question) with "single-axis function requires firmware not installed"
(false). ASI's actual position, confirmed directly: single-axis function
works on the SIGNAL_DAC_4CH firmware already on these cards, full stop
-- no reflash needed, no open decision to make. Their suggested
architecture (galvo free-running, ETL triggered from the PLC across the
backplane) is implemented now, not blocked on a firmware update:

- `singleaxis.py`'s module docstring corrected to remove the false
  "not currently installed" framing.
- New `trigger_in_backplane_addr()` / `ttl_out_backplane_addr()` /
  `axis_slot_index()` / `enable_backplane_trigger_mode()` -- the
  backplane addressing ASI confirmed (42/44/46/48 trigger-in,
  41/43/45/47 TTL-out per axis slot 0-3), cross-checked against the
  values already in this file from the SAP docs and matching exactly.
- New `PLCCard.route_to_dac_trigger()` -- routes any PLC signal (a cell,
  a BNC input, etc.) onto a DAC axis's backplane trigger-in line.
- New `tools/asi_tiger_galvo_etl_demo.py` -- the full worked recipe:
  galvo free-running sawtooth on card 37 axis B, ETL sawtooth armed for
  external trigger on card 34 axis I, PLC routing a chosen BNC (default
  BNC1, e.g. camera trigger) onto the ETL's trigger-in line. Includes
  the same voltage-safety refusals as the other scripts (galvo
  +/-10.00V, ETL 0-4.096V unipolar) and full cleanup (stop both
  patterns, zero both outputs, reconfigure the PLC's backplane line back
  to a harmless input) on exit, Ctrl-C, or error. Run this on real
  hardware to confirm -- mock-tested against a fake serial device that
  tracks command state, not run against the actual rack yet.

Still genuinely open: the **jumper on header SV9** on the ETL card is a
physical prerequisite this script can't verify -- if the ETL never
responds to trigger pulses, check that first before assuming the
software is wrong.

## Rollback

This patch is purely additive at the mesoSPIM-control level -- the only
change to existing files is the 2-line `elif` branch in
`mesoSPIM_Core.py`. Setting `waveformgeneration = 'NI'` (or `'cDAQ'` /
`'DemoWaveFormGeneration'`) in your config continues to use the
untouched original code path.
