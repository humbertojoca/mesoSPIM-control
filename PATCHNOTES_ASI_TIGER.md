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
following that exact pattern.

**Current state (this section is kept up to date; everything below it
is a chronological journal, not a current-state summary):** what
started as a software-timed serial adapter has grown into a full
hardware-timed acquisition backend, validated end-to-end on real
hardware:

- **`asi_tiger/singleaxis.py`**: hardware-timed galvo/ETL waveforms
  (`SAA`/`SAF`/`SAO`/`SAP`/`SAM`) -- entirely on-card, no per-sample
  serial traffic. Confirmed up to 40kHz on the galvo card's fast axes
  (A/C) after a firmware update -- see "New TGGALVO firmware" below.
  This fully replaced the original software-timed bandwidth ceiling;
  there is no remaining galvo/ETL bandwidth gap for periodic waveforms.
- **`asi_tiger/stage_trigger.py`**: TTL-triggered Z-stage ring-buffer
  stepping, plus `move_absolute()` for simple host-commanded row-level
  moves.
- **`asi_tiger/plc.py`**: trigger routing/fan-out, a pulse-pass-through
  counter (gates "stop after N frames"), laser enable lines, and the
  primitives (latching pulse catchers, backplane addressing) used
  throughout this project's hardware discovery testing.
- **`asi_tiger/zstack_chain.py`**: `configure_zstack_trigger_chain()`
  -- the full self-sustaining per-frame acquisition loop (camera ->
  ETL -> Z -> counter -> next camera trigger, entirely autonomous in
  hardware after one host-issued kick). **Confirmed working on real
  hardware end to end**, exact position match at both `n_planes=3` and
  `n_planes=300` -- see the MILESTONE entry below.
- **`asi_tiger/row_setup.py`**: row-level (once-per-row, not
  per-frame) setup -- laser enable, L/R illumination-arm switch,
  absolute Z move. Laser intensity needs no new code, already covered
  by `ASITigerDAC.set_voltage()`.
- **16 bench-test scripts in `tools/`**, each validating one signal or
  mechanism in isolation before it was combined with anything else --
  the methodology this whole project followed throughout, and the
  reason the entries below read the way they do (isolate, test, THEN
  combine).

Not yet done: the actual mesoSPIM-control integration (wiring this
library into mesoSPIM's device-backend pattern so it's usable from the
real acquisition GUI, not just standalone bench scripts).

---

## Files added

```
mesoSPIM/
├── src/
│   ├── mesoSPIM_ASITigerWaveFormGenerator.py     # the adapter (early draft -- predates the
│   │                                              # hardware-timed work below, will need updating
│   │                                              # when the mesoSPIM-control integration happens)
│   └── devices/
│       └── asi_tiger/                            # standalone driver library, no mesoSPIM dependency
│           ├── __init__.py
│           ├── controller.py       # low-level pyserial wrapper for the Tiger command/reply protocol
│           ├── dac.py              # ASITigerDAC: volts-based set/get, card-wide range handling
│           ├── waveform.py         # waveform math + software-timed streaming (WaveformStreamer) --
│           │                       # superseded by singleaxis.py for periodic waveforms, kept for
│           │                       # arbitrary/non-periodic waveforms if ever needed
│           ├── plc.py              # PLCCard: logic-cell programming, trigger routing, pulse counter
│           ├── singleaxis.py       # SingleAxisWaveform: hardware-timed galvo/ETL waveform generation
│           ├── stage_trigger.py    # StageRingBuffer: TTL-triggered Z-stepping + move_absolute()
│           ├── zstack_chain.py     # configure_zstack_trigger_chain(): the full acquisition loop
│           └── row_setup.py        # laser enable, L/R switch
└── config/
    └── examples/
        ├── config_asi_tiger_example.py           # config additions to copy into your own config
        └── asi_dac_channels_example.json         # channel layout used by the standalone hardware test

tools/    # 16 bench-test scripts, each validating one signal/mechanism -- see individual
          # journal entries below for what each one found. Notably:
          # asi_tiger_full_loop_test.py            the real, full closed-loop test (step 4 of 4)
          # asi_tiger_camera_trigger_test.py       camera trigger, confirmed working
          # asi_tiger_etl_camera_sync_test.py      ETL syncs to real Expose-Out (step 2 of 4)
          # asi_tiger_z_camera_sync_test.py        Z syncs to real Expose-Out (step 3 of 4)
          # asi_tiger_lr_switch_test.py            L/R switch voltage transitions on a scope
          # (the rest are earlier hardware-discovery and diagnostic tests -- ring buffer, PLC
          # counter, TTL OUT0 modes, galvo/ETL demo, etc.)
```

`mesoSPIM/src/devices/asi_tiger/` has no dependency on mesoSPIM, PyQt5,
or nidaqmx -- only `pyserial`. It can be tested and used completely
standalone (that's what every `tools/` script does).

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

**VERIFIED against the real mesoSPIM-control source** (this diff was
written against a real, current clone of
github.com/mesoSPIM/mesoSPIM-control, commit `98d74d35bdc6dbc9523cf9ddbed59f262b629a66`
(2026-07-22) -- fetched directly for the "full integration" push, not
guessed): `git apply --check` on that exact checkout confirms this
diff applies cleanly. A ready-to-use `mesoSPIM_Core.py.diff` (proper
`a/`/`b/` paths, apply from the repo root with `git apply
mesoSPIM_Core.py.diff`) and a fully patched reference copy of the
whole file are included in the delivered package
(`mesoSPIM_Core_patch_reference/`) -- if your checkout is a different
commit and the diff doesn't apply cleanly, the two edits are small
enough to make by hand from the reference copy.

### `requirements-conda-mamba.txt` / `requirements-clean-python.txt`

Add `pyserial` if not already present (mesoSPIM's own ASI stage driver,
`StageControlASI`, already depends on it, so it's likely already there
-- check before adding a duplicate).

No other upstream files are touched. `mesoSPIM_Stages.py` and
`asicontrol.py` (the existing ASI stage driver) are read from, not
modified -- see "Connection sharing" below.

**Note (per the target hardware's actual config,
`config_benchtop_standard2025_v1.2.py`)**: this rack's real
`stage_assignment` axis-letter mapping is per-installation and was
confirmed physically correct against this specific rack -- don't copy
another installation's mapping without checking the `N` command output
first.

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



## Known limitations

1. ~~Galvo/ETL bandwidth~~ -- **RESOLVED.** See "New TGGALVO firmware
   for card 37" below: hardware-timed `SingleAxisWaveform` confirmed
   working, up to 40kHz on the galvo card's fast axes (A/C) after a
   firmware update. No remaining bandwidth gap for periodic galvo/ETL
   waveforms.
2. ~~DAC ring-buffer / hardware-triggered playback is unimplemented~~
   -- **RESOLVED**, via a different mechanism than originally
   envisioned here: rather than a DAC ring-buffer, `SingleAxisWaveform`
   (on-card `SAM`/`SAP` waveform generation) fully replaces the need
   for this for periodic waveforms. `WaveformStreamer` (the original
   software-timed path this item was about improving on) is kept for
   arbitrary/non-periodic waveforms if that's ever needed, but is no
   longer the primary path.
3. **PLC laser-toggle wiring is a reference implementation, not
   hardware-verified.** `PLCCard.configure_two_laser_toggle()` reproduces
   the 2-laser toggle scheme described in ASI's PLC manual (also used by
   ASI's own diSPIM plugin), and its command sequence has been checked
   against a mocked serial port, but not against a scope on real BNC
   outputs. Verify before connecting real laser hardware --
   `tools/asi_tiger_hardware_test.py --test-laser-toggle` is built for
   exactly this and requires an explicit typed confirmation before it
   drives anything. Note: for simple, independent per-laser ON/OFF
   (not this AND-gated toggle scheme), `row_setup.configure_laser_enable_lines()`
   is the newer, preferred path -- this item specifically concerns the
   older two-laser toggle mechanism, which remains unverified and
   unused elsewhere in this project.
4. **No GUI for PLC configuration.** The standalone `tools/asi_dac_standalone_gui.py`
   GUI covers DAC sliders and waveform preview (PyQt5, matching mesoSPIM's
   own dependency, not PySide6 -- verified by actually running it headless
   against PyQt5==5.15.11, the exact version pinned in
   `requirements-conda-mamba.txt`, including the QThread-based waveform
   streaming path), but PLC setup is library/script-only for now.

## Update: hardware-timed waveform generation found (`asi_tiger/singleaxis.py`) [now confirmed -- see "New TGGALVO firmware" below]

ASI's `SINGLEAXIS_FUNCTION` firmware module (`SAA`/`SAF`/`SAO`/`SAP`/`SAM`
commands, see https://asiimaging.com/docs/singleaxis) generates
sawtooth/triangle/square/sine waveforms **entirely on-card**, off a 4kHz
internal clock (documented up to 40kHz on "fast DAC" axes on cards that
have them). This is categorically different from `WaveformStreamer`:
there are no per-sample serial commands at all -- configure once, start
once, the card runs the waveform autonomously. If confirmed working on
your `SIGNAL_DAC_4CH` cards, **this replaces the original software-timed
bandwidth ceiling** for sawtooth/triangle/square/sine galvo/ETL
waveforms specifically (arbitrary/non-periodic waveforms would still
need the ring-buffer path or NI).

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

## Cleanup pass: removed dead code, unused two-counter form, and superseded bench scripts

With the staged test plan complete, did a systematic review for
redundancy before starting the next phase of work (row-level setup
functions, mesoSPIM-control integration) -- ran `pyflakes` across the
whole codebase and traced actual usage of anything that looked
duplicated or unused.

**Mechanical fixes** (unused imports/variables, no behavior change):
`Optional` (singleaxis.py, zstack_chain.py), `List` (waveform.py),
`SingleAxisWaveform` (zstack_chain.py -- leftover from an earlier
design), `numpy` (mesoSPIM adapter + DAC GUI, neither ever used arrays
directly), `backplane_addr` (mesoSPIM adapter + hardware_test.py). One
unused variable (`self_test_ok` in `asi_tiger_hardware_test.py`) --
harmless, the function it came from already prints its own pass/fail.
Two magic numbers (`64`) in the same file replaced with the actual
`inverted(0)` call they were already commented as meaning.

**Removed dead code**: `PLCCard.set_trigger_source()`,
`PLCCard.load_preset()`, and the five `TRIGGER_*` constants -- defined
and exported but never called anywhere except a docstring example
inside `plc.py` itself. Built early as general PLC command coverage,
never actually needed once the real trigger architecture (SAM/SAP,
then the camera/ETL/Z chain) took shape.

**Removed the unused two-counter pulse-pass-through form**:
`configure_pulse_pass_through_counter()` (the `n_inner*n_outer` form)
was only ever exercised by its own dedicated bench test --
`zstack_chain.py` and everything downstream of it uses exclusively
`configure_pulse_pass_through_counter_single()`, since realistic
Z-stack plane counts fit comfortably within one counter's 65535-pulse
range. Removed the two-counter method entirely; cleaned up
`configure_pulse_pass_through_counter_single()`'s docstring (previously
written as a comparison against the two-counter version, now stands on
its own). Simplified `asi_tiger_pulse_counter_test.py` to match --
removed the `--single`/`--n-inner`/`--n-outer` branching, now tests
only the single-counter mechanism that's actually used.

**Removed three superseded bench-test scripts** (their findings are
now fully captured by later, more complete tests -- not deleted
because they were wrong, but because they answered questions this
project has since resolved more thoroughly):
- `asi_tiger_zstack_loop_isolated_test.py` -- its zero-delay-
  retriggering finding is now explained and resolved by the real
  full-loop test (`asi_tiger_full_loop_test.py`), which is strictly
  more complete (includes the camera's natural dead-time the isolated
  version lacked).
- `asi_tiger_z_spacing_test.py` -- built to isolate one specific
  question (is it `TTL Y=2` or retrigger speed causing the glitch?)
  that's now definitively answered (speed, not `TTL Y=2`).
- `asi_tiger_shared_trigger_test.py` -- overlapped significantly with
  `asi_tiger_etl_camera_sync_test.py`, which tests the same general
  idea with real camera signals instead of a synthetic PLC-generated
  pulse.

Kept deliberately: `PLCCard.configure_shutter_gate()`/
`disable_shutter_gate()` -- zero callers today, but this is likely
exactly the AND-gated enable mechanism the upcoming row-level
laser-enable/L-R-switch work will need; removing it now would probably
mean rebuilding the same thing shortly. `route_to_dac_trigger()` (a
single-output special case of the more general `fan_out_trigger()`) --
still has its one real caller (`asi_tiger_galvo_etl_demo.py`), low-value
to consolidate right now.

Verified after every removal: full syntax sweep across every remaining
file, `pyflakes` clean (aside from harmless f-string cosmetic
warnings, not fixed), and the full `configure_zstack_trigger_chain()`
lifecycle (configure/reset/kick/disarm) still runs correctly against a
mocked serial device.

## New: row-level setup functions, part 1 -- laser enable + absolute Z move

Started the row-level (once-per-row, not per-frame) setup functions:
laser enable, laser intensity, L/R illumination-arm switch, absolute Z
move to start position.

**Laser intensity**: no new code needed -- already fully covered by
`ASITigerDAC.set_voltage()` (this rack: card_addr 35, axes P/Q/R/S).

**Absolute Z move**: new `StageRingBuffer.move_absolute()` (a direct
`M <axis>=<position>` command, distinct from that same class's
TTL-triggered ring-buffer mechanism). Checked ASI's own `command:rdstat`
docs before assuming which status character means "move in progress" --
confirmed it's `M`, NOT `A` (single-axis/SAM mode, what
`SingleAxisWaveform.is_active()` already correctly checks for a
different mechanism) -- reusing that check here would have been wrong.
Verified against a mocked serial device: sends the move, correctly
polls `RDSTAT <axis>+` until idle then returns `True`; a
never-completes scenario correctly returns `False` after `timeout_s`
rather than hanging.

