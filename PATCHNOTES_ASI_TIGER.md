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

## Fix: trigger-input BNC could be left as an output by a prior session

Found via real hardware testing: after running `shared_trigger_test.py`
(which can drive a BNC as an *output* via `--camera-trigger-bnc`) and
then switching to `asi_tiger_galvo_etl_demo.py` (which needs that same
BNC number as an *input*, reading the real camera signal), the demo
script silently failed to trigger -- despite the camera's real signal
being clearly visible on a scope. Root cause: the controller's live I/O
configuration persists across separate script invocations as long as
the physical controller stays powered -- reconnecting the serial port
does NOT reset it, only an actual power cycle would (and possibly not
even then, if the state was ever saved with `SS`). The leftover
output-mode config from the earlier session silently carried forward.

Diagnosed by comparing two test paths directly: manually toggling a PLC
cell (`CCA F=1`) worked, because that's a different signal path
entirely (PLC-generated pulse, same mechanism `shared_trigger_test.py`
uses) -- it didn't confirm the BNC1-as-input path was healthy, and
wasn't a like-for-like comparison.

**Considered and rejected**: configuring the BNC as a fixed output and
saving that with `SS` so it "just persists." This is wrong for this
BNC's actual role -- it needs to keep listening to the camera, and
`SS`-saving an output configuration would make it stop doing that
permanently, surviving even a real power cycle.

**Actual fix**: `asi_tiger_galvo_etl_demo.py` now defensively forces the
trigger-source BNC to `IO_TYPE_INPUT` at the start of every run, before
routing it to the ETL's trigger-in line -- the same self-healing
pattern already used for the DAC axes (`stop_and_zero()`) and PLC cell
state (`clear_state()`). This doesn't depend on remembering to save
anything or on whether a power cycle happened; it just makes the
correct state true every time, regardless of what any previous session
(this script with a different `--trigger-bnc`, `shared_trigger_test.py`
with `--camera-trigger-bnc` pointing at the same number, or manual
`--raw` testing) left behind.

Verified by simulating the exact failure: a persistent mock controller
instance shared across two separate simulated script invocations (so
its live state survives "reconnecting," matching real hardware
behavior) -- pre-set the BNC to output in "session 1," ran the actual
demo script as "session 2" against that same leftover state, confirmed
it starts as `type=2` (output) and ends as `type=0` (input) after the
script's defensive reset runs.

## Confirmed: SS Z survives a real controller reset, and saves EVERYTHING

Follow-up to the previous fix (trigger-input BNC left as output by a
prior session): the user clarified they meant an actual reset/power
cycle of the ASI Tiger controller itself, not just reconnecting the
serial port -- and confirmed physical I/O type (`CCA Y`) DOES survive a
real reset if it was ever saved with `SS Z`. Manually reconfiguring BNC1
to input and running `36SS Z` fixed it permanently across resets.

Important caution surfaced by this: **`SS Z` saves the card's entire
current live configuration**, not just whatever you meant to fix. If
other test config was live at the moment of saving (leftover PLC cells,
other BNCs), that's now baked into the permanent power-on default too --
easy to not notice until it resurfaces after some future reset. This is
exactly how BNC1 ended up defaulting to output in the first place:
traced to an earlier `SS Z` call that saved that state before anyone
had reconfigured it correctly.

**Clarified relationship to the previous fix**: the software-level
defensive reset in `asi_tiger_galvo_etl_demo.py` (forcing the trigger
BNC to `IO_TYPE_INPUT` at the start of every run) does NOT depend on
what's saved -- it self-heals the live config every run regardless,
which is why that script kept working correctly across the reset even
before this was traced to a stale saved default. `PLCCard.save()` is
for giving OTHER tools/manual sessions against this controller a sane
starting point, not a substitute for that defensive pattern.

`PLCCard.save()`'s docstring now documents both the confirmed
reset-survival behavior and the "saves everything, not just your
change" caution, with a recommended safe pattern (clear_state()/
safe_all_outputs() first, configure exactly what you want, then save)
for future use.

## New: comprehensive PLC reset, for establishing a clean saved default

Follow-up to the SS Z / stale-saved-default finding above. User
confirmed no other BNCs (1-8) were touched besides the one being fixed
-- but that doesn't cover PLC logic cells (1-16), a separate address
space. `safe_all_outputs()` only resets what THIS session's own
tracking set knows it configured -- it has no visibility into leftover
cell config from a *different* prior session (e.g. `shared_trigger_test.py`'s
cell 8 used as a toggle source, or the laser-toggle cells 1-3), so it
can't guarantee a clean baseline before a `save()` call by itself.

New:
- **`PLCCard.reset_all_cells_and_io()`** -- unconditionally resets all
  16 logic cells to constant-0 and all 16 physical I/O addresses (BNC
  1-8, backplane 0-7) to input, regardless of this instance's own
  tracking history. Verified by address range against a mocked serial
  device: exactly cells 1-16 and physical addresses 33-48 touched, no
  gaps.
- **`tools/asi_tiger_plc_factory_reset.py`** -- deliberate, heavily
  confirmed tool wrapping this: confirms before resetting (disrupts any
  running logic), then a SEPARATE explicit typed confirmation
  (`"save clean default"`) before actually persisting it with `SS Z`.
  Without `--save`, only affects live state (a real controller reset
  would still revert to the old saved default) -- this distinction is
  stated explicitly in the script's own output so it's never ambiguous
  which one happened. All four paths (decline reset, reset only, reset
  + confirmed save, reset + declined save) verified against the mock.

Recommended use: run this with `--save` once, deliberately, when you
know the PLC is in a state you're happy to have as the permanent
power-on default (e.g. right after fixing the BNC1 issue) -- rather
than relying on ad-hoc `--raw` `SS Z` calls where it's easy to not
notice what else was live at that moment.

## Hardware finding: axis K has a trigger fault on this card (card 34)

Systematic debugging, all against the confirmed-good SAM=2 clean-pulse
protocol established earlier in this document (arm once, manual
low->high->low pulses via a PLC cell, checking the scope after each,
from a fresh controller `reset`):

- Axis I (backplane trigger-in 44): responds correctly to every pulse.
- Axis K (backplane trigger-in 48): **alternates** -- 1st and 3rd pulses
  fire, 2nd and 4th don't. Not "doesn't rearm" (that would mean nothing
  after the first); not a timing/margin issue (pulses were sent
  manually, seconds apart, no possibility of the axis still being mid-
  cycle when the next arrived).
- To rule out the trigger *architecture* (jumper position, backplane
  addressing, PLC routing) as the cause: moved the physical SV9 jumpers
  from 43/44+47/48 (I+K) to 41/42+45/46 (H+J) and re-ran the identical
  protocol. **Both H and J responded correctly to every pulse.**

Three axes (H, I, J) on the same card, same firmware, same protocol,
all correct. Only K exhibits this fault -- isolates it to K's own
trigger circuit on this specific card, not the general architecture,
not the software (address/slot math was independently verified correct
for K throughout: `axis_slot_index('K','H')` -> slot 3 ->
`trigger_in_backplane_addr(3)` -> 48, matching exactly what the manual
`--raw` tests also used).

