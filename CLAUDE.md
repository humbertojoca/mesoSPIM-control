# ASI Tiger backend for mesoSPIM-control: project handoff

Put this file at the root of your mesoSPIM-control fork as `CLAUDE.md`. It is a condensed state of the work so far. `PATCHNOTES_ASI_TIGER.md` has the full dated history.

## Where we left off (2026-10-06)
- Branch `asitiger-v0.9` was created from v0.8 (pushed) as the working branch for the next round. v0.7 and v0.8 are pushed.
- The user is doing physical alignment and building a structured phantom. Software testing resumes after that.
- First tasks on return: image quality with the phantom in 20 ms ASLM on both arms (ETL offset / amplitude / sweeptime tuning against real structure), then a multi-row acquisition with real Z steps (images, not just logs).
- Current daily config (user's, gitignored): 20 ms exposure, `etl_period_source: 'sweeptime'` (sweeptime 0.270 s, ramps L 90 / R 90 -> 243 ms), `etl_follow_ramp_direction: True`, `laser_gate_with_expose_out: True` + `laser_blanking = 'stack'`, `acq_track_plc_pointer: True`, `ttl_motion_enabled: False`, `camera_line_time_base_us: 9.5`.
- Measured: ~0.26 s per plane in live and acquisition (was 0.78 s at the start of 2026-10-06).

## What this is
A Python library (`asi_tiger`, in `mesoSPIM/src/devices/asi_tiger/`) that drives an ASI Tiger TG-1000 (stage, PLogic and DAC cards) as a replacement for NI hardware in mesoSPIM-control. It is integrated as `mesoSPIM_ASITigerWaveFormGenerator` plus `ASITiger_LaserEnabler`. Base commit of the stock repo: `98d74d35bdc6dbc9523cf9ddbed59f262b629a66`. Core changes live in `mesoSPIM_Core_patch_reference/mesoSPIM_Core.py.diff` (apply with `git apply`).

## Working rules (the user insists on these)
- Mock-test everything first, then the user confirms on real hardware. Never assume from docs.
- Keep `PATCHNOTES_ASI_TIGER.md` honest: mark entries as hardware-confirmed, mock-only or superseded.
- Pass raw numbers from mesoSPIM state to hardware. No amplitude scale factors (removed on request).
- The user's own config is `config/config_benchtop_ASI_2026.py`. The example is `config/examples/config_asi_tiger_example.py`.

## Design
- **Per-frame triggering.** Each `run_tasks()` fires one camera trigger through a PLC cell (BNC 4), then polls the camera's Expose-Out (PLC BNC 3) for high and then low. Z steps either by Core's serial move per plane (`ttl_motion_enabled` False) or by TTL: the PLC pulses `stage_ttl_bnc` on each Expose-Out falling edge (`ttl_motion_enabled` True, which requires `stage_ttl_bnc`).
- **Live mode.** Core calls `begin_live()` and `end_live()` around the live loop. The ETL and galvo keep running between frames and are re-armed only when an `arm_key` of side, ETL amplitude, offset, ramp percentages, period, or galvo amplitude, offset, frequency or duty changes. The laser DAC is held between frames. `live_hold_laser_dac` defaults to True, and the DAC is zeroed at `end_live` or when the laser line changes.
- **PLC pointer.** Every `CCA F=` write needs a preceding `M E=<cell>` pointer move, about 103 ms each on this rack. Cell states are cached. Pointer tracking (`track_pointer`) is on in live (`live_track_plc_pointer`) and in acquisition rows and snaps (`acq_track_plc_pointer`). Both default True and both are hardware-confirmed.
- **Laser blanking.** PLC enable lines (cells 12–15, BNCs 5–8). `enable()` switches off only the other lines, so the target line is not blinked off and on.
- **Galvo amplitude.** Both sides use `galvo_l_amplitude`, as stock mesoSPIM does. There is no `galvo_r_amplitude` state key. Offset, frequency and duty are per side.
- **Frame timing log.** One line per frame, written from `stop_tasks()` (in acquisition `close_tasks()` runs once per row).
- **ETL.** SAM=2 re-arms on every rising edge of Expose-Out (hardware-confirmed). mesoSPIM's ETL amplitude is half peak-to-peak, while Tiger SAA is total peak-to-peak. The ETL driver (Optotune EL-E-4i) is the real voltage limit. The DAC range of 0–4.096 V is fine, and the min/max guard is opt-in (`etl_min_volts`, `etl_max_volts`). A negative SAA reverses the ramp direction, but that geometry is not bench-confirmed.
- **Shared serial port.** The stage driver and this backend share one serial connection. A `serial_lock` in `asicontrol.py` serializes them.
- **Stage position during live and acquisition.** Slow polling through the Core patch (`stage_poll_interval_ms_during_run`, default 1000 ms), ASI_Tiger only.

## Hardware-confirmed
- live() works at about 2 fps. The galvo runs continuously. Laser blanking, laser line and intensity changes during live, and left/right switching all work.
- **Camera Expose-Out mode must be Any Row (`exp_out_mode: 2`).** Log e977f8da: Expose-Out rises about 8 ms after the trigger and is high for about 380–418 ms at a 200 ms exposure (exposure plus a rolling-shutter sweep of about 190 ms). With All Rows (mode 1) the pulse is only about 20 ms wide, near the end of the exposure, so the ETL starts late and laser blanking ends early. The user's earlier "short pulse" report was a mis-set config, not a camera or mesoSPIM bug. The camera applies the mode exactly as set, and stock mesoSPIM never overrides it.
- Frame time at 200 ms exposure is about 0.66 s with Any Row. This is mostly physical, not overhead.
- Acquisition rows (2026-10-06, `ttl_motion_enabled: False`): rows with different illumination arms and lasers switch correctly.
- Stage x/f display and focus fixed by putting `stage_assignment` in card order (2026-10-06).
- Right galvo scans after the `galvo_r_amplitude` fix (2026-10-06).
- `acq_track_plc_pointer` (2026-10-06): 25-plane row ~0.78 -> ~0.67 s per plane, 1.29 -> 1.49 fps. Laser blanking intact, all frames saved. Acquisition now matches live (~0.66 s per frame). One timing line per plane.
- Hardware laser gating (`laser_gate_with_expose_out` + `laser_blanking = 'stack'`, on in the user's config; and2 cells 8/9/11/16 = enable AND Expose-Out). Live 0.66 -> 0.43-0.44 s per frame (~2.1 fps), acquisition 0.67 -> 0.44 s per plane (2.08 fps). Per frame is now ~Expose-Out window (~0.39 s) + ~40 ms.
- TTL Z (2026-10-06, `stage_ttl_bnc: 1`, `ttl_motion_enabled`, `ttl_cards: (2,)`, `ttl_axis_masks: {2: 1}`): 31 frames -> +310 um, +10 um per frame = the set step; theta and F unchanged (user). Frame time unchanged (~0.44 s). Each TTL step CORRUPTS a serial reply in flight (garbled bytes, ~1 per 2 frames); the Expose-Out poll now retries (returns None, never 0), the stage driver's poll just fails once.
- Daily use: `ttl_motion_enabled: False` (user decision 2026-10-06: TTL Z saves no time per frame here, but it is kept and needed for a future hardware-only loop). The other TTL keys stay in the config.
- v0.8 `etl_period_source: 'sweeptime'` (2026-10-06, Left, fluorescent solution): ETL ramp = sweeptime x max(ramp rising %, falling %), independent of exposure. 20 ms ASLM exposure: 0.25-0.26 s per frame, live 3.3-3.8 fps, acquisition 0.25-0.26 s per plane (2 rows, both arms, log 20261006-192438). At 200 ms (~2780 of 2960 rows expose at once) the ETL sweep still helps (swept-focus average: the waist passes each row once, so the FOV is in focus) but there is no rolling-slit rejection of the out-of-focus part; 20 ms adds that rejection and is ~1.7x faster. Do not call 200 ms "non-ASLM" (corrected after the user's observation). Measured sweep ~195-200 ms (predicted 213 ms with the 10.26 us line-time base).
- Galvo duty cycle is PERCENT in mesoSPIM (50 = triangle); the backend had treated it as a fraction, so the galvo always ran as a sawtooth. Fixed in v0.8 (40-60 % -> triangle); log line `ASI Tiger galvo (...)`; user: fine (2026-10-06).
- Right arm needs `etl_follow_ramp_direction: True` (Right 5/85-90 -> down ramp, negative SAA); confirmed in the fluorescent solution 2026-10-06, on in the user's config.
- Display skipping frames during acquisition is stock mesoSPIM (`camera_display_temporal_subsampling`, default 2), not a bug.

## Mock-only, still waiting for hardware confirmation
- v0.8: image quality on a structured sample; optional sweeptime tuning (~0.22 s to match the ~197 ms sweep; the user runs 0.270 s / 90 %, 243 ms).
- TTL Z extras: a second step size, multi-row, and images of a real sample (none available on 2026-10-06).
- Laser gating: optional scope check of laser BNC vs. Expose-Out (laser change during live is confirmed, log 20261006-150530).
- Slow stage-position polling during live (Core patch needs re-applying).
- ETL raw amplitude, ramp direction and limits, and the `PR` range query logging.
- Whether the ETL ramp period (200 ms) matches the roughly 190 ms sweep in the actual images.

## Open items and possible next steps
- The user is aligning lasers and setting galvo and ETL offsets and amplitudes now.
- Acquisition-rows testing on hardware is next. The user's config has `ttl_motion_enabled: False` for this (set 2026-10-06 at the user's request).
- **TTL motion (next after laser gating; the user chose "gating, then TTL Z").** With gating, a whole plane is ~0.44 s of which ~0.39 s is the Expose-Out window, and Z step + Core overhead is ~10 ms, so TTL Z alone is ~2%. Further speed has to come from the camera side (exposure, line delay/sweep). It matters as the step toward the autonomous loop. The current per-frame design needs `ttl_motion_enabled: False` (Core steps Z/F over serial each plane). Supporting TTL motion means driving the stage cards' TTL input from the PLC (for example from Expose-Out falling or the trigger cell) so Z steps in hardware, and letting Core skip `move_relative`. This touches the earlier zstack_chain and stage-TTL work. Not started.
- Set `frame_timing_log` to False once troubleshooting is done.
- Possible further per-frame trimming in live: the remaining laser DAC set and zero, and two pointer moves.

## Tools (`tools/`)
- Python env: mesoSPIM-control runs in `conda activate C:\Users\Public\mamba\envs\mesoSPIM-py312` (Python 3.12, has `serial`, PyQt5, `nidaqmx`). Use `C:/Users/Public/mamba/envs/mesoSPIM-py312/python.exe` for compiling and mock tests. `python` is not on PATH.
- Mock testing: no harness is saved in the repo (the session scratchpad had `mock_infra.py`, `mock_acq_rows.py` and `mock_gating.py`). Fake the serial port under the real `TigerController`, and stub the NI base class.
- `asi_tiger_camera_expose_out_probe.py`: pyvcam-only probe of Expose-Out and scan modes. The Internal Trigger run stops quickly, so it is not representative of mesoSPIM's Edge Trigger path.
- `asi_tiger_singleaxis_test.py` and the other bench scripts: see their docstrings.

## Known pitfalls (already fixed, do not reintroduce)
- A serial write hang. The fix is a finite write timeout and a 5 ms poll interval.
- Wiring severed by a PLC reset. Wiring persists now.
- The left/right switch DAC channel being zeroed. Only laser channels are zeroed.
- Pausing stage polling for non-ASI backends. Only ASI_Tiger pauses now.
- `asi_parameters['stage_assignment']` order. The ASI letters must read in card order (X, Y, Z, T, V). Stock `read_position()` matches the `W` reply by position, so another order swaps the displayed axes and the saved row markers. The user's config is now `{'f':'X', 'x':'Y', 'z':'Z', 'theta':'T', 'y':'V'}` (2026-10-06).