**Laser enable**: new module `asi_tiger/row_setup.py` --
`configure_laser_enable_lines()` wires up to 4 independent, directly-
toggleable lines (default PLC BNC5-8, this rack's convention), each
driven by its own manually-controlled cell (same pattern as every
manual toggle/kick source elsewhere in this project). Returns a
`LaserEnableLines` handle (`.enable(i)`/`.disable(i)`/`.disable_all()`).
Deliberately NOT the AND-gated `configure_shutter_gate()` mechanism --
that ANDs an enable bit against some other trigger source, useful for
gating one signal by another; laser enable is a plain persistent
row-level ON/OFF setting, a different concern. Verified against a
mocked serial device: correct BNC address math (BNC5-8 -> addr 37-40),
`enable()`/`disable_all()` send exactly the expected command sequences.

**L/R illumination-arm switch**: NOT yet built -- this rack has all 8
PLC BNCs already allocated (Z: 1-2, camera: 3-4, laser enable: 5-8), so
this needs a different signal path. Two candidates identified, not yet
decided between: (1) ASI TTL `OUT0_mode=21` routes to backplane TTL1
per ASI's docs -- untested, and touches the same `OUT0` mechanism the
Z-stepping chain depends on, so would need its own isolated discovery
test (same pulse-catcher technique as the `OUT0_mode=2` discovery test)
before being trusted; (2) a free DAC axis (this rack: I/K on card 34,
B/D on card 37) driving a binary voltage via the already-validated
`ASITigerDAC.set_voltage()` -- lower risk, touches nothing in the
currently-working trigger chain. Leaning toward (2) as the default
given the risk profile, with (1) available to test if preferred for
its BNC-freeing benefit.

## New: TTL OUT0_mode=21 discovery test, for deciding the L/R switch approach

Before committing to either L/R switch candidate (mode 21 freeing a
BNC vs. a free DAC axis), built a real-hardware discovery test for
mode 21 -- reusing the pulse-catcher technique from the OUT0_mode=2
discovery test, but with two important differences from that earlier
test, both checked rather than assumed:

- **Must use a genuine ring-buffer move**, not a plain commanded move:
  ASI's own docs describe mode 21 as tied specifically to "a ring
  buffer move," narrower than mode 2's "MOVE, MOVREL, ring buffer, or
  array module." A plain M-command move (like `move_absolute()`) would
  not be a valid test of this mode.
- **Checks for a sustained level, not just a pulse**: mode 21's
  documented wording is "TTL OUT0 SET," different from mode 2's
  "generates TTL pulse" -- polls the raw backplane state (`RDADC Y?`,
  `PLCCard.read_backplane()`) immediately after triggering and again
  after a longer wait, to distinguish a brief pulse from a persistent
  "set" state that needs its own reset logic to clear.

**`tools/asi_tiger_ttl_mode21_discovery_test.py`** also explicitly
handles the "card rejects the command" case (a `TigerError` on
`TTL Y=21` would indicate the required MM_TARGET firmware module isn't
present) rather than letting it crash uninformatively, and optionally
checks the physical OUT0 connector too (via `--physical-out-bnc`, if
still jumpered from the mode=2 test) to rule out mode 21 landing there
instead of the backplane, despite what the docs say.

Verified against a mocked serial device: the rejection path reports
clearly; the "nothing caught" path (the mock's actual behavior, since
it can't simulate real hardware) runs cleanly without crashing; a
simulated "caught, sustained level" scenario correctly identifies
which backplane line and correctly reports the pulse-vs-level
distinction with the right guidance either way.

## L/R switch decided: mode 21 ruled out on real hardware, free-DAC-axis approach confirmed

`asi_tiger_ttl_mode21_discovery_test.py` run on real hardware: `TTL Y=21`
accepted without error, a genuine ring-buffer move confirmed to occur
(position changed correctly), but nothing detected across all 8
backplane lines. Since mode 21 is documented as keying off the
ring-buffer move completing (not the trigger mechanism used to start
it), and that move demonstrably completed, this is a real negative
result rather than an artifact of using `software_trigger()` -- rules
out the documented backplane-routing behavior on this card/firmware
combination. Decided: free-DAC-axis approach (axis I on card 34,
avoiding K's known unrelated trigger fault), lower risk regardless
since it touches nothing in the currently-working trigger chain.

New in `row_setup.py`: `LRSwitch` / `configure_lr_switch()` -- a thin
wrapper picking LEFT/RIGHT voltage levels on top of the already-
validated `ASITigerDAC.set_voltage()` (including its existing safety
bounds and optional slew-rate protection).

**Found and fixed a real bug while testing this**: the function's own
default `right_v=5.0` exceeded the default `range_code=1`'s hardware
span (0-4.096V, confirmed elsewhere in this project) -- calling
`configure_lr_switch()` with its own defaults and then `.select_right()`
would always fail `ASITigerDAC.set_voltage()`'s own safety check.
Changed the default to 4.0V, safely within range. Verified against a
mocked serial device using the function's own defaults end-to-end
(not just a hand-picked valid value) -- both `select_left()` and
`select_right()` now send the correct commands without error.

Row-level setup functions are now all decided and implemented: laser
intensity (`ASITigerDAC`, no new code), absolute Z move
(`StageRingBuffer.move_absolute()`), laser enable
(`configure_laser_enable_lines()`), L/R switch
(`configure_lr_switch()`).

## Confirmed: L/R switch signal shape matches mesoSPIM's own convention; found and fixed real DAC range bugs

User asked about using `PR` to widen a DAC channel's range for a 5V
pulse, and separately whether the L/R switch should be edge/toggle or
level-defined. Both questions led to real findings.

**L/R switch signal shape, confirmed from mesoSPIM's own existing NI-
card implementation** (this fork doesn't contain the full
mesoSPIM-control repo, so fetched it from
github.com/mesoSPIM/mesoSPIM-control): `demo_config.py`'s
`shutterdict`/`shutterswitch` documents `'shutter_right' : ...  # the
left/right switch (Right==True)`, driven via a plain NI digital output
line. NI DO lines are inherently level-based (a persistent write, no
toggle semantics), confirming level-defined, not edge/toggle -- exactly
what `LRSwitch` already does. Added `LRSwitch.select(is_right: bool)`
to mirror mesoSPIM's own `Right==True` boolean convention directly, for
smoother future integration.

**Checked ASI's own `command:pr` docs before assuming `set_range()` was
safe to use as-is** -- found two real, related bugs:

1. **`set_range()` updated the cached `range_code` (and thus
   `set_voltage()`'s safety limits) immediately** -- but ASI's docs are
   explicit: "Controller reset or restart is needed for setting to take
   effect." The old code created a dangerous window where software could
   believe a wider range was already active and permit/encode a
   `set_voltage()` call accordingly, while the hardware was still
   running the OLD range. Fixed: `set_range()` now only records the
   requested change as pending; a new `confirm_range_change(card_addr)`
   applies it to the cache, and callers should only call that AFTER an
   actual reset/restart, confirmed. Added `query_range()` (`PR <axis>?`,
   read-only, safe anytime) so that confirmation is actually checkable
   against real hardware, not just assumed.

2. **`DAC_RANGE_LIMITS_MV`/`DAC_RANGE_CODES` had codes 3-6 shifted by
   one position relative to ASI's CURRENTLY documented table, and code 7
   was missing entirely** -- e.g. this project's code claimed
   `range_code=6` (the DAC default used throughout this whole project,
   including every galvo channel) meant +/-10.24V; ASI's own docs say
   `6` is +/-5.12V and `7` is +/-10.24V. Fixed both tables to match
   ASI's current docs exactly.

**Important, stated plainly rather than assumed**: `set_range()`/`PR`
has never actually been called anywhere in this project as of this
fix -- every DAC channel's `range_code`, including the galvo channels'
default, has been a software-side label only, never confirmed against
what range the real hardware is actually running. The earlier
scope-confirmed galvo/ETL voltage tests show the commanded voltages
were correctly encoded and stayed safely within whatever the real
range actually is (no clipping observed) -- but that doesn't confirm
the safety-check table itself was accurate at the time, only that the
values tested never approached wherever the real boundary is. Worth
running `query_range()` against real hardware before trusting
`limits_mv` for anything safety-relevant, especially the galvo
channels, given how extensively they've been used.

Verified all of this against a mocked serial device: `set_range()`
correctly leaves the cached value unchanged and `set_voltage()`
correctly still enforces the OLD limits until `confirm_range_change()`
is called; after confirming, the new limits apply correctly;
`query_range()` returns the real value; the corrected tables match
ASI's documented values exactly; `LRSwitch.select()` correctly maps to
`select_left()`/`select_right()`.

## L/R switch scope test passed; range widened to 5V for real laser-switching hardware

L/R switch confirmed on a scope (`asi_tiger_lr_switch_test.py`) --
clean transitions, both directions, real hardware.

Worked out the correct sequence to widen the I-axis DAC channel's range
for a real 5V control level: `PR I=2` (0-10.24V), then a **card-
addressed** reset (`34~`) -- confirmed from ASI's own `command:reset`
docs that a bare `~` broadcasts to the whole rack and zeroes every
axis's position (including Z's, currently sitting at a real,
non-trivial value from all prior testing), while a card-addressed reset
scopes this to card 34 only. Then `PR I?` to confirm, not assume.

Updated `configure_lr_switch()`'s defaults to match: `right_v=5.0`,
`range_code=2` (was 4.0/1, the placeholder pair from before this was
decided). Also updated `range_code` together with `right_v` --
changing one without the other would have reintroduced the exact
self-inconsistent-defaults bug found and fixed earlier.

**Flagged a subtler risk while updating this**, different from what
`set_range()`/`confirm_range_change()` protect against: those exist
specifically because changing an EXISTING channel's range shouldn't be
trusted in software until confirmed on hardware. `configure_lr_switch()`
registers a BRAND NEW channel each call, so there's no "old, trusted"
cached value being protected -- it trusts `range_code=2` immediately,
whether or not the real hardware has actually been reconfigured yet in
this session. Documented this plainly in the docstring rather than
leave it as a silent trap, with a clear recommendation to run
`dac.query_range()` right after to verify.

Verified against a mocked serial device: the function's own new
defaults (not a hand-picked valid value) correctly produce `M I=5000`
(5V) without hitting the safety check; separately confirmed the safety
check still correctly rejects 5V when `range_code=1` (i.e. before the
real hardware has actually been reconfigured), proving the protection
itself wasn't weakened by this change.

## New: move_absolute() bench test, and a real answer to "how does software know the loop is done"

**`move_absolute()` bench test**: `tools/asi_tiger_move_absolute_test.py`
-- the last untested row-level piece. Moves to several targets both
directions, checks the blocking wait (`wait=True`) only returns after
the move is REALLY complete (independently confirmed via a fresh
`WHERE` read), and checks `wait=False` returns immediately without
affecting the move's own eventual correctness. Verified against a
mocked serial device (simulated `RDSTAT` busy/idle sequence) -- real
hardware confirmation is the next step.

**How does the software know a row's acquisition loop is done, to move
to the next row/laser line/tile position?** The prior approach
(`asi_tiger_full_loop_test.py`) just waited a fixed, conservative
duration -- functional for a bench test, not something to build real
acquisition timing on. The user independently started prototyping a
fix: polling the pulse counter's internal state (`CCA F?` on the count
cell) during the active loop, sharing a draft with a real, working
`run_state_check()` against actual hardware.

Checked ASI's own `tiger_programmable_logic_card` docs before building
on this: confirmed real and documented -- a one-shot's "state" (read/
write via `CCA F`) is genuinely its internal countdown ("the current
clock counter value... counter decreases with each clock"), distinct
from its binary output (`read_cell_outputs()`/`RDADC Z?`). Added
`PLCCard.read_cell_state()` (`CCA F?`, the read side -- only the write
side, `set_cell_state()`, existed before) and
**`tools/asi_tiger_counter_state_test.py`** -- pure software, maps this
countdown against a KNOWN exact pulse count (same manual-pulse-source
methodology as `asi_tiger_pulse_counter_test.py`), rather than assume
either the user's empirical `-2` offset or a theoretical derivation of
my own without checking. Also checks whether `read_cell_state()` stays
stable when polled repeatedly at rest, the same category of concern
that made `WHERE` unreliable during active Z triggering.

**For the actual completion DECISION, used a different, more robust
signal than the countdown value**: the counter's LATCH cell (the 3rd
of 5 cells) goes high once and stays high until the next reset --
unlike the countdown (which needs an exact expected value understood
first) or the AND-gate output (which only pulses briefly per real pass-
through, so a single read can't tell "still counting, between planes"
from "genuinely done" -- the same ambiguity that broke `WHERE`). New on
`ZStackTriggerChain`: `is_complete()` (reads the latch cell via
`read_cell_outputs()`/`RDADC Z?`, which has never shown any of
`WHERE`'s reliability issues anywhere in this project), and
`wait_until_complete(timeout_s, poll_interval_s)` (polls `is_complete()`
until true or timeout). Also added `planes_remaining()` -- the user's
`CCA F?` countdown idea, kept for what it's actually good at (a
progress indicator), not the safety-critical completion check; its
exact numeric mapping isn't confirmed yet, `asi_tiger_counter_state_test.py`
is for that.

`asi_tiger_full_loop_test.py` now uses `wait_until_complete()` instead
of a fixed wait -- `--seconds-per-plane-estimate`/`--safety-margin-s`
are now just a timeout bound (a safety net if the loop somehow never
completes), not a blind estimate that needed careful tuning. Also fixed
a real oversight while touching this file: `--etl-period-ms`'s default
was still the stale 147.0 from before the confirmed "150ms exposure ->
~120ms real pulse" finding -- the user's own draft had already
corrected this locally to 120.0; adopted that fix into the shared
script.

Verified against a mocked serial device: `is_complete()`/
`wait_until_complete()`/`planes_remaining()` all behave correctly,
including `wait_until_complete()` returning as soon as the latch bit
appears (not waiting for the full timeout) and correctly reporting
`False` on a genuine timeout; the updated `asi_tiger_full_loop_test.py`
runs cleanly end to end with the new polling mechanism in place of the
fixed wait.

## Two real-hardware findings: counter countdown cycles after blocking; move_absolute() had a genuine settling bug

**Counter state test, run on real hardware**: confirmed exactly the
relationship `is_complete()` was designed around, and revealed
something important about the countdown value. Tracing the real
output: reset -> state=0 (idle); pulse 1 (the trigger) loads the
counter to 4 and passes; each subsequent pulse decrements by 1,
reaching 0 exactly on pulse 5 (still correctly passing, matching
n_pulses=5). Then pulse 6 (correctly BLOCKED) shows state=4 again, not
0 -- the one-shot's own trigger input doesn't know the downstream latch
has already blocked the output, so it keeps re-arming and cycling
(4,3,2,1,0,4,3,2,1,0...) on every further incoming pulse, even though
the row is genuinely finished. This is exactly why `is_complete()` uses
the latch cell rather than the countdown -- checking `state == 0` would
have produced a false "not done yet" on an already-finished row, five
pulses later.

**Fixed** `ZStackTriggerChain.planes_remaining()` to check
`is_complete()` first and return 0 once the row is done, rather than
returning the now-meaningless cycling countdown value.

**`move_absolute()` bench test, run on real hardware -- found a real
bug**: every move reported success (`RDSTAT` cleared 'M' in ~0.02s,
faster than even one poll interval) but landed 1-3 microns from the
commanded target, on every single move, both directions. Far larger
than the ~0.1 micron mechanical settling noise already characterized
elsewhere in this project for the ring buffer -- a real, reproducible
problem, not noise. Root cause: `RDSTAT`'s 'M' flag clears before the
stage has physically finished settling to its target.

**Fixed**: added `settle_s` to `move_absolute()` (default 0.3s,
matching the same pattern already used by `arm_relative()`/
`arm_absolute()` for this exact kind of gap) -- a fixed delay added
AFTER `RDSTAT` reports idle, before the method returns. Also added a
direct confirmation diagnostic to the bench test itself, rather than
just asserting the settling-time explanation: on any mismatch, waits a
further 2s and re-checks position -- if it converges to the correct
target during that extra wait, that's real evidence for a settling gap
(and that a larger `settle_s` fixes it); if it doesn't, that would have
pointed somewhere else instead. Verified against a mock that simulates
a genuine settling gap (position doesn't reach target until real time
has passed after `RDSTAT` clears): `--settle-s 0` correctly reproduces
the mismatch AND the diagnostic correctly reports convergence;
`--settle-s` set long enough to cover the simulated gap correctly
passes.

`settle_s` is a starting-point default (0.3s), not a measured value for
any specific hardware -- confirm the real minimum needed via
`--settle-s` sweeps on your own rack.

## move_absolute() re-tested on real hardware: fix confirmed working, default bumped for larger moves

Re-ran `asi_tiger_move_absolute_test.py` with `settle_s=0.3` at two
scales. At +/-50 (5 micron) moves: all 5 passed exactly. At +/-1000
(100 micron) moves: 4/5 passed exactly, one landed 0.1 micron short --
which converged to the exact target after 2 more seconds (confirmed by
the bench test's own diagnostic), and 0.1 micron is itself within the
already-characterized mechanical settling noise range for the ring
buffer, not a new, distinct bug. The original bug (every move
consistently 1-3 microns off) is gone; this is a much smaller,
edge-of-sufficiency effect specific to the larger move distance.

**Bumped** `move_absolute()`'s default `settle_s` from 0.3 to 0.5,
which comfortably covers the 100-micron case in the same real-hardware
run. Documented plainly, from both real data points, rather than
picking a number and hoping: 0.3s was enough at 5 microns but not
quite at 100; 0.5s covered 100. Row-level moves aren't performance-
critical (once per row, not per frame), so a modest, honestly-labeled
safety margin is the right tradeoff over chasing a sub-noise-floor
residual with more complexity (e.g. distance-proportional settling)
that isn't yet justified by enough data.

**Not yet characterized**: much larger moves (multi-mm, e.g. a large
tile-to-tile jump) -- the relationship between travel distance and
required settle time isn't established beyond "it exists and these two
points fit a 0.5s default." Updated the bench test's own `--settle-s`
default and help text to match, and to point at sweeping both
`--settle-s` and `--max-travel-tenths-um` together if larger moves are
needed in practice.

## Redesigned move_absolute() -- fixed settle_s replaced with self-adapting WHERE-based convergence check

Correctly flagged: a fixed settle delay can't be right across the real
range of row-level Z moves, which span from small row-to-row nudges to
multi-mm tile-to-tile jumps. The two real-hardware data points already
in hand (0.3s sufficient at 5 microns, not quite at 100 microns) were
themselves direct evidence of this -- a single constant was always
going to be wrong at some scale: wasteful for small moves, insufficient
for large ones.

**Fixed properly rather than picking a bigger constant**: `settle_s`
removed. `move_absolute()` now polls `WHERE` directly after `RDSTAT`
reports idle, waiting until position converges to within
`settle_tolerance` (new, default 2.0 raw units -- a small margin above
the ~1-unit mechanical noise floor already characterized for the ring
buffer) of the commanded target, instead of assuming any fixed or
distance-scaled wait is correct. This self-adapts to whatever the real
settling time actually is for that specific move -- no tuning needed
for either end of the range. Kept a small `extra_settle_s` (default
0.1s) as a cheap buffer after the tolerance check passes, in case of
brief residual mechanical ringing an encoder-position check alone
might not catch -- not yet independently confirmed necessary, kept
small rather than assumed away.

Polling `WHERE` this way is a different situation from the earlier-
confirmed `WHERE`-reliability problem during active, high-rate
camera-triggered Z stepping -- that issue was specific to querying a
busy axis under fast concurrent hardware triggering; this is a single
slow settling check with nothing else competing for the axis's
attention, the same context `move_absolute()`'s own `RDSTAT` polling
has already been working correctly in throughout this whole testing
sequence.

Rewrote `asi_tiger_move_absolute_test.py` to validate the self-adapting
behavior directly rather than just re-test a fixed value: runs across a
wider distance range and reports actual convergence time per move in a
summary table, so the scaling (near-instant for small moves, longer for
large ones, no manual tuning) is visible directly rather than asserted.
Added an opt-in `--test-large-move-mm` for a genuinely multi-mm round
trip -- the actual scenario this fix targets -- kept opt-in and
separately confirmed given it's a much bigger physical move than the
rest of the test.

Verified against a mock simulating distance-proportional settling time
(not a fixed gap): small/medium moves converge in ~0.6-2s, a 1mm move
correctly takes ~10s and converges exactly on target, and a case
lacking sufficient timeout correctly reports non-convergence rather
than a false pass -- the summary table's convergence-time column
directly demonstrates the scaling this fix is meant to provide.

## move_absolute() redesign confirmed on real hardware; fixed an overly strict test check found in the process

Re-ran `asi_tiger_move_absolute_test.py --test-large-move-mm 1.0` on
real hardware after the settle_tolerance redesign: 6 of 7 moves landed
with zero residual (small moves ~0.24-0.34s, including the 1mm move out
at 0.76s -- convergence time scaling with distance exactly as
intended), one move showed a 1.0-unit (0.1 micron) residual, flagged by
the test as a MISMATCH.

**That flag was a bug in the test script, not in move_absolute()**:
the function's own contract is "converge to within settle_tolerance"
(default 2.0 units), and 1.0 is comfortably inside that -- it's also
exactly the magnitude of mechanical settling noise already
characterized elsewhere in this project for this stage. The test's
pass/fail check used an exact-match comparison instead of checking
against the same tolerance the function itself promises, holding it to
a stricter standard than its own contract and flagging ordinary,
expected noise as a failure.

**Fixed**: both pass/fail checks in the test (per-move, and the final
wait=False check) now compare against `args.settle_tolerance`, matching
what `move_absolute()` itself guarantees, and report the actual
residual value rather than a bare pass/fail. Verified against a mock
with a simulated 1.0-unit residual (now correctly reported as OK
throughout) and separately against a mock with a simulated 5.0-unit
residual, exceeding tolerance (still correctly caught as a real
failure in both the library function itself, which now correctly
returns `False` after exhausting the timeout since it never converges,
and the test's reporting) -- confirming the fix didn't make the check
too lenient in the process of fixing it.

With this, `move_absolute()`'s self-adapting redesign is confirmed
working correctly on real hardware across the full range tested: 5
microns through 1mm round trip.

## New: laser command bench tests -- the last two untested row-level pieces

**`tools/asi_tiger_laser_enable_test.py`** -- cycles each configured
laser BNC (default 5-8) ON/OFF individually, tests independence (all
lines together, then `disable_all()`), scope-based confirmation
matching the L/R switch test's pattern. The underlying mechanism (a
manually-toggled cell driving a BNC) is proven repeatedly elsewhere in
this project; this is the first real-hardware check on this specific
BNC range and with multiple independent lines active together. Found
and fixed a real bug in the script itself while testing: `--n-lasers`
sliced `laser_bncs` but not `toggle_cells` to match, and
`configure_laser_enable_lines()`'s own length-mismatch validation
(added earlier specifically for this class of error) correctly caught
it -- verified both the limited and full-default paths after the fix.

**`tools/asi_tiger_laser_intensity_test.py`** -- sweeps a small set of
voltage levels on each of card 35's (ASI numbering: slot 5) P/Q/R/S
axes in turn. `ASITigerDAC.set_voltage()` itself needs no new code
(thoroughly validated elsewhere -- ETL, galvo, the L/R switch), but
this specific card has never been touched, and P/Q/R/S's physical-to-
electrical mapping hasn't been independently confirmed the way the
ETL/galvo cards' layout was (via the `N` command, early in this
project) -- the test's own confirmation prompt asks specifically
whether the axis letter driving each output matched what was expected,
not just whether the sweep worked.

Both verified against a mocked serial device (control flow and command
sequencing only -- real hardware confirmation, including which
physical connector each axis letter actually drives, is next).

With these two, every row-level setup function (laser enable, laser
intensity, L/R switch, absolute Z move) has now been checked on real
hardware. Remaining before mesoSPIM-control integration: the adapter
architecture question (whether mesoSPIM's 6-method NI-shaped I/O
interface fits this self-sustaining autonomous loop, or needs
rethinking).

## Confirmed: no BNC8 collision between the trigger chain and laser enable lines

Flagged before testing the laser scripts: `configure_zstack_trigger_chain()`'s
`counter_monitor_bnc` and `row_setup.configure_laser_enable_lines()`'s
default `laser_bncs=(5,6,7,8)` both had a claim on BNC8 at one point --
z_in0/z_out0/camera_expose/camera_trigger (1-4) plus all 4 default
laser BNCs (5-8) already claim every physical BNC on this rack, so an
earlier default of `counter_monitor_bnc=8` would have silently
collided with the 4th laser's enable line once both were used
together in a real acquisition.

This was already fixed in the code (`counter_monitor_bnc` defaults to
`None` -- no physical monitor output configured at all unless
explicitly requested, correctly skipped end-to-end through
`configure_pulse_pass_through_counter_single()`'s own `Optional[int] =
None` handling) and the fix is documented in both functions'
docstrings, but never got its own changelog entry -- adding one now
for the record.

Directly verified rather than just re-read the code: configured the
full trigger chain and the laser enable lines together against a
mocked serial device, both using their own full defaults with nothing
explicit passed for either. Confirmed the chain never touches BNC8 at
all by default, and `configure_laser_enable_lines()` correctly claims
it for the 4th laser with nothing to collide with.

## Drafted the real mesoSPIM-control adapter, mapping onto the 6-method NI-shaped pattern

With every device-layer piece confirmed on real hardware, drafted
`mesoSPIM_ASITigerWaveFormGenerator` for real -- rewrote its internals
from the old software-timed `WaveformStreamer` draft to actually use
`zstack_chain`/`row_setup`/`SingleAxisWaveform`, while keeping the
earlier draft's still-valuable infrastructure (connection sharing with
mesoSPIM's own ASI stage driver, `config_check()`'s validation pattern,
and critically `close_tasks()`'s safety note that PLC outputs persist
in hardware after the host disconnects).

**The central architectural decision, made explicit rather than
papered over**: mesoSPIM's NI-based model calls
`write_waveforms_to_tasks()` -> `start_tasks()` -> `run_tasks()` ->
`stop_tasks()` once per FRAME (one buffered analog sweep = one camera
trigger via a counter output, per mesoSPIM's own hardware
documentation). `zstack_chain`'s self-sustaining loop runs an entire
multi-plane stack autonomously from one kick -- a different
granularity. Resolved by detecting whether the requested row's
parameters have changed since the last call (`_row_signature()`,
comparing n_planes/z_step/z_start/laser/side/ETL amplitude+offset): the
FIRST call for a new row does the real work (configure the whole
chain, kick it, block in `run_tasks()` until `wait_until_complete()`
confirms the WHOLE stack is done); repeat calls with the same
signature become fast no-ops. This preserves the actual point of
everything built in `asi_tiger/` (no host involvement during the fast
per-frame loop) -- but it assumes mesoSPIM's own camera/frame-grabbing
loop tolerates `run_tasks()` blocking for a whole stack's duration on
the first call and returning near-instantly on repeats, rather than
expecting exactly one camera frame per individual call. Documented
prominently in the module docstring as the one specific thing that
needs checking against mesoSPIM's real acquisition-loop source
(`mesoSPIM_Core.py`) before this is trusted -- everything else in this
project got real-hardware verification; this file can't be checked
that way, since it has to slot into mesoSPIM's actual Core/config
objects.

**Verified what's actually checkable without that source**: built a
fake stand-in for the real base class (a minimal class exposing
`cfg`/`state`/`config_check()`, since the real one needs PyQt5/nidaqmx
neither available nor necessary to test THIS file's own logic) and a
fake `parent`, then exercised the full lifecycle against a mocked
serial device. Confirmed: all `asi_tiger` imports resolve correctly;
`create_tasks()` instantiates every device wrapper without error;
`write_waveforms_to_tasks()`/`start_tasks()`/`run_tasks()` correctly
do real work on a new row and correctly no-op on a repeat call with an
unchanged signature; changing `n_planes` correctly resets the no-op
state for a genuinely new row; `stop_tasks()`/`close_tasks()` run
cleanly.

**Found and fixed a real bug in the process**: `create_tasks()`
originally called `configure_laser_enable_lines()` with only
`laser_bncs` (from config) and no matching `toggle_cells` -- the exact
same length-mismatch bug already caught once in
`asi_tiger_laser_enable_test.py`. The smoke test's fake 2-laser config
(fewer than the library's 4-element default `toggle_cells`) triggered
the same validation error immediately. Fixed by computing
`toggle_cells` to match `len(laser_bncs)`, same as the test script's
fix, and re-verified.

Extended `config_check()`'s required-keys validation to match the new
`asi_dac_parameters` shape (z/etl/camera card addresses and BNCs,
`laser_bncs`, `laser_dac_channels`) instead of the old draft's
`galvo_etl_channels`/`laser_channels` shape.

Not yet done: updating `config_asi_tiger_example.py` to match this new
`asi_dac_parameters` shape (it still reflects the old draft's config
keys), and the real integration check against `mesoSPIM_Core.py`
described above.

## Fetched the real mesoSPIM_Core.py -- corrected and simplified the adapter's architecture assumption

The earlier draft's central assumption (write_waveforms_to_tasks() also
gets called once per frame, needing its own "is this a new row"
detection) was WRONG, confirmed by fetching mesoSPIM_Core.py directly
rather than continuing to guess. The real call pattern for an
acquisition series: `prepare_image_series()` calls `create_tasks()` +
`write_waveforms_to_tasks()` -- ONCE. `snap_image_in_series()` then
gets called repeatedly, ONCE PER FRAME (laser enable ->
`start_tasks()` -> `run_tasks()` -> `stop_tasks()` -> laser disable).
`close_image_series()` calls `close_tasks()` -- ONCE -- at the end.
(Separately, `snap()`/`live()` call all 6 methods together per call --
a different path, for single snaps/live preview, not series.)

mesoSPIM's own code already guarantees the once-per-row granularity
for waveform SETUP -- `write_waveforms_to_tasks()` never needed its own
row-signature detection at all. **Removed** `_row_signature()` and
`_current_row_signature` entirely; `write_waveforms_to_tasks()` now
always does the full setup and simply resets `_row_kicked`, matching
what mesoSPIM's own call pattern already provides. The REAL, narrower
mismatch is exactly what `_row_kicked`'s existing skip-logic in
`start_tasks()`/`run_tasks()` was already built for: those three DO get
called once per frame, while `zstack_chain`'s loop runs the whole stack
from one kick -- unchanged, since this part turned out correct already.

Re-verified against a mock, this time simulating the REAL confirmed
call sequence exactly (create_tasks+write_waveforms_to_tasks once,
then start_tasks+run_tasks+stop_tasks three times for a 3-frame row,
then close_tasks once) rather than a generic lifecycle smoke test --
confirms `_row_kicked` correctly stays `True` after the first frame,
matching the real pattern exactly.

**Still open, and now more narrowly scoped**: the specific per-plane
loop that calls `snap_image_in_series()` repeatedly (likely in
`run_acquisition()`, further down in `mesoSPIM_Core.py` than could be
fetched -- GitHub's raw file access is blocked by robots.txt from this
environment, and the rendered blob view truncates a file this size).
In particular, whether that loop has any timing expectation that
`run_tasks()` takes roughly the same short duration on every call --
this design's first call blocks for the whole stack's duration, every
call after is a fast no-op. Worth checking that loop specifically
before trusting this on real hardware, though the scope of what needs
checking is now much smaller than before.

## User-supplied run_acquisition() source: two real bugs found and fixed, one genuinely new open question

The user pasted `run_acquisition()`'s real source (the actual per-plane
loop, not reachable via fetch from this environment). Confirmed the
per-frame call pattern exactly as this file already assumed --
`snap_image_in_series()` called once per frame via `for i in
range(steps)`. But reading the actual body surfaced two real bugs in
this adapter, and one genuinely new integration question.

**Bug 1 -- laser enable/disable didn't belong in this file at all.**
`run_acquisition()` calls `self.laserenabler.enable(laser)` once before
its loop; `self.laserenabler` is a separate object mesoSPIM_Core
instantiates independently (`mesoSPIM_LaserEnabler`/
`Demo_LaserEnabler`), the same pattern as `NI_Shutter`/`Demo_Shutter`
for shutters -- not something the waveformer touches. This file's
`write_waveforms_to_tasks()` was calling
`row_setup.configure_laser_enable_lines()` and enabling/disabling
lasers itself. Removed entirely -- `self._lasers`, the import, the
`create_tasks()` setup, the `config_check()` validation for
`laser_bncs`. `row_setup.LaserEnableLines` belongs behind a separate
`mesoSPIM_ASITigerLaserEnabler` class implementing the same
`.enable(laser)`/`.disable_all()` interface -- not built yet.

**Bug 2 -- Z's row-starting position didn't belong here either.**
`prepare_acquisition()` already calls
`self.move_absolute(startpoint, wait_until_done=True)`, routed through
the existing stage driver, BEFORE it ever calls `prepare_image_series()`
(which is what eventually reaches this file). By the time this file
runs, Z is already at its starting position. Removed the redundant
`self._z.move_absolute(z_start)` call, along with the now-entirely-
unused `self._z`/`StageRingBuffer` field this file no longer needs --
`zstack_chain` creates its own internal `StageRingBuffer` for per-frame
stepping, a genuinely different responsibility from the one-time
row-start move.

**Bug 3 (smaller, caught while fixing the above) -- wrong state keys
for laser intensity/selection.** The removed code read
`self.state.get("laser_intensity_v", 0.0)` and
`self.state.get("laser_index", 0)` -- neither key exists in the real
state schema. Fixed to the real keys: `self.state['laser']` is the
laser NAME (matches `cfg.laserdict`'s keys, confirmed from
`set_laser()`'s docstring), and `self.state['intensity']` is 0-100%
(confirmed from `set_intensity()`'s docstring), converted to volts via
the already-validated `max_laser_voltage`. Verified against a mock:
`laser='561 nm'` (index 1) correctly selects `laser_dac_channels[1]`,
and `intensity=40%` × `max_laser_voltage=5V` correctly wire-encodes as
2.0V (2000 raw units).

**Genuinely new, still-open question**: does the EXISTING ASI stage
driver's own TTL/ring-buffer setup for Z (hinted at by
`prepare_acquisition()`'s move-backward-then-forward "priming" dance,
done once before enabling `ttl_movement_enabled_during_acq`) conflict
with `zstack_chain`'s own Z ring-buffer configuration when both are
active for the same axis? Documented prominently in the module
docstring rather than guessed at -- needs `mesoSPIM_Stages.py`'s ASI
stage implementation specifically to resolve, the same way this file
itself needed `mesoSPIM_Core.py`.

Re-verified the full per-frame lifecycle against a mock built to match
the real state schema exactly (laser name + intensity%, not the
made-up keys) -- all three fixes confirmed correct, `_row_kicked`
still correctly persists across the 3-frame per-row loop.

## User-supplied mesoSPIM_Stages.py and asicontrol.py: resolved the Z-ownership question

The user uploaded both files directly, resolving the "does the
existing ASI stage driver's TTL setup conflict with zstack_chain's"
question left open after the `run_acquisition()` read.

**Confirmed: two genuinely different mechanisms, but no real
conflict.** `asicontrol.py`'s `enable_ttl_mode()` sends `RM Y=3` (both
Z and Theta armed -- the code comment says "assume 2 axes per card")
then `TTL X=2 Y=2`, per card in `cfg.asi_parameters['ttl_cards']`. `TTL
X=2` is NOT this project's `TTL X=12` -- `asicontrol.py` never calls
`LOAD` anywhere; its own comment is explicit ("Relative movements
prior to turning on TTL determines which axes move in response to
pulse"), meaning X=2 repeats whatever relative move was last sent
directly via `R` (`asicontrol.py`'s own `move_relative()`), not a
`LOAD`-ed ring-buffer value. Checked against ASI's own `command:ttl`
docs to confirm rather than guess: X=12 is specifically documented as
the `LOAD`-dependent ring-buffer relative mode ("the values entered
into the ring buffer using the LOAD command represent RELATIVE
coordinates") -- exactly this project's own confirmed usage. 2 and 12
share no set bits in binary -- genuinely different modes.

Both write the SAME underlying card registers, so whichever runs last
wins -- not a real conflict once the ordering is known. Confirmed from
`mesoSPIM_Core.py`: `prepare_acquisition()` calls the existing driver's
TTL setup (the "priming" R-move dance, then `enable_ttl_mode()`)
BEFORE it calls `prepare_image_series()` -- which is what eventually
reaches `write_waveforms_to_tasks()` -> `configure_zstack_trigger_chain()`
-> `arm_relative()` + `set_output_mode()`. Since this project's setup
runs LAST, it correctly supersedes theirs: `arm_relative()` sends `TTL
X=12` (overwrites their 2); this project's own `z_axis_mask` (typically
Z-only) overwrites their `RM Y=3` (which included Theta);
`set_output_mode(OUT0_MODE_MOVE_COMPLETE)` sends `TTL Y=2` -- the SAME
value `asicontrol.py` also sets, so no conflict there either way. The
priming dance itself never touches the ring buffer (a direct `R` move,
not `LOAD`), so it's harmless -- just two wasted, net-zero physical
moves before this project's own setup takes over.

**One real, minor residual risk kept, not dismissed**: between
`enable_ttl_mode()` running and this project's own setup overwriting
it, there's a brief window where Theta (not just Z) is armed to
respond to TTL. Low-risk in practice (nothing should be triggering
before the acquisition genuinely starts), but documented in the
adapter's architecture note as where to look first if Theta motion
during setup is ever observed.

Updated `mesoSPIM_ASITigerWaveFormGenerator.py`'s architecture note
substantially: this finding replaces the earlier "STILL OPEN" Z-
ownership flag with a resolved, evidence-based conclusion, and the
note's opening now points to the one item that's still genuinely
open -- whether `sig_add_images_to_image_series`'s handler (in
`mesoSPIM_Camera.py`, not yet read) expects exactly one new frame per
`run_tasks()` call or just pulls whatever's currently queued. That's
now clearly the single remaining unknown, separated out from
everything else this and the previous two source reads have resolved.

## User-supplied utils/acquisitions.py: corrected a misunderstanding, found a serious real bug

The user asked whether `sig_add_images_to_image_series` connects to
`utils/acquisitions.py` (uploaded directly). It doesn't -- that file
has no PyQt import at all, `Acquisition`/`AcquisitionList` are plain
data structures (`indexed.IndexedOrderedDict`/`list` subclasses), not
QObjects, with no slots. The signal's declared argument *types*
happen to be defined there, which is different from being a connection
target. `mesoSPIM_Camera.py` remains the file needed to resolve that
question.

**But reading the file surfaced something much more important**: a
real, serious bug in the adapter, not just a misunderstanding to
correct. `n_planes` and `z_step` are NOT `self.state` keys -- they
don't exist anywhere in `mesoSPIM_Core.py`'s `prepare_acquisition()`.
`write_waveforms_to_tasks()` was reading `self.state.get("n_planes")`/
`self.state.get("z_step")`, which would always have evaluated to
`None`, since neither key is ever set anywhere in the real source.

**Fixed**: the real values are computed from the current row's
`Acquisition` object itself -- `acq.get_image_count()` for plane
count, `acq.get_delta_dict()['z_rel']` for the SIGNED step (positive
or negative depending on whether `z_end > z_start`).
`self.state['selected_row']` (set by `prepare_acquisition()` before
this method ever runs) indexes into `self.state['acq_list']` to reach
the current `Acquisition`. Bundled with this fix, a units bug: this
project's `z_step` has been tenths of a micron throughout;
`Acquisition`'s own docstring says its `z_step` is in microns --
multiplied by 10. Also cached `n_planes` as `self._n_planes` (was
previously re-read from the same nonexistent state key inside
`run_tasks()`'s timeout calculation too).

Verified against a mock built to mirror `Acquisition`'s real logic
exactly (`get_image_count()`/`get_delta_dict()`, not a simplified
stand-in) -- specifically tested a DESCENDING stack (z_end < z_start,
51 planes, -2.0 micron step) to confirm the sign handling, not just
the easier ascending case: correctly derived `n_planes=51` and loaded
`-20.0` (tenths of a micron) into the ring buffer.

Updated the module's architecture note with both the correction (the
`acquisitions.py`/signal-connection question) and the fix (the real
bug), keeping them clearly separate since they were easy to conflate
but are genuinely different findings.

## User-supplied mesoSPIM_Camera.py: the last open architectural question is resolved

The user uploaded `mesoSPIM_Camera.py` directly, resolving the one
item left open after the `run_acquisition()`/`mesoSPIM_Stages.py`
reads -- whether `run_tasks()` blocking for a whole row on its first
per-frame call is compatible with how mesoSPIM actually grabs camera
frames.

**Confirmed safe, at two levels, not inferred from thread
architecture alone.** `sig_add_images_to_image_series` connects to
`mesoSPIM_Camera.add_images_to_series()`
(`QueuedConnection`). Its own guard --
`if self.cur_image < self.max_frame:` -- means it's a no-op once all
of a row's frames are accounted for, regardless of how unevenly they
arrived across the per-frame loop's N calls; it calls
`self.camera.get_images_in_series()` each time and extends the queue
by however many images that returns, never assumed to be exactly one.
For the actual camera this project targets, confirmed from
`mesoSPIM_PhotometricsCamera.get_images_in_series()`:
`self.pvcam.poll_frame()`, a blocking call waiting for the NEXT frame
in the camera's own hardware buffer, always returning exactly one.
Combined: since `run_tasks()`'s first call blocks until every hardware
trigger has fired, all N frames are already sitting in the camera's
own buffer by the time it returns -- each subsequent `poll_frame()`
call then returns near-instantly, correctly draining one frame per
loop iteration, exactly matching `run_acquisition()`'s cadence.

**New, genuinely practical limit surfaced by this same read, not
eliminated by it**: nothing drains the camera's buffer WHILE
`run_tasks()` is blocking for the whole stack. For a very large row
(hundreds or thousands of planes), the camera's own hardware buffer
could overflow before the host ever gets a chance to poll a single
frame. A real scaling constraint on this design, not a bug -- the
camera's own buffer depth caps how large a single kick can safely be.
Not yet characterized: what that depth actually is for the
Photometrics Iris 15 this project targets, or whether it's
configurable.

Updated the module's architecture note: the opening summary now
states every architectural question this file raised has been
resolved by reading real sources (five files now: `mesoSPIM_Core.py`,
`mesoSPIM_Stages.py`, `asicontrol.py`, `utils/acquisitions.py`,
`mesoSPIM_Camera.py`), replacing the "STILL GENUINELY OPEN" flag with
a resolved, evidence-based conclusion and the new buffer-depth caveat
in its place.

With this, every architectural question raised while building this
adapter has been checked against mesoSPIM's real source rather than
assumed -- the device layer was validated on real hardware throughout
this project; this file's own logic has now had the equivalent
scrutiny applied against the real codebase it has to integrate with.

## Major redesign: true per-frame triggering, replacing zstack_chain for THIS adapter (zstack_chain itself untouched)

The buffer-overflow finding (previous entry) led to a real architecture
decision, discussed rather than made unilaterally: postpone the
autonomous, hardware-driven multi-plane design (zstack_chain) for the
mesoSPIM-control adapter specifically, in favor of true per-frame
triggering matching the original NI design's granularity exactly.
zstack_chain.py itself is completely untouched -- still fully working,
still hardware-confirmed at n_planes=3 and 300, still used by
`tools/asi_tiger_full_loop_test.py` and the other standalone bench
scripts. Only unwired from this one file, for now, with a clear path
back once buffer-depth/chunking questions are worth resolving.

**Realized while designing this**: with `ttl_movement_enabled_during_acq`
kept False (the config this new design requires), mesoSPIM's own
EXISTING, already-working ASI stage driver handles Z (and F) stepping
entirely on its own, via `run_acquisition()`'s per-iteration
`move_relative()` call -- confirmed from `asicontrol.py`, unchanged,
no code needed here at all. This adapter's job shrank to exactly:
fire one camera trigger, wait for that one exposure, sync the ETL to
it -- nothing about Z or F.

**Rewrote the adapter accordingly**: `create_tasks()` now configures a
manual, host-toggled camera-trigger cell (the same confirmed pattern
`tools/asi_tiger_camera_trigger_test.py` validated on real hardware)
plus the camera-Expose-Out-to-ETL-sync wiring, extracted directly from
`zstack_chain.py`'s own internals (confirmed correct there --
raw-level trigger to the ETL's backplane input, not a PLC-computed
edge, per that module's own hardware-confirmed finding) but without
any of the Z/counter machinery this design no longer needs.
`write_waveforms_to_tasks()` no longer reads `n_planes`/`z_step` at
all -- removed entirely, since Z stepping isn't this file's
responsibility anymore. `start_tasks()`/`run_tasks()`/`stop_tasks()`
now do real work on EVERY per-frame call (no more `_row_kicked`
no-op-after-first-call pattern -- that was specific to the
zstack_chain design and no longer applies). `run_tasks()` does a
two-phase wait -- first for Expose-Out to go HIGH (confirms the
exposure actually started, not just that the trigger pulse was sent),
then LOW (confirms it finished) -- polling `read_bnc_inputs()`, the
same PLC/BNC-state mechanism that's never shown `WHERE`'s
reliability issues anywhere in this project.

Added a `config_check()` warning if `asi_parameters['ttl_motion_enabled']`
is True, since that would disable the exact per-frame host-stepping
loop this design depends on -- the opposite requirement from the
earlier zstack_chain-based version of this file.

Verified against a mock simulating realistic Expose-Out timing (delay
before rising, then a real exposure duration before falling): all 3
of 3 simulated frames correctly did full, real per-frame work (fire,
wait-high, wait-low) -- not a kick-once pattern. Separately verified
both error paths don't crash: a simulated broken camera (Expose-Out
never rises) correctly logs and proceeds rather than hanging or
raising, and the `ttl_motion_enabled=True` misconfiguration correctly
produces the warning.

Noted for whenever the autonomous design is revisited: the user
reports PVCAMTest shows the Iris 15's real buffer around 50 frames
(not yet independently confirmed here) -- worth confirming precisely
before any future chunked-kick redesign sizes batches against it.

## New: standalone per-frame bench test, and a diagnostic lead on the full_loop_test inconsistency report

**`tools/asi_tiger_per_frame_trigger_test.py`** -- standalone bench
test replicating `mesoSPIM_ASITigerWaveFormGenerator`'s new per-frame
design EXACTLY (the same manual trigger cell, camera-expose-to-ETL-sync
wiring, two-phase wait, per-frame arm/disarm cycle), without needing
the full mesoSPIM-control software (PyQt5, a real Camera object,
Core/Camera threading) at all -- the right level to validate the
mechanism itself at, before adding the full software stack on top.
Verified against a mock simulating realistic Expose-Out timing: 5/5
frames correctly completed.

**Investigated a real-hardware report of inconsistent behavior in
`asi_tiger_full_loop_test.py --n-planes 10`.** Worth noting: the
`n_planes=3`/`n_planes=300` "milestone" runs that first confirmed the
full loop happened BEFORE `wait_until_complete()` existed -- they used
the old fixed-wait approach. This may be the first real-hardware
exercise of the latch-based completion check specifically. Added two
diagnostics rather than guessing at a fix: (1) right after `reset()`,
before `kick()`, checks `is_complete()` -- should be `False`; if it's
ever `True` here, the latch wasn't actually cleared by `reset()` (a
stale "done" from an earlier run), which would make
`wait_until_complete()` report done immediately, before the real loop
has done anything -- directly explaining "works when the latch happens
to be clear, fails when it isn't". (2) right before `disarm()`, prints
both `is_complete()` and `planes_remaining()`, to distinguish "reported
done but position was wrong" from "never reported done at all" if
something goes wrong. Verified against a mock deliberately simulating
a stuck/stale latch: the first diagnostic correctly caught and flagged
it.

Not yet resolved -- these diagnostics are built to gather the evidence
needed to actually diagnose the report, not a fix in themselves. Next
step is running `--n-planes 10` again with these diagnostics in place.

## Two real bugs found from real-hardware reports -- both traced to a root cause, both fixed

**Bug 1: is_complete()'s latch fires one full frame too early.** User
reported the full-loop test's `--n-planes 10` result was inconsistent
(one exact match, then two runs each short by exactly one step), and
directly observed on a scope that the ETL waveform dropped mid-sweep
on the LAST frame, before that frame's own Expose-Out had gone low.
Traced the exact mechanism rather than patching symptoms: the latch
cell clocks on the count cell's falling edge, which happens the moment
the one-shot's internal countdown reaches zero -- DURING the
(n_planes-1)th Z-move-complete pulse, the SAME pulse that the delay-
cell mechanism deliberately lets pass through the AND-gate ("so the
FINAL pulse still makes it through") to start the FINAL frame's own
exposure. So `is_complete()` reads "done" at the exact moment the last
frame BEGINS, not when it finishes -- explaining both the scope
observation and the inconsistency (whether a run happened to match
depended entirely on whether `poll_interval_s`'s own polling delay by
chance exceeded the final frame's real duration).

**Fixed**: added `final_frame_settle_s` to `wait_until_complete()`
(default 1.0s, explicitly documented as needing to cover the final
frame's real exposure+ETL+Z-move+settle time, not a cosmetic buffer --
same honesty standard as `move_absolute()`'s `settle_tolerance`).
Exposed as `--final-frame-settle-s` in `asi_tiger_full_loop_test.py`.
Verified directly and in isolation (not through the full test script,
which has too many other moving parts for this specific check): with
`final_frame_settle_s=0.4`, `wait_until_complete()` correctly blocks
for exactly that long after the latch goes high; with `0.0`, it
correctly reproduces the old, buggy near-instant return -- confirming
the mechanism itself works as designed, not just that numbers happened
to look right in one run.

**Bug 2: the per-frame adapter re-armed/disarmed the ETL every single
frame.** User pointed out the ETL was being armed and disarmed on
every frame in `asi_tiger_per_frame_trigger_test.py`'s runs, and
correctly suggested it should arm once and disarm once after all
frames -- matching this project's own earlier, confirmed finding that
external-trigger arming (SAM=2-style) auto-rearms on every subsequent
edge on its own, no host round-trip needed between triggers. Re-
arming via a fresh serial command immediately before firing each
trigger pulse is exactly the kind of race that would explain the
intermittent "Expose-Out never went high" failures seen on a random
frame each run, not a fixed one.

**Fixed**: `write_waveforms_to_tasks()` now configures AND arms the
ETL once, for the whole row. `start_tasks()`/`stop_tasks()` are now
no-ops (kept only because the base class's call pattern expects them
to exist). `close_tasks()` now stops/zeros the ETL once, at the actual
end. Verified against a mock tracking every `SAM=2` (arm) and `SAM=0`
(stop) command sent: across a full row of 5 simulated frames, arm
count stayed at exactly 1 (not re-armed per frame) and stop count
stayed at its `create_tasks()`-only baseline until `close_tasks()`
incremented it by exactly 1 more -- confirming the fix's actual
behavior, not just that the code compiles.

Both fixes came from real-hardware evidence the user gathered and
reported precisely (exact numbers, an oscilloscope observation, a
correctly-reasoned suggestion) -- neither would have been found from
the mocks alone.

## Two more real-hardware reports: a genuine ETL-arm ordering bug fixed, and a real inconsistency between the adapter and its matching test script fixed; one question still open

**Confirmed ordering bug in `asi_tiger_full_loop_test.py`**: `etl.arm_triggered()`
was called BEFORE `configure_zstack_trigger_chain()` -- which is what
actually wires the ETL's backplane trigger-in to the camera's real
Expose-Out. An armed ETL (SAM=2, fires on ANY rising edge) was
therefore watching a line that hadn't been wired yet, reflecting
whatever the PREVIOUS run/script left it as. Matches the user's report
exactly: running the full-loop test right after the per-frame test
produced one spurious ETL sweep on arm, then normal behavior after --
but NOT when running two full-loop tests back to back. **Fixed**:
moved the arm call to after the chain is fully configured, matching
the order already correct in `mesoSPIM_ASITigerWaveFormGenerator.py`
(wiring in `create_tasks()`, arming only after, in
`write_waveforms_to_tasks()`). Verified the reordered script still
runs cleanly end to end against a mock.

**Found and fixed a real inconsistency this session's own doing**:
`asi_tiger_per_frame_trigger_test.py` was never updated to match the
arm-once/disarm-once fix already made to the real adapter -- it still
re-armed the ETL inside the per-frame loop, every frame, exactly the
pattern already confirmed as both unnecessary (SAM=2 auto-rearms on
its own) and a real source of intermittent trigger failures. Fixed to
match the adapter exactly: arm once before the loop, stop once at the
very end. Verified against a mock tracking every `SAM=2`/stop command:
exactly 1 arm across all 5 simulated frames (was silently still doing
5 before this fix) -- and the real per-frame cycle time dropped from
~370ms to ~31ms in the same mock, a direct, visible demonstration of
the serial-round-trip overhead the redundant re-arming was adding.

**Still open, not yet explained**: the user's separate report of
PVCAMTest confirming 6 frames when the per-frame test requested 5 --
one extra exposure, seen before the script's own 5 deliberate
triggers. The camera-trigger cell (10) is explicitly forced low before
being wired to its BNC, so the setup sequence itself doesn't obviously
explain an extra pulse the way the ETL-arm-ordering bug explained the
other report. Worth checking on real hardware whether PVCAMTest's own
external-trigger-mode entry produces an initial frame independent of
this project's code entirely (e.g. watch PVCAMTest's frame counter
before running the script at all) before assuming this is a bug in the
PLC wiring -- not yet distinguished from a camera/PVCAM SDK behavior
outside this project's control.

## Ordering fix confirmed working; found and fixed a real crash from sustained PLC polling

Re-run after the ETL-arm-ordering fix: first run's spurious pre-kick
ETL sweep is gone, and the loop matched almost exactly (+102 vs +100
expected -- 0.2 of a 10-unit step, well within the mechanical settling
noise already characterized elsewhere in this project for this stage,
not a new bug worth chasing).

**Second run crashed with an unhandled IndexError**, inside
`is_complete()` -> `read_cell_outputs()`'s reply parsing, mid-poll
during `wait_until_complete()`. A genuinely new finding: `RDADC Z?`
CAN return a malformed/empty reply -- not previously observed anywhere
in this project, but also never previously polled this intensively.
`wait_until_complete()`'s default `poll_interval_s=0.1` over its
default `timeout_s=60` means up to ~600 reads in a single call --
`RDADC Z?` simply hadn't been exercised at that rate before. Same
underlying category of issue as `WHERE`'s known unreliability during
active triggering, just a different command hitting it for the first
time because nothing had polled a PLC read this hard before.

**Fixed**: `is_complete()` now catches a failed `read_cell_outputs()`
call and returns `False` ("not confirmed done yet") rather than
propagating the exception -- the same "treat a failed read as not
done" pattern already used for `move_absolute()`'s `RDSTAT` polling.
Lets the caller's own polling loop simply retry on the next interval
instead of crashing the whole wait.

Verified against a mock reproducing the exact real error first
(confirmed `read_cell_outputs()` itself does raise `IndexError` on a
reply with nothing after `=`, matching the real traceback exactly),
then confirmed `is_complete()` no longer propagates it across 10 calls
with 1-in-3 deliberately malformed (never crashes, still correctly
returns `True` on the good reads), then confirmed `wait_until_complete()`
itself -- the exact call that crashed on real hardware -- completes
successfully against the same flaky pattern.

### `configure_io()`: CCA Y/CCA Z command-ordering race (spurious camera
### exposure "before ETL is armed", real-hardware report)

**Real-hardware report** (precisely characterized by the user, order
matters): running `asi_tiger_per_frame_trigger_test.py` back to back,
or running `asi_tiger_full_loop_test.py` right after
`asi_tiger_per_frame_trigger_test.py`, produced ONE extra camera
exposure before the ETL was armed -- i.e. a spurious trigger during
setup, not from the intended per-frame/loop trigger logic. Running
`asi_tiger_full_loop_test.py` back to back, or running
`asi_tiger_per_frame_trigger_test.py` right after
`asi_tiger_full_loop_test.py`, did NOT show this -- only a PRECEDING
per-frame-test run reproduced it, regardless of what ran second.

**Root cause, found by inspection of `PLCCard.configure_io()`** (not
yet independently confirmed as the sole mechanism on real hardware --
see below): `CCA Y=<type>` (I/O direction) and `CCA Z=<source
address>` (which cell/I/O drives this line) are two SEPARATE serial
round-trips, not one atomic write. The previous version sent Y first,
then Z. Every camera-trigger BNC reconfiguration in this project
(`asi_tiger_per_frame_trigger_test.py`,
`mesoSPIM_ASITigerWaveFormGenerator.create_tasks()`,
`configure_zstack_trigger_chain()`) calls `configure_io(camera_trigger_bnc,
OUTPUT, source_addr=<some cell>)` as part of its OWN setup, on a BNC
whose CCA Z register may still hold whatever source address was
stored by the PREVIOUS script/session -- `safe_all_outputs()` (called
by every script's teardown) reverts the I/O TYPE back to input, but
the previous version never reset the stored SOURCE address, so it
persisted across scripts. With Y sent before Z, the pin went briefly
LIVE (as an output) still pointing at that stale source for the
several-millisecond gap until the new Z command landed. The per-frame
test's own camera-trigger cell (cell 10, a manually-toggled D-flop) is
the one stateful, host-controlled cell in this whole project whose
value isn't purely a live combinational function of other signals --
making it the one residual source whose value at any given instant
depends on exactly when it was last written, unlike
`configure_zstack_trigger_chain()`'s own camera-trigger source (an
OR-gate cell fed only by other combinational/settled-low signals,
which is presumably why loop-test-first was never observed to
reproduce this).

**Fixed**: `configure_io()` now writes `CCA Z` (source) BEFORE `CCA Y`
(type) when configuring a non-input I/O, so the pin only ever goes
live already pointing at the intended source -- the same "wire fully
before it can go live" principle already applied to `etl.arm_triggered()`'s
ordering relative to `configure_zstack_trigger_chain()` earlier in this
project. Symmetrically, reverting an I/O back to input now ALSO resets
its stored source address to constant-low (`CCA Z=0`) right after the
type change, so a stale non-zero source can never again be inherited
by a future `configure_io()` call on the same address that (for any
reason) doesn't pass its own `source_addr`. All existing call sites
already pass `source_addr` explicitly for every non-input
configuration (checked across the whole tree), so this reorder is
behavior-preserving for every caller except for closing exactly the
race window described above.

Also added: a diagnostic at the very start of
`asi_tiger_per_frame_trigger_test.py`'s and
`asi_tiger_full_loop_test.py`'s setup (before ANY reconfiguration) that
reads and prints `camera_trigger_bnc`'s raw level (and, in the
per-frame test, the camera-trigger cell's raw output) -- so if the
spurious exposure recurs after this fix, the next report will show
directly whether the stale condition this fix targets was actually
present, rather than relying on inference alone.

Verified against a mock that the exact command sequence for a
non-input `configure_io()` call is now `CCA Z=<source>` THEN
`CCA Y=<type>` (previously the reverse), that reverting to input sends
`CCA Y=0` THEN `CCA Z=0`, and that `_output_addrs` tracking (used by
`safe_all_outputs()`) is unaffected by the reorder.

**Not yet independently confirmed on real hardware as the complete
explanation** for the reported spurious exposure -- shipped as a real,
independently-justified correctness fix (the race it closes is a
genuine one, provable from the protocol alone) regardless of whether
it turns out to be the entire story. If the spurious exposure recurs
after this fix, the new diagnostic prints are the next thing to check.

### `safe_all_outputs()`: revert-to-INPUT itself was the glitch (real
### root cause, confirmed by the user pointing at the actual asymmetry)

**The CCA-Y/CCA-Z ordering fix above did NOT resolve the report** --
user confirmed "not fixed, same behavior" after retesting. The user
then pointed at the real structural difference directly: full-loop's
`ZStackTriggerChain.disarm()` and per-frame's teardown use genuinely
different mechanisms to quiesce the PLC at the end of a run --
`disarm()` only disarms Z's ring buffer and sets Z's OUT0 mode to LOW;
it never touches any BNC's I/O TYPE at all, so every BNC the loop
configured (including camera_trigger_bnc) stays a live, actively-driven
OUTPUT for the rest of the process's life. Per-frame's (and the
adapter's) teardown, by contrast, calls `PLCCard.safe_all_outputs()`,
which explicitly reconfigures every output BNC back to `IO_TYPE_INPUT`.
That revert-to-input step, not anything about WHAT it was sourced from,
is the actual asymmetry that lines up exactly with the report (only a
preceding per-frame-test run reproduced the spurious exposure; a
preceding loop-test run never did, because loop-test's `disarm()` never
performs this revert at all).

**Root cause**: reconfiguring a push-pull output that has been actively
driving a line LOW straight to a floating, high-impedance INPUT
(`CCA Y=0`) can itself produce a brief glitch on real hardware -- for
the instant between the PLC's own drive disengaging and whatever the
downstream device's input biasing settles the now-undriven line to,
the line isn't being held to any defined level by either side. A
camera in external-trigger mode only needs a few microseconds of a
rising edge to latch a real exposure, so this is a plausible,
electrically sound explanation for a spurious trigger appearing right
around teardown/setup -- and it explains why ONLY the revert-to-input
path (never the loop chain's leave-as-a-driven-output path) could ever
produce it, regardless of what either script does afterward.

**Fixed**: `safe_all_outputs()` no longer reverts tracked output
addresses to `IO_TYPE_INPUT`. It now reconfigures them to REMAIN an
OUTPUT, but repoints their source to `CONST_LOW` (address 0, a
hardwired constant-0 register, not any logic cell). This still
achieves the original purpose the method exists for (see
`clear_state()`'s docstring: a still-live logic cell, e.g. a toggle
that keeps switching on every camera trigger, must not be left driving
a physical line after the host disconnects) -- the line is
permanently disconnected from any cell whose state could ever change
again -- while never producing an undriven/floating transition at all:
the line goes from "driven low by a cell" straight to "driven low by a
hardwired constant," with no gap where nothing is driving it.

Verified against a mock: `safe_all_outputs()` now sends `CCA Z=0` then
`CCA Y=2` (push-pull output) for every previously-tracked address,
never `CCA Y=0` (input) again, and the addresses remain in
`_output_addrs` (still genuinely outputs, just hardwired-safe now)
rather than being dropped from tracking.

**Still not independently confirmed as the complete explanation on real
hardware** -- this is the most electrically sound mechanism found so
far, and it directly targets the exact asymmetry the user identified
between the two scripts' teardown paths, but the earlier CCA-Y/CCA-Z
ordering fix looked well-justified too and did not resolve the report
on its own. If the spurious exposure still recurs after this fix, the
diagnostic prints added earlier (BNC4/cell 10 raw state at the start of
setup) are the next evidence to check, and the two fixes together
should be considered as a pair, not independently, since either one
alone was insufficient.

## Added galvo driving + per-arm ETL (real integration gap found and closed)

**Found while surveying what's actually needed to run this against real
mesoSPIM-control**: `write_waveforms_to_tasks()` never touched a galvo
axis at all -- it only set laser intensity, the L/R switch, and
configured/armed the ETL. A real row would have triggered the camera
and swept the ETL correctly, but the light-sheet-forming galvo mirror
would never move -- a static line, not a scanned sheet. This gap dates
to the per-frame redesign; an earlier, since-superseded draft config
(`config_asi_tiger_example.py`) still had `galvo_etl_channels` entries
with real bench-confirmed card/axis mappings, but nothing in the
CURRENT adapter ever consumed them, and `config_check()` didn't even
require the key anymore.

**Also found**: the existing ETL code was driving a SINGLE shared axis
(`ah["etl_card_addr"]`/`ah["etl_axis"]`) regardless of `side`, while
reading side-specific `self.state` keys (`etl_l_amplitude` vs
`etl_r_amplitude`). Confirmed directly with the user: the rack has TWO
physically separate illumination arms, each with its own galvo mirror
AND its own ETL -- not one pair shared/switched optically. So the
single-shared-ETL-axis code was a real bug: correct amplitude/offset
math, wrong physical lens.

**Fixed, both together**: the adapter now holds four separate
`SingleAxisWaveform` objects (`_etl_l`, `_etl_r`, `_galvo_l`,
`_galvo_r`), all instantiated and defensively stopped/zeroed in
`create_tasks()`. `write_waveforms_to_tasks()` (still called once per
row, refreshed EVERY row per the user's explicit requirement, since
side/amplitude/offset/frequency can all change row to row):
- Explicitly `stop_and_zero()`s BOTH the inactive side's ETL and its
  galvo first -- necessary because rows can alternate sides, and an
  axis left armed/running for a PREVIOUS row's opposite side would
  otherwise stay that way into the new row.
- Configures and arms the ACTIVE side's ETL (unchanged logic, just now
  addressed to the correct axis).
- Configures and starts (free-running, SAM=1) the ACTIVE side's galvo,
  reading `self.state['galvo_{l,r}_amplitude'/'_offset'/'_frequency'/
  '_duty_cycle']` -- confirmed these are the real mesoSPIM state keys
  by fetching `mesoSPIM_WaveFormGenerator.py`'s actual source directly
  (not assumed). `_phase` is read by mesoSPIM's own NI-based software
  waveform math but has no equivalent here (galvo runs free, on its
  own internal clock, with nothing to phase against) and is not used.
  `_duty_cycle` likewise has no exact hardware equivalent -- the card
  only offers fixed pattern shapes -- approximated as PATTERN_TRIANGLE
  for duty_cycle in [0.4, 0.6] (matching `asi_tiger_galvo_etl_demo.py`'s
  own default/rationale: smoother, no flyback discontinuity) and
  PATTERN_SAWTOOTH otherwise; NOT yet confirmed on real hardware that
  this is visually/optically fine for every duty_cycle mesoSPIM's GUI
  allows.
- Galvo safety: mirrors `asi_tiger_galvo_etl_demo.py`'s own pre-flight
  check (ASI's own limit -- "Limit command voltage to +/-10.00V to
  guarantee galvo amplifier safety") -- `amplitude/2 + abs(offset)` is
  checked against `asi_dac_parameters['galvo_max_volts']` (default
  10.0V) BEFORE `configure()`/`start()`; if exceeded, logs an error and
  leaves that row's galvo stopped/zeroed rather than commanding an
  out-of-range voltage. `SingleAxisWaveform.configure()` has NO
  built-in safety clamping of its own (checked directly in
  `singleaxis.py`'s source, unlike `ASITigerDAC`'s `DacChannel`), so
  this check is the adapter's own responsibility.

`close_tasks()` now stops/zeros all four axes (previously only the one
shared ETL axis).

`config_check()`'s required keys updated to match: `etl_l_axis`/
`etl_r_axis` (removed the old single `etl_axis`), `galvo_card_addr`/
`galvo_l_axis`/`galvo_r_axis` added. Documented assumption (not yet
independently confirmed): both sides of each pair share ONE card
(`etl_card_addr`, `galvo_card_addr`) -- true for the historical bench
mapping (etl_l/etl_r on card 34's H/J, galvo_l/galvo_r on card 37's
A/C); if a real rack instead has them on different cards, this would
need a further `_card_addr` split per side.

**`config_asi_tiger_example.py` rewritten from scratch** to match the
CURRENT `config_check()` shape exactly -- the previous version still
reflected an even earlier draft (`galvo_etl_channels`, `laser_channels`,
`plc_camera_trigger_bnc`, `plc_laser_bncs`, `plc_camera_trigger_cell`,
`software_stream_rate_hz`, none of which the current adapter reads) and
would have failed `config_check()` immediately if used as-is. All the
real ASI-confirmed hardware facts from the old file (TGGALVO firmware
units-per-volt change, K axis's confirmed trigger fault, the production
H/J pair, the galvo amplifier safety limit) were carried over, just
reorganized under the current key names. Verified the new example
satisfies `config_check()`'s exact required-key list by executing it
and checking every required key is present.

**Verified against a mock** (a synthetic stand-in base class, since the
real `mesoSPIM_WaveFormGenerator` needs PyQt5/nidaqmx neither available
nor necessary to test this file's own logic, following the same
approach used earlier in this project): a Left-side row starts galvo L
(`SAM A=1`) and arms ETL L (`SAM H=2`), while stopping galvo R/ETL R
(`SAM C=0`/`SAM J=0`); switching to a Right-side row correctly reverses
this -- stops galvo L/ETL L, starts galvo R/arms ETL R; an out-of-range
galvo amplitude (peak > `galvo_max_volts`) is refused with no `SAA`/
`SAM=1` commands sent at all, logging an error, without crashing;
`close_tasks()` stops/zeros all four axes and clears all four
references. Not yet run against real hardware -- unlike the rest of
this project's per-frame design, this piece hasn't had a bench
verification pass of its own yet (no galvo+per-arm-ETL-specific test
script exists; `asi_tiger_galvo_etl_demo.py` confirmed the underlying
free-running-galvo + triggered-ETL mechanism works, but not through
this adapter's own row-refresh/side-switch/safety-check logic).

## New: `asi_tiger_galvo_per_arm_bench_test.py` -- bench test for the galvo/per-arm-ETL logic above

Closes the gap noted immediately above: a dedicated bench test for
`write_waveforms_to_tasks()`'s row-refresh/side-switch/safety-refusal
logic specifically (not just the underlying free-running-galvo +
triggered-ETL mechanism, which `asi_tiger_galvo_etl_demo.py` already
covers for one pair in isolation).

Mirrors the adapter's exact sequence -- two ETL axes, two galvo axes,
quiesce-then-activate per row -- against a configurable sequence of
simulated rows (`--rows L,R,L,R` by default). After each row, in
addition to firing real camera trigger pulses (same manual-cell
mechanism as `asi_tiger_per_frame_trigger_test.py`) so the ETL sweep
and galvo scan can be watched on a scope, it does a REAL HARDWARE
READBACK -- `SAM <axis>?` on all four axes -- and checks that exactly
the expected axis reads armed/running and the other three read idle,
rather than just trusting that no exception was raised. `--test-safety-refusal`
appends one row with a deliberately out-of-range galvo amplitude
(30Vpp) and confirms via the same readback that it was left un-driven.

**Caveat, found and fixed during mock verification, worth knowing before
reading real results**: the safety-refusal row is placed on whichever
side did NOT run in the immediately preceding row, deliberately --
placing it on the SAME side as the previous row would make a refused
row correctly leave that axis in whatever state the PREVIOUS
(successful) row left it, since `write_waveforms_to_tasks()`'s galvo
safety check only skips THIS row's `configure()`/`start()` on refusal,
it does not stop an axis that's already legitimately running from
before. An earlier version of this test script assumed a refused row
always reads back idle (SAM=0) regardless, which is only true when the
axis started from idle -- fixed by choosing the refusal row's side to
guarantee that starting condition. This was caught by the mock, not
real hardware, so it's a test-script correctness note, not a finding
about the adapter itself.

`SAM <axis>?` readback support itself is NOT independently confirmed
against real Tiger firmware anywhere else in this project -- every
other use of SAM in this codebase only ever WRITES it. The script
degrades gracefully (prints a note, skips the state check for that
row, but still fires the frames/watches-on-scope path) if the query
isn't supported, rather than crashing.

Verified end-to-end against a purpose-built stateful mock (tracks SAM
mode per card/axis so the readback assertions are meaningful, not just
canned replies): a 3-row L/R/L sequence plus a safety-refusal row all
show the expected exactly-one-axis-active pattern and the refusal
correctly leaves its target axis idle. Not yet run against real
hardware.

## Bench test run on real hardware confirmed working; ETL period now derived from the real camera exposure time

User ran `asi_tiger_galvo_per_arm_bench_test.py` against real hardware
(L,R,L,R sequence, 3 frames/row): all 4 rows passed their SAM-readback
checks, with real Expose-Out cycles completing (~295-340ms each).
Side-switching (quiescing the inactive arm, arming/starting the active
one) confirmed working correctly on real hardware, not just the mock.

Separately, the user changed the camera's PVCAM/Photometrics readout
mode to "All Rows" specifically so Expose-Out would visibly span the
camera's FULL configured exposure duration. This supersedes an
earlier, different-conditions finding (Expose-Out ~30ms shorter than
the exposure setting, under rolling-shutter readout) -- under "All
Rows", Expose-Out now genuinely matches the configured exposure time.
Consequence: the ETL's SAM=2 sweep period should now track the real
camera exposure time (default example: 150ms), not stay pinned to the
flat 120.0ms guessed value the adapter and bench test both used
before this readout-mode change.

Fixed by adding `_etl_period_ms(ah)` to
`mesoSPIM_ASITigerWaveFormGenerator.py`:
- If `asi_dac_parameters['etl_period_ms']` is explicitly set, it wins
  unchanged (manual override escape hatch, e.g. for a rack whose
  Expose-Out timing doesn't track exposure the same way).
- Otherwise, the period is derived every row from
  `self.state['camera_exposure_time']*1000 - etl_period_margin_ms`
  (new optional key, default 3.0ms) -- the same safety margin
  reasoning as before (the ETL's SAM=2 period must stay SHORTER than
  the camera's real trigger interval, or the axis misses every other
  trigger edge), just computed from the real exposure instead of a
  constant.
- If the derived period comes out under 2.0ms, it's clamped to 2.0ms
  with a logged error (1ms is documented as undefined behavior for
  this firmware) -- this should only trip on a misconfigured
  `camera_exposure_time` or an unreasonably large margin, not normal
  use.

Both call sites in `write_waveforms_to_tasks()` (the ETL's own
`configure()` call, and the galvo's period fallback used only when
`galvo_freq <= 0`) now go through this helper instead of a flat
`ah.get("etl_period_ms", 120.0)`.

Mock-verified in isolation (method extracted and exercised against a
synthetic stand-in, not the full adapter, since only `self.state` and
the `ah` dict are needed): explicit override returns unchanged;
150ms exposure with the default 3.0ms margin derives 147.0ms; a
custom margin is respected; a 3ms exposure (net 0ms after margin)
correctly clamps to 2.0ms and logs the expected error; a missing
`camera_exposure_time` key falls back to the documented 0.5s default.
All five cases passed on the first attempt.

`asi_tiger_galvo_per_arm_bench_test.py` updated to match exactly, so
it doesn't silently diverge from the adapter's new behavior (the
user's last real-hardware run used this exact script): added a new
`resolve_etl_period_ms(args)` function with identical arithmetic,
`--etl-period-ms` now defaults to `None` (auto-derive) instead of a
flat 120.0, and a new `--etl-period-margin-ms` flag (default 3.0)
matches the adapter's new config key. Mock-verified against the same
three cases (explicit override, 150ms-exposure derivation matching
the adapter's 147.0ms exactly, and the 2.0ms clamp path) with
identical results. Also fixed one pre-existing, unrelated pyflakes
nit in the same file (an f-string with no placeholders) while making
these edits.

`config_asi_tiger_example.py`'s `etl_period_ms` entry updated to
reflect that it's now optional/auto-derived by default (commented out,
with the new `etl_period_margin_ms` key documented in its place).
Verified the example still executes cleanly and satisfies
`config_check()` (which never required `etl_period_ms` in the first
place -- it was always read via `ah.get()`, so no required-key list
needed updating).

Not yet re-run on real hardware with this change -- the user's
"changes between axis worked ok" run predates it. Next real-hardware
pass should confirm the ETL sweep duration now visibly tracks the
configured exposure time (e.g. ~147ms of actual sweep for a 150ms
exposure, default margin) rather than the old flat ~120ms.

## Default `etl_period_margin_ms` dropped from 3.0ms to 0.0ms -- margin found negligible under "All Rows"

User, directly, after the change above: "in reality, margin period is
negligible if the expose out is in 'all rows'". The 2-3ms margin this
project had used previously was established under the OLD
rolling-shutter Expose-Out timing, where Expose-Out was itself a
separate, shorter, derived pulse with its own jitter relative to the
real exposure window -- margin against THAT made sense. Under "All
Rows" (the readout mode now in use), Expose-Out spans the exposure
window directly, so there's no known remaining source of jitter for a
margin to protect against.

Changed `_etl_period_ms()`'s default `etl_period_margin_ms` from 3.0
to 0.0 (derived period = the real camera exposure time, exactly -- a
150ms exposure now derives a 150.0ms ETL period, not 147.0ms). The key
itself is left in place, not removed, as a manual escape hatch in case
a different rack or readout mode is later found to still need some
margin.

Updated in lockstep: `asi_tiger_galvo_per_arm_bench_test.py`'s
`--etl-period-margin-ms` default (3.0 -> 0.0, help text updated with
the user's quote); `config_asi_tiger_example.py`'s
`etl_period_margin_ms` entry (previously set explicitly to 3.0, now
commented out since 0.0 is already the default, with a note on when to
uncomment it).

Mock-verified: with the new default, a 0.15s (150ms) exposure derives
exactly 150.0ms in both the adapter's `_etl_period_ms()` and the bench
test's matching `resolve_etl_period_ms()` -- confirmed identical.
Explicit `etl_period_ms` override and explicit non-zero
`etl_period_margin_ms` are both still respected in both places.

**Confirmed on real hardware by the user** ("running well without
errors") -- the exposure-derived ETL period (0.0ms margin) runs
cleanly with the galvo + per-arm ETL bench test, no regressions from
the margin default change.

## Full mesoSPIM-control integration push: cloned the real repo, found and fixed a serious call-frequency bug, fixed an incomplete config example

User asked to move on to full mesoSPIM-control integration. Previously
every claim about mesoSPIM's real call pattern was based on the file
being fetched piecemeal or user-pasted (mesoSPIM_Core.py,
mesoSPIM_Stages.py, asicontrol.py, utils/acquisitions.py,
mesoSPIM_Camera.py) as specific questions came up -- correct as far as
it went, but incomplete: `snap()`/`live()` (the single-snap and
live-preview call paths) had never actually been read. For this push,
cloned the real repo directly (`github.com/mesoSPIM/mesoSPIM-control`,
commit `98d74d35bdc6dbc9523cf9ddbed59f262b629a66`) instead of
continuing to fetch piecemeal, so the WHOLE call surface could be
checked at once before the user spends real-hardware time on it.

**Found a serious bug: `close_tasks()` was nulling all of this
backend's persistent device handles on EVERY call, but `close_tasks()`
is called far more often than assumed.** Confirmed directly from the
real `mesoSPIM_Core.py`:
- `snap_image()` (used by BOTH `snap()` and `live()`'s per-frame loop)
  calls ALL SIX overridden methods -- `create_tasks()` through
  `close_tasks()` -- EVERY SINGLE CALL. For `live()`, that's every
  single preview frame.
- `close_acquisition()` (-> `close_image_series()` -> `close_tasks()`)
  is called ONCE PER ROW inside `run_acquisition_list()`'s `for acq in
  acq_list:` loop -- NOT once at the end of a whole multi-row
  acquisition list, as an earlier module docstring incorrectly
  claimed (written before `snap()`/`live()` had been read, generalized
  too far from just the per-frame-series path).

This backend's `create_tasks()` was already written to be a no-op
after the first call (`if self._tiger is not None: return`) --
correct and deliberate, since the Tiger connection/PLC config/
SingleAxisWaveform objects are meant to be set up ONCE, unlike NI's
`create_tasks()`/`close_tasks()`, which legitimately rebuild finite
`nidaqmx.Task()` buffers every call by design (confirmed from the real
`mesoSPIM_WaveFormGenerator.py` base class too -- NI's per-call
rebuild is genuinely how that hardware model works, not a pattern this
project was supposed to be copying literally). But `close_tasks()`
nulled `self._tiger`/`self._plc`/`self._dac`/all four axis handles
unconditionally at the end of every call -- which, combined with the
real call frequency above, meant: every live-preview frame, and every
row of a multi-row acquisition, would have triggered a FULL re-setup
(PLC `clear_state()`, DAC channel re-add, ETL/galvo re-instantiate +
`stop_and_zero()`, `enable_backplane_trigger_mode()` re-sent, camera-
trigger cell + Expose-Out wiring reconfigured from scratch) immediately
followed by tearing it all back down again. For `live()` specifically,
the free-running galvo would never have actually run continuously --
it would have been started, then immediately `stop_and_zero()`'d,
every single frame, instead of scanning smoothly for the whole
preview. Caught by reading the real source before ever reaching real
hardware -- no real-hardware evidence of the broken behavior exists to
report, only the source-level confirmation of why it would have
happened, which is the entire point of doing this check now rather
than after the user burns a live-preview session on it.

**Fix**: `close_tasks()` now does only the "return to safe idle" part
(stop/zero both ETL axes and both galvo axes, `safe_all_outputs()` on
the PLC, zero the DAC) and no longer nulls/disconnects anything.
`create_tasks()`'s existing no-op guard is what now actually delivers
"set up once, for the life of this backend instance" regardless of
which call path (per-frame series, single snap, or live preview) is
driving it. Checked whether there's a genuine "application really is
closing" hook this could instead key off of -- there isn't one exposed
to the waveformer anywhere in the real `mesoSPIM_Core.py` -- so an
owned serial connection is now left open for the life of the process,
same as mesoSPIM's own `StageControlASI` already does with its
connection (no explicit final close call either) -- an existing
convention in this codebase, not a new risk. The OS reclaims the port
on process exit regardless.

Both the module-level ARCHITECTURE NOTE and `create_tasks()`'s/
`close_tasks()`'s own docstrings rewritten to state the corrected,
now-source-verified call pattern plainly, instead of the
per-frame-series-only picture from before.

**Mock-verified the fix directly** (synthetic stand-in base class +
`FakeSerial`, same pattern as this project's other mock tests):
simulated `snap_image()`'s exact real call sequence (create -> write ->
start -> run -> stop -> close) repeated multiple times in a row, as
`live()`'s loop does per frame. With the fix: `self._tiger`/`self._plc`
identity is the SAME object across every cycle, and a marker command
that only `create_tasks()`'s one-time PLC cell setup ever sends
(`CCB X=0 Y=0 Z=0`, from `configure_cell()`) appears exactly ONCE
across 4 full cycles. For contrast, patched a scratch copy back to the
OLD null-everything behavior and re-ran the same sequence: the same
marker command appeared once PER cycle (3 of 3) -- confirming both
that the bug was real and that the fix eliminates it.

**Fixed a second, smaller gap found from the same real-source read:
`config_asi_tiger_example.py`'s commented-out `asi_parameters` block
was incomplete.** It had `COMport`/`baudrate`/`ttl_motion_enabled`
right, but was missing `stage_assignment` and `encoder_conversion` --
both read unconditionally by the real `StageControlASI.__init__()`
(confirmed directly from `devices/stages/asi/asicontrol.py`) -- and
`ttl_cards`, read unconditionally by `mesoSPIM_ASI_Stages.__init__()`
(confirmed from the real `mesoSPIM_Stages.py`). A config built from the
old example block would have failed immediately on startup with a
`KeyError`, before ever reaching this backend's own code. Fixed with
the real required keys, `stage_type` confirmed as the exact string
`'TigerASI'` (not `'ASI'`), and `ttl_cards: None` (confirmed safe --
only read inside `enable_ttl_mode()`, which is itself gated by
`ttl_motion_enabled_during_acq`, which this design always sets False).

**Also confirmed, directly from the real source, two things that were
previously asserted from inference rather than verification:**
- The COM-port connection-sharing mechanism this backend's
  `_get_shared_tiger_connection()` relies on
  (`self.parent.serial_worker.stage.asi_stages.asi_connection`) is the
  real attribute path -- confirmed against `mesoSPIM_Core.py` (`self.
  serial_worker = mesoSPIM_Serial(self)`) and `mesoSPIM_Stages.py`
  (`mesoSPIM_ASI_Stages.__init__`: `self.asi_stages =
  StageControlASI(...)`), not assumed.
- No threading race is possible on that shared serial connection during
  an acquisition: `mesoSPIM_Core.py` keeps `serial_worker` on the Core
  thread (the comment there says so explicitly -- never moved to a
  QThread) and calls `sig_polling_stage_position_stop.emit()` before
  `prepare_acquisition()`, resumed only after the whole acquisition
  list finishes -- so no background position-poll traffic competes for
  the shared connection during a run. The only other traffic on it
  (per-frame `move_relative()` Z/F stepping, since `ttl_motion_enabled`
  is False for this design) is issued by the SAME thread, sequentially,
  interleaved with this backend's own commands by `run_acquisition()`'s
  loop itself -- never concurrently.

Delivered this session: the two adapter fixes (`close_tasks()`
call-frequency fix), the fixed `config_asi_tiger_example.py`
(`asi_parameters` block completed, connection-sharing/threading-safety
comment rewritten with the real confirmation), and the
`mesoSPIM_Core.py` patch materials
(`mesoSPIM_Core_patch_reference/mesoSPIM_Core.py.diff` +
`mesoSPIM_Core.py.patched`, both checked against the real commit
above).

Not yet run on real hardware -- the `close_tasks()` fix specifically
needs confirming with an actual `live()` preview session (the exact
path it was written for) once the user has applied the
`mesoSPIM_Core.py` patch and built a real config file.

## Real lasers arrived (Oxxius L4Cc, 638/561/488/405): found the laser DAC range was never actually set on real hardware

User received real Oxxius L4Cc lasers and ran
`asi_tiger_laser_intensity_test.py`: commanding up to ~4V (near
range_code=1's 4.096V ceiling) only reached ~70mW of these lasers'
rated 100mW.

**Confirmed directly from Oxxius's own real L4Cc/L6Cc user manual**
(fetched, not assumed --
https://www.oxxius.com/wp-content/uploads/2023/05/UserManual_LnCc_v177aa2.pdf):
"The analog modulation functions allow the user to deliver an output
power proportionally to the input voltage: 0V for a nil power, 5V for
the maximal power" -- linear 0-5V = 0-100%. 4V is 80% of that range;
~70% measured is in the right ballpark (some calibration slop) but
genuinely short of 100%, because the DAC has never been able to reach
5V at all under range_code=1's 4.096V ceiling.

**Root cause, found by re-reading `asi_tiger/dac.py`'s own docstrings
closely -- a pre-existing gap in this project, not a new regression:**
`ASITigerDAC.add_channel(range_code=...)`, used everywhere in this
project so far (including `asi_tiger_laser_intensity_test.py` and
`config_asi_tiger_example.py`'s `laser_dac_channels`), only sets a
CLIENT-SIDE label used for `set_voltage()`'s own safety-limit math --
it does **not** touch the real hardware range at all. The actual
electrical output range is a CARD-WIDE hardware setting changed only
by the `PR` command (`ASITigerDAC.set_range()`), and per ASI's own
`command:pr` docs (quoted directly in `set_range()`'s own docstring,
already written months ago but never acted on): "Controller reset or
restart is needed for setting to take effect." `set_range()`/`PR` had
never actually been called anywhere in this project before now --
`query_range()`'s own docstring already said as much ("every
channel's range_code ... is a software-side label only, never
confirmed against the real hardware"). The ~4.096V ceiling the user
hit is consistent with the card's real, never-touched hardware range
simply happening to already be at or near code 1.

**Fix -- a one-time hardware commissioning procedure, not a code
change to the adapter itself** (the adapter correctly never resets the
whole Tiger controller mid-session on its own; that's disruptive and
unsafe to do automatically, and the range is meant to be set once,
ahead of time):
- New `tools/asi_tiger_laser_dac_range_setup.py`: queries the REAL,
  currently-active range via `PR <axis>?` for every laser-card axis
  (read-only, safe); with `--set-range`, sends `PR <axis>=2` (0-10.24V,
  comfortable headroom above the needed 5V) for every axis and then
  explicitly STOPS, telling the user to power-cycle/reset the Tiger
  controller (per ASI's docs, PR needs that to take effect, and
  nothing in this project has a confirmed software reset command); on
  a **separate** run with `--verify` (deliberately refuses to combine
  `--set-range` and `--verify` in one run), re-queries `PR <axis>?` to
  confirm the new range is genuinely active before doing a real
  voltage sweep up to 5.5V, so the user can confirm against a real
  power meter that 100% is reached at/near 5V, not before or needing
  more.
- `config_asi_tiger_example.py`: `laser_dac_channels`'s `range_code`
  changed 1 -> 2, with a note that this is a label describing an
  assumed-already-done hardware state, not something the config itself
  changes -- pointing at the new setup script. Also added guidance
  (previously entirely missing from this example) for
  `cfg.startup['max_laser_voltage'] = 5` -- confirmed from the real
  `mesoSPIM_WaveFormGenerator.py`/`mesoSPIM_Core.py` that this key
  lives in a DIFFERENT top-level config dict (`cfg.startup`, not
  `asi_dac_parameters`) and is read by the base class before this
  backend's own `config_check()` even runs; 5V matches the Oxxius
  spec exactly (real `demo_config.py` uses the same pattern for other
  laser brands, e.g. "5V for Toptica MLEs").
- `asi_tiger_laser_intensity_test.py`: defaults changed to match
  (`--range-code` 1 -> 2, `--max-volts` 4.0 -> 5.5), docstring/help
  updated with the real finding and a pointer to the new setup script
  as a prerequisite.

**Mock-verified** with a new stateful fake serial that tracks `PR`
state per (card, axis) across simulated "power cycles" (a module-level
dict, since the real controller's EEPROM persistence can't be
faked within one process the same way a stateless fake would): the
full query -> set-range -> (simulated power cycle) -> verify -> sweep
sequence runs correctly end to end, including the 5.5V sweep
succeeding under the new range_code=2 safety limit; separately
confirmed both refusal paths work (`--verify` before any real range
change correctly refuses to sweep; `--set-range` and `--verify`
together in one run are refused outright, since the range genuinely
isn't active until a real power cycle happens in between).

**Not yet run on real hardware.** This needs the user to actually run
`asi_tiger_laser_dac_range_setup.py --set-range`, physically power-
cycle the Tiger controller, then run it again with `--verify` and
watch a real power meter to confirm 100% is reached at 5V -- only then
should `config_asi_tiger_example.py`'s `range_code: 2` and
`max_laser_voltage: 5` guidance be trusted as matching real hardware.

## Real-hardware report: `--set-range` alone did not survive a power cycle -- `PR` needs an explicit `SS Z` save too

User tried `asi_tiger_laser_dac_range_setup.py --set-range` on real
hardware (card 35): it did not work as documented. The user set the
range manually and separately sent `SS Z` (card-addressed -- `35SS Z`)
to save it, and confirmed: **the range now stays at mode 2 after a
power cycle.**

This refines (not contradicts) what `set_range()`'s docstring already
said from ASI's own `command:pr` docs ("controller reset or restart is
needed for setting to take effect") -- that described activating PR's
new range, but said nothing about SURVIVING A REAL POWER CYCLE
specifically, and this project had never actually tested that
distinction against real hardware until now. The user's finding: `PR`
alone was not enough for the new range to survive a real power-down;
an explicit `SS Z` (save-to-flash, card-addressed) was required.

Tried to independently confirm the exact general semantics of `SS Z`
against ASI's own documentation before writing this up (fetching
`docs.asiimaging.com`'s SS/PR command pages, the TG-1000 manual PDF,
and a plain web search) -- every attempt failed (provenance-approval
timeouts, a 429 rate limit), not a lookup that came back empty. So
this fix is written up as **real-hardware-confirmed by the user
only**, not doc-confirmed -- flagged explicitly in the code so a
future reader doesn't mistake it for an ASI-documented guarantee.

**Fix:**
- New `ASITigerDAC.save_settings(card_addr)` in `asi_tiger/dac.py`:
  sends `SS Z` (card-addressed), with a docstring stating plainly that
  this is confirmed on real hardware only, that `Z` is the one value
  this project has actually tested (not assumed to be the only valid
  one or necessarily "save everything" in general), and that it must
  be called once per `card_addr` AFTER `set_range()`, separately (not
  folded silently into `set_range()` itself, since committing
  persistent hardware state silently as a side effect of a
  differently-named method is exactly the kind of surprise this
  project tries to avoid).
- `set_range()`'s own docstring updated with a pointer to this real
  finding and to `save_settings()`.
- `asi_tiger_laser_dac_range_setup.py`'s `--set-range` step now calls
  `dac.save_settings(args.card_addr)` right after sending `PR` for
  every axis, and both its module docstring and its runtime messages
  (`Sent SS Z (card N) to commit the new range to flash...`) say so
  plainly, instead of the earlier (incomplete) "PR + power-cycle is
  enough" framing.

**Mock-verified**: `save_settings(35)` sends the exact wire command
`'35SS Z'` -- checked byte-for-byte against the user's own command
notation, not just "a command containing SS". Re-ran the full
`--set-range` mock scenario from the previous entry; it now sends `PR`
for every axis followed by exactly one `SS Z` for the card, with
updated messaging, and still correctly refuses to combine
`--set-range`/`--verify` in one run or to sweep in `--verify` before a
real range change is confirmed.

Not yet re-run on real hardware with this exact script version --
the user's confirmation so far is of their own manual `PR` + `35SS Z`
sequence, not of this updated script. Worth a real run to confirm the
script's automated version behaves identically to what the user did
by hand.

## New: ASITiger_LaserEnabler -- `cfg.laser = 'ASI_Tiger'` was a real gap

Resuming the mesoSPIM-control integration push, a question about the
real `config.py` ("should the laser parameter be ASI?") surfaced a
genuine gap: `cfg.laser` is a config field SEPARATE from
`cfg.waveformgeneration` -- confirmed directly from `mesoSPIM_Core.py`
-- and it picks `self.laserenabler` independently of `self.waveformer`.
Before this fix, only `'NI'`/`'cDAQ'` (-> `mesoSPIM_LaserEnabler`, real
NI-DAQmx DO lines) or a string containing `'demo'` (->
`Demo_LaserEnabler`, no-op) were recognized. Setting `cfg.laser =
'ASI_Tiger'` without this fix would leave `self.laserenabler` never
assigned -- a guaranteed `AttributeError` on the very first
`self.laserenabler.enable(...)` call, and that call happens in
`snap_image()`, `snap_image_in_series()`, `live()`, and
`run_acquisition()` -- confirmed directly from `mesoSPIM_Core.py`, not
assumed, every single one of those call sites was checked.

The device-layer mechanism this needed already existed and was
already real-hardware-confirmed, just not wired into Core: this
project's own `tools/asi_tiger_laser_enable_test.py` had already
exercised `row_setup.configure_laser_enable_lines()` -- one
independent, directly-toggleable TTL enable line per laser on PLC
BNCs 5-8 (cells 12-15) -- against real hardware (see the "New: laser
command bench tests" entry above: "every row-level setup function...
has now been checked on real hardware"). What was missing was purely
the Core-integration layer connecting that to `cfg.laser`.

**Fix:**
- New `mesoSPIM/src/devices/lasers/ASITiger_LaserEnabler.py` --
  matches `mesoSPIM_LaserEnabler`'s public interface exactly
  (`enable(laser)`, `disable_all()`, `state()`), so it's a true
  drop-in; Core.py's existing call sites need no changes. Wraps
  `row_setup.configure_laser_enable_lines()`, built lazily on first
  real use rather than at `__init__` (see next point).
- **A real ordering hazard, found and fixed before it could bite**:
  `self.laserenabler` is constructed in `Core.__init__`, before any
  image is ever taken -- at that point
  `mesoSPIM_ASITigerWaveFormGenerator.create_tasks()` hasn't run yet,
  so its `_plc` (PLCCard instance) this class needs is still `None`.
  Checked all four `enable()` call sites directly: three are always
  preceded by a `create_tasks()` call earlier in the same chain
  (`snap_image()` calls it directly; `prepare_acquisition()` ->
  `prepare_image_series()` runs it before `run_acquisition()`), but
  `live()` is the exception -- it calls `self.laserenabler.enable(laser)`
  *before* entering the loop that calls `snap_image()` (which is what
  would normally trigger `create_tasks()`). So a bare first-ever
  `live()` click, before any prior `snap()` in that session, would hit
  `_plc is None`. Fixed in `ASITiger_LaserEnabler._ensure_lines()`: if
  `_plc` isn't ready yet, it calls `self.parent.waveformer.create_tasks()`
  itself first -- safe because `create_tasks()` is explicitly
  documented as idempotent/safe to call repeatedly (its own docstring,
  from the earlier call-frequency fix), so forcing it a few lines
  earlier than it would have run anyway changes nothing else.
- `mesoSPIM_Core_patch_reference/`'s `.orig`/`.patched`/`.diff`
  regenerated: now carries TWO additions (the pre-existing
  `waveformgeneration` elif branch, plus this new `laser` elif branch
  and its import), re-verified with `git apply --check` against the
  same real commit (`98d74d35bdc6dbc9523cf9ddbed59f262b629a66`) --
  still applies cleanly.
- `config_asi_tiger_example.py`: added `laser = 'ASI_Tiger'` right next
  to `waveformgeneration = 'ASI_Tiger'` with a comment explaining why
  both are needed, plus optional `plc_laser_bncs`/
  `plc_laser_toggle_cells` keys (defaulting to `(5,6,7,8)`/
  `(12,13,14,15)`, this rack's existing convention) documented next to
  `laser_dac_channels`.

**Mock-verified** (synthetic package + `FakeSerial`, mirroring the
existing `mesoSPIM_ASITigerWaveFormGenerator` test harness): confirmed
(1) `enable()` called *before* any `create_tasks()` call (replicating
`live()`'s exact ordering) does not crash and correctly forces setup
first; (2) the one-time PLC cell/BNC configuration (`CCB` IO-config
commands) runs exactly once across repeated `enable()`/`disable_all()`
cycles, not once per call; (3) the laser-index mapping matches
`sorted(laserdict.keys())`, the same convention used elsewhere in
mesoSPIM; (4) an unknown laser name raises `ValueError`, matching
`mesoSPIM_LaserEnabler`'s own exact behavior; (5) the ordinary case
(`create_tasks()` already run first) still works unchanged. Inspected
the raw sent-command sequences directly rather than trusting pass/fail
alone -- confirmed the right toggle cell (matching each laser's sorted
index) is the only one left high after each `enable()` call.

**Not yet real-hardware-confirmed** as an integrated whole (the
underlying `configure_laser_enable_lines()` mechanism is
real-hardware-confirmed on its own, per the bench test above, but this
new adapter class gluing it to `cfg.laser` has only been mock-tested).
Confirm on real hardware -- scope on PLC BNC5-8, or real laser drivers
if wired -- before trusting it for an actual acquisition.

## Real-hardware report: first live() attempt crashed -- `self.state.get()` doesn't exist

**User report, first real attempt at `live()` after applying both
Core.py elif branches**: `AttributeError: 'mesoSPIM_StateSingleton'
object has no attribute 'get'`, raised inside
`write_waveforms_to_tasks()` at `self.state.get("shutterconfig",
"Left")`.

Root cause, confirmed directly from the real `mesoSPIM_State.py`:
`mesoSPIM_StateSingleton` is NOT a dict and does not subclass one --
it only implements `__getitem__`/`__setitem__`/`__len__` (mutex-locked
custom methods), deliberately, for thread-safe access. No `.get()`
exists anywhere on it. This file had **11 separate
`self.state.get(key, default)` calls** -- every single one of them
would have crashed the same way, the first time each was reached
(`shutterconfig`, `laser`, `intensity`, `camera_exposure_time` x2, and
four each of `etl_*`/`galvo_*` amplitude/offset/frequency/duty_cycle).

**Why this wasn't caught sooner, despite extensive mock testing**: every
mock test in this project's harness used a `FakeState(dict)` --
subclassing `dict` for convenience, since it behaves like the real
state almost everywhere (`state['key']`, `state['key'] = value`). But
`dict` ALSO provides `.get()`, which the real `mesoSPIM_StateSingleton`
does not -- so every mock run silently exercised a DIFFERENT, more
permissive interface than real hardware actually has. This is exactly
the kind of gap the project's own "verify against real hardware, don't
assume from mocks alone" rule exists to catch, and it did -- just on
first real use rather than in testing.

**Fix:**
- New `_state_get(state, key, default=None)` helper added to
  `mesoSPIM_ASITigerWaveFormGenerator.py`: tries `state[key]`, catches
  `KeyError`, returns `default` -- works identically against the real
  singleton and a plain dict, without relying on `.get()` existing on
  either. All 11 call sites now route through it.
- In practice every key this file reads is always pre-populated by
  `mesoSPIM_StateSingleton.__init__()`'s own hardcoded defaults (confirmed
  directly: `shutterconfig`, `laser`, `intensity`, `camera_exposure_time`,
  all four `etl_*`/`galvo_*` groups are all in its literal `_state_dict`),
  so the `default` argument is realistically never hit on real hardware
  -- it's a defensive fallback, not load-bearing.
- **Mock harness itself fixed too**, not just the production code: added
  `RealStateStub` (no dict inheritance, only `__getitem__`/`__setitem__`/
  `__len__`/`__contains__`, matching the real class's actual interface)
  and re-ran every existing scenario (`snap_image()`-style cycles,
  `live()`'s enable-before-create_tasks ordering hazard) against it
  instead of the old dict-based fake. All pass with the fix; re-running
  them against the OLD code (before `_state_get()`) reproduces the
  exact real-hardware crash, confirming the mock gap is now closed, not
  just papered over.

**Lesson applied going forward**: the dict-based `FakeState` is not
representative of `mesoSPIM_StateSingleton`'s real interface and
should not be used for new mock tests of this file -- `RealStateStub`
(or an equivalent subscript-only stand-in) is the correct one from now
on.

## Real-hardware report: intermittent garbled serial reply during live() -- shared connection had no cross-driver lock

**User report, next real `live()` attempt after the state-interface
fix**: `ValueError: could not convert string to float:
'\x00\x00\x00\x000\x00\x00'`, raised inside
`PLCCard.read_bnc_inputs()` parsing the reply to `RDADC X?`. Mostly
null bytes with one surviving `'0'` character -- not a malformed
single reply, but the signature of two unsynchronized readers/writers
colliding on one half-duplex serial line.

**Root cause, confirmed directly from the real source on both sides**:
an ASI Tiger rack is one physical device behind ONE serial port,
hosting many logical cards (stage, DAC, PLC). `_get_shared_tiger_connection()`
(added earlier in this project specifically to avoid opening a second,
conflicting `serial.Serial` on that same port) reuses mesoSPIM's own
ASI stage driver's connection object directly via
`TigerController.from_open_serial()`. That part was always correct.
What was missing: `StageControlASI._send_command()` (asicontrol.py,
the REAL mesoSPIM-control file, not this project's) had **no lock at
all** around its own `write()`/`readline()` calls, and
`TigerController`'s own `self._lock` (created fresh in
`from_open_serial()`) only serialized OUR commands against each
other -- never against the stage driver's. mesoSPIM runs stage
position polling on a GUI-thread timer continuously (confirmed from
mesoSPIM_Core.py's own comment: "Position polling runs in MainWindow
GUI thread, not in Core thread!"); `prepare_acquisition()` explicitly
stops that polling before a multi-row acquisition for exactly this
reason, but `live()` never does. So during `live()`, the position
poller and our `RDADC X?` reads race on the same wire, occasionally
interleaving -- intermittent, not every call, matching what was
reported.

Asked the user first whether their Tiger exposes one COM port or two
(two would have allowed a much simpler fix -- just not sharing the
connection at all); confirmed one port, so the real fix is a shared
lock across both drivers, not a workaround.

**Fix:**
- `TigerController.from_open_serial()` (`asi_tiger/controller.py`) now
  takes an optional `lock=` parameter; if given, it REPLACES the
  instance's own private `self._lock` with that exact object, so this
  controller's commands and whoever else holds that same lock are
  truly mutually exclusive, not just self-consistent.
- New `self.serial_lock = threading.Lock()` on `StageControlASI`
  (`asicontrol.py`) -- a real mesoSPIM-control file, patched the same
  way `mesoSPIM_Core.py` is (see `mesoSPIM_Core_patch_reference/
  asicontrol.py.diff`, verified with `git apply --check` against the
  same real commit) -- and `_send_command()`'s existing
  write/readline/`_reset_buffers()` sequence now runs inside `with
  self.serial_lock:`. Exposed as a public attribute specifically so
  another driver sharing the connection can use the SAME lock object,
  not just add its own separate one.
- `_get_shared_tiger_connection()` now looks for `asi_stages.serial_lock`
  and passes it through to `from_open_serial(ser, lock=...)`. If it's
  missing (an unpatched/older `StageControlASI`), this does NOT crash
  -- it falls back to the old private-lock behavior, but now logs a
  loud `logger.error()` explaining exactly why that fallback is unsafe,
  instead of silently reproducing the same race.

**Mock-verified** (can't reproduce a real OS-level serial race in a
mock, so this verifies the WIRING is correct, not the race itself):
confirmed (1) when the stage driver exposes `serial_lock`, the
resulting `TigerController._lock` IS that exact same object (identity
check, not just "a lock") -- the actual thing that makes the two
drivers mutually exclusive; (2) when `serial_lock` is absent, `create_tasks()`
still completes without raising, falling back to a private lock, with
the expected error logged; (3) the different-port case (no sharing at
all) is unchanged. Re-ran every existing regression scenario
(`snap_image()`-style cycles, the `live()` ordering-hazard case, the
laser-enabler tests, all against the non-dict `RealStateStub`) against
the updated `controller.py` -- all still pass.

**Not yet re-tested on real hardware** -- this fix requires applying
BOTH the updated files AND the new `asicontrol.py` patch to your real
checkout. The intermittent nature of the original bug means a single
clean `live()` run afterward is encouraging but not conclusive; a
longer live-preview session (watching for the same `ValueError`
recurring) is the real confirmation.

**Follow-up real-hardware report**: the `asicontrol.py` patch hadn't
actually reached the user's checkout yet (only the `.diff` had been
sent, which needs `git apply` against a checkout that may not even be
a git repo) -- confirmed by asking the user to grep their real file
for `serial_lock` and finding it absent. Sent the complete patched
file directly as a drop-in replacement instead of a diff, to remove
the git step entirely for this one.

## Real-hardware report: live() technically correct now, but unusably slow -- the lock fixed corruption, not contention

**User report, after the `serial_lock` fix actually landed**: no more
corrupted replies or permanent deadlock (confirmed via
`faulthandler.dump_traceback_later()` dumps spaced 15s apart -- Python's
`Ctrl+C`/SIGINT only interrupts the main/GUI thread, which turned out
to be running fine the whole time, so it was the wrong tool; switched
to `dump_traceback_later()`, which dumps every thread and doesn't rely
on a signal Windows doesn't support for `faulthandler.register()`
anyway). The dumps showed the Core thread actually progressing --
different call sites across successive dumps (laser-enable PLC setup,
`disable_all()`, the per-frame `RDADC` poll, finally winding down
inside `live()` itself) -- genuinely moving, just extremely slowly:
each individual serial round-trip was taking 15-30+ seconds instead of
milliseconds.

**Root cause**: the `serial_lock` fix made the two drivers safely take
turns on the shared port, but did nothing to stop them from
constantly CONTENDING for it. mesoSPIM's own stage position-polling
timer keeps firing throughout `live()`/`snap()` (confirmed: `asicontrol.py`
opens its connection with a 5-second native timeout). Every time our
per-frame Tiger traffic and the GUI's position poll happened to land
at the same time, whichever side lost the lock had to wait for the
other to finish -- and if THAT side's command didn't get a timely
reply (plausible if the Tiger controller's single serial front-end was
still mid-reply to the other driver's command), it could eat its own
full multi-second timeout before releasing the lock. These delays
stack: not broken, just serialized at a granularity far too coarse for
real-time preview.

mesoSPIM already has the right tool for this, just never applied to
`live()`/`snap()`: `prepare_acquisition()` already emits
`sig_polling_stage_position_stop` before a multi-row acquisition
(confirmed directly from `mesoSPIM_Core.py`), specifically to avoid
exactly this class of contention -- `live()` and `snap()` just never
got the same treatment, presumably because the NI backend's `self.waveformer`
never shares a serial port with the stage at all, so the contention
this prevents never existed for that backend.

**Fix** (third addition to the same `mesoSPIM_Core.py`, folded into
the existing `mesoSPIM_Core_patch_reference/mesoSPIM_Core.py.{orig,patched,diff}`
rather than a new file -- regenerated and re-verified with `git apply
--check` against the same real commit, still applies cleanly):
- `snap()`: emits `sig_polling_stage_position_stop` right after
  `sig_prepare_live`, and `sig_polling_stage_position_start` right
  after `close_shutters()` -- `snap()` previously had no
  polling-pause of its own at all, since nothing else manages it for a
  single one-shot snap.
- `live()`: emits `sig_polling_stage_position_stop` once before the
  per-frame loop starts, and `sig_polling_stage_position_start` once
  after the loop exits (alongside `laserenabler.disable_all()`/
  `close_shutters()`). `stop()` (the GUI's Stop button handler) ALSO
  already emits the resume signal on its own -- both emits are
  harmless/idempotent (restarting an already-running `QTimer` is a
  no-op), so this is a defensive belt-and-suspenders resume that
  covers any exit path, not just a user-initiated stop.
- This is a **generic** mesoSPIM-control improvement, not conditioned
  on `cfg.waveformgeneration` -- harmless for the NI backend (which
  never contends for the stage's serial port in the first place) and
  specifically fixes the real contention for the ASI Tiger backend's
  shared-connection design.

**Not yet re-tested on real hardware** -- needs the updated
`mesoSPIM_Core.py` patch applied (same two files as before, `.diff` or
`.patched`, now with this third change folded in).

## Real-hardware report: live() now genuinely HUNG, not just slow -- a bounded fix for two pyserial behaviors that can block forever

A third `hang_dump.txt` (`faulthandler.dump_traceback_later`, same
technique as before) showed something different from the "slow but
progressing" pattern the polling-pause fix (above) was built for: the
Core thread got stuck on the EXACT SAME LINE across four consecutive
15-second dumps (60+ seconds, no progress at all) -- first in
`serialwin32.flush()`, then in `serialwin32.read()` -- both reached
from `plc.py`'s `read_bnc_inputs()`, called from this backend's own
`run_tasks()`.

Two real, confirmed-from-source pyserial behaviors, both now fixed in
`asi_tiger/controller.py`:

1. **`flush()` has NO timeout, ever, by design.** Read directly from
   the installed pyserial package (`serialwin32.py`):
   ```python
   def flush(self):
       while self.out_waiting:
           time.sleep(0.05)
   ```
   It does not honor `write_timeout` (that only bounds `write()`
   itself) and there is no attribute that changes this. If
   `out_waiting` ever gets stuck non-zero -- the OS-level output
   buffer stops draining, for any reason -- real pyserial `flush()`
   hangs forever, full stop, no exception to catch anywhere.
   `TigerController.send_command()` called `self._ser.flush()`
   directly after every write. Fixed: replaced with a new
   `_flush_bounded()` that does the same wait but gives up after
   `self._timeout` and raises `TimeoutError` instead of looping
   forever.

2. **`from_open_serial()` was wrapping the shared port without ever
   touching its timeout settings**, so our side's `_timeout` (default
   0.5s) was purely cosmetic -- the ACTUAL pyserial timeout/write_timeout
   in effect were still whatever `StageControlASI` set when it first
   opened the port. Confirmed directly from `asicontrol.py`:
   ```python
   self.asi_connection = serial.Serial(self.port, self.baudrate,
       parity=serial.PARITY_NONE, timeout=5, xonxoff=False,
       stopbits=serial.STOPBITS_ONE)
   ```
   `timeout=5` (not our 0.5s), and no `write_timeout` at all -- pyserial
   defaults that to `None`, i.e. an unbounded, can-block-forever
   `write()`. Fixed: `from_open_serial()` now explicitly sets
   `obj._ser.timeout = obj._timeout` and
   `obj._ser.write_timeout = obj._timeout` on the shared Serial
   instance right after wrapping it. This DOES also shorten
   `StageControlASI`'s own read timeout from 5s to 0.5s (same
   instance, shared setting) -- a deliberate trade: a stage reply that
   doesn't arrive within 0.5s on this hardware almost certainly isn't
   coming at all, and a bounded, catchable `TimeoutError` beats a
   connection wedged solid with no exception.

**What was actually driving the port into this state in the first
place:** this backend's `run_tasks()` polls `read_bnc_inputs()` (a
full write+flush+read round-trip) every **1 millisecond** while
waiting for the camera's Expose-Out BNC to flip, both at trigger-start
and at exposure-end -- far more command traffic per frame than the NI
backend this class mirrors ever generated, and, confirmed as the
likely trigger for the stall, enough sustained flood to occasionally
back up the shared port's output buffer. Fixed alongside the above:
the poll interval is now 5ms by default (`camera_expose_poll_interval_s`
in `asi_dac_parameters`, overridable), cutting round-trip volume 5x
while still resolving well inside any real exposure time. A single
timed-out/garbled poll (now a catchable `TimeoutError`/
`SerialTimeoutException` instead of a hang) is logged and retried --
only the phase's own overall timeout (`camera_expose_start_timeout_s`
/ `..._end_timeout_margin_s`) decides when to actually give up, same
as before.

Net effect: a port that genuinely stalls now raises a normal,
catchable exception within about half a second instead of hanging the
whole Core thread (and the GUI with it) indefinitely with Stop
unresponsive. This does not change `close_tasks()`'s own behavior on
such an exception -- if `close_tasks()` needs hardening too (e.g. not
aborting the rest of teardown if one step times out), that's the
logical next thing to check if it comes up on real hardware.

**No code changes to mesoSPIM-control proper this round** -- both
fixes are entirely inside this project's own files
(`asi_tiger/controller.py`, `mesoSPIM_ASITigerWaveFormGenerator.py`),
nothing new to re-apply to `mesoSPIM_Core.py`/`asicontrol.py`.

**Not yet confirmed on real hardware.**

## Real-hardware report: "same, frozen" -- dump shows live() is PROGRESSING, just extremely slowly; added per-frame timing to find where the time goes

The fourth `hang_dump.txt` (run with the bounded-flush/5 ms-poll build:
line numbers match) is NOT a hang. Across ~15 dumps the Core thread is
at a different place almost every time -- `_poll_expose_bit`, the
`sleep` in the Expose-Out start-wait loop, `LaserEnabler.enable`,
`disable_all`, `close_tasks -> stop_and_zero / safe_all_outputs /
zero_all` -- always inside `live() -> snap_image()`. So the frame loop
is running; each frame is just very slow, which looks frozen. Two
suspects, neither confirmed yet:
1. `run_tasks()` spends its full 2 s start-timeout every frame if the
   camera's Expose-Out never reaches the PLC BNC (the dump catches it
   in that wait loop about a third of the time).
2. Every frame re-runs the full enable / disable_all / stop_and_zero /
   safe_all_outputs / zero_all sequence -- on the order of 50-100
   serial commands per frame, each a full round trip.

Rather than guess, this build logs ONE WARNING line per frame:
`ASI Tiger frame timing: <s since last frame> | <N> serial cmds, <s> on
wire (avg/slowest + which command) | run_tasks <s>, Expose-Out high
after: <ms | NEVER went high>`. Backed by `TigerController.take_stats()`
(per-command wire time, excluding lock wait). Silence with
`asi_dac_parameters['frame_timing_log'] = False`.

**Why nothing appeared on the console:** mesoSPIM-control's
`get_logger()` calls `logging.basicConfig(filename=...)`, so ALL
`logger.*` output (these timing lines, and every `logger.error`/
`logger.warning` in this backend) goes to `mesoSPIM/log/<timestamp>.log`,
not the console. The timing line is now ALSO `print()`ed to the console.

No behavior change otherwise. Mock-tested (all prior suites + the new
line prints). **Needs real-hardware output of that line.**

## Real-hardware log: frame timing shows ~4.65 s/frame, Expose-Out NEVER seen, and a real wiring bug -- close_tasks() severed the PLC wiring every frame

The frame-timing log (mesoSPIM/log/<timestamp>.log) from a ~2-minute live() run:
- every frame ~4.65 s = ~2.25 s waiting in run_tasks() for Expose-Out (it
  "NEVER went high", all 30 frames, including frame 1) + ~225 serial
  commands at ~16 ms each (~100 of them are the Expose-Out polls).
- (Also: `_ensure_lines` took ~2.5 s the first time; the first frame is
  295 commands / 6.4 s.)

**Bug found by reading the command stream (mock, per-phase count): `close_tasks()`
called `safe_all_outputs()` after EVERY frame.** That reconfigures every PLC
output this backend set up -- camera-trigger BNC, the two ETL backplane
trigger-in lines, the four laser-enable BNCs -- to push-pull driven by
constant-low, i.e. it disconnects them. `create_tasks()` only wires them
once and never re-runs, so from frame 2 on the camera-trigger cell was
toggled but no longer reached the BNC, laser-enable cells no longer reached
theirs, and the ETL never saw a trigger. (It also cost ~20-30 commands/frame.)
The earlier docstring ("close_tasks() deliberately never undoes create_tasks()'s
setup") was wrong about exactly this call.

Fix: `close_tasks()` now only idles the camera-trigger cell (plus the existing
ETL/galvo stop+zero and DAC zero); lasers are already switched off per frame by
mesoSPIM's `laserenabler.disable_all()`. A new `safe_outputs()` does the full
teardown (and forces `create_tasks()` to rewire next time) for a future exit
hook. Steady-state mock frame: 67 -> 48 commands, and a regression test asserts
steady-state frames send no `CCA Y=`/`CCA Z=` at all.

**NOT explained by this fix:** Expose-Out never going high on frame 1 (wiring
was intact then). Open candidates: (a) the 20 ms live exposure is shorter than
our ~21 ms poll cadence (16 ms round trip + 5 ms sleep) so the pulse can be
missed; (b) camera not receiving the trigger / Expose-Out not wired to the
BNC. Quick discriminator, no rebuild: set live exposure to ~200 ms and see
whether "never went high" disappears and the image updates.

**Not yet confirmed on real hardware.**

## Real-hardware report: live() WORKS (camera triggers, lasers fire) -- but "Left" selected, light on the right arm

Log from a run after the wiring fix, exposure 200 ms: Expose-Out caught every
frame (~100 ms after trigger -- i.e. the earlier "never went high" was the
severed wiring, not poll cadence), ~2.0 s/frame, ~62 commands/frame, 0.49 fps.

Left/Right: the code path is consistent -- shutterconfig "Left" -> `l` ETL +
galvo axes and `lr_switch_left_v`; "Right" -> `r` axes and `lr_switch_right_v`
(write_waveforms_to_tasks()). The polarity of the L/R switch voltage is
hardware-specific and was never established in this project (docs only say 5 V
is "the level expected"), so the most likely cause is `lr_switch_left_v` /
`lr_switch_right_v` being the wrong way round for the real switch -> swap them in
asi_dac_parameters. NOT yet confirmed: needs the user's Right-selected observation
to tell "inverted" from "switch not changing".

Defect fixed on the way: `close_tasks()` used `dac.zero_all()`, which also drove the
L/R switch channel to 0 V after EVERY frame (Right: 5 V -> 0 V -> 5 V each frame,
parked on the 0 V arm between frames). It now zeroes only the laser-intensity
channels; the switch holds its side. Mock test added (test_lr_switch_hold).

**Not yet confirmed on real hardware.**

## Real-hardware report: "galvo resets every frame, is that necessary?" -- no; live() now keeps ETL/galvo running between frames (NEW CORE HOOK, not yet hardware-confirmed)

The user, after confirming live() works with L/R voltages swapped in
config, noticed the galvo restarts its sweep every frame. Log
(daeb8603): 61 commands/frame, ~2.0 s/frame (0.48 fps) at 200 ms
exposure. Not necessary: close_tasks() stopped and zeroed all four
ETL/galvo axes after EVERY frame, and the next frame re-armed them.
That design was inherited from "stop everything after each close",
which is right for snap()/acquisition rows but pointless in live().

Change (mesoSPIM_ASITigerWaveFormGenerator.py):
- `begin_live()` / `end_live()` hooks. While live mode is on,
  close_tasks() does NOT stop the axes, and write_waveforms_to_tasks()
  skips re-arming when nothing it programs has changed (key = side,
  ETL amplitude/offset/period, galvo amplitude/offset/frequency/duty).
  Any change to those mid-live (e.g. dragging a slider, switching
  Left/Right) reconfigures on the next frame. end_live() stops and
  zeroes the axes.
- Outside live (snap, acquisition rows) behavior is unchanged.
- Core change #4 (mesoSPIM_Core.py live()): calls
  `waveformer.begin_live()` before the loop and `end_live()` in a
  `finally` (guarded by hasattr, so NI/Demo waveformers are
  unaffected). The patch reference .patched/.diff were regenerated;
  `git apply --check` passes on commit 98d74d35.
  YOU MUST RE-APPLY THE UPDATED mesoSPIM_Core.py.diff. Without it the
  backend still works exactly as before (hooks never called).

Mock-verified (test_live_mode.py): steady-state live frame sends 8
commands vs 30 in non-live (zero ETL/galvo-card commands); changing
galvo amplitude, ETL offset or side triggers reconfigure; end_live()
stops/zeroes the 4 axes; non-live frames return to stopping every
frame; all earlier suites still pass. NOT yet confirmed on hardware:
expected effect is fewer commands/frame in the timing log and a
continuous galvo sweep. Remaining per-frame cost is mostly laser
enable/disable cell toggles and DAC zero/set, still resent each frame
(candidate for a later round).

## Real-hardware log after live-mode change: 0.64 fps, 39 cmds/frame -- remaining cost is slow PLC pointer moves; added a PLC cell-state cache (not yet hardware-confirmed)

Log d12c4103 (200 ms exposure): first frame 127 cmds (setup, one-off);
steady frames 39-40 cmds, ~1.47 s on the wire (avg ~37 ms), 1.51 s/frame,
camera-reported 0.64 fps (was 61 cmds, 2.0 s, 0.48 fps). The log cannot
say whether the galvo sweep is now continuous -- that needs the user's
eyes. Frames show "Expose-Out high after" ~90-105 ms every frame (wiring
intact), run_tasks ~0.35 s.

Where the time goes: the "slowest command" is always `36M E=<n>` at
~103 ms -- the PLC pointer move that precedes every `CCA F=` write.
Arithmetic from the log (an ESTIMATE, not a measurement): if k commands
cost ~103 ms and the rest ~15 ms, 39 cmds in 1.47 s gives k ~ 10, i.e.
~1 s of every frame is pointer moves. The mock shows 12 of them per
frame: laser enable (re-clears 4 lines, sets 1), laser disable (clears 4),
camera cell set/clear in run_tasks, and a redundant camera-cell clear in
close_tasks.

Change (devices/asi_tiger/plc.py): PLCCard keeps a cache of the last
state it wrote to each cell and set_cell_state() skips a write that
repeats it. Deliberately NOT cached: the pointer position (a skipped
`M E=` would send a later `CCA F` to the wrong cell, possibly the wrong
laser). The cache is cleared on any error, on configure_cell() of that
cell, on reset_all_cells_and_io / clear_state / safe_all_outputs /
reset_pulse_pass_through_counter, and by begin_live() (so the first frame
of each live session re-establishes real states). Anything that changes
a cell behind this object's back must call invalidate_state_cache().

Mock (live steady state, laser + camera): 26 -> 10 commands/frame,
`M E=` 12 -> 4. test_plc_cache.py replays commands against a model of
the cell states: switching 488 -> 561 -> off leaves exactly the right
lines on, redundant disable sends nothing, a failed write invalidates,
configure_cell / begin_live invalidate. All earlier suites still pass.
Expected on hardware: ~12 fewer pointer-move-bound commands per frame
(~0.8 s) so roughly 0.7 s/frame at 200 ms exposure -- an estimate until
a new log confirms it. Still resent each frame: laser DAC set/zero
(`35M P=...`, 2 cmds) and 4 pointer moves.

## HARDWARE-CONFIRMED: live-mode hooks + PLC cell-state cache (log a2cae9e8)

User report: galvo runs fine (continuous), laser blanking as expected,
laser line and intensity can be changed during live. Log: steady frames
23 serial cmds, ~0.57 s on wire (avg ~25 ms), 0.60-0.64 s/frame,
camera-reported 1.54 fps at 200 ms exposure (was 0.48 -> 0.64 -> 1.54
across the live-mode and cache changes). run_tasks ~0.35 s of each frame
is the exposure plus ~100 ms until Expose-Out goes high, so ~0.25 s/frame
is what is left to trim (remaining pointer moves, laser DAC set/zero,
the poll reads in run_tasks) -- not attempted. No errors in the log.

## Live-mode round 2: hold the laser DAC between frames + PLC pointer tracking (two config switches; mock-tested, NOT yet hardware-confirmed)

Asked by the user: is it necessary to zero the laser DAC every frame, and
what else can be trimmed? Answers: (1) No -- close_tasks() zeroed every
laser DAC channel and write_waveforms_to_tasks() set it again each frame,
an NI-era habit; the PLC enable line already blanks. (2) Of the 4 slow
`M E=` pointer moves per frame, 2 repeat the position the card already
holds. (3) Logging costs ~nothing: the timing stats are in-process
counters (no serial commands), the log line + print ~1 ms/frame; the one
real risk is a Windows console blocking print() after a click inside it.
Set frame_timing_log False when done troubleshooting.

Both changes are LIVE ONLY (snap/acquisition unchanged) and independently
switchable in asi_dac_parameters (both default True):
- `live_hold_laser_dac`: during live the DAC is written only when the
  voltage or laser line changes; a laser line you switch away from is
  zeroed at once; end_live() zeroes all laser channels (L/R switch keeps
  its side). Blanking then relies solely on the enable line -- CHECK for
  stray light between frames on your hardware.
- `live_track_plc_pointer`: PLCCard.track_pointer (default OFF, turned on
  by begin_live and off by end_live) skips an `M <axis>=<addr>` that
  repeats the last position this instance set. Safe in live because the
  stage poller is paused and nothing else addresses the PLC card; every
  skip sits between two of our own commands (the second camera-cell move,
  and the laser enable that follows the previous frame's disable), never
  across an RDADC poll, so it does not depend on RDADC leaving the
  pointer alone (unverified). Dropped on any error, in
  invalidate_state_cache(), and at begin_live/end_live.

Mock (replay against a model of PLC pointer/cells and DAC axes): steady
live frame 10 -> 6 commands, `M E=` 4 -> 2 (with this test config's two
laser channels, flags off = 11). Verified: held level; one DAC write on
intensity change; 488 -> 561 zeroes the old channel, sets the new one and
turns exactly the right enable line on at trigger time; end_live zeroes
all; flags independently switch each behavior; failed PLC/DAC writes
drop the tracking; non-live frames unchanged; all earlier suites pass.
Expected on hardware (ESTIMATE): ~0.2-0.25 s/frame saved, i.e. ~0.35-0.4
s/frame at 200 ms exposure. Log line "ASI Tiger live mode: hold laser
DAC ... track PLC pointer ..." at live start records which are active.

## HARDWARE-CONFIRMED: live-mode round 2 (held laser DAC + PLC pointer tracking), log a541ed4e

User report: everything in order -- laser blanking, laser switching and
Left/Right switching all fine with both switches ON (log line confirms
"hold laser DAC ... =True, track PLC pointer =True"). Log (590 frames):
steady frames ~0.47-0.50 s, 23-24 serial cmds (mostly Expose-Out polls
during the exposure), ~0.39 s on wire (avg ~17 ms), camera-reported
2.03 fps (was 1.54 before this round; 0.48 at the start of the live-mode
work). No errors beyond the known ttl_motion_enabled config message.
Note: "Expose-Out high after" now reads ~200 ms vs ~100 ms in earlier
logs; the exposure/trigger timing for this run was not compared, so no
conclusion is drawn from it. run_tasks (~0.35 s) is now most of the frame.

## Stage-position updates back during live/snap/acquisition (slow poll, opt-in) + a Core-patch correction (mock/Qt-tested, NOT yet hardware-confirmed)

Asked by the user: put the stage-position updates back (slow is fine) so
you can move stages during live/acquisition and still see correct
positions. Cause of the gap: the Core patch paused mesoSPIM's 100 ms
position poller for the whole of live()/snap() (and stock mesoSPIM
already paused it for acquisitions), so GUI positions froze after any
move. Position reads are one `W <axes>` command on the shared port; at 1
s that is a few % of the link, not the contention that made live crawl
earlier (that was 100 ms polling plus unbounded serial timeouts, both
since fixed).

New config key (asi_dac_parameters): `stage_poll_interval_ms_during_run`
(example config: 1000). Set -> polling continues at that interval during
live/snap/acquisition and returns to the normal 100 ms when idle.
Absent/None/0 -> polling is paused exactly as before (confirmed-working
default; existing configs are unaffected until you add the key).

Core patch change (mesoSPIM_Core.py, now 5 changes): new signal
`sig_polling_stage_position_set_interval(int)` -> pos_timer.setInterval
(a queued cross-thread call; calling setInterval directly from the Core
thread would be unsafe); helpers `_stage_polling_pause()`,
`_stage_polling_pause_for_live()`, `_stage_polling_resume()` replace the
raw start/stop emits. YOU MUST RE-APPLY the updated
mesoSPIM_Core.py.diff (git apply --check passes on commit 98d74d35).

CORRECTION to earlier notes: I described the snap()/live() polling pause
as "harmless for every backend". It was not -- stock mesoSPIM never
paused polling in live/snap, so NI/Demo users would have lost position
updates there. The pause in snap()/live() now applies ONLY when
cfg.waveformgeneration == 'ASI_Tiger'; the acquisition pause stays
stock for everyone (or becomes the slow poll for ASI Tiger when the key
is set).

Verified: helper logic against a fake Core (NI = stock behaviour; ASI
without the key = stop/start as before; ASI with 1000 = interval 1000 +
start, then interval 100 + start on resume, no spurious interval emits;
0/None/non-dict tolerated); real PyQt5 smoke test of the signal wiring
from a non-Qt thread (100 ms: 5 ticks/0.55 s; 1000 ms: 2 ticks/2.2 s;
restored: 5; stopped: 0). NOT tested: the full Core on real hardware.
On hardware, check: positions update after moving a stage during live,
and the frame time barely changes (log's "serial cmds"/"slowest").

## ETL "not much action": amplitude convention was off by 2x (mesoSPIM amplitude is HALF peak-to-peak) -- fix is config-controlled (mock-tested, NOT yet hardware-confirmed)

> **SUPERSEDED by the next entry** -- `etl_amplitude_scale` / `galvo_amplitude_scale` and the default 0-4.096 V ETL limits described below were removed at the user's request. The findings (items 1-3) stand.

User report: "Not sure about ETL movement. Not seeing much action on ETLs."

Findings (read from the code, not guessed):
1. mesoSPIM's NI ETL waveform (utils/waveforms.tunable_lens_ramp) swings
   offset-amplitude .. offset+amplitude, i.e. `amplitude` is HALF
   peak-to-peak (galvo `sawtooth()` is `amplitude*wave+offset`, same
   convention). The Tiger's SAA is the TOTAL peak-to-peak amplitude
   (ASI command:saa: "sets the peak-to-peak amplitude of the pattern").
   This backend passed mesoSPIM's number straight into SAA, so the ETL
   (and galvo) swept HALF the NI-calibrated range -- e.g. 0.15 -> a
   0.15 Vpp sweep where NI gave 0.30 Vpp.
2. Ramp direction: NI Left (rise 90 / fall 5) ramps up, NI Right
   (rise 5 / fall 85) ramps DOWN. This backend always used an upward
   sawtooth. The delay/rise/fall shape is only approximated by the
   card's fixed sawtooth.
3. Not the cause: re-arming. arm_triggered() docs record hardware
   confirmation that SAM=2 auto-rearms every Expose-Out edge, so
   leaving the ETL armed between live frames is expected to be fine.

Changes (asi_dac_parameters, all optional):
- `etl_amplitude_scale` (default 2.0): SAA = amplitude x scale, ETL
  centred on its offset. 1.0 restores the old half-swing behaviour.
- `etl_follow_ramp_direction` (default False): when True, a side whose
  ramp_falling_% > ramp_rising_% gets a NEGATIVE SAA (downward ramp).
  Off by default because the geometry of a reversed ramp (centred on the
  offset?) is documented but not yet bench-confirmed here.
- `etl_min_volts`/`etl_max_volts` (default 0 / 4.096, this rack's ETL
  card range): a swing outside them is REFUSED -- ETL left stopped and
  zeroed, error logged -- instead of commanding an out-of-range SAA.
- `galvo_amplitude_scale` (default 1.0 = unchanged; the user reported
  the galvo fine). 2.0 would match NI-era scan width.
- New INFO log on every ETL (re)arm: offset, mesoSPIM amplitude, scale,
  Vpp, volt range, direction, period -- so a log shows what was sent.
- tools/asi_tiger_singleaxis_test.py safety math now uses
  abs(amplitude) (a negative amplitude only reverses direction).

Mock-tested: default 0.15 -> SAA H=300 / SAO H=3528; scale 1.0 -> 150;
Right side centred on its own offset; direction option gives SAA J=-300
only on the falling-dominant side; out-of-range refused (no SAA/SAM=2,
axis stopped+zeroed); galvo scale default unchanged and 2.0 doubles;
in live, ramp% changes trigger reconfigure; all earlier suites pass.

BENCH CHECK (scope on the ETL BNC, nothing connected is changed by this):
  python tools/asi_tiger_singleaxis_test.py --port COM4 --card-addr 34 --axis H \
      --pattern sawtooth --amplitude 0.3 --offset 3.528 --frequency 5 --duration 15 --max-volts 4.096
expect a ramp between ~3.378 and ~3.678 V (0.30 Vpp). Repeat with
`--amplitude -0.3` to see the reversed ramp direction and whether it
stays centred on 3.528 V; only then set etl_follow_ramp_direction True.

## ETL: amplitude scale removed (raw pass-through) and ETL voltage limits made opt-in; real range is now logged (mock-tested, NOT yet hardware-confirmed)

User feedback: the ETL DAC output is fine (the check is on the
microscope); the scale knob is unnecessary -- pass the raw number, each
system gets tuned anyway -- and they were unsure about the ETL voltage
range.

Changes:
- `etl_amplitude_scale` and `galvo_amplitude_scale` REMOVED. Amplitudes
  go to SAA exactly as mesoSPIM's state holds them. Tuning note kept in
  the code/config comments: SAA is TOTAL peak-to-peak (ASI command:saa),
  NI-era mesoSPIM swung offset +/- amplitude, so the same number is a
  half-size sweep here.
- ETL voltage limits: my default 0-4.096 V was an ASSUMPTION taken from
  the rack notes ("card 4, 0-4.096 V"). It may be wrong: range is a
  per-card `PR` setting, and the L/R switch axis I on the same card 34
  was raised to 0-10.24 V (`PR I=2` + reset, see that entry). So
  `etl_min_volts`/`etl_max_volts` now default to NONE (no check); set
  them only if you want a guard (a swing outside them is refused: ETL
  left stopped and zeroed, error logged).
- create_tasks() now does a one-time, read-only `PR <axis>?` for the two
  ETL axes (card 34 H and J) and LOGS the raw reply (INFO: "ASI Tiger ETL
  range: card 34 `PR H?` -> ..."). It is not parsed or enforced -- the
  reply format has not been captured in this project yet. A failed query
  only logs a warning. Use the logged value to choose limits if wanted.
- Kept: `etl_follow_ramp_direction` (default False; bench-check a
  negative amplitude first) and the per-(re)arm ETL log line (offset,
  Vpp, sweep range, direction, period).

Mock-tested: default raw 0.15 -> SAA H=150 / SAO H=3528; Right raw and
centred on its own offset; direction option negative only on the
falling-dominant side; no default limits (6.0 V offset is sent);
opt-in limits refuse/allow correctly; galvo unchanged (SAA A=391);
`34PR H?`/`34PR J?` sent once at setup and a failing query does not
break setup; all earlier suites pass.

## ETL voltage limit: it is the driver's analog input (0-5 V, 10-bit), not the DAC range -- example config now enables the guard

User: the 0-4.096 V DAC range is fine; the real limit is the Optotune
EL-E-4i driver, whose usable voltage "seems very low".

Looked up (Lens Driver 4i manual, as hosted by Edmund Optics; the
optotune.com product page has no analog-input numbers): "The analog
voltage applied on hardware pin B ... must be between 0 V and 5 V."
10-bit ADC (0-1023), "linearly mapped in firmware to the range defined
by the lower and upper software limits"; no absolute-maximum or input
impedance is given there. Check against the manual for the user's own
driver revision.

Consequences (arithmetic, not hardware-measured):
- The DAC's 0-4.096 V never exceeds the driver's 5 V, so nothing here
  can over-drive the input unless that card's range is raised (PR is
  card-wide -- the L/R switch axis on the same card was raised to 10.24
  V; see the startup `PR H?` log line). `etl_max_volts` is the guard.
- Resolution: 5 V / 1023 ~ 4.9 mV per driver step, so a 0.15 Vpp sweep is
  only ~30 steps (0.30 Vpp ~ 61). A coarse sweep is a plausible reason
  for "not much action". The sweep's effect per volt is set by the
  driver's software current limits, which can be widened in Optotune's
  software instead of raising voltages.
- Example config now ships the guard ON: etl_min_volts 0.0 /
  etl_max_volts 4.096 (the user's DAC range). No code change.

## "Not much ETL action": Expose-Out is only a short pulse, not the ~200 ms All-Rows level -- measured width + one-time warning + camera probe (mock-tested; root cause NOT yet found)

User finding: camera_parameters['exp_out_mode'] = 1 (All Rows) is set,
but a scope on Expose-Out shows only a short pulse, nowhere near the
200 ms exposure -- they suspect Line Output (4, the mesoSPIM default).

Why it matters here: this backend (a) starts the ETL on Expose-Out's
RISING edge and (b) treats Expose-Out going low as "exposure finished"
(then laser blanking is released). A short pulse means the ETL sweep is
not aligned with the exposure and run_tasks() can return before the
exposure really ends.

Evidence already in the logs (inference from timings, not a
measurement): run_tasks was ~0.35 s every frame with a 200 ms exposure,
while its own serial overhead before polling (pointer move ~103 ms +
trigger set/clear) was ~0.15-0.25 s and "Expose-Out high after" was
~100-200 ms -- i.e. high was first seen ~0.35 s in and low followed
almost at once. A 200 ms level would have pushed run_tasks to >= ~0.5
s. This agrees with the scope.

What the docs say (Photometrics PVCAM "Exposure modes"): All Rows is
high for exactly the exposure time (last-row start to first-row end);
First Row = first row's exposure; Any Row = longer (adds the rolling
sweep); Line Output = a short pulse per line readout. PVCAM docs for
programmable scan mode are silent on whether it forces or restricts the
Expose-Out mode. In pyvcam (v2.3.2 source read) exp_out_mode is OR-ed
into the exposure mode when acquisition starts, so setting it once in
open_camera() should apply; the stock configs pair scan_mode=1 (Line
Delay) with exp_out_mode=4. WHY the camera does not follow mode 1 on
this bench is therefore still UNKNOWN.

Changes:
- run_tasks() now measures how long Expose-Out stayed high (resolution
  ~ one poll round-trip, ~25 ms), adds "high for ~N ms (exposure M ms)"
  to the per-frame timing line, and logs ONE WARNING (also printed) per
  live session when it is shorter than expose_width_warn_fraction
  (default 0.5) of the exposure; exposures < 0.1 s are not checked
  (too short to resolve). Silence with expose_width_warn_fraction 0.
- New tools/asi_tiger_camera_expose_out_probe.py (pyvcam only; no ASI
  hardware): applies mesoSPIM's own camera setup, READS BACK
  PARAM_EXPOSE_OUT_MODE / scan params right after setting and while
  running (shows whether the value survives), free-runs the camera so
  Expose-Out can be scoped, and --sweep [--sweep-scan] walks modes 0-4
  (and scan modes Auto/Line Delay). Every pyvcam call it makes was
  checked against pyvcam 2.3.2's source; it has NOT been run against a
  camera.

PROBE FIX (user's first run): the probe aborted with PL_ERR_ACCESS_DENIED on PARAM_SCAN_LINE_DELAY -- that parameter is only writable in Line Delay scan mode, not Auto. apply_setup() now skips it outside scan mode 1 and reports (instead of aborting on) any setting the camera refuses. First run also confirmed: camera IRIS_15MP_MONO / GSENSE5130; Expose-Out modes offered = First Row 0, All Rows 1, Any Row 2, Rolling Shutter 3, Line Output 4; exposure modes Internal Trigger 1792, Edge Trigger 2304, Trigger first 2048.

NOTE: the earlier comment in _etl_period_ms() that, after switching to
All Rows, "Expose-Out's duration now equals the REAL configured
exposure exactly" is now in question for the current setup.

Mock-tested: a ~170 ms level on a 200 ms exposure -> no warning; a 30 ms
pulse -> exactly one warning, not repeated, re-armed by begin_live(),
silenced by fraction 0, skipped for 50 ms exposures; all earlier suites
pass.

## Expose-Out "short pulse" explained (log 44929253, hardware): All Rows is short when the sweep is long

**Camera mode is correct, mesoSPIM does not override it.** Read-back logged at open_camera(): Expose-Out mode 1, exposure mode 2304 (Edge Trigger), config file `config_benchtop_ASI_2026.py` (cfg `exp_out_mode` 1). The only write of `exp_out_mode` in stock mesoSPIM_Camera.py is in open_camera().

**What the log shows (200 ms exposure, every frame):** Expose-Out first seen high ~198 ms after the trigger pulse ended, high for ~1 poll (~22 ms). The probe's two-pulse pattern (long, then short) was the probe's own start/stop artifact (Internal Trigger, `poll_frame` raised 'Acquisition not active' 0.4 s after start_live), not the mesoSPIM situation.

**Working hypothesis (NOT yet bench-confirmed):** All Rows is high only while every row exposes at once, about (exposure - sweep time). With ASLM line delay the sweep takes most of the exposure, so All Rows is a short pulse near the END of the exposure. The numbers fit (pulse ~215-230 ms after the trigger edge, ~20 ms wide). If so, the ETL (started by Expose-Out's rising edge) starts ~180 ms late and laser blanking ends early. Prediction to test: `exp_out_mode: 2` (Any Row, high from first row start to last row end) should log "high after" near 0-40 ms and "high for" about exposure + sweep; `exp_out_mode: 0` about the exposure length from the start.

**HARDWARE-CONFIRMED (log e977f8da, `exp_out_mode: 2` Any Row, read-back 2):** every frame logs "Expose-Out high after: 8 ms, high for ~380-418 ms (exposure 200 ms)" versus ~198 ms / ~22 ms with All Rows. So the rolling-shutter sweep is about 190 ms, All Rows was a short window near the end of the exposure, and Any Row gives the rise at the first row's start (ETL starts with the sweep) and a level spanning the whole exposure + sweep (laser blanking covers every row). Cost: frame time 0.48 s -> 0.66 s at 200 ms exposure (the window really is longer). Example config now documents the modes; recommended for ASLM: `exp_out_mode: 2`. Not yet checked: ETL ramp period (200 ms) vs the ~190 ms sweep in the actual images.

**Change:** the one-time width warning no longer claims a wrong camera mode; it explains the All Rows / line delay case and suggests Any Row.

## 2026-10-06: Acquisition-row prep -- Right galvo amplitude bug, per-frame timing in rows, laser-enable trim, opt-in pointer tracking outside live (mock-tested, NOT yet hardware-confirmed)

New mock harness (not in the repo): replays mesoSPIM_Core's exact acquisition-row call sequence (prepare_image_series, per-plane snap_image_in_series + Z move over the shared port, close_image_series, stop mid-row) and live -> acquisition, using the user's real `config_benchtop_ASI_2026.py`, the real TigerController / PLCCard / DAC / SingleAxisWaveform / ASITiger_LaserEnabler code, and a fake serial port that models the PLC pointer, the cell states and an Any Row Expose-Out window per trigger. The state mock only has `__getitem__`/`__setitem__` and raises KeyError like `mesoSPIM_StateSingleton`.

**Bug (found by the mock, present since the galvo was added): the Right galvo was driven at 0 Vpp.** write_waveforms_to_tasks() read `galvo_r_amplitude`, but stock mesoSPIM has no such state key (it is commented out in mesoSPIM_State.py; mesoSPIM_WaveFormGenerator drives both galvos with `galvo_l_amplitude`, "always use same amplitude for both galvos"). `_state_get` returned the 0.0 default, so for every Right row and Right live the galvo was configured `SAA C=0` and only parked at its offset, so the light sheet did not scan. HEAD under the mock: `37SAA C=0`. Fixed: both sides use `galvo_l_amplitude` (raw, as stock); offsets, frequencies and duty cycles stay per side. **Check on hardware: the Right arm's sheet should now scan.** The earlier "left/right switching works" confirmation did not cover the right galvo's amplitude.

**Per-frame timing line during acquisition rows.** `_log_frame_timing()` ran only in close_tasks(), which is once per ROW in an acquisition, so a whole stack showed as one line. It now runs in stop_tasks() (once per frame on every path). close_tasks() only logs if no frame was logged since the last run_tasks() (e.g. stop before the first plane). The close's own commands are counted in the next frame's line. Live lines are unchanged in practice (close_tasks() sends ~0 commands in live).

**Laser-enable trim.** ASITiger_LaserEnabler.enable() called disable_all() before enabling, including the target line. Core calls enable() before the plane loop and again for plane 1 (and live: before the loop and every frame), so the already-on line went OFF then ON: 2 extra pointer moves (~200 ms) per row or live session, plus a short laser blink before the first frame. It now switches off only the OTHER lines (same "all other lines off" semantics as mesoSPIM_LaserEnabler).

**New opt-in `acq_track_plc_pointer` (default False).** The live-only pointer tracking, now also for acquisition rows and single snaps: turned on in write_waveforms_to_tasks() (pointer forgotten first, cell cache kept), off in close_tasks(). Safe for the same reason as live: only this PLCCard (shared with the laser enabler) addresses the PLC axis E; the stage driver's slow polling and Z/F moves use X/Y/Z/T/V. Mock: pointer moves per plane ~4 -> ~2 (rows of 4/3/2 planes: 34/12/8 -> 23/7/5 including row setup), i.e. ~200 ms per plane on this rack. Every laser-ON write still lands on the correct cell in the pointer model.

**Mock-verified for acquisition rows (3 rows: Left 488 / Right 561 / Left 638, 4/3/2 planes, tracking off and on):** one trigger and one Z step per plane; active-side ETL armed exactly once per row (SAM=2), inactive ETL and galvo stopped; laser DAC set once per row and zeroed at close; the correct laser cell switched on once per plane and off after; all PLC cells low at the end; tracking off after each row and snap; stop mid-row stops the ETL/galvo and the next row does not run; live (Right) -> acquisition works with no leftover live state.

**Blocker in the user's config (set to False on 2026-10-06 at the user's request, for testing; TTL motion is wanted later for faster acquisitions):** `config_benchtop_ASI_2026.py` has `asi_parameters['ttl_motion_enabled']: True`. Core then skips its per-plane `move_relative` and puts the ASI stage cards in TTL mode, so every plane of a stack would be taken at the same Z (config_check() already logs an ERROR about this). It must be False for this per-frame design before acquisition-row tests.

Also: docstrings and example-config comments that still described Expose-Out under "All Rows" (`_etl_period_ms`, `_check_expose_width`, the etl_period / margin comments) were updated to the hardware-confirmed Any Row behaviour. Wording only, no behaviour change.

## 2026-10-06: HARDWARE-CONFIRMED acquisition rows; stage x/f display swap fixed in the user's config (HARDWARE-CONFIRMED)

**Hardware-confirmed (user):** acquisition rows work as intended with `ttl_motion_enabled: False`. Rows with different illumination arms and lasers switch correctly. The Right galvo now scans as intended (the `galvo_r_amplitude` fix above).

**Bug reported:** with `stage_assignment {'x':'Y', 'f':'X', ...}` the GUI buttons moved the right stages, but the display showed focus and x swapped. Starting an acquisition with markers set in focus changed the focus.

**Cause (stock `asicontrol.py`, not this backend):** moves send explicit letters (`M X=...`), so they were right. `read_position()` sends `W` with the axes in the dict's order (`W YXZTV`) and assigns the reply's numbers to those letters BY POSITION, while the controller answers in card order (X, Y, Z, T, V). So the real X (focus) was shown as mesoSPIM x and vice versa. Row markers are taken from that displayed position, so `f_abs` held the x value and the stage moved F there at the start of the row. The config comment already warned "The dictionary order is important here! Must match the ASI cards". The card-order reply is inferred from the symptom; the logs don't record raw replies.

**Fix (config only):** `stage_assignment` reordered to `{'f':'X', 'x':'Y', 'z':'Z', 'theta':'T', 'y':'V'}`, so the ASI letters read X, Y, Z, T, V. The same mapping, only in card order. `encoder_conversion` is also matched by position and was already in X, Y, Z, T, V order. Nothing else uses the dict's order (only membership checks). Mock (real `StageControlASI`, fake port that answers `W` in card order): the old order reproduces the x/f swap, the new order shows every axis correctly.

**HARDWARE-CONFIRMED (user, 2026-10-06):** after the reorder, the display and focus are correct (x and f shown correctly, focus no longer changes when an acquisition starts). Acquisition rows saved before this fix have x and f swapped in their markers and must be re-marked.

## 2026-10-06: HARDWARE-CONFIRMED `acq_track_plc_pointer`, now default True

Logs 20261006-124314 (False) and 20261006-124524 (True), 25-plane single-row acquisition, 488 nm Left, 200 ms exposure:
- Time per plane: 0.76-0.80 s -> 0.65-0.70 s (~0.12 s, ~15%). Camera frame rate 1.29 -> 1.49 fps. Acquisition now matches live (~0.66 s).
- Serial wire time per plane: 0.62-0.65 s -> 0.48-0.50 s (about 1.5 of the ~103 ms pointer moves removed; the mock estimated 2). The command count goes UP (29-31 -> 35-37): those are cheap Expose-Out polls, since polling starts sooner.
- User: laser blanking works, no dark frames, every frame saved.

Also hardware-confirmed: one frame-timing line per plane during acquisition, and the laser-enable trim (the first plane of the row has no off/on blink).

Default changed to True (code, example config). The live option `live_track_plc_pointer` is unchanged.

Note on the Expose-Out width in the timing line: with tracking OFF it reads ~250-312 ms instead of ~390 ms. That is a measurement artifact: switching the trigger off costs an extra ~103 ms pointer move before polling starts, so Expose-Out has already been high ~100 ms at the first poll. "High after" and "high for" are measured from the start of polling, not from the trigger.

Display skipping frames during acquisition (user report) is stock mesoSPIM: mesoSPIM_Camera.add_images_to_series() shows only every `camera_display_temporal_subsampling`-th frame (default 2; set it in `startup`). All frames are saved.

## 2026-10-06: Hardware laser gating by camera Expose-Out (`laser_gate_with_expose_out`, HARDWARE-CONFIRMED: timing and normal use; see the end of this entry)

**Why (instead of TTL motion first):** the 25-plane log (`acq_track_plc_pointer` True) puts each ~0.66 s plane at: ~0.40 s Expose-Out high (physical), ~0.21 s for 2 PLC pointer moves (trigger cell 10 <-> laser cell, because Core switches the laser line every frame), ~0.04 s small commands, and only ~0.01-0.02 s for the serial Z step. So TTL Z alone would save ~3%. Per-frame laser blanking in hardware removes the pointer moves. The user chose "gating, then TTL Z".

**What:** opt-in `asi_dac_parameters['laser_gate_with_expose_out']` (default False). `configure_laser_enable_lines()` gains `gate_source_addr` / `gate_cells`: each laser BNC (5-8) is driven by an `and2` cell = enable cell (12-15, unchanged d_flop) AND the camera Expose-Out BNC input level (`bnc_addr(camera_expose_bnc)`, the same signal that starts the ETL). Gate cells: `plc_laser_gate_cells`, default (8, 9, 11, 16) (avoids trigger cell 10, enables 12-15, and cells 1-7 used by zstack_chain). The enable cell is written low before the BNC is switched to the gate, so there is no glitch. Use with stock `laser_blanking = 'stack'`: Core then switches the laser line once per row (acquisition) or session (live), and the PLC blanks each frame from Expose-Out (Any Row: first row start to last row end).

**Live laser change:** with 'stack', Core's live() enables the laser line only once before the loop, so a laser change during live would leave the OLD line enabled (no light). With gating on, write_waveforms_to_tasks() calls `laserenabler.enable(current laser)` every live frame. The cell cache makes it free unless the line changed.

**config_check warnings:** gating on with laser_blanking not 'stack' (works, but no speed-up), and 'stack' with gating off (laser on between frames for the whole stack).

**Mock (new `mock_gating.py` + shared `mock_infra.py`; the fake PLC now records cell types, CCB inputs and I/O sources):** gate cells 8/9/11/16 = and2(35, 12/13/14/15) and BNCs 5-8 sourced from them; default-off wiring unchanged. Steady-state PLC pointer moves per frame: 2 -> **0** (acquisition and live). The laser line is switched on once per row. Two rows with different laser and arm. Live 488 -> 638 mid-session: the old line goes off, the new one on, the DAC channel follows. Warnings fire as intended. A gate cell that collides with the trigger cell raises. The regression suite `mock_acq_rows.py` still passes. **Expected on hardware: ~0.66 -> ~0.45 s per frame at 200 ms exposure, live and acquisition.**

**User config for testing:** `laser_gate_with_expose_out: True` and `laser_blanking = 'stack'` (revert: False + 'images').

**Hardware checks:** frame time; no light between frames (the laser output only during exposure, e.g. on a scope: laser BNC vs. Expose-Out BNC 3); no dark frames; laser change during live; Left/Right and laser changes between rows.

**HARDWARE-CONFIRMED (log 20261006-145515, gating on + 'stack', 200 ms exposure):**
- Live: 0.43-0.44 s per frame (was ~0.66), camera live frame rate 1.98-2.17 fps. Steady-state frames show no `36M E=` pointer move (slowest command is an Expose-Out poll, ~11 ms); ~31-34 commands per frame, mostly polls. The user tuned ETL offset/amplitude and galvo offset/amplitude live on both arms with this running.
- Acquisition list, 2 rows (488 nm Left, 561 nm Right, 10 planes each): 0.43-0.44 s per plane, camera frame rate 2.07-2.08 fps (was 1.49 with `acq_track_plc_pointer` alone, 1.29 before that).
- Per frame now: run_tasks ~0.43 s = trigger set/clear + Expose-Out high ~0.39 s (exposure + rolling sweep); everything else (Z step, processEvents) ~0.01 s. The per-frame design is within ~40 ms of the Expose-Out window.
- Laser change DURING live (log 20261006-150530): 488 -> 561 nm at 15:06:12. The next frame shows the enable-line switch (53 commands, pointer moves `36M E=..`, run_tasks 0.53 s), then live returns to 0.43-0.44 s per frame. Left -> Right during live at 15:06:21: one reconfigure frame (0.69 s), then normal. The user kept live running through both changes.
- Still optional: a scope check of laser BNC vs. Expose-Out BNC 3.

## 2026-10-06: TTL Z stage motion (`stage_ttl_bnc`, HARDWARE-CONFIRMED: Z steps; see the end of this entry)

**Expected gain is small:** after gating, a plane is ~0.44 s and the serial Z step + Core overhead ~10 ms (~2%). Done at the user's request as the step toward the autonomous loop.

**Design:** reuse stock mesoSPIM's `ttl_motion_enabled` path unchanged in Core: prepare_acquisition() primes the step with a back-and-forth relative move, `enable_ttl_mode()` arms `RM Y=<mask>` + `TTL X=2 Y=2` (repeat the last relative move on each TTL IN0 pulse) on `ttl_cards`, run_acquisition() skips its per-plane `move_relative`, close_acquisition() disarms (`TTL X=0`). The backend only supplies the pulse: `create_tasks()` routes `falling_edge(Expose-Out)` (addr 227, a brief PLC pulse) to `asi_dac_parameters['stage_ttl_bnc']` as a push-pull output, only when `ttl_motion_enabled` is True. Same signal and wiring as the earlier HARDWARE-CONFIRMED Z sync test (BNC1 -> Z/T card IN0), but that test used the ring-buffer mode `TTL X=12`; **`TTL X=2` with this pulse is untested on hardware.** The stage steps after EVERY frame including the last (N steps per row), the same as the serial path; close_acquisition_list() then moves back to the start.

**asicontrol.py (stock driver, patch reference regenerated):** new optional `asi_parameters['ttl_axis_masks'] = {card: RM Y mask}`. Stock sends `RM Y=3` (both axes on the card); for the Z/T card that also arms theta, which would repeat any stale relative theta move on every pulse. Default unchanged.

**Per-row check logged:** with TTL armed, the backend reads Z (`W Z`, encoder_conversion) before the first trigger and after the row, and logs `ASI Tiger TTL Z: row of N frames moved Z a -> b um (+d um, +s um per frame)`. s should equal the row's Z step.

**config_check:** `ttl_motion_enabled` without `stage_ttl_bnc` is an ERROR (Z would never move); with it, an INFO line.

**Mock (`mock_ttl.py`; the fake rack steps Z at the end of each Expose-Out window only if BNC1 is really sourced from falling_edge(Expose-Out) and Core has TTL armed):** 8 planes -> 8 steps, the log line reads +40 um / +5 um per frame, no serial per-plane moves; without stage_ttl_bnc -> ERROR, Z unchanged; ttl off -> BNC1 untouched; snaps and live unaffected; asicontrol emits `2 RM Y=1` with the mask and `RM Y=3` without. Earlier suites still pass.

**User config for testing:** `ttl_motion_enabled: True`, `ttl_cards: (2,)` (only the Z/T card is wired; F fixed per stack), `ttl_axis_masks: {2: 1}`, `stage_ttl_bnc: 1`. Revert: `ttl_motion_enabled: False`.

**First hardware run (log 20261006-151618) CRASHED on frame 1:** `run_tasks()` -> `read_bnc_inputs()` -> `int(float(reply.split("=")[-1].split()[0]))` failed on an empty reply. Cause: with TTL armed, the stage card acts on the pulse at Expose-Out's falling edge -- exactly while `run_tasks()` polls `RDADC X?` for "low" -- and the Tiger answers the query in flight with a bare `:A` (the same effect seen earlier for WHERE under active triggering). The poll handler caught only timeouts, so the parse error escaped and killed the acquisition. **Fixed:** a failed poll (timeout, TigerError, ValueError, IndexError) now returns None and both waits simply poll again. It previously returned 0, which in the "wait for low" phase would read as "exposure finished" and end a frame early -- a latent bug for timeouts too. The Z read for the TTL log retries 3x. The frame timing line adds `| N bad poll replies` when any occurred. Mock: 2 bare replies after every TTL step -> all frames complete, none ends early (high ~0.41 s each), Z steps and the TTL log stay correct.

**Hardware checks:** the TTL Z log line's per-frame step equals the set Z step for a few rows (different step sizes); images step through the sample; theta and F do not move; frame time unchanged or slightly lower.

**HARDWARE-CONFIRMED (log 20261006-170305, single row, 31 planes):** `ASI Tiger TTL Z: row of 31 frames moved Z 0.0 -> 310.0 um (+10.00 um per frame)`; the stage poll at 17:04:24 independently read Z = 900 counts (90 um) ~9 frames in. So stock `TTL X=2` + `RM Y=1` on card 2 steps on the PLC falling-edge pulse, once per frame. Frame time 0.43-0.44 s (unchanged, as predicted).

**Finding -- the replies are CORRUPTED, not just empty:** at each TTL step the Tiger's serial output glitches: `RDADC X?` replies like `'36�'`, `'?2'`, `'3v'`, `'-'`, empty; and the stage driver's own `W XYZTV` poll failed on a `0x8c` byte and once returned 4 of 5 positions. About one bad reply every other frame, always in the "wait for low" phase. Handled: the poll retries, no frame ended early (high 386-407 ms), the stage poll just retries next second. Why it is safe for frame timing: the glitch is time-locked to Expose-Out's real falling edge (that is what triggers the step), so even a corrupted reply that parsed as "low" could end a frame at most one poll (~10 ms) early. Still open: whether a serial COMMAND (not a query) issued at that instant could be corrupted -- during a TTL row only polls are sent, so not a current risk.

User-confirmed: the row's Z step was 10 um (matches exactly), and theta and F did not move. Not checked: images (no sample available; Z motion itself is confirmed), a second step size, multi-row.

## 2026-10-06 (v0.8): ETL period from the rolling sweep (`etl_period_source: 'sweeptime'`, HARDWARE-CONFIRMED timing; see the end of this entry)

**Finding (camera config analysis):** the Iris 15 runs Line Delay scan mode, line time = 10.26 us x (scan_line_delay + 1) = 71.82 us at delay 6, so the rolling sweep over 2960 rows is ~213 ms. At the user's 200 ms exposure ~2780 of 2960 rows expose at once -- no narrow rolling slit (see the CORRECTION below: the ETL sweep still helps at 200 ms). The stock mesoSPIM benchtop configs for the same camera and line delay use a 20 ms exposure (a ~280-row slit) with `sweeptime` 0.267 s and ETL ramp 5 / 90 / 5 %. The backend's ETL period has been = exposure, which only matched the sweep by coincidence at 200 ms; at 20 ms it would ramp in 20 ms.

**Change:** optional `asi_dac_parameters['etl_period_source']`: `'exposure'` (default, unchanged) or `'sweeptime'` = `state['sweeptime']` x max(`etl_<side>_ramp_rising_%`, `_falling_%`) / 100 -- mesoSPIM's own GUI-editable NI ETL timing, passed through raw. max() because the Right arm's 5 / 85 settings sweep on the falling part (direction stays `etl_follow_ramp_direction`'s job). `etl_period_ms` still overrides both. The period is part of the live arm key, so editing sweeptime or ramp % in live re-arms. The SAM=2 ramp starts on Expose-Out's rise (first row) while the slit centre lags by exposure/2; on a linear ramp that lag equals an offset shift, so it is tuned with the ETL offset as usual (no PLC delay cell).

**Diagnostics:** the arm log line now says `period N ms (from <source>)` and `predicted Expose-Out window W ms` (= exposure + rows x line time; `camera_line_time_base_us` default 10.26, rows / binning). WARNINGs: period < 0.5 x sweep (focus cannot follow the slit -> use 'sweeptime'), or period > window + 30 ms (SAM=2 might miss the next trigger).

**Mock (`mock_period.py`):** 'exposure' 200 ms -> SAF 200, predicted window 413 ms; 'sweeptime' Left 267.34 x 90 % -> 241 ms, Right x 85 % -> 227 ms; 20 ms exposure with 'exposure' -> 20 ms + warning; with 'sweeptime' -> 241 ms, no warning; sweeptime 0.5 s -> long-ramp warning; explicit etl_period_ms wins; live sweeptime change re-arms (241 -> 180). Earlier suites pass.

**Expected:** 20 ms exposure + 'sweeptime': Expose-Out ~20 + ~213 ms, frame ~0.28 s (was 0.44 s), and a real ASLM slit. Note the measured Any Row width at 200 ms was ~390-400 ms vs 413 predicted; the line-time base is from the config comment, so compare the log's prediction with the measured width.

**User config for testing:** `etl_period_source: 'sweeptime'`. Revert: `'exposure'`.

**Hardware checks:** live at 200 ms: arm log shows ~241 ms (L) / ~227 ms (R) and the predicted window; then set the exposure to 20 ms in the GUI: Expose-Out "high for" ~230 ms, frame ~0.28 s, ETL ramp on a scope spans the window; image quality needs a sample (slit focus via ETL offset / amplitude).

**HARDWARE-CONFIRMED (log 20261006-173720, Left arm, fluorescent solution in the chamber):**
- 200 ms exposure: `period 241 ms (from sweeptime)`, predicted window 413 ms, measured "high for" ~387-399 ms, 0.44 s per frame, 1.94 fps. User: the sweeptime-timed ETL was "very off" at 200 ms. Cause not determined from the log; most likely the ETL offset/amplitude were tuned under the old 200 ms (exposure-derived) ramp, and the best offset differs between modes (the waist should pass a row mid-exposure: ~100 ms lag at 200 ms vs ~10 ms at 20 ms).
- 20 ms exposure (stock ASLM): predicted window 233 ms, measured ~208-219 ms, **0.25-0.26 s per frame, live 3.71 fps** (was 0.44 s / ~2.1 fps). User: "20 ms was better"; the waist, light sheet and ETL focus sweep are visible in the solution.
- Measured sweep ~195-200 ms (window - exposure + poll lag), so the prediction (10.26 us x 7 x 2960 = 213 ms) is ~7% high; ~9.5 us base would fit (inferred from poll-resolution data, only affects the log line). Consequence: the 241 ms ramp (sweeptime 0.267 x 90 %) is ~20 % longer than the sweep -- tune sweeptime to ~0.22 s (90 % -> ~198 ms) and fine-tune by eye so the waist stays in the slit across the frame; centre with the ETL offset.
- **CORRECTION (2026-10-06, after the user's observation):** an earlier note here called 200 ms "effectively non-ASLM". Wrong. At 200 ms each row integrates ~200 ms, about the whole ETL sweep, so the waist passes every row once: a time-averaged swept-focus sheet. Points ARE in focus across the FOV (user sees this), but each row also integrates the out-of-focus part of the sweep (broader tails, more background). At 20 ms each row integrates only while the waist is near it, so the rolling slit also rejects the out-of-focus light: thinner effective sheet, better contrast, and ~1.7x faster. Both are valid; `etl_period_source` lets the user choose ('exposure' with its tuned offset suits 200 ms). A structured sample should decide.
- Not yet: Right arm with 'sweeptime' (227 ms from 85 %), acquisition at 20 ms, image quality on a structured sample.

## 2026-10-06 (v0.8): BUG -- galvo ran as a SAWTOOTH, not a triangle (duty-cycle units; HARDWARE-CONFIRMED)

mesoSPIM stores `galvo_*_duty_cycle` in **percent** (config/state 50; `utils/waveforms.sawtooth(dutycycle=50)` is a symmetric triangle). `write_waveforms_to_tasks()` compared it as a fraction (`0.4 <= duty <= 0.6`), so 50 never matched and every galvo was configured `PATTERN_SAWTOOTH`: a ramp with a hard flyback each period instead of the intended back-and-forth scan. **Hardware evidence:** log 20261006-173720 shows `'37SAP A=0'` (pattern 0 = sawtooth). Present since the galvo was added; it affected every live / acquisition frame on both arms.

**Fix:** the duty is read as percent (a value <= 1 is still accepted as a fraction); 40-60 % -> `PATTERN_TRIANGLE` (period rounded to an even ms, as before), else sawtooth. A new INFO line per (re)arm: `ASI Tiger galvo (<side>): triangle (duty 50 -> 50 %), period 10 ms, amplitude ..., offset ..., free-running`.

**Mock (`mock_galvo.py`):** duty 50 -> `37SAP A=1` / `C=1` (triangle), `SAF 10`; 0.5 -> triangle; 30 and 100 -> sawtooth; 150 Hz triangle -> 6 ms (even). Earlier suites pass.

**Hardware checks:** the new galvo log line says triangle; galvo output (card 37 A/C) on a scope is a symmetric triangle at 100 Hz; sheet illumination in the solution is at least as even as before (a triangle has no flyback, but each point is now crossed twice per period instead of once).

**HARDWARE-CONFIRMED (log 20261006-190703):** `ASI Tiger galvo (Right): triangle (duty 50 -> 50 %), period 10 ms`; user: sheet fine in the fluorescent solution, only parameter fine-tuning. (No scope trace taken.)

## 2026-10-06 (v0.8): Right-arm ETL ramp direction (`etl_follow_ramp_direction: True`) HARDWARE-CONFIRMED

The option existed (mock-only, "reversed-ramp geometry not bench-confirmed") and was off, so BOTH arms ramped up. Both arms share one rolling direction but propagate opposite ways, so the Right ETL must ramp the opposite way. With the option on, a side whose ramp falling % > rising % (Right 5 / 85-90) gets a negative SAA (down ramp, same voltage window, offset unchanged). Mock (`mock_direction.py`): Left `SAA H=480`, Right `SAA J=-500`, live Left -> Right re-arms with the right sign. **Hardware (log 20261006-190703, Right, 20 ms ASLM, fluorescent solution):** `down ramp`, 2.586..3.086 V, 243 ms; user: the waist follows the slit, everything fine after fine-tuning. On in the user's config.

## Rollback

This patch is purely additive at the mesoSPIM-control level. The only
changes to EXISTING mesoSPIM-control files are: in `mesoSPIM_Core.py`
-- two small `elif` branches (plus their imports; one for
`waveformgeneration`, one for `laser`), `sig_polling_stage_position_*`
handling (new set_interval signal + three small helpers; snap()/live()
pause polling only for the ASI_Tiger backend), and a guarded `begin_live()`/`end_live()`
call pair (try/finally) around the live() loop (harmless for every backend, since
restarting an already-running timer is a no-op) -- and in
`asicontrol.py`, the `serial_lock` addition (new import, one new
attribute, one `with` block around `_send_command()`'s existing body
-- no behavior change for plain ASI-stage-only setups, since a lock
only this file's own code ever acquires changes nothing observable).
Setting `waveformgeneration = 'NI'` (or `'cDAQ'`/`'DemoWaveFormGeneration'`)
and `laser = 'NI'`/`'cDAQ'`/`'Demo'` in your config continues to use
the untouched original code path.