**Action taken**: `config_asi_tiger_example.py`'s `etl_r` channel
changed from axis K to axis J (confirmed working) until ASI can
diagnose K. No code changes needed elsewhere -- this is a hardware
finding, not a bug in this library.

**Recommended report to ASI** (reproducible, specific):
> On card 34 (H/I/J/K, ETL card, SIGNAL_DAC_4CH firmware) -- SAM=2
> triggered via backplane addresses 42 (H), 44 (I), and 46 (J) responds
> correctly to every pulse. The same protocol on address 48 (K) only
> responds to every other pulse (1st/3rd fire, 2nd/4th don't). Tested
> from a fresh controller reset, several seconds between manually-sent
> pulses (ruling out re-arm timing), jumpers confirmed correctly
> installed at all four positions. Three other axes on the same card
> work correctly under an identical protocol -- isolated to K
> specifically.

## New: Z-stage ring buffer trigger (`asi_tiger/stage_trigger.py`)

A separate diagnostic script (`asi_trigger_test.py`, produced by another
agent working on Z-stack acquisition-loop design) confirmed on real
hardware that the Z/Theta stage card's ring buffer (`RM`/`LOAD`/`TTL X=12`)
works correctly for TTL-triggered relative stepping -- both a
software-simulated trigger (bare `RM`) and a real electrical pulse
(PLC BNC jumpered to the card's physical TRIG IN) moved the stage. This
is genuinely new capability -- distinct from the DAC/ETL/galvo
triggering (`SAM`/`SAP`) already built and validated in this project.

**Deliberately NOT adopted**: that same diagnostic script explored an
analogous ring-buffer approach (`TTL X=1`, absolute mode) for the DAC
cards too, as an alternative to `SAM`/`SAP`. Not adopted here -- it was
never confirmed working on DAC hardware in that script's own testing
(its code hedges "skip if the DAC card has no separate TTL IN BNC"
without a working backplane-triggered result), and `SAM`/`SAP` is
already extensively validated (including finding the real K-axis fault
documented above). Two competing, only-one-proven mechanisms for the
same job isn't worth the confusion.

New:
- **`asi_tiger/stage_trigger.py`**: `StageRingBuffer` (clear/load/arm/
  disarm/software_trigger/where) + `load_ring_buffer_point()`, wrapping
  the confirmed `RM`/`LOAD`/`TTL X=12` sequence. `LOAD` is deliberately
  NOT card-addressed in this wrapper -- confirmed real Tiger quirk (the
  axis letter alone identifies the card), and verified this exact
  omission against a mocked serial device.
- **`tools/asi_tiger_stage_ring_buffer_test.py`**: reimplements the
  diagnostic script's confirmed software-self-test + electrical-test
  sequence through the library, with this project's usual safety
  conventions (confirmation prompts, defensive cleanup in `finally`).

### Transport: adopted the more lenient reply terminator

The diagnostic script's `TigerSerial.send()` accepts either `\r\n` or a
bare `\r` as a valid reply terminator; our `TigerController` previously
required an exact `\r\n` match (via pyserial's `read_until`). Not a
correctness bug -- pyserial's `read_until` still returns whatever was
buffered once the port timeout elapses even without finding the exact
terminator -- but it would silently cost a full timeout's stall (0.5s)
on any command that replies with a bare `\r`. Adopted the lenient
polling read as `TigerController._read_reply_lenient()`, given this
project is now sending several command types (`LOAD`, `WHERE`, bare
`RM`, `RM Y?`) that hadn't been exercised with our transport before.
Re-verified against everything already validated (DAC set/read, PLC
`clear_state()`) after the change -- no regressions.

## New: ring-buffer capacity bench test (before designing PLC counter logic)

The design doc's hardest remaining piece is a PLC counter cell topology
to gate the self-sustaining Z-move-complete->next-frame-trigger loop so
it runs exactly N times per row then stops. Could not find ASI's own
documented "N pulses then stop" example to build from with confidence
-- hand-designing custom flip-flop/one-shot sequential logic blind, for
something where a mistake means a real Z-stage overrun (not just a
software bug), isn't something to guess at.

**Reframed the bench test around a simpler question first**: does the
ring buffer itself already provide this behavior for free? If exactly
N points are loaded and the axis is triggered more than N times, does
it move N times and then simply stop (nothing left queued -- "N then
stop" for free, no counter cells needed), or does something else happen
(wraparound, repeat, error)?

New: **`tools/asi_tiger_ring_buffer_capacity_test.py`** -- loads N
points, triggers N+extra times via the software self-test (safe, no
wiring), reports moves-per-trigger and whether the simple hypothesis
held. Verified the script's interpretation logic correctly distinguishes
both possible outcomes against two different mock behaviors (exhausts-
and-holds vs. wraps-and-repeats) -- but the mock's specific behavior is
just an assumption I built into the simulator, NOT a confirmation of
what real hardware does. Only a real-hardware run answers the actual
question. If the hypothesis holds, the design doc's hardest open item
may not need any new PLC logic at all; if not, the exact failure mode
observed (which trigger, what happened) is the input needed to design
the real fix correctly rather than guessing.

## New: pulse-pass-through counter, ported from ASI's own documented example

Found ASI's actual documented "N pulses then stop" example --
https://www.asiimaging.com/docs/tiger_programmable_logic_card#fixed_number_of_pulses_from_trigger
and, more directly applicable, "Pass through pulse N*M times" (a
real customer's deployed solution for exactly this project's problem:
"pass an incoming pulse through a set number of times and then disable
the pass-through"). This replaces the earlier plan to hand-design
custom flip-flop/one-shot sequential logic from scratch, which was
correctly flagged as too risky to invent blind given the real-world
consequences of getting it wrong (Z-stage overrun).

`PLCCard.configure_pulse_pass_through_counter()` / `reset_pulse_pass_through_counter()`
are a faithful port of ASI's BeanShell example -- six cells (two
cascaded one-shot NRT counters for a total of n_inner*n_outer up to
~65535^2, a latch flop, a delay flop to let the final pulse complete
before blocking, and an output AND gate). Verified by hand, address by
address, against ASI's literal script values (not just trusting the
docstring's claim of fidelity): every `rising_edge()`/`falling_edge()`/
`inverted()`-computed address matches their hardcoded numbers exactly
(e.g. `falling_edge(cell_addr(2))` = 194 = their
`addrEdge+addrInvert+addrInnerCount`), and the reset sequence's
`CONST_HIGH=64` deliberately matches their literal script value rather
than a stricter reading of the cell-type reference table (real
customer-tested values should win over a possibly-imprecise table
entry).

**Constraint carried over from the one-shot cell's documented
behavior**: both `n_inner` and `n_outer` must be >= 2 -- a one-shot's
`duration=0` config (`n=1`) is documented to "never go high," which
breaks the counting logic entirely rather than blocking after 1 pulse.
Raises `ValueError` for `n < 2`.

New: **`tools/asi_tiger_pulse_counter_test.py`** -- Stage 1 bench test,
pure software validation (a PLC cell simulates the incoming pulse via
direct `CCA F` state writes -- the same confirmed mechanism used
elsewhere in this project -- and `RDADC Z?` reads the output AND
gate's computed value directly, no scope needed). Tests both the main
counting behavior and that `reset_...()` correctly re-arms for a fresh
count. Verified the exact command sequence sent matches the hand-
derived expectations command-by-command against a mocked serial
device -- but deliberately did NOT build a simulated one-shot/D-flop
model to fake a PASS/FAIL result, since a bug in a custom simulator
built specifically to validate this could mask a real bug rather than
catch one. The mock intentionally cannot validate the actual counting
behavior (confirmed by running it: reports FAIL across the board, since
`RDADC Z?` always returns a fixed value unrelated to real cell state) --
**this script's real purpose is a Stage 1 bench test on actual
hardware**, not something to trust from a mock run.

Stage 2 (a real electrical test with an actual external pulse source
and a scope on the real output BNC) should follow only after Stage 1
passes on real hardware -- not built yet, and not needed until Stage 1
is confirmed.

## CORRECTION: the ring buffer wraps around, it does not exhaust and stop

An earlier section of this document floated an untested hypothesis: if
you `LOAD` exactly N points into the Z-stage ring buffer, maybe it
naturally stops responding once exhausted, giving "N triggers then
stop" for free without needing the PLC counter at all. **This was
wrong**, confirmed against ASI's own ring buffer documentation
(https://asiimaging.com/docs/ring_buffer):

> "Each press of the @ button causes the stage to advance to the next
> position. When you reach the last position, the next press... will
> take you back to the first position."

It's a genuine **circular buffer**, not a queue that exhausts. The
earlier capacity-test script tested the wrong hypothesis and has been
replaced.

### What this clarifies about the design

1. **The PLC pulse-pass-through counter is not optional or a fallback
   -- it's the only thing that gates "stop after N frames."** The ring
   buffer will happily repeat forever on its own, by design. This
   confirms the original design doc's architecture was right all along.
2. **Usefully, for uniform Z-stepping** (the actual use case -- same
   relative delta every plane, not distinct positions): loading exactly
   **one** relative step means every trigger wraps to that same single
   entry. You never need more than 1 loaded point regardless of how
   many planes are in the stack -- which means the ring buffer's
   documented 50-position default (250 on request from ASI) capacity
   limit is **irrelevant to this design** entirely, not something that
   needs to be worked around.

### Corrected architecture

- Row-level (once per row, slow): absolute `MOVE` to `Z_start`.
- Row-level (once per row, slow): `LOAD` exactly one relative step, arm
  `TTL X=12`.
- The PLC pulse-pass-through counter (single-counter or two-counter
  version) gates the actual "stop after N frames" -- confirmed this is
  necessary, not a nice-to-have.

### What changed in the code

- `asi_tiger/stage_trigger.py`'s module docstring corrected -- states
  the wraparound behavior plainly, with the corrected architecture, and
  points to the earlier wrong assumption rather than silently deleting
  it (matching how prior corrections in this document are handled).
- **`tools/asi_tiger_ring_buffer_capacity_test.py` removed**, replaced
  by **`tools/asi_tiger_ring_buffer_wraparound_test.py`**, which tests
  the actual documented behavior: (1) single-entry consistency -- one
  loaded step, triggered repeatedly, moves by exactly that amount every
  time, no ceiling; (2) multi-entry wraparound proof -- several
  *distinct* loaded steps, triggered through multiple full cycles,
  confirming the delta sequence repeats in order rather than stopping.
  Verified the script's own detection/reporting logic against a
  correspondingly-fixed mock (the earlier mock modeled exhaustion via
  list-popping; fixed to model wraparound via a cycling pointer,
  matching the real documented behavior) -- this validates the script's
  logic is sound, not a substitute for the real-hardware confirmation
  that matters here.

## Confirmed on real hardware: ring buffer capacity is 50, and two distinct native modes exist

Directly tested on real hardware via `RM F?`/`RM F=0`/`RM X?`/`RM F=1`,
cross-referenced against ASI's `command:rbmode` docs, resolving the
wraparound correction above with an important addition: there are TWO
genuinely different native ring buffer modes, not one.

- **`RM F=1` (TTL Triggered Mode, default)** -- moves to the next
  position and **wraps** around at the end. This is the mode used
  throughout this project (the single-relative-step architecture).
  `RM X?` here reports the number of *used* positions.
- **`RM F=0` (Consume Mode)** -- a trigger *consumes* a position if one
  exists: genuine hardware exhaust-and-stop behavior, confirmed for
  real this time (unlike the earlier wrong hypothesis about F=1).
  Per ASI's docs, "this mode reduces the capacity of the ring buffer by
  1" -- confirmed on real hardware: `RM X?` read `50` immediately after
  switching to consume mode (this specific rack's actual capacity, not
  the optional 250-position upgrade), meaning **49 usable positions**
  in this mode specifically. `RM X?` here reports *open* (not used)
  positions instead. Also confirmed: **switching `F` in either
  direction clears the ring buffer and resets its indices** -- don't
  assume anything loaded before a mode switch survives it.

**Decision, now justified by both the capacity number and the
underlying mechanism**: Consume Mode is not used for this project. 49
positions is too few for realistic Z-stack sizes, and it doesn't offer
anything the PLC pulse-pass-through counter doesn't already provide
without a capacity ceiling (validated to N=400). F=1 (wrap) plus the
PLC counter remains the architecture.

New: `StageRingBuffer.set_mode()`/`query_mode()`/`query_position_count()`
and `RB_MODE_*` constants (CONSUME/TTL_TRIGGERED/ONESHOT_AUTOPLAY/
REPEAT_AUTOPLAY/ONESHOT_AUTOPLAY_NO_RETURN), documenting all of
`RBMODE`'s F-mode options for completeness/future reference, even
though only F=1 is used in the chosen architecture. Verified the exact
command sequence generated (`RM F=0`, `RM X?`, `RM F=1`, `RM X?`)
matches the user's own real-hardware terminal session character-for-
character.

## RESOLVED: read index not reset by clear(), AND arming itself silently advances it

Real hardware run of `asi_tiger_ring_buffer_wraparound_test.py` (single
entry, 20 triggers; 3 distinct entries, 3 cycles) surfaced two distinct,
real anomalies -- diagnosed by pattern-matching the observed deltas
against the expected cycle at different offsets rather than dismissing
the "FAIL" results as noise. Both are now understood and fixed.

1. **`clear()` (`RM X=0`) does not reset the read index** -- confirmed
   against ASI's docs (only an `RM F=<mode>` transition is documented
   to reset read/write indices). **Fixed**: `clear()` now also sends
   `RM Z=0` explicitly.

2. **Arming itself silently advances the read index by one position**
   -- confirmed directly and conclusively on a second real-hardware run
   using the new `query_read_index()` visibility: `read_index=Z=1`
   immediately after `arm_relative()`, with **zero triggers sent**,
   despite the index having been confirmed still at `Z=0` right after
   loading (isolating the cause to arming, not loading). This is an
   undocumented side effect of `TTL X=12`, not something in this
   library's control to prevent. Once accounted for, the previously
   "FAILed" Test 2 data lines up cleanly against the cycle shifted by
   exactly this offset (4 of 9 exact matches, rest within the same ±1
   settling noise seen in Test 1). **Fixed**: `arm_relative()` (and
   defensively, `arm_absolute()`) now send `RM Z=0` again as their
   final step, immediately after `TTL X=`, compensating for the
   advance so the first real trigger reliably applies the first loaded
   point.

**Test 1's remaining ±9/±11 alternating deltas (avg exactly +10) are
NOT a logic bug** -- the read index stayed rock-solid at `Z=0->Z=0` for
all 20 triggers (airtight proof the single-entry wraparound logic
itself is correct), and the alternating pattern around the true value
is a classic position-reporting/settling signature at the 0.1 micron
level, not a counting error. Not worth chasing further unless it
becomes relevant at that precision for real acquisitions.

Re-run needed on real hardware to confirm the arm-time Z=0 fix fully
resolves Test 2's offset now that the actual cause (not just its
symptom) is addressed.

## CONFIRMED FIXED: follow-up real hardware run

Re-ran `asi_tiger_ring_buffer_wraparound_test.py` after the arm-time
`RM Z=0` fix above. Result: **Test 2's systematic offset is gone.**
`read_index=Z=0` after arming (was `Z=1` before the fix), and the delta
sequence now follows the loaded `10,20,30` cycle correctly, cycling
`0->1->2->0` exactly as expected. The remaining "UNEXPECTED" markers in
both tests are all within +/-1 (tenths-of-micron) of the expected
value, and the read index itself stayed correct throughout every single
trigger in both tests -- confirms the actual counting/wraparound logic
is correct; what's left is real position-reporting/settling noise at
the 0.1 micron level, not a software bug.

Also observed, separately: arming produced a small (~1 loaded-step)
physical move on its own in this run, but did NOT in the prior run --
inconsistent between two otherwise-identical runs, unlike the
deterministic Z=0->Z=1 index bug that's now fixed. Most likely ordinary
backlash/static-friction take-up on first motor engagement after the
stage sits idle, not a ring-buffer logic issue (the architecture's
row-level absolute MOVE to Z_start, which happens before arming, should
already absorb most of this in practice). Not treated as something to
fix in software given the inconsistency -- instead added an optional
`settle_s` parameter to `arm_relative()`/`arm_absolute()` (default 0,
preserves prior behavior) as a cheap hedge: pass e.g. 0.3-0.5s to let
any residual settling finish before an acquisition loop's first real
trigger. Verified the parameter actually delays by the requested amount
and that the default remains delay-free.

## New: TTL Y= (OUT0) support + discovery test for the loop's missing link

Before starting `configure_zstack_trigger_chain()` (the design doc's
orchestration function), identified that ONE piece of the self-
sustaining loop had never been tested: the Z card emitting its own
completion pulse (`TTL Y=2`) that's supposed to become the next frame's
trigger. Every other piece (galvo free-run, ETL triggered sweep, Z
ring-buffer stepping, PLC pulse-pass-through counter) was already
bench-validated -- building the orchestration function around this
untested link would have repeated exactly the mistake avoided earlier
with the pulse counter (guessing at PLC logic instead of testing it).

Checked ASI's actual `command:ttl` docs before assuming anything about
`OUT0_mode=2`: confirmed it "generates TTL pulse at end of a commanded
move" as expected, but also surfaced an important wrinkle -- OUT0 is
documented as normally being the card's own **physical OUT connector**
(paired with the IN0 connector the ring-buffer electrical test already
jumpers), NOT automatically a numbered backplane line. Backplane
routing for a completion-style pulse is only documented for a
*different*, unrelated mode (21). Didn't want to guess which applies to
this specific card.

New:
- **`StageRingBuffer.set_output_mode()`/`set_output_pulse_duration()`**
  (`TTL Y=`/`RT Y=`) and `OUT0_MODE_*` constants.
- **`PLCCard.configure_pulse_catchers()`/`reset_pulse_catchers()`** --
  a bank of latching D-flops that catch any rising edge on a set of
  addresses and hold it, so a brief completion pulse can be reliably
  detected via a slow serial readback afterward regardless of exact
  timing -- no scope needed for discovery, matching this project's
  established software-first-test methodology.
- **`tools/asi_tiger_z_output_discovery_test.py`** -- arms the Z ring
  buffer (confirmed-working), sets `TTL Y=2`, catches across all 8
  backplane lines simultaneously plus an optional physical-OUT-jumpered
  BNC, fires one real move, reports which address (if any) caught the
  pulse. Verified end-to-end against a mocked serial device (control
  flow, command sequencing, catcher cell assignment for both 8-address
  and 9-address configurations) -- the mock cannot simulate a real
  OUT0 pulse existing anywhere, so this only validates the script
  itself, not the real answer.

Next step: run this on real hardware to find where (if anywhere) the
pulse actually appears, before writing `configure_zstack_trigger_chain()`
around it.

## Fix: pulse catchers never forced BNC addresses to input -- repeat of an earlier bug class

Confirmed via real hardware and an oscilloscope: the Z/Theta card DOES
have a physical OUT0 connector, and it DOES emit a real, clean 1-second
pulse (with `RT Y=1000`) on every `RM` trigger -- direct evidence
`RT Y` units are milliseconds. But `asi_tiger_z_output_discovery_test.py`
still reported "nothing" on the jumpered BNC even with this confirmed
signal present, which ruled out a wiring/timing explanation and pointed
at a real bug in `configure_pulse_catchers()`.

**Root cause**: the method configured a D-flop to *listen* to each
address's rising edge, but never explicitly set BNC addresses (33-40)
to `IO_TYPE_INPUT` first. Per ASI's docs (already documented elsewhere
in this same file): front-panel BNCs default to being **push-pull
outputs**, not inputs -- backplane lines (41-48) default to input,
which is why the earlier all-backplane sweep result was actually
correct data (nothing there, consistent with the scope test showing
the pulse lives on the physical connector) while the BNC-specific check
was silently broken. This is the exact same bug class found and fixed
once already in this project (`asi_tiger_galvo_etl_demo.py`'s trigger
BNC defaulting to output from a prior session) -- missed applying the
same defensive pattern here on review.

**Fixed**: `configure_pulse_catchers()` now explicitly forces any BNC
address (33-40) in its input list to `IO_TYPE_INPUT` before wiring its
catcher cell, leaving backplane addresses untouched (already correct by
default). Verified the exact command sequence: a backplane address
goes straight to its catcher's D-flop config with no extra I/O call,
while a BNC address gets an explicit `CCA Y=0` immediately before its
catcher is wired.

Next: re-run `asi_tiger_z_output_discovery_test.py --physical-out-bnc <N>`
with this fix -- given the pulse is scope-confirmed real and present,
it should now actually be caught.

## New: isolated full-loop test, before committing to configure_zstack_trigger_chain()

With every individual piece of the design doc's fast per-frame loop now
validated (galvo, ETL, Z-stepping, pulse counter, and -- just confirmed
-- the Z card's OUT0 completion pulse on its physical connector),
deliberately did NOT jump straight to writing
`configure_zstack_trigger_chain()` as permanent library code. Combining
several individually-correct pieces into a genuinely new thing (here: a
real closed hardware loop, not just a sequence) is exactly the point
where a subtle integration mistake becomes possible even when every
piece is separately proven -- worth one more isolated check first.

New: **`tools/asi_tiger_zstack_loop_isolated_test.py`** -- wires the
full topology: a one-time host "kick" OR'd with the pulse counter's
output drives a BNC jumpered to Z's IN0; Z's OUT0 (confirmed physical,
not backplane) feeds back into the counter's `pulse_in` via a second
jumper. Once kicked, the loop runs entirely autonomously in hardware --
the script only polls `WHERE` to observe it, never participates. Bounded
by `--max-wait-s` with a defensive disarm if no clean stop is detected
in time. Requires two jumpers (Z OUT0 -> PLC BNC2, PLC BNC1 -> Z IN0)
matching wiring already in place from the discovery test.

Verified before real hardware use: the OR gate correctly combines the
kick cell and the counter's own AND-gate cell (not a redundant BNC
readback), the loop-output BNC is correctly sourced from the OR gate,
and there's no cell-address collision between the counter's cells (1-5)
and the new kick/OR-gate cells (6-7). The mock cannot simulate the real
cross-card physical loop (PLC output -> real Z IN0 -> real move -> real
Z OUT0 -> real PLC input) -- only real hardware can confirm the loop
actually closes and stops at N -- so this was checked at the level the
mock CAN meaningfully verify (wiring logic, cell allocation, script
control flow and safety cleanup), not faked into reporting false
confidence about the dynamic behavior.

Not yet promoted to `configure_zstack_trigger_chain()` -- waiting on a
real-hardware run of this isolated test first.

## New: isolating whether TTL Y=2 or zero-delay retriggering caused the loop glitch

The isolated full-loop test moved Z only 1 of 5 expected times, with
unexpected brief pulses seen on a scope. Before writing more PLC logic
to fix it, worked through the user's own restatement of the intended
architecture (global trigger -> camera; Expose-Out RISING edge -> ETL;
Expose-Out FALLING edge -> Z; Z's OUT0, if count < N -> next global
trigger) against what the isolated test actually did, and found a real
scope mismatch: the isolated test skips camera+ETL entirely, replacing
that whole chain with a direct OUT0->IN0 feedback with essentially
ZERO delay (bounded only by the PLC's ~250us eval cycle). The real
architecture always has at least one full camera exposure period
between consecutive Z moves -- a substantial natural dead-time the
isolated test didn't have. Given Z was ALREADY confirmed to handle
repeated triggers cleanly at 200-300ms host-paced spacing (the
wraparound tests), the leading hypothesis shifted from "IN0 signal
shape/re-triggering is fundamentally broken" to "zero-delay
retriggering specifically is the problem, not TTL Y=2 or IN0 in
general."

New: **`tools/asi_tiger_z_spacing_test.py`** -- tests exactly this,
using ONLY already-validated primitives (no new PLC logic): arms Z with
TTL Y=2 configured (matching the failing test's setup) and RT Y=2000,
but triggers it via well-spaced host `software_trigger()` calls instead
of a zero-delay hardware loop. Two possible outcomes and what each
means: clean 5-for-5 confirms the fix is inserting real dead-time into
the hardware loop rather than direct feedback; failure even with
generous spacing would point at TTL Y=2 configuration itself
interfering with normal triggering, a more fundamental question for
ASI. Verified end-to-end against the mock (control flow only, not a
real answer -- only real hardware resolves which hypothesis is correct).

## New: confirming the real camera's Expose-Out signal, before wiring anything to it

First real external (non-Tiger-generated) signal in this project.
Confirmed by the user directly: Photometrics Iris 15, Rolling Shutter
mode (matching the design doc's specific recommendation -- avoids "All
Rows" mode, whose falling edge fires before the full frame actually
finishes), genuine TTL (5V) levels confirmed via scope + PVCAMTest --
directly compatible with a PLC BNC input, no signal conditioning
needed.

New: **`tools/asi_tiger_camera_expose_test.py`** -- before wiring ETL
(rising edge) and Z (falling edge) triggers to this signal, confirms
the PLC reliably detects BOTH edges using the same latching-catcher
technique validated for the Z OUT0 discovery test, run across multiple
reset-wait-read cycles (not just once) to rule out a one-off fluke.
Applies the BNC-defaults-to-output fix from earlier directly (forces
the camera BNC to input before wiring catchers to it). Verified the
exact rising/falling edge address computation against the mock
(`rising_edge(bnc_addr(3))=163`, `falling_edge(bnc_addr(3))=227`,
correctly distinct) and that BNC3 is forced to input before either
catcher references it -- the mock cannot simulate a real external
camera signal, so only real hardware answers whether detection is
actually reliable.

## New: configure_zstack_trigger_chain() -- the full orchestration function

With every individual signal in the design doc's fast per-frame loop
now validated on real hardware (galvo, ETL, Z-stepping, Z's OUT0 on its
physical connector, the pulse counter, the camera's Expose-Out edges),
built the actual orchestration function wiring them together:

    Camera trigger (gated by counter)
        -> camera exposes
        -> Expose-Out RISING edge  -> ETL external trigger (SAM=2)
        -> Expose-Out FALLING edge -> Z ring-buffer trigger
        -> Z's OUT0 (move complete, physical connector)
        -> pulse-pass-through counter's pulse_in
        -> if count < n_planes: counter's output -> camera trigger again

`asi_tiger/zstack_chain.py`: `configure_zstack_trigger_chain()` returns
a `ZStackTriggerChain` handle (`.reset()`/`.kick()`/`.disarm()`). Scoped
narrowly -- owns Z's ring-buffer setup directly (since step/n_planes are
core parameters of this function), but requires ETL's waveform to
already be configured and armed by the caller (SingleAxisWaveform),
keeping optics-specific configuration separate from trigger-chain
routing. Includes BNC-collision validation (`counter_monitor_bnc` can't
collide with `z_out0_bnc` or the other BNCs already used in the chain).

Verified against a mocked serial device: the full chain configures
without error (53 commands, no exceptions); ETL's trigger-in (backplane
addr 44 for axis I) is correctly sourced from the rising edge of the
camera Expose-Out BNC; Z's trigger BNC is correctly sourced from the
falling edge of that SAME address (not two different addresses by
mistake); the BNC-collision check correctly rejects a colliding
`counter_monitor_bnc`; `.kick()` and `.reset()` send exactly the
expected command sequences.

### The one genuinely new, untested piece: driving the camera's trigger input

Everything above is a recombination of individually-validated signals.
`configure_zstack_trigger_chain()` also drives a BNC meant for the
camera's physical trigger input -- unlike Expose-Out, which has only
ever been READ in this project, nothing has yet driven a signal INTO
the camera. New: **`tools/asi_tiger_camera_trigger_test.py`** -- two
stages (single pulse, confirms any response; several well-spaced
repeated pulses, confirms reliable per-trigger response, not just the
first) -- relies on the user watching PVCAMTest's frame counter, since
this script has no way to query the camera itself. Verified the
pass-through and fail-early (stops before stage 2 if stage 1 doesn't
respond) paths both work correctly against the mock.

### Staged test plan for the full chain, before trusting it for real acquisitions

Given the complexity of combining four subsystems (camera, ETL, Z,
counter) for the first time, do NOT jump straight to running the whole
chain. Recommended order:

1. **`asi_tiger_camera_trigger_test.py`** (above) -- confirm the camera
   actually responds to a PLC-driven trigger at all, in isolation.
2. **ETL responds to Expose-Out's rising edge** -- with the camera
   free-running (not yet controlled by the chain), route Expose-Out's
   rising edge to ETL's trigger-in and confirm ETL sweeps in sync,
   watching a scope or using RDSTAT, over several camera cycles.
3. **Z responds to Expose-Out's falling edge** -- same idea, confirm Z
   steps correctly in sync with exposure end, camera still free-running,
   not yet gated by the counter.
4. **Full loop** -- only after 1-3 pass individually, wire the counter
   +kick+OR-gate to actually control the camera (no longer free-running)
   via `configure_zstack_trigger_chain()`, and confirm the loop runs
   exactly `n_planes` times then stops, with camera/ETL/Z all
   participating together autonomously.

Steps 2 and 3 aren't built yet -- next up.

## Confirmed: camera trigger works (staged test plan, step 1 of 4)

`tools/asi_tiger_camera_trigger_test.py` run on real hardware: both
stages passed cleanly (single pulse captured a frame; 5 repeated
well-spaced pulses correctly advanced the frame counter by exactly 5).
The genuinely new piece -- driving a signal INTO the camera -- works.

## New: steps 2 and 3 of the staged test plan (ETL/Z respond to real camera edges)

With camera triggering confirmed, built the next two steps -- testing
whether ETL and Z each correctly respond to the camera's REAL
Expose-Out edges, with the camera FREE-RUNNING (not yet controlled by
the counter/kick), before wiring the full closed loop.

- **`tools/asi_tiger_etl_camera_sync_test.py`** (step 2): routes
  Expose-Out's rising edge directly to ETL's backplane trigger-in
  (same `configure_io(..., source_addr=rising_edge(...))` pattern
  verified in `configure_zstack_trigger_chain()`), arms ETL (SAM=2,
  already confirmed auto-rearming), and asks for visual scope
  confirmation across several camera cycles -- no clean software-only
  way to confirm a waveform actually swept, unlike Z's simple position
  readback.
- **`tools/asi_tiger_z_camera_sync_test.py`** (step 3): routes
  Expose-Out's falling edge to a BNC jumpered to Z's IN0, then polls
  `WHERE` over a watch window, fully software-confirmable (reports
  exact deltas and inter-move timing, no scope needed).

Both verified end-to-end against the mock (control flow, correct
backplane/BNC address routing for ETL's axis I -> addr 44) -- the mock
cannot simulate a real external camera signal, so both correctly report
"nothing detected" against it; only real hardware answers the actual
question. Full regression swept across every file in the patch after
adding these two.

Staged plan progress: step 1 (camera trigger) confirmed on real
hardware. Steps 2 and 3 (this entry) are built and ready to run. Step 4
(the full closed loop via `configure_zstack_trigger_chain()`) should
only follow once 2 and 3 both pass.

## Fix: routed brief PLC-computed edge pulses instead of sustained levels

Step 2 of the staged test plan (ETL sync to camera Expose-Out) failed
on real hardware. The user's own instinct to double-check "PLC input
and mode for single axis function," recalling past fixes in this exact
area, pointed at the right place before any new guessing was needed.

**Root cause**: `rising_edge(expose_addr)`/`falling_edge(expose_addr)`
compute a PLC-INTERNAL signal that's only high for about one PLC
evaluation cycle (~250us) at the exact moment of the real transition --
a brief, PLC-computed pulse. Every previously confirmed-working ETL/Z
trigger in this project instead used a genuine, SUSTAINED voltage-level
change on the backplane/BNC line, letting the destination axis card's
own trigger hardware do its own edge detection on a signal it can
actually catch -- not a PLC-precomputed brief pulse. This is backwards
from how every earlier confirmed trigger in this project worked, and
likely too brief for the ETL/Z cards' own edge-detection logic to
reliably catch, even though the address math itself was "correct" in a
pure combinational-logic sense.

**Fixed** in both `tools/asi_tiger_etl_camera_sync_test.py` and
`tools/asi_tiger_z_camera_sync_test.py`, and in
`configure_zstack_trigger_chain()` (same bug, would have failed
identically once tested -- caught here first thanks to the staged test
plan's whole purpose: isolating exactly this kind of issue before it's
buried inside the full closed loop):

- ETL's trigger-in is now sourced from Expose-Out's **raw level**
  directly (`source_addr=expose_addr`) -- Expose-Out is already high
  for the entire exposure, a real sustained transition.
- Z's trigger-in is now sourced from Expose-Out's **inverted level**
  (`source_addr=inverted(expose_addr)`) -- gives Z's IN0 a genuine
  sustained low->high transition at the exact moment Expose-Out falls,
  since `inverted(low)=high`.

Verified the corrected addressing against a mocked serial device: ETL
correctly sourced from the raw level address (35), Z correctly sourced
from the inverted level address (35+64=99) -- both genuine sustained-
level signals, confirmed distinct from the old brief-pulse addresses
(163/227) they replaced. Full regression swept across every file in
the patch; both sync test scripts still run cleanly end-to-end.

## Fix: stale ETL axis default (I) in two test scripts, should be H

The design doc's own hardware table says "ETL-L (H), ETL-R (J)" for
card 34 -- H was always the intended production axis, not I. I was
used throughout the earliest exploratory ETL testing (before this
table existed in the conversation) and its default lingered in two
scripts after everything else moved on. `config_asi_tiger_example.py`
and `asi_tiger_galvo_etl_demo.py` were already correctly set to H;
fixed the same stale default in `asi_tiger_etl_camera_sync_test.py`
and `asi_tiger_shared_trigger_test.py`.

**Validation-coverage note, stated plainly rather than assumed away**:
the ETL trigger-period margin requirement (2-3ms minimum below the
camera's real interval, confirmed earlier in this document) was
empirically characterized specifically on axis I, before the switch to
H. H's trigger response was separately confirmed via the jumper-swap
diagnostic during the K-axis fault investigation, and the underlying
mechanism (`SAM=2`/`TTL X=30`) is architecturally identical across axes
on the same card/firmware, so the same margin requirement is expected
to hold -- but this is an expectation carried over from I, not an
independent confirmation on H specifically. The ETL sync test that just
passed used a 147ms period, far above the 2-3ms floor, so it doesn't
actually stress-test this either way. Worth a quick recheck on H
specifically if real acquisitions push toward tight frame rates near
that margin.

## CORRECTION: Z needs a brief pulse, not a sustained level -- generalized the ETL fix by analogy without testing it

Step 3 of the staged test plan (Z sync to camera Expose-Out) showed a
real problem, caught by the user directly checking Z's trigger line on
a scope: it was held **sustained high for the entire inter-frame gap**
(the whole period between Expose-Out's falling edge and the next rising
edge), not a brief pulse -- a direct consequence of using
`inverted(expose_addr)` (a level, not an edge) for Z's trigger.

Summing the 72 observed position deltas from the real-hardware run
confirmed this was a genuine problem, not a polling artifact: total was
590, not the expected 720 (72*10) -- a real 130-unit shortfall. A pure
sampling/timing artifact would still sum to the correct total
eventually; this didn't, which rules that explanation out. Several
pairs of adjacent deltas that summed to 9 instead of 10 (e.g. 4+5, 1+8,
8+1) are consistent with a second trigger arriving while the previous
move was still being processed -- the same zero-delay-retriggering
effect found earlier in the isolated full-loop test, just via a
different path (a sustained level giving Z's internal trigger detection
multiple opportunities to fire, rather than literal zero delay).

**Root cause of the fix being wrong in the first place**: the earlier
ETL fix (brief PLC-computed edge pulses are too short for ETL's SAM=2
detection, use a sustained level instead) was generalized to Z **by
analogy, without independently testing whether Z has the same
limitation**. It doesn't -- Z's `TTL X=12` ring-buffer trigger is a
different subsystem (stage motion control, not the DAC waveform
engine), and evidently does NOT tolerate a sustained level the way
ETL's `SAM=2` does. Assuming two different trigger mechanisms behave
identically because they're both "TTL inputs on the same controller"
was the actual mistake, not the original brief-pulse approach for Z.

**Fixed**: reverted `tools/asi_tiger_z_camera_sync_test.py` and
`configure_zstack_trigger_chain()`'s Z routing back to
`falling_edge(expose_addr)` (a brief pulse), while leaving ETL's
routing on the sustained raw level (still correct, still confirmed
working, untouched by this fix). Verified both addresses independently
against a mocked serial device: Z correctly back on `falling_edge`
(addr 227, a brief pulse), ETL still correctly on the raw level (addr
35, a sustained level) -- confirming the two axes now have genuinely
different, independently-verified signal shapes rather than one
assumed-equivalent treatment for both.

**Lesson for the rest of this integration**: don't generalize a fix
found on one subsystem to another subsystem just because they look
similar from the outside (both "TTL trigger inputs"). Test each one's
actual requirement independently, the same way every other piece of
this project has been validated -- this is exactly what the staged test
plan's step-by-step structure is for, and it caught this before it was
buried inside the full closed loop.

## Fix: WHERE can return an empty reply under active triggering, and a more trustworthy verification method

Re-running the Z sync test with the brief-pulse fix in place showed
real improvement (deltas mostly clean +10/+11, no more of the earlier
extreme swings like +23/-3) but the script crashed partway through:
`WHERE` returned a bare `:A` with no position data attached, which the
script's `parse_position()` couldn't handle. This is informative on its
own -- consistent with Z being momentarily busy processing an
actively-arriving electrical trigger and unable to immediately answer a
serial query, not a crash-worthy failure.

This matters beyond just the crash: if `WHERE` can silently return
stale or incomplete data during active triggering (not just the one
case that happened to produce an empty string outright), then some of
the polled per-move deltas from BOTH this run and the earlier
sustained-level run may be **measurement artifacts** -- a poll catching
Z mid-update -- not necessarily all genuine missing motion. Summing the
25 deltas from this run gives 209 against an expected 250 (25*10) --
still a real, unresolved shortfall even with the brief-pulse fix, but
now confounded by an independently-confirmed measurement reliability
issue, so it can't be fully trusted as a measure of genuine physical
behavior on its own.

**Fixed**: `read_position_robust()` retries briefly on any failure
(empty reply, parse error, or an exception from the controller itself)
instead of crashing or misrecording bad data, and skips (rather than
fabricates) a poll cycle if retries are exhausted. **More importantly**,
the script now also takes a clean, DISARMED, settled final position
reading after the watch window ends and compares total displacement
against the expected total (moves observed x step) -- this final
check is immune to any in-flight polling reliability issues, since
Z is no longer being actively triggered when it's taken, and is the
number to actually trust over the polled per-move deltas.

Verified against a mocked serial device: the script survives simulated
empty `WHERE` replies without crashing (both a transient failure that
recovers via retry, and a persistent one that exhausts retries and is
correctly skipped rather than crashing or fabricating a value).

**Still open**: whether the brief-pulse fix fully resolved the original
shortfall, or whether a smaller, genuine discrepancy remains once
measurement artifacts are correctly excluded via the clean final check
-- needs a real hardware re-run with this fix to get a trustworthy
answer, not assumed either way.

## New: independent camera-frame-count cross-check, replacing the unreliable polled move count

A real-hardware run at 500ms camera exposure showed polled move
intervals clustering around ~0.21s AND ~0.41s (roughly double each
other) -- consistent with the poll loop occasionally catching Z
mid-transition and splitting one real move across two polls (a
small/negative delta immediately followed by a compensating larger
one), not genuine back-and-forth motion. This means the polled move
COUNT itself (not just individual deltas) can't be fully trusted --
the earlier "expected = moves_observed * step" comparison was
comparing against a number that could itself be wrong.

**Fixed properly** rather than trying to make the polling more precise
(which can't fully solve a fundamental measurement-during-active-
triggering problem): `asi_tiger_z_camera_sync_test.py` now prompts for
PVCAMTest's own frame counter before and after the watch window -- a
ground-truth trigger count entirely independent of this script's
Z-position polling -- and compares the clean, disarmed, settled final
displacement against (real camera frames x step). This is the
comparison to trust. The polled per-move data is retained for
diagnostic visibility only, explicitly labeled as less trustworthy in
the output.

Verified both outcomes against a mocked serial device (a background
thread simulates a real external move occurring mid-watch-window,
independent of the script's own trigger logic, matching how a real
camera-driven move would appear): a MISMATCH scenario correctly
reports the real discrepancy, and a MATCH scenario correctly reports
success -- including correctly showing the old polled-count-based
comparison as clearly wrong in the same MATCH case, directly
demonstrating why the camera-based check is the one to rely on.

## Replaced: free-running + manual frame-counter approach was rightly rejected as imprecise

Correctly flagged: asking a human to type PVCAMTest's frame counter
before/after a watch window, with the camera free-running, is a
race-prone, approximate ground truth -- not a real fix for the
"is the polled move count trustworthy" problem, just a different
source of imprecision.

**Better approach, adopted**: drive the camera with a precise,
host-controlled trigger count instead of letting it free-run --
reusing the exact mechanism already confirmed working in
`asi_tiger_camera_trigger_test.py` (PLC BNC -> camera trigger input).
"How many real exposures occurred" is then known exactly by
construction (we generated them), not estimated by a human or inferred
from noisy position polling. Combined with the clean, disarmed,
settled final position reading (already established as reliable),
this gives a fully precise comparison with no ambiguity on either side.

`asi_tiger_z_camera_sync_test.py` rewritten around this: fires exactly
`--n-triggers` camera pulses (same confirmed-working 10ms pulse width),
well-spaced (`--trigger-spacing-s`, must exceed the camera's real
exposure+readout cycle), routes Expose-Out's falling edge to Z's
trigger as before (still the confirmed-correct brief-pulse signal
shape), then compares total displacement against exactly
`n_triggers x step`. No manual counter-reading, no free-running camera,
no per-move polling relied on for the verdict.

Verified against a mocked serial device: exact routing confirmed for
both signals (Z sourced from Expose-Out's falling edge, camera trigger
BNC sourced from the manual kick cell) via direct command inspection;
a synchronous mock camera simulation (hooked into the serial write()
call itself, avoiding the race conditions a polling-based simulation
had) confirms both the MATCH and MISMATCH reporting paths compute the
correct arithmetic.

## New: step 4 (final step) -- the actual full closed-loop test

With steps 1-3 all precisely confirmed (camera trigger exact, ETL
syncs to Expose-Out rising edge, Z syncs to Expose-Out falling edge
with an exact host-controlled trigger-count match), built the test for
the real thing: `configure_zstack_trigger_chain()` with camera, ETL,
and Z all participating together, camera gated by the plane counter
instead of free-running or host-triggered individually.

New: **`tools/asi_tiger_full_loop_test.py`**. Configures and arms ETL
separately first (the chain function only handles trigger routing, not
ETL's waveform/SAM=2 arming, per its documented prerequisites), then
calls `configure_zstack_trigger_chain()` with the exact BNC wiring
already established across steps 1-3 (BNC1->Z IN0, BNC2<-Z OUT0,
BNC3<-camera Expose-Out, BNC4->camera trigger -- no new jumpers
needed). Deliberately does NOT poll `WHERE` during the autonomous run
(confirmed unreliable during active triggering in step 3) -- waits a
generous fixed duration instead, then takes one clean, disarmed,
settled final reading and compares against exactly `n_planes x z_step`,
the same precise methodology that resolved step 3.

Verified end-to-end against a mocked serial device: full setup runs
without error (ETL configuration, chain configuration, kick, wait,
disarm, cleanup), no exceptions, correctly reports the expected
mismatch given the mock has no real closed-loop hardware behind it.

This is the last piece of the staged test plan -- real hardware
confirmation is the next and final step before this library's Z-stack
trigger chain can be considered validated end-to-end.

## Fix: off-by-one in the full loop -- n_planes=3 produced 4 frames, confirmed on real hardware

First real-hardware run of `asi_tiger_full_loop_test.py` (`--n-planes 3`)
showed 4 camera exposures and 4 ETL sweeps on a scope, and a position
mismatch of exactly +1 step -- both independently confirming the same
extra frame. The user's own diagnosis ("the counter lets the cycle
re-arm and only blocks after N+1") pointed precisely at the right area
and matched a deeper trace exactly.

**Root cause, confirmed not to be a counter bug**: re-running
`asi_tiger_pulse_counter_test.py --single --n-pulses 3` (and separately
at 300) in isolation showed the counter passing exactly 3 and blocking
the rest, both times, confirmed on a scope too -- the counter's own
pass/block logic is correct. The actual bug is in how
`configure_zstack_trigger_chain()` uses it: the kick independently
starts frame 1 before the counter has seen anything, so passing
through N pulses (as the code did) causes N *retriggers* on top of that
first frame -- N+1 total frames, not N. Tracing the full sequence for
n_planes=3: kick->frame1, frame1's pulse passes->frame2, frame2's pulse
passes->frame3, and critically frame3's pulse ALSO passes (the
confirmed-correct Nth pass)->frame4, which is one too many.

**Also found**: this function's own docstring had ALREADY been updated
to describe this exact fix in full detail (kick accounts for frame 1,
counter should use n_planes-1, n_planes must be >=3) -- but the actual
code still said `n_pulses=n_planes`, not `n_planes-1`. A real
documentation/implementation mismatch that would have been actively
misleading (reads as already fixed, wasn't). Now actually fixed to
match what the docstring already said, plus the missing `n_planes>=3`
validation the docstring described but the code never implemented.

**Fixed**: `configure_pulse_pass_through_counter_single()` is now
called with `n_pulses=n_planes-1`. Added the missing `if n_planes < 3:
raise ValueError(...)` check (the counter's own n_pulses>=2 constraint,
combined with n_planes-1, means n_planes must be >=3 for this specific
architecture -- tighter than the counter's own standalone minimum).

Verified against a mocked serial device: `n_planes=2` now correctly
raises `ValueError`; `n_planes=3` correctly configures the underlying
one-shot counter cell with `config=1` (i.e. `n_pulses-1` where
`n_pulses=n_planes-1=2`), not the old buggy value that would have
configured it for 3.

## MILESTONE: staged full-loop test plan confirmed complete on real hardware

`asi_tiger_full_loop_test.py` confirmed MATCH at both `n_planes=3` and
`n_planes=300` -- exact position match, no host involvement after the
kick, camera/ETL/Z genuinely running together autonomously, gated
correctly by the now-fixed counter. This closes the entire staged test
plan (camera trigger, ETL sync, Z sync, full loop), each step
individually confirmed precisely before being combined, catching three
real bugs along the way (the sustained-level-vs-brief-pulse mistake,
the WHERE-during-active-triggering reliability issue, and the kick/
counter off-by-one) that would otherwise have been much harder to
isolate inside the full assembly.

Two additional real-hardware findings from this confirmation run:

**PLC BNC types saved as the permanent default (`36SS Z`)**: user
configured BNC1=output/BNC2=input/BNC3=input/BNC4=output (exactly
matching this project's confirmed wiring: BNC1->Z IN0, BNC2<-Z OUT0,
BNC3<-camera Expose-Out, BNC4->camera trigger) and saved it. This is a
sound, deliberate choice -- and since `configure_zstack_trigger_chain()`
already reconfigures these fresh every time it runs, the save is a
safety net for other tools/manual `--raw` sessions rather than
something the chain itself depends on. Worth the same honest caveat as
before, though: `SS Z` saves the ENTIRE current PLC state, so whatever
cells 1-7 (counter/kick/OR-gate) happened to be configured for at the
moment of saving (e.g. whichever `n_planes` was last tested) is now
part of the permanent default too -- likely harmless in practice since
those get reconfigured fresh on every real call, but worth knowing if
inspecting the saved default later looks unexpectedly specific rather
than "clean."

**Camera Expose-Out pulse duration != configured exposure time in
Rolling Shutter mode**: confirmed on real hardware, a 150ms exposure
setting produced a ~120ms real Expose-Out pulse -- about 30ms shorter.
ETL's period must be set relative to the REAL pulse duration (with the
usual 2-3ms margin below that), not the raw exposure setting, or the
same missed-trigger margin issue found earlier will resurface. Whether
this exact ~30ms offset is a fixed camera characteristic or scales with
exposure time is NOT yet confirmed (only one data point) -- updated
`--etl-period-ms` help text in both `asi_tiger_full_loop_test.py` and
`asi_tiger_etl_camera_sync_test.py` to state this plainly rather than
leave the stale "confirmed safe" claim that predates this finding.

## Rollback

This patch is purely additive at the mesoSPIM-control level -- the only
change to existing files is the 2-line `elif` branch in
`mesoSPIM_Core.py`. Setting `waveformgeneration = 'NI'` (or `'cDAQ'` /
`'DemoWaveFormGeneration'`) in your config continues to use the
untouched original code path.
