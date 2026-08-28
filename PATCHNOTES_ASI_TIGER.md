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

## Fix: ETL period was hardcoded to the galvo's period, plus a real trigger-mode question

Found via real hardware testing (galvo confirmed working, ETL jumpers
installed): `asi_tiger_galvo_etl_demo.py` had `etl_period_ms = galvo_period_ms`
-- the ETL's waveform period was silently forced to match the galvo's,
with no way to set it independently. This meant the ETL sawtooth
duration could never actually match a camera's real exposure time
unless it happened to equal the galvo's scan period. Fixed:

- New `--etl-period-ms`, fully decoupled from `--galvo-frequency` --
  set it to your camera's actual exposure time.
- New `--etl-trigger-mode {once, free_run}`. The demo previously only
  used `free_run` (`SAM=4`): waits for the first trigger, then runs on
  its own internal clock indefinitely -- it does NOT re-sync to later
  camera triggers and will drift over an acquisition. `once` (`SAM=2`)
  is architecturally the right mode for true per-frame sync (one ramp
  cycle per trigger), but whether it auto-rearms for the next trigger or
  needs the host to re-arm it after every single frame is genuinely
  unconfirmed here -- that distinction matters a lot (auto-rearm is
  usable at real frame rates; host re-arm over serial almost certainly
  isn't). The script now prints an explicit note to test this on a scope
  (pulse the trigger BNC multiple times, watch whether the ETL responds
  to each pulse or only the first) rather than asserting an answer.

Both `SAM=2` and `SAM=4` command generation verified against a mocked
serial device; not yet re-verified on real hardware which trigger mode
is actually correct for repeated per-frame triggering.

## Confirmed on real hardware: SAM=0 is required before M works at all

Found via real hardware testing: while an axis is under single-axis
control (including after a one-shot `SAM=2` cycle finishes -- it does
NOT return to `SAM=0` on its own), a plain `M` command is **silently
accepted (`:A`) but has no effect on the output.** No error, no
indication anything was ignored -- the axis just sits at whatever
voltage the pattern left it at (confirmed case: a one-shot sawtooth
holding at its peak indefinitely after `M {axis}=0` returned success).

This turned a documentation note ("stop() doesn't zero the output,
follow with M=0") into something that needed to be load-bearing in the
code, not just advisory text -- and exposed a real bug: both
`asi_tiger_galvo_etl_demo.py` and `asi_tiger_singleaxis_test.py` zeroed
axes with a plain `M=0` at the very START of the script too, as a
"baseline safety" step -- which silently does nothing if an axis is
already stuck in single-axis mode from a previous run (exactly the
scenario that surfaced this). Fixed:

- New `SingleAxisWaveform.stop_and_zero()` -- `SAM=0` then `M=0`, in the
  required order, as one call. `stop()`'s docstring now states the
  hardware-confirmed behavior explicitly rather than implying `M` would
  just work once you got around to it.
- Both scripts now call `stop_and_zero()` defensively at startup
  (creating a `SingleAxisWaveform` purely to reset a possibly-stuck axis,
  even before `configure()` is ever called) instead of a bare `M=0`, and
  use it in their cleanup paths instead of hand-rolling `stop()` + `M=0`
  in the right order every time.
- Cleaned up stale text in `asi_tiger_singleaxis_test.py` left over from
  before the GALVO_SPIM correction (still said "not yet confirmed,
  expected to fail" -- galvo triangle wave has since been confirmed
  working on a scope).

Verified the fixed command order (`SAM {axis}=0` then `M {axis}=0`)
against a mocked serial device; the underlying "M is silently ignored
under SAM control" behavior itself is the real-hardware-confirmed part,
not something mocked.

**Still open**: this same footgun applies to `ASITigerDAC.zero_all()`/
`set_voltage()` if a channel is ever under single-axis control when
those are called -- they'd silently no-op on that channel too, same as
plain `M`. Not currently a live issue since `mesoSPIM_ASITigerWaveFormGenerator`
doesn't yet use `SingleAxisWaveform` (only `ASITigerDAC` + `PLCCard`),
but this needs handling when/if single-axis gets wired into the adapter
-- `close_tasks()` would need to `SAM=0` any single-axis-controlled
channels before calling `zero_all()`, or that zero_all() call would give
a false sense of safety on shutdown.

## Shared trigger source: addressing camera-vs-ETL jitter

Diagnostic history: confirmed the camera's own trigger output has
negligible jitter on a scope; confirmed `free_run` mode's apparent
jitter is actually independent-clock phase drift (not fixable in
software -- once/free_run architecture is fundamentally two separate
clocks after the first trigger); confirmed `once` mode does not
auto-rearm (real hardware, both `--raw` and script agree: after one
cycle it holds at the pattern's end value until re-armed or stopped).

That leaves the camera->PLC->DAC relay chain itself as the remaining
jitter source: two independently-quantized hardware stages (the PLC's
own internal evaluation cycle, up to 4kHz so up to ~250us of
quantization uncertainty; the DAC's own internal trigger-sampling loop)
sit between the camera's clean output and the ETL's response, each
adding independent uncertainty to their *relative* timing.

**Fix direction**: have the PLC generate the trigger itself and fan it
out to both the camera's external trigger input and the ETL's backplane
trigger-in simultaneously, instead of relaying an externally-arriving
camera edge. Both branches then share the exact same output transition,
so the PLC's own quantization affects them identically and cancels out
of their relative timing -- only the DAC's own trigger response latency
remains as an unknown.

This pairs with the "once mode doesn't auto-rearm" finding rather than
fighting it: mesoSPIM already calls `run_tasks()` once per frame from
host software (matching the original NI per-sweep `single_pulse`
pattern), so host-mediated re-arming isn't architecturally foreign to
this codebase -- the open question is purely empirical: is a serial
round-trip fast enough relative to real frame rates?

New:
- **`PLCCard.fan_out_trigger(source_addr, output_addrs)`** -- routes one
  signal onto multiple physical outputs at once (any mix of `bnc_addr()`
  and `trigger_in_backplane_addr()`). Verified against a mocked serial
  device: both outputs configured from the same source cell, both
  correctly tracked by `_output_addrs` for `safe_all_outputs()` cleanup.
- **`tools/asi_tiger_shared_trigger_test.py`** -- runs N simulated
  frames, each: re-arm ETL (`SAM=2`), fire the shared PLC pulse to
  camera BNC + ETL trigger-in, wait. Reports min/max/mean/stdev of the
  `SAM=2` round-trip time so you have real numbers (not a guess) for
  whether this approach fits your target frame rate. Mock-tested
  end-to-end; the reported statistics on real hardware are the actual
  answer to that question, not something I can predict from here.

Still unconfirmed: whether this measurably reduces the observed jitter
on a scope (the reasoning above is sound, but hasn't been bench-verified
yet), and whether the per-frame re-arm round-trip is fast enough for
this project's actual target frame rate -- both are exactly what
`asi_tiger_shared_trigger_test.py` is for.

## New TGGALVO firmware for card 37: units_per_volt, and A/C become the fast axes

ASI provided new firmware for card 37 (galvo) specifically, to fix the
original speed limitation. Bench-confirmed working: **100Hz triangle
waveforms on axes A/C are visibly smooth** (no stepping), vs. the
visible stepping seen at 50Hz on the original firmware's B/D axes.

ASI's own description of the firmware:

> This firmware will provide 40KHz update rate on the first and third
> axes (A and C) and the normal 1KHz update rate on axes B and D. The
> single-axis commands all function the same, as does the B command for
> changing the filter cutoffs. The only difference is that the original
> firmware (SIGNAL_DAC firmware) takes millivolts as the input control
> units ([-10240, 10240] control range -> [-10.24v, 10.24v] voltage
> range), but the new firmware uses a control range of -4000 to 4000
> ([-4000, 4000] control range -> [-10.24v, 10.24v] voltage range). The
> new firmware is an adaptation of a micromirror control whose input
> range was in milliradians. To translate old amplitude (SAA) and
> offset (SAO) values to the new DAC coordinate system (+-4K), multiply
> the old SAA and SAO numbers by around 0.4.

Two real, breaking changes follow from this:

1. **Which axes are fast, reversed.** A/C are now the 40kHz pair, B/D
   the normal ~1kHz pair -- the OPPOSITE of the original SIGNAL_DAC_4CH
   firmware, where B/D were fastest. Every earlier reference to "B/D
   are the fast galvo axes" in this codebase was correct for the
   original firmware and is now wrong for card 37 specifically.
2. **SAA/SAO's units changed**, and by strong inference (not
   independently confirmed by ASI in this exchange, since their email
   only explicitly discusses SAA/SAO) plain M/W almost certainly changed
   too, since they address the same underlying axis position
   representation -- a firmware wouldn't typically split a single axis's
   position between two different unit conventions depending on which
   command touches it. Verified the exact ratio against ASI's "~0.4"
   approximation: 4000/10240 = 0.390625, and 1/0.390625 = 2.561 --
   matches their approximation closely enough to trust the precise
   fraction over the rounded one.

### What changed in the code

Added `units_per_volt` throughout (`asi_tiger/dac.py`,
`asi_tiger/singleaxis.py`) rather than hardcoding the mV conversion:

- **`DacChannel.units_per_volt`** (default `1000.0`, the original
  firmware's millivolt convention) and new constants
  `UNITS_PER_VOLT_SIGNAL_DAC` (1000.0) / `UNITS_PER_VOLT_TGGALVO`
  (4000/10.24 = 390.625, exact fraction, not the rounded ~0.4).
- **`ASITigerDAC.set_voltage()`/`get_voltage()`** rewritten so that
  **safety bounds (`limits_mv`/`safety_limit_mv`) stay expressed in
  real voltage terms, firmware-agnostic** -- only the final wire
  encoding (the integer actually sent in the `M` command) uses
  `units_per_volt`. This means ASI's galvo amplifier safety limit
  (+/-10.00V) keeps working correctly regardless of which firmware/
  encoding a channel uses, rather than needing separate safety logic
  per firmware. `max_step_v` ramping now interpolates in volts and
  encodes each intermediate step individually, for the same reason.
- **`SingleAxisWaveform.units_per_volt`** (new constructor field, same
  default/meaning) -- `configure()`'s SAA/SAO encoding now uses this
  instead of a hardcoded `*1000`.
- **`config_asi_tiger_example.py`**: `galvo_l`/`galvo_r` now map to
  **A/C** (not B/D), each with `units_per_volt: 4000/10.24` set
  explicitly. ETL/laser channels unchanged -- still the original
  firmware, `units_per_volt` defaults correctly for them.
- **`asi_tiger_galvo_etl_demo.py`** and **`asi_tiger_singleaxis_test.py`**:
  new `--galvo-units-per-volt`/`--units-per-volt` flags (default to the
  new firmware's constant), galvo axis default changed to `A`, usage
  examples and printed summaries updated to show which firmware's
  encoding is active.

### Testing performed

All against a mocked serial device (real hardware re-verification of
the encoding itself is still pending, though the *pattern smoothness*
at 100Hz on A/C is already bench-confirmed independent of this specific
code path):

- Backward compatibility: channels with no `units_per_volt` specified
  encode identically to before this change (verified exact command
  strings).
- New-firmware channel: 2.0V correctly encodes to 781 raw units
  (`round(2.0 * 390.625)`), cross-checked against ASI's own "~0.4"/
  "~2.56x" approximation.
- Safety limit (`safety_limit_mv=10000`) still correctly rejects 10.2V
  on a new-firmware channel -- confirms the safety check truly stayed
  firmware-agnostic, not just coincidentally correct.
- Voltage readback round-trips correctly through the new encoding
  (small float rounding only, ~0.05mV, expected from a non-power-of-10
  units-per-volt ratio).
- Slew-rate ramping (`max_step_v`) produces correctly-encoded
  intermediate steps and lands exactly on the target raw value.
- `SingleAxisWaveform.configure()`'s SAA encoding independently
  cross-checked against the same 2.56x ratio.
- Config file (`config_asi_tiger_example.py`) loads correctly through
  the exact `add_channel(**ch)` call pattern
  `mesoSPIM_ASITigerWaveFormGenerator.create_tasks()` actually uses,
  not a synthetic shortcut.
- Full syntax sweep across every file in the patch after these changes.

### Still open

- Whether plain `M`/`W` really do use the new units on card 37 is
  inferred, not independently confirmed by ASI in the text quoted above
  -- worth a direct one-line confirmation from them, or an empirical
  check (`M A=1000` on the new firmware, read back with `W A`, see what
  voltage results) before fully trusting `ASITigerDAC` on this axis.
- Whether `PR`/`range_code` (the original firmware's card-wide range
  selection) behaves the same way on the new firmware, which appears to
  have a fixed native range rather than PR's original selectable codes.
- The BACKLASH filter-cutoff investigation from earlier in this project
  (whether 1.6kHz could be raised) is superseded by this firmware
  update for the fast axes specifically -- worth revisiting whether it's
  still relevant for B/D, now the normal-speed pair.

## CORRECTION: SAM=2 ("once" mode) DOES auto-rearm

An earlier section of this document (and the code/docstrings it
described) stated, as a confirmed real-hardware finding, that SAM=2
does not auto-rearm -- that after one triggered cycle it holds at the
pattern's end value until the host re-arms it. **That finding was
wrong**, and traced to a testing-methodology artifact, not a real
hardware limitation.

What actually happened: earlier testing held the trigger line at a
sustained high level rather than producing a genuine low->high->low
pulse for each attempted trigger. A held-high level is exactly ONE
rising edge, no matter how long it's held -- indistinguishable on a
scope from "doesn't respond to further triggers," but not the same
thing as testing repeated triggering at all.

**Corrected via ASI's own docs plus direct re-testing**: `command:ttl`'s
description of `TTL X=30` (the input mode single-axis triggered modes
require) says, of SAM mode 2 specifically: *"On the rising edge of a
TTL pulse, the routine is performed once"* -- describing behavior on
*each* edge, not just the first. Re-tested with a proper pulse train (a
PLC cell explicitly toggled low->high->low three separate times via
`--raw`): **all three pulses produced a fresh ramp**, each one starting
right at its own rising edge. SAM=2 auto-rearms correctly, with no host
intervention needed between triggers.

### Practical implications

This is good news architecturally -- it removes a real constraint the
earlier design was built around:

- **No host-mediated per-frame re-arming needed.** Arm once (`SAM=2`)
  at acquisition start; every subsequent genuine trigger edge (camera-
  or PLC-generated) drives a correctly-timed cycle autonomously in
  hardware. The serial-round-trip-per-frame concern from earlier in this
  document no longer applies to this specific mode.
- **`once` is now the recommended default** for camera-synced ETL
  scanning, not `free_run` -- it doesn't have `free_run`'s independent-
  clock drift problem, and doesn't need host re-arming either.

### What changed in the code

- `SingleAxisWaveform.arm_triggered()` and `enable_backplane_trigger_mode()`
  docstrings corrected -- both explain the actual confirmed behavior and
  the earlier testing artifact, rather than silently deleting the wrong
  claim (so the mistake and its correction are both visible in history).
- `asi_tiger_galvo_etl_demo.py`: `--etl-trigger-mode` default changed
  from `free_run` to `once`; help text and runtime print updated.
- `asi_tiger_shared_trigger_test.py`: significantly simplified. The
  previous version re-armed `SAM=2` before every single simulated frame
  and measured that round-trip's timing -- built entirely on the wrong
  finding. Rewritten to arm ONCE at the start, then fire N pulses with
  zero `SAM` commands in the per-frame loop, so it now actually tests
  what matters: does auto-rearm hold up over many cycles (not just the
  3 tested by hand), and does timing stay consistent frame-to-frame.

### Still open

- The 3-pulse manual test is a small sample by hand-triggered standards.
  `asi_tiger_shared_trigger_test.py`'s rewritten form is built to check
  this at realistic frame counts and rates -- run it before fully
  trusting auto-rearm at your actual target frame rate.
- Whether there's an upper rate limit where auto-rearm starts missing
  edges (e.g. if a new trigger arrives before the DAC has finished
  processing the previous cycle) is unknown -- worth checking at your
  real frame rate, not just the slow hand-triggered pace used so far.

## Confirmed: SAM=2 needs real margin below the trigger interval

Found via real hardware testing (camera-triggered ETL, both directions
of the trigger architecture): setting `--etl-period-ms` to *exactly*
match the camera's real trigger interval causes the ETL to fire on only
**every other** trigger. Reducing the period by 2-3ms was enough to fire
on every single trigger reliably.

Mechanically this makes sense given everything else confirmed about
`SAM=2` in this document: it runs one full cycle over the programmed
`SAF` period before it's ready for the next trigger. With zero margin,
the axis is still mid-cycle exactly when the next edge arrives -- close
enough to the boundary that it misses that edge, then has a full two
periods of slack before the *following* edge, which it catches. That's
precisely an every-other-trigger pattern.

**Fix**: added an explicit margin safety check to both
`asi_tiger_galvo_etl_demo.py` (`--camera-period-ms`, purely for this
check, doesn't touch any hardware command) and
`asi_tiger_shared_trigger_test.py` (compares `--etl-period-ms` against
`--frame-interval-s` directly, since that script already knows both).
Both warn and ask for confirmation if the margin is under 2ms rather
than letting this be silently rediscovered as a mysterious
every-other-pulse pattern on a scope. Verified the warning fires
correctly at exactly zero margin and cancels on decline, and that
sufficient margin (3ms in testing) shows a clean confirmation and
proceeds without interruption.

**Still open**: whether 2ms is the right minimum margin at other frame
rates, or whether it should scale with the period rather than being a
fixed value -- 2-3ms was sufficient at the rate tested, not
independently verified across a range of frame rates. Treat the 2ms
default as a starting point to verify on a scope, not a guaranteed-safe
number for every configuration.

## Rollback

This patch is purely additive at the mesoSPIM-control level -- the only
change to existing files is the 2-line `elif` branch in
`mesoSPIM_Core.py`. Setting `waveformgeneration = 'NI'` (or `'cDAQ'` /
`'DemoWaveFormGeneration'`) in your config continues to use the
untouched original code path.
