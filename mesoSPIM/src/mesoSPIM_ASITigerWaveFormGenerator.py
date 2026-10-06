"""
mesoSPIM_ASITigerWaveFormGenerator
====================================
A drop-in alternative to mesoSPIM_WaveFormGenerator (NI-DAQmx) backed by
the asi_tiger library (ASI Tiger DAC/PLC/ETL cards) instead of an NI
card.

DESIGN: subclasses mesoSPIM_WaveFormGenerator and overrides ONLY the 6
hardware I/O methods (create_tasks, write_waveforms_to_tasks,
start_tasks, run_tasks, stop_tasks, close_tasks) plus config_check --
mirroring exactly how mesoSPIM_DemoWaveFormGenerator is structured in
mesoSPIM_WaveFormGenerator.py.

======================================================================
ARCHITECTURE NOTE -- READ BEFORE TRUSTING THIS FOR REAL ACQUISITIONS
======================================================================
TRUE PER-FRAME TRIGGERING, matching the original NI design's
granularity exactly -- NOT the autonomous, hardware-driven multi-plane
loop this project built and validated (zstack_chain, PATTERN_SAWTOOTH
ETL sync, the pulse-pass-through counter, all still fully working,
still hardware-confirmed at n_planes=3 and 300). That capability is
DELIBERATELY POSTPONED, not abandoned -- see "WHY NOT zstack_chain, FOR
NOW" below.

Each run_tasks() call here does real work: fires one camera trigger
pulse, waits for that one exposure to start and finish (polling the
camera's real Expose-Out signal), and returns. Z (and F) stepping is
NOT this file's job at all -- mesoSPIM's own existing, already-working
per-frame host-stepping loop (run_acquisition()'s move_relative() call
each iteration, via the existing ASI stage driver) handles it, entirely
unchanged. This REQUIRES ttl_movement_enabled_during_acq to be FALSE
for rows run with this backend -- the opposite of what an earlier
version of this file required. config_check() warns if
asi_parameters['ttl_motion_enabled'] is set, since that would disable
mesoSPIM's own per-frame stepping loop that this design depends on.

WHY NOT zstack_chain, FOR NOW: run_tasks() blocking for an entire row
(zstack_chain's actual design) is fundamentally incompatible with how
mesoSPIM retrieves frames. Confirmed directly from mesoSPIM_Camera.py:
add_images_to_series() -> self.camera.get_images_in_series() ->
(for the Photometrics camera this project targets)
self.pvcam.poll_frame(), which pulls from the camera's OWN hardware
buffer. That buffer is FIXED SIZE and, per PyVCAM's own source
(pvcmodule.cpp's NewFrameHandler -- confirmed directly, not assumed),
pops and silently DISCARDS the oldest unread frame once full ("Lost
frame"), with no exception or signal the Python side can catch.
mesoSPIM_Camera.py's initialize_image_series() calls
self.pvcam.start_live() with no explicit buffer size, so it's using
PyVCAM's small, live-view-oriented default -- not sized to hold a
whole row's frames. If run_tasks() blocks for the WHOLE stack before
any poll_frame() call happens (zstack_chain's actual behavior), frames
captured early in a row risk being silently overwritten before
mesoSPIM's per-frame loop ever gets a chance to retrieve them. The
Iris 15's real buffer depth via PVCAMTest looks like roughly 50 frames
(reported, not yet independently confirmed here) -- not a niche
"very large stacks" concern, a real limit on ordinary row sizes.

The fix for THAT, if the autonomous design is revisited later: either
size PyVCAM's buffer to the row's real frame count (touches
mesoSPIM_Camera.py, not just this file) or chunk zstack_chain's kicks
to the camera's actual buffer depth, letting mesoSPIM's per-frame loop
drain each chunk before kicking the next. Neither is implemented here.
True per-frame triggering sidesteps the whole problem: each frame is
polled immediately after being triggered, exactly matching what
mesoSPIM's frame-grabbing loop already expects, with no buffer
accumulation risk at all -- at the cost of losing the "no host
overhead during the fast per-frame loop" benefit that motivated
zstack_chain in the first place. That trade was made deliberately, to
get a working mesoSPIM-control + ASI Tiger integration built and
running first; zstack_chain.py itself is untouched and still fully
available once it's worth revisiting.

CONFIRMED FROM THE REAL mesoSPIM_Core.py, mesoSPIM_Stages.py,
asicontrol.py, utils/acquisitions.py, and mesoSPIM_Camera.py (fetched
and read directly, or user-supplied -- not assumed or inferred),
findings that still apply to THIS design:

  Call pattern for a ROW/ACQUISITION SERIES: prepare_acquisition() calls
  prepare_image_series() -- create_tasks() + write_waveforms_to_tasks(),
  once per row. run_acquisition()'s `for i in range(steps)` loop then
  calls snap_image_in_series() EACH iteration (laser enable ->
  start_tasks() -> run_tasks() -> stop_tasks() -> laser disable) --
  once per frame. Every run_tasks() call in THIS design does real,
  per-frame work (no "first call does everything, rest no-op" pattern
  -- that was specific to the zstack_chain design).

  CORRECTED (re-verified directly against the real mesoSPIM_Core.py --
  an earlier version of this note said close_tasks() runs "ONCE, at
  the end" of an acquisition list; that undersold how often it's
  actually called): close_acquisition() -> close_image_series() ->
  close_tasks() runs ONCE PER ROW, inside run_acquisition_list()'s
  `for acq in acq_list:` loop -- not once for a whole multi-row list.
  SEPARATELY, and even more frequently: snap_image() (used by BOTH
  snap() and live()'s per-frame loop) calls ALL SIX methods --
  create_tasks() THROUGH close_tasks() -- every single call, i.e.
  every single live-preview frame. See close_tasks()'s own docstring
  for the real consequence this had (an earlier version nulled all of
  this class's device handles in close_tasks(), which combined with
  this call frequency would have forced a full hardware re-setup, and
  broken the free-running galvo's continuity, on every live frame and
  every acquisition row -- fixed before ever reaching real hardware,
  by reading the real source instead of assuming the call pattern from
  the per-frame-series path alone). create_tasks()'s own
  `if self._tiger is not None: return` guard is what makes setup
  actually happen once, for the whole life of this backend instance,
  regardless of which of these paths is calling it.

  Laser enable/disable is NOT this class's job -- run_acquisition()
  calls self.laserenabler.enable(laser)/disable_all() on a SEPARATE
  object mesoSPIM_Core instantiates independently
  (mesoSPIM_LaserEnabler/Demo_LaserEnabler), the same pattern as
  NI_Shutter/Demo_Shutter for shutters. This file never touches it.

  Z's row-starting position is NOT this class's job either --
  prepare_acquisition() already moves Z (and F) to the row's start via
  self.serial_worker.stage (the existing stage driver) BEFORE it calls
  prepare_image_series(). With ttl_movement_enabled_during_acq=False
  (what THIS design requires), that SAME existing stage driver also
  handles every per-frame Z/F step during run_acquisition(), via its
  own already-working move_relative() call each iteration -- nothing
  in this file needs to touch Z or F at all, for the row-start move OR
  the per-frame stepping.

  Laser intensity/selection: self.state['laser'] is the laser NAME
  (matches cfg.laserdict's keys), self.state['intensity'] is 0-100%
  (confirmed from set_intensity()'s docstring), converted to volts via
  the already-validated max_laser_voltage. laser_dac_channels is
  indexed the same order as cfg.laserdict's keys.

======================================================================
GALVO + PER-ARM ETL -- added after the per-frame design above was
already working end-to-end without them (see PATCHNOTES_ASI_TIGER.md).
======================================================================
Confirmed by the user directly (not assumed): the rack has TWO
physically separate illumination arms, each with its OWN galvo scan
mirror AND its own ETL -- not one shared pair switched optically. This
matches the ORIGINAL config_asi_tiger_example.py's galvo_l/galvo_r
(card 37, axes A/C) and etl_l/etl_r (card 34, axes H/J) channel
mapping from earlier bench work, and corrects a real bug this file had
until now: write_waveforms_to_tasks() was driving a SINGLE shared ETL
axis (ah["etl_card_addr"]/ah["etl_axis"]) regardless of `side`, reading
side-specific self.state keys (etl_l_amplitude vs etl_r_amplitude) but
sending them to the same physical axis either way -- correct for
amplitude/offset math, silently wrong for which physical lens actually
moved. Both ETL and galvo now use genuinely separate per-side
SingleAxisWaveform objects.

Galvo state keys (confirmed from mesoSPIM_WaveFormGenerator.py's real
source, fetched directly): self.state['galvo_l_frequency'/'_amplitude'/
'_offset'/'_duty_cycle'/'_phase'] and the 'galvo_r_*' equivalents.
'_phase' is NOT used here -- this hardware waveform generator has no
phase-offset concept relative to anything else (galvo runs free,
un-triggered, on its own internal clock -- there's nothing to phase
against). '_duty_cycle' has no exact hardware equivalent either: the
card offers fixed PATTERN_TRIANGLE/PATTERN_SAWTOOTH/etc. shapes, not a
continuously-variable duty cycle. Approximated as PATTERN_TRIANGLE for
duty_cycle in [0.4, 0.6] (a symmetric back-and-forth scan, matching
asi_tiger_galvo_etl_demo.py's own default and rationale -- smoother, no
flyback discontinuity) and PATTERN_SAWTOOTH otherwise. NOT yet
confirmed on real hardware that this approximation is visually/
optically acceptable for every duty_cycle mesoSPIM's GUI allows --
worth checking if a strongly asymmetric ramp is ever actually used.

Galvo is FREE-RUNNING (SAM=1 via start()), matching ASI's own confirmed
architecture ("the idea is to use single-axis function on both the
galvo and ETL DAC cards, where the galvo is free-running, and the etl
sawtooth wave is triggered") and asi_tiger_galvo_etl_demo.py's
bench-confirmed recipe -- started once per row in
write_waveforms_to_tasks(), left running for the whole row (not
per-frame -- there is no per-frame galvo action, unlike the camera
trigger/ETL sweep), stopped once in close_tasks(). Re-configuring and
restarting EVERY row (not just once, ever) is deliberate -- the user's
own requirement, since amplitude/offset/frequency/side can all differ
row to row.

Galvo safety: mirrors asi_tiger_galvo_etl_demo.py's own pre-flight
check (ASI's own limit: "Limit command voltage to +/-10.00V to
guarantee galvo amplifier safety") -- amplitude/2 + abs(offset) is
checked against asi_dac_parameters['galvo_max_volts'] (default 10.0V)
BEFORE calling configure()/start(); if exceeded, logs an error and
leaves that row's galvo un-driven (stopped, zeroed) rather than
commanding an out-of-range voltage. Unlike the DAC's own DacChannel
(ASITigerDAC), SingleAxisWaveform.configure() has NO built-in
safety_limit_mv/max_step_v clamping of its own -- checked directly in
singleaxis.py's source -- so this check is this file's own
responsibility, not inherited protection from the library.

Whichever side is NOT active for a given row has its galvo AND ETL
explicitly stopped_and_zeroed at the START of write_waveforms_to_tasks()
-- necessary because rows can alternate sides, and an axis armed/
running for a PREVIOUS row's opposite side would otherwise be left
running (galvo) or armed-and-triggerable (ETL) into the new row.

Everything else (connection sharing, config validation, the
close_tasks() PLC-safety note) follows established, straightforward
patterns and carries less risk than the above.
======================================================================

Config: expects a `cfg.asi_dac_parameters` dict -- see
config_asi_tiger_example.py in mesoSPIM/config/examples/.
"""

import logging
import time
from typing import Optional

import serial

from .mesoSPIM_WaveFormGenerator import mesoSPIM_WaveFormGenerator

from .devices.asi_tiger import (
    TigerController, ASITigerDAC, PLCCard, SingleAxisWaveform,
    PATTERN_SAWTOOTH, PATTERN_TRIANGLE, enable_backplane_trigger_mode,
    axis_slot_index, trigger_in_backplane_addr,
    IO_TYPE_INPUT, IO_TYPE_PUSH_PULL_OUTPUT, cell_addr, bnc_addr,
    configure_lr_switch,
)

logger = logging.getLogger(__name__)


def _state_get(state, key, default=None):
    """self.state on real hardware is mesoSPIM_StateSingleton
    (mesoSPIM_State.py), which ONLY implements __getitem__/__setitem__
    -- NOT dict's .get() -- confirmed directly from its source.
    REAL-HARDWARE BUG, caught by the user: every self.state.get(...)
    call in this file crashed with "'mesoSPIM_StateSingleton' object
    has no attribute 'get'" the first time write_waveforms_to_tasks()
    ran. This went completely unnoticed in mock testing because the
    mock harness's FakeState subclassed dict (which DOES have .get()),
    silently matching the wrong interface. Fixed by routing every
    state read through this helper instead, which works for both: the
    real singleton (via __getitem__, catching the KeyError its own
    mutexed access raises for a missing key) and a plain dict mock
    (whose subscript access behaves identically). In practice every
    key used in this file is always pre-populated by
    mesoSPIM_StateSingleton.__init__()'s own hardcoded defaults, so
    `default` here is a belt-and-suspenders fallback, not something
    expected to be hit on real hardware."""
    try:
        return state[key]
    except KeyError:
        return default


class mesoSPIM_ASITigerWaveFormGenerator(mesoSPIM_WaveFormGenerator):
    """See module docstring -- true per-frame triggering, matching the
    original NI design's granularity. Z/F stepping is NOT handled here
    -- requires ttl_movement_enabled_during_acq=False so mesoSPIM's own
    existing per-frame host-stepping loop does it instead."""

    def __init__(self, parent):
        self._tiger: Optional[TigerController] = None
        self._owns_tiger_connection = False
        self._plc: Optional[PLCCard] = None
        self._dac: Optional[ASITigerDAC] = None
        self._etl_l: Optional[SingleAxisWaveform] = None
        self._etl_r: Optional[SingleAxisWaveform] = None
        self._galvo_l: Optional[SingleAxisWaveform] = None
        self._galvo_r: Optional[SingleAxisWaveform] = None
        self._lr_switch = None  # LRSwitch
        self._live_mode = False      # set by begin_live()/end_live() (see mesoSPIM_Core patch)
        self._expose_width_warned = False
        self._frame_expose_high_width_s = None
        self._live_hold_dac = False  # live-only options, read from config in begin_live()
        self._live_dac_levels = {}   # laser DAC channel -> volts last written during live
        self._armed_key = None       # settings the ETL/galvo are currently armed/running with (live mode only)

        self._camera_trigger_cell = None
        self._camera_expose_bnc = None

        super().__init__(parent)  # base __init__ calls self.config_check() at the end

    # ------------------------------------------------------------------
    # Config validation (overrides the NI-specific version)
    # ------------------------------------------------------------------
    def config_check(self):
        """ASI equivalent of the base class's config_check(). Validates
        cfg.asi_dac_parameters instead of cfg.acquisition_hardware."""
        assert hasattr(self.cfg, "asi_dac_parameters"), (
            "Config file must define 'asi_dac_parameters' when "
            "waveformgeneration == 'ASI_Tiger'. See config_asi_tiger_example.py."
        )
        ah = self.cfg.asi_dac_parameters
        required = (
            "port", "baudrate", "plc_card_addr",
            "etl_card_addr", "etl_card_first_axis", "etl_l_axis", "etl_r_axis",
            "galvo_card_addr", "galvo_l_axis", "galvo_r_axis",
            "camera_expose_bnc", "camera_trigger_bnc",
            "laser_dac_channels",
        )
        for key in required:
            if key not in ah:
                raise ValueError(f"Config file: 'asi_dac_parameters' must contain {key!r}")
        # NOT z_card_addr/z_axis/z_axis_mask/z_in0_bnc/z_out0_bnc/laser_bncs here --
        # this design touches neither Z (the existing stage driver's own per-frame
        # stepping handles it) nor laser enable (mesoSPIM Core's own job).
        #
        # etl_l_axis/etl_r_axis and galvo_l_axis/galvo_r_axis, NOT a single shared
        # etl_axis/galvo_axis -- confirmed directly by the user: this rack has TWO
        # physically separate illumination arms, each with its own galvo mirror and
        # ETL, not one pair shared/switched optically. Assumes both L/R axes of each
        # pair share ONE card (etl_card_addr / galvo_card_addr) -- true for the
        # historical bench config (etl_l/etl_r on card 34's H/J, galvo_l/galvo_r on
        # card 37's A/C) -- if your rack instead has them on DIFFERENT cards, this
        # config shape and create_tasks()/close_tasks() below would need a
        # galvo_l_card_addr/galvo_r_card_addr split too; not implemented, since
        # nothing so far has indicated that's needed.
        if len(ah["laser_dac_channels"]) != len(self.cfg.laserdict):
            raise ValueError(
                f"Config file: len('asi_dac_parameters[\"laser_dac_channels\"]') "
                f"({len(ah['laser_dac_channels'])}) must equal num(lasers) in 'laserdict' "
                f"({len(self.cfg.laserdict)})."
            )

        if self.state["max_laser_voltage"] > 10:
            self.state["max_laser_voltage"] = 10
            msg = f"Config parameter 'max_laser_voltage' ({self.state['max_laser_voltage']}) is > 10V. Setting to 10V."
            print(msg); logger.warning(msg)
        elif self.state["max_laser_voltage"] > 5:
            msg = f"Config parameter 'max_laser_voltage' ({self.state['max_laser_voltage']}) is > 5V, may damage the laser controller."
            print(msg); logger.warning(msg)

        ttl_enabled_in_cfg = False
        try:
            ttl_enabled_in_cfg = bool(self.cfg.asi_parameters.get("ttl_motion_enabled", False))
        except AttributeError:
            pass
        if ttl_enabled_in_cfg:
            logger.error(
                "ASI Tiger backend (per-frame design): asi_parameters['ttl_motion_enabled'] is "
                "True, but this design REQUIRES ttl_movement_enabled_during_acq to be False -- "
                "it depends on mesoSPIM's own existing per-frame host-stepping loop for Z/F, "
                "which that flag disables. Set ttl_motion_enabled to False, or this row's Z/F "
                "position will not track the acquisition at all."
            )

        logger.warning(
            "ASI Tiger backend: true per-frame triggering (see this module's ARCHITECTURE "
            "NOTE) -- each run_tasks() call fires one camera trigger and waits for that one "
            "exposure. Z/F stepping is handled entirely by mesoSPIM's existing stage driver, "
            "not this file -- requires ttl_motion_enabled=False in asi_parameters."
        )

    # ------------------------------------------------------------------
    # Shared-connection lookup (unchanged from earlier drafts -- avoids
    # opening a second serial handle on the same COM port as mesoSPIM's
    # own ASI stage driver, if present)
    # ------------------------------------------------------------------
    def _get_shared_tiger_connection(self) -> Optional[TigerController]:
        """See asi_tiger.controller.TigerController.from_open_serial for
        why sharing matters. Returns None (create_tasks() opens its own
        connection instead) if mesoSPIM's stage isn't ASI, or is on a
        different COM port than asi_dac_parameters['port'].

        REAL-HARDWARE BUG, caught by the user: merely reusing the
        connection OBJECT isn't enough -- see from_open_serial()'s
        updated docstring. This now also looks for a `serial_lock`
        attribute on the stage driver (asi_stages) and passes it
        through, so our commands and the stage driver's own
        concurrent I/O (position polling, moves) actually serialize
        against EACH OTHER, not just against themselves. Requires the
        matching asicontrol.py patch (see
        mesoSPIM_Core_patch_reference/asicontrol.py.diff) -- if
        asi_stages has no `serial_lock` (an unpatched/older
        StageControlASI), this falls back to the old, race-prone
        behavior and logs a loud warning, rather than silently being
        unsafe."""
        try:
            stage = self.parent.serial_worker.stage
            asi_stages = getattr(stage, "asi_stages", None)
            if asi_stages is None:
                return None
            ser = asi_stages.asi_connection
            ah = self.cfg.asi_dac_parameters
            if ser.port != ah["port"]:
                logger.info(
                    f"ASI stage is on {ser.port}, asi_dac_parameters['port'] is "
                    f"{ah['port']} -- different ports, opening a separate connection."
                )
                return None
            shared_lock = getattr(asi_stages, "serial_lock", None)
            if shared_lock is None:
                logger.error(
                    "ASI Tiger: sharing the stage's serial connection, but it has no "
                    "'serial_lock' attribute -- StageControlASI (asicontrol.py) needs the "
                    "serial_lock patch (see PATCHNOTES_ASI_TIGER.md) for this sharing to be "
                    "safe. Proceeding WITHOUT cross-driver locking -- this WILL intermittently "
                    "corrupt replies on either side if stage position polling or moves happen "
                    "concurrently with ASI Tiger triggered I/O (confirmed on real hardware)."
                )
            return TigerController.from_open_serial(ser, lock=shared_lock)
        except AttributeError:
            return None

    # ------------------------------------------------------------------
    # The 6 overridden hardware I/O methods
    # ------------------------------------------------------------------
    def create_tasks(self):
        """Opens (or reuses) the Tiger connection and instantiates the
        persistent device wrappers, INCLUDING the manual camera-trigger
        cell and the camera-expose -> ETL-sync wiring this per-frame
        design needs. Safe to call multiple times -- only does real
        work the first time, for the whole life of this backend
        instance (this is load-bearing, not just an optimization --
        see close_tasks()'s docstring: it's called far more often than
        "once at the end", including once per live-preview frame, and
        deliberately never undoes this method's setup)."""
        if self._tiger is not None:
            return

        ah = self.cfg.asi_dac_parameters
        shared = self._get_shared_tiger_connection()
        if shared is not None:
            self._tiger = shared
            self._owns_tiger_connection = False
            logger.info("ASI Tiger: reusing the ASI stage driver's serial connection.")
        else:
            self._tiger = TigerController(ah["port"], baudrate=ah["baudrate"])
            self._tiger.connect()
            self._owns_tiger_connection = True
            logger.info(f"ASI Tiger: opened its own connection on {ah['port']}.")

        self._plc = PLCCard(self._tiger, card_addr=ah["plc_card_addr"], axis=ah.get("plc_axis", "E"))
        self._plc.clear_state()

        self._dac = ASITigerDAC(tiger=self._tiger)
        for ch in ah["laser_dac_channels"]:
            self._dac.add_channel(**ch)
        self._dac.zero_all()

        # Per-arm ETL: two separate axes on (by assumption -- see config_check()'s
        # docstring note) the same card. Both stopped/zeroed defensively at setup;
        # only the active side's axis gets configured/armed per row, in
        # write_waveforms_to_tasks().
        self._etl_l = SingleAxisWaveform(self._tiger, card_addr=ah["etl_card_addr"], axis=ah["etl_l_axis"])
        self._etl_r = SingleAxisWaveform(self._tiger, card_addr=ah["etl_card_addr"], axis=ah["etl_r_axis"])
        self._etl_l.stop_and_zero()
        self._etl_r.stop_and_zero()
        # Read-only, one-time: log what the ETL axes' output range really is (`PR <axis>?`
        # -- the same query used for the laser/L-R-switch range work). The raw reply is
        # only LOGGED, never parsed or enforced; use it to decide on etl_min/max_volts.
        for _ax in (ah["etl_l_axis"], ah["etl_r_axis"]):
            try:
                _reply = self._tiger.send_command(f"PR {_ax}?", card_addr=ah["etl_card_addr"])
                logger.info(f"ASI Tiger ETL range: card {ah['etl_card_addr']} `PR {_ax}?` -> {_reply!r}")
            except Exception as exc:
                logger.warning(f"ASI Tiger ETL range: `PR {_ax}?` failed ({exc}); continuing.")
        enable_backplane_trigger_mode(self._tiger, card_addr=ah["etl_card_addr"])

        # Per-arm galvo: free-running (ASI's own confirmed architecture -- see
        # this module's ARCHITECTURE NOTE), two separate axes, same assumption as
        # the ETL pair above. Both stopped/zeroed here; started per row in
        # write_waveforms_to_tasks(), since amplitude/offset/frequency/side can
        # all change row to row (per the user's own requirement).
        galvo_units_per_volt = ah.get("galvo_units_per_volt", 1000.0)
        self._galvo_l = SingleAxisWaveform(self._tiger, card_addr=ah["galvo_card_addr"],
                                            axis=ah["galvo_l_axis"], units_per_volt=galvo_units_per_volt)
        self._galvo_r = SingleAxisWaveform(self._tiger, card_addr=ah["galvo_card_addr"],
                                            axis=ah["galvo_r_axis"], units_per_volt=galvo_units_per_volt)
        self._galvo_l.stop_and_zero()
        self._galvo_r.stop_and_zero()

        if ah.get("lr_switch_card_addr") is not None:
            self._lr_switch = configure_lr_switch(
                self._dac, card_addr=ah["lr_switch_card_addr"], axis=ah["lr_switch_axis"],
                left_v=ah.get("lr_switch_left_v", 0.0), right_v=ah.get("lr_switch_right_v", 5.0),
                range_code=ah.get("lr_switch_range_code", 2),
            )

        # Manual camera-trigger cell: the same confirmed-on-real-hardware pattern as
        # tools/asi_tiger_camera_trigger_test.py -- a manually-toggled D-flop cell
        # driving camera_trigger_bnc as a push-pull output.
        self._camera_trigger_cell = ah.get("camera_trigger_cell", 10)
        self._plc.configure_cell(self._camera_trigger_cell, "d_flop", inputs={"a": 0, "b": 0, "c": 0})
        self._plc.set_cell_state(self._camera_trigger_cell, False)
        self._plc.configure_io(bnc_addr(ah["camera_trigger_bnc"]), IO_TYPE_PUSH_PULL_OUTPUT,
                                source_addr=cell_addr(self._camera_trigger_cell))

        # Camera Expose-Out readback + ETL sync: the same confirmed-on-real-hardware
        # wiring zstack_chain.py uses internally (raw level, not a PLC-computed edge --
        # see that module's docstring for why), extracted here since this design needs
        # it WITHOUT the rest of zstack_chain's Z/counter machinery.
        # Wired to BOTH ETL axes' backplane trigger-in, once, here -- not
        # reconfigured per row when the active side changes. Only the active
        # side's axis is actually ARMED for external trigger (in
        # write_waveforms_to_tasks()); the inactive side stays stopped/zeroed, so
        # it doesn't respond even though its trigger-in line is live. This avoids
        # reconfiguring PLC I/O mid-session on every row (see PATCHNOTES_ASI_TIGER.md
        # for why that reconfiguration path -- CCA Y/CCA Z ordering, revert-to-input
        # floating -- turned out to be worth avoiding wherever a one-time setup
        # can do the job instead).
        self._camera_expose_bnc = ah["camera_expose_bnc"]
        expose_addr = bnc_addr(self._camera_expose_bnc)
        self._plc.configure_io(expose_addr, IO_TYPE_INPUT)
        for etl_axis_letter in (ah["etl_l_axis"], ah["etl_r_axis"]):
            etl_slot = axis_slot_index(etl_axis_letter, ah["etl_card_first_axis"])
            etl_trigger_addr = trigger_in_backplane_addr(etl_slot)
            self._plc.configure_io(etl_trigger_addr, IO_TYPE_PUSH_PULL_OUTPUT, source_addr=expose_addr)

    def _etl_period_ms(self, ah) -> float:
        """
        The ETL's SAM=2 waveform period, in ms.

        Camera Expose-Out must be in "Any Row" mode
        (camera_parameters['exp_out_mode'] = 2) -- HARDWARE-CONFIRMED (log
        e977f8da): Expose-Out rises ~8 ms after the trigger, i.e. at the
        start of the rolling-shutter sweep, and stays high for ~exposure +
        sweep (~380-418 ms at a 200 ms exposure). SAM=2 starts the ETL
        ramp on that rising edge, so the ramp begins with the sweep. (An
        earlier "All Rows" setting gave only a ~20 ms pulse near the END
        of the exposure -- ETL started late; that was a mis-set config.)

        The period is derived from self.state['camera_exposure_time'] (the
        same key run_tasks() uses for its Expose-Out-low timeout) minus
        asi_dac_parameters.get('etl_period_margin_ms', 0.0), so it tracks
        the exposure setting (which can vary row to row by channel) instead
        of a hand-tuned constant. NOT the Expose-Out width: under Any Row
        that is ~2x the exposure and would not match the ramp to the sweep.
        Whether exposure-length (200 ms) actually matches the ~190 ms
        sweep in the images is NOT yet bench-confirmed.

        CONFIRMED ON REAL HARDWARE, earlier: the SAM=2 period must be
        SHORTER than the camera's trigger interval, or the axis misses
        every other trigger edge. With per-frame triggering the interval
        is ~0.66 s at 200 ms exposure, far longer than the period, so the
        default margin is 0.0 ms. The key stays as an escape hatch --
        set asi_dac_parameters['etl_period_margin_ms'] if a rack or
        readout mode needs it.

        asi_dac_parameters['etl_period_ms'], if explicitly set, OVERRIDES
        this derivation entirely (manual escape hatch, e.g. for a fixed
        value independent of exposure time) -- the default (unset) is to
        auto-derive as described above.
        """
        explicit = ah.get("etl_period_ms")
        if explicit is not None:
            return explicit
        margin_ms = ah.get("etl_period_margin_ms", 0.0)
        exposure_s = _state_get(self.state, "camera_exposure_time", 0.5)
        period_ms = exposure_s * 1000.0 - margin_ms
        if period_ms < 2.0:
            logger.error(
                f"ASI Tiger ETL: derived period ({period_ms:.1f}ms, from camera_exposure_time="
                f"{exposure_s}s minus etl_period_margin_ms={margin_ms}ms) is too short to be "
                f"meaningful (1ms is documented as undefined behavior for this firmware) -- "
                f"clamping to 2ms. Check the real exposure time and/or etl_period_margin_ms."
            )
            period_ms = 2.0
        return period_ms

    def write_waveforms_to_tasks(self):
        """
        Configures this row's specifics -- ETL amplitude/offset, laser
        intensity, L/R side. Runs once per row (mesoSPIM's own call
        pattern guarantees this, confirmed from mesoSPIM_Core.py -- see
        ARCHITECTURE NOTE). Does NOT touch Z/F or laser enable/disable
        -- both confirmed to be someone else's job, see ARCHITECTURE
        NOTE.

        Reads ETL amplitude/offset from self.state (populated by the
        INHERITED create_waveforms()/create_etl_waveforms(), unchanged
        from the base class) rather than computing them independently,
        so any calibration/adjustment logic living in the base class's
        waveform math is correctly reflected here rather than silently
        bypassed.

        CONFIGURES AND ARMS the row's ACTIVE-side ETL here, ONCE per row
        -- NOT per frame (an earlier version of this file armed in
        start_tasks() and stopped/zeroed in stop_tasks(), every frame).
        CONFIRMED ON REAL HARDWARE this was a real bug: external-trigger
        arming (SAM=2-style) auto-rearms on every subsequent trigger
        edge on its own -- already confirmed earlier in this project
        ("SAM=2 DOES auto-rearm"), so re-arming every frame was both
        unnecessary and a real source of the intermittent "Expose-Out
        never went high" failures seen on real hardware -- re-arming
        via a serial command immediately before firing the next trigger
        pulse creates exactly the kind of race that would explain
        failures on a random frame, not a fixed one. Armed once here,
        left armed for the whole row, disarmed once in close_tasks().

        ALSO configures and starts the row's ACTIVE-side galvo (free-
        running -- see this module's ARCHITECTURE NOTE), and explicitly
        quiesces whichever side is NOT active this row (both its ETL and
        its galvo), since a previous row may have left the opposite side
        armed/running.
        """
        ah = self.cfg.asi_dac_parameters
        side = _state_get(self.state, "shutterconfig", "Left")
        letter = "l" if side == "Left" else "r"
        other_letter = "r" if letter == "l" else "l"
        # Stock mesoSPIM drives BOTH galvos with galvo_l_amplitude ("always use same
        # amplitude for both galvos", mesoSPIM_WaveFormGenerator.create_galvo_waveforms);
        # there is no 'galvo_r_amplitude' state key, so reading it returned the 0.0
        # default and the Right galvo never scanned.
        galvo_amp_key = "galvo_l_amplitude"

        if not self._live_mode and self._plc is not None and ah.get("acq_track_plc_pointer", True):
            # Outside live (default ON, hardware-confirmed 2026-10-06: 25-plane row 0.78 -> 0.67 s
            # per plane, laser blanking intact): skip repeated `M E=` pointer moves (~103 ms each) for
            # this row/snap, until close_tasks() turns it off. Safe for the same reason as
            # in live: only this PLCCard (shared with ASITiger_LaserEnabler) addresses the
            # PLC axis; the stage driver's polling and Z/F moves use other axes.
            self._plc.forget_pointer()
            self._plc.track_pointer = True

        # Laser intensity: self.state['intensity'] is 0-100% (per mesoSPIM_Core.set_intensity()'s
        # own docstring), NOT a voltage -- convert using max_laser_voltage, already
        # validated/clamped in config_check(). laser_dac_channels is indexed the same
        # order as cfg.laserdict's keys -- confirm that ordering matches your config
        # (not independently verified here).
        laser_name = _state_get(self.state, "laser")
        laser_idx = list(self.cfg.laserdict.keys()).index(laser_name) if laser_name in self.cfg.laserdict else 0
        intensity_pct = _state_get(self.state, "intensity", 0)
        voltage = (intensity_pct / 100.0) * self.state["max_laser_voltage"]
        dac_ch = ah["laser_dac_channels"][laser_idx]["name"]
        if self._live_mode and self._live_hold_dac:
            # Live: hold the level between frames (the PLC enable line does the
            # blanking); write only on change, and zero a laser line we just left.
            try:
                for other, volts in list(self._live_dac_levels.items()):
                    if other != dac_ch and volts != 0.0:
                        self._dac.set_voltage(other, 0.0)
                        self._live_dac_levels[other] = 0.0
                if self._live_dac_levels.get(dac_ch) != voltage:
                    self._dac.set_voltage(dac_ch, voltage)
                    self._live_dac_levels[dac_ch] = voltage
            except BaseException:
                self._live_dac_levels.clear()  # unknown -> rewrite next frame
                raise
        else:
            self._dac.set_voltage(dac_ch, voltage)

        if self._lr_switch is not None:
            self._lr_switch.select(is_right=(side == "Right"))

        # LIVE MODE ONLY: if the ETL + galvo are already armed/running with exactly
        # these settings (previous live frame), leave them alone. Re-stopping,
        # re-zeroing and re-starting them every frame cost ~40 of ~60 serial commands
        # per frame AND visibly reset the light-sheet scan each frame. Any change to a
        # setting changes the key and falls through to a full reconfigure below.
        arm_key = (
            side,
            _state_get(self.state, f"etl_{letter}_amplitude", 0.0),
            _state_get(self.state, f"etl_{letter}_offset", 0.0),
            _state_get(self.state, f"etl_{letter}_ramp_rising_%", None),
            _state_get(self.state, f"etl_{letter}_ramp_falling_%", None),
            self._etl_period_ms(ah),
            _state_get(self.state, galvo_amp_key, 0.0),
            _state_get(self.state, f"galvo_{letter}_offset", 0.0),
            _state_get(self.state, f"galvo_{letter}_frequency", 100.0),
            _state_get(self.state, f"galvo_{letter}_duty_cycle", 0.5),
        )
        if self._live_mode and arm_key == self._armed_key:
            return
        self._armed_key = None  # until (re)armed successfully below

        # --- Quiesce the INACTIVE side first (galvo AND ETL) -- a previous row may
        # have left it armed/running for the opposite side. ---
        etl_by_letter = {"l": self._etl_l, "r": self._etl_r}
        galvo_by_letter = {"l": self._galvo_l, "r": self._galvo_r}
        etl_by_letter[other_letter].stop_and_zero()
        galvo_by_letter[other_letter].stop_and_zero()

        # --- ETL: configure + arm the ACTIVE side only ---
        active_etl = etl_by_letter[letter]
        etl_amp = _state_get(self.state, f"etl_{letter}_amplitude", 0.0)
        etl_off = _state_get(self.state, f"etl_{letter}_offset", 0.0)
        etl_period_ms = self._etl_period_ms(ah)
        # Amplitude is passed through RAW (the user's decision: each system gets its
        # numbers tuned anyway). Note for tuning: the Tiger's SAA is the TOTAL
        # peak-to-peak amplitude (ASI command:saa), whereas mesoSPIM's NI ETL ramp
        # swung offset +/- amplitude, so the same CSV number is a HALF-size sweep here.
        amp_hw = etl_amp
        # Optional: follow mesoSPIM's ramp DIRECTION. Its NI ramp rises over
        # ramp_rising_% and falls over ramp_falling_%; the usual Right-arm settings
        # (rise 5 / fall 85) are a DOWNWARD ramp. SAA's sign reverses the ramp
        # (documented; geometry of the reversed ramp not yet bench-confirmed here,
        # hence opt-in).
        direction = "up"
        if ah.get("etl_follow_ramp_direction", False):
            rise = _state_get(self.state, f"etl_{letter}_ramp_rising_%", None)
            fall = _state_get(self.state, f"etl_{letter}_ramp_falling_%", None)
            if rise is not None and fall is not None and fall > rise:
                amp_hw = -amp_hw
                direction = "down"
        etl_lo = etl_off - abs(amp_hw) / 2
        etl_hi = etl_off + abs(amp_hw) / 2
        # Voltage limits are OPT-IN: the ETL axes' real output range is a per-card PR
        # setting that this code has not read from the hardware (see the one-time
        # `PR <axis>?` log line written by create_tasks()), so no range is assumed.
        etl_min_v = ah.get("etl_min_volts")
        etl_max_v = ah.get("etl_max_volts")
        out_of_range = ((etl_min_v is not None and etl_lo < float(etl_min_v)) or
                        (etl_max_v is not None and etl_hi > float(etl_max_v)))
        if out_of_range:
            logger.error(
                f"ASI Tiger ETL ({side}): offset {etl_off:.3f} V with {abs(amp_hw):.3f} Vpp would "
                f"swing {etl_lo:.3f}..{etl_hi:.3f} V, outside your configured limits "
                f"[etl_min_volts={etl_min_v}, etl_max_volts={etl_max_v}]. "
                f"REFUSING to drive the ETL -- leaving it stopped and zeroed.")
            active_etl.stop_and_zero()
        else:
            active_etl.configure(pattern=PATTERN_SAWTOOTH, amplitude_v=amp_hw, offset_v=etl_off,
                                  period_ms=etl_period_ms, external_trigger=True)
            active_etl.arm_triggered(free_running=False)
            logger.info(f"ASI Tiger ETL ({side}): offset {etl_off:.3f} V, amplitude {abs(amp_hw):.3f} Vpp "
                        f"(raw from state) -> sweep {etl_lo:.3f}..{etl_hi:.3f} V, {direction} ramp, "
                        f"period {etl_period_ms:.0f} ms, armed on external trigger")

        # --- Galvo: configure + start (free-running) the ACTIVE side only ---
        active_galvo = galvo_by_letter[letter]
        galvo_amp = _state_get(self.state, galvo_amp_key, 0.0)
        galvo_off = _state_get(self.state, f"galvo_{letter}_offset", 0.0)
        galvo_freq = _state_get(self.state, f"galvo_{letter}_frequency", 100.0)
        galvo_duty = _state_get(self.state, f"galvo_{letter}_duty_cycle", 0.5)

        galvo_max_volts = ah.get("galvo_max_volts", 10.0)
        galvo_peak = abs(galvo_amp) / 2 + abs(galvo_off)
        if galvo_peak > galvo_max_volts:
            logger.error(
                f"ASI Tiger galvo ({side}): amplitude {galvo_amp}Vpp / offset {galvo_off}V gives a "
                f"peak of {galvo_peak:.3f}V, exceeding galvo_max_volts ({galvo_max_volts}V, ASI's own "
                f"amplifier safety limit). REFUSING to drive the galvo this row -- leaving it stopped "
                f"and zeroed. The light sheet will NOT scan until this row's galvo parameters are "
                f"within range."
            )
            return

        galvo_period_ms = 1000.0 / galvo_freq if galvo_freq > 0 else self._etl_period_ms(ah)
        # PATTERN_TRIANGLE/PATTERN_SQUARE require an even period in ms (see
        # asi_tiger_galvo_etl_demo.py) -- rounding here matches that script's own logic.
        galvo_pattern = PATTERN_TRIANGLE if 0.4 <= galvo_duty <= 0.6 else PATTERN_SAWTOOTH
        if galvo_pattern == PATTERN_TRIANGLE:
            galvo_period_ms = 2 * round(galvo_period_ms / 2)

        active_galvo.configure(pattern=galvo_pattern, amplitude_v=galvo_amp, offset_v=galvo_off,
                                period_ms=galvo_period_ms)
        active_galvo.start()
        if self._live_mode:
            self._armed_key = arm_key

    def start_tasks(self):
        """No-op for this design -- the ETL is armed once per row in
        write_waveforms_to_tasks(), not per frame. See that method's
        docstring for why (a real bug, confirmed on real hardware, in
        an earlier version that re-armed here every frame). Exists
        because the base class's call pattern expects this method."""
        pass

    def run_tasks(self):
        """
        Fires ONE camera trigger pulse and waits for that ONE exposure
        to start, then finish -- polling the camera's real Expose-Out
        signal via the PLC (read_bnc_inputs(), the same reliable
        mechanism used throughout this project for reading PLC/BNC
        state -- never shown the WHERE-during-active-triggering
        reliability issue, since this reads PLC logic/IO state, not an
        axis's busy status).

        Two-phase wait, not one: first for Expose-Out to go HIGH
        (confirms the camera actually started exposing, not just that
        the trigger pulse was sent), then for it to go LOW (confirms
        the exposure finished). Checking only for LOW immediately after
        triggering would risk reading the PRE-trigger idle state as
        "already done".

        POLL INTERVAL, revised after a real-hardware hang: this
        originally polled read_bnc_inputs() every 1ms, which is one
        full serial round-trip (write+flush+read) over the shared
        Tiger port every 1ms -- far faster than the controller can
        actually answer, and, confirmed from a hang_dump.txt, enough
        sustained command flood (on top of this backend's already
        heavier-than-NI traffic pattern) to eventually stall a write
        completely. controller.py's from_open_serial() now gives that
        write a finite timeout so it fails loudly instead of hanging
        forever, but the real fix is not flooding the port that hard
        in the first place. 5ms still resolves Expose-Out transitions
        well inside any real exposure time (typically tens to
        hundreds of ms) while cutting command volume 5x; each
        round-trip is also wrapped so one flaky/timed-out poll logs
        and keeps waiting instead of aborting the whole frame.
        """
        ah = self.cfg.asi_dac_parameters
        pulse_ms = ah.get("camera_trigger_pulse_ms", 10.0)
        expose_bit = 1 << (self._camera_expose_bnc - 1)
        t_run0 = time.perf_counter()
        self._frame_logged = False
        self._frame_expose_high_wait_s = None
        self._frame_expose_high_width_s = None
        t_high_seen = None
        poll_interval_s = ah.get("camera_expose_poll_interval_s", 0.005)

        self._plc.set_cell_state(self._camera_trigger_cell, True)
        time.sleep(pulse_ms / 1000.0)
        self._plc.set_cell_state(self._camera_trigger_cell, False)

        def _poll_expose_bit():
            """read_bnc_inputs(), swallowing a single timed-out/garbled
            round-trip so one bad poll doesn't abort the whole frame --
            the surrounding while-loop's own timeout is still what
            decides when to give up for real."""
            try:
                return self._plc.read_bnc_inputs()
            except (TimeoutError, serial.SerialTimeoutException) as exc:
                logger.error(f"ASI Tiger: read_bnc_inputs() poll failed ({exc}) -- "
                              f"retrying until this phase's own timeout.")
                return 0

        start_timeout_s = ah.get("camera_expose_start_timeout_s", 2.0)
        start = time.perf_counter()
        while time.perf_counter() - start < start_timeout_s:
            if _poll_expose_bit() & expose_bit:
                t_high_seen = time.perf_counter()
                self._frame_expose_high_wait_s = t_high_seen - start
                break
            time.sleep(poll_interval_s)
        else:
            logger.error(f"ASI Tiger: camera Expose-Out never went high within "
                          f"{start_timeout_s:.1f}s of triggering -- exposure may not have "
                          f"started. Proceeding anyway.")

        exposure_time_s = _state_get(self.state, "camera_exposure_time", 0.5)
        end_timeout_s = ah.get("camera_expose_end_timeout_margin_s", 2.0) + exposure_time_s
        start = time.perf_counter()
        while time.perf_counter() - start < end_timeout_s:
            if not (_poll_expose_bit() & expose_bit):
                t_low_seen = time.perf_counter()
                self._frame_run_tasks_s = t_low_seen - t_run0
                if t_high_seen is not None:
                    # Resolution is about one poll round-trip (~20-30 ms on this link).
                    self._frame_expose_high_width_s = t_low_seen - t_high_seen
                    self._check_expose_width(exposure_time_s)
                return
            time.sleep(poll_interval_s)
        self._frame_run_tasks_s = time.perf_counter() - t_run0
        logger.error(f"ASI Tiger: camera Expose-Out never went low within "
                      f"{end_timeout_s:.1f}s -- exposure may still be in progress. "
                      f"Proceeding anyway.")

    def _check_expose_width(self, exposure_time_s):
        """One-time (per live session / backend instance) WARNING when the measured
        Expose-Out high time is far shorter than the exposure. Found on the user's
        bench: with camera_parameters['exp_out_mode'] = 1 (All Rows) Expose-Out was
        only ~20 ms wide, because All Rows is high only while EVERY row exposes
        (~ exposure - sweep). The required mode is 2 (Any Row, ~ exposure + sweep;
        hardware-confirmed, log e977f8da). This backend (a) starts the ETL on
        Expose-Out's rising edge and (b) treats Expose-Out going low as 'exposure
        finished' (which also releases the laser blanking), so a short pulse means
        the ETL sweep and the end-of-exposure detection are not aligned with the
        real exposure."""
        ah = self.cfg.asi_dac_parameters
        width = getattr(self, "_frame_expose_high_width_s", None)
        if width is None or getattr(self, "_expose_width_warned", False):
            return
        if exposure_time_s < float(ah.get("expose_width_check_min_exposure_s", 0.1)):
            return  # too short to measure with ~one-poll (~25 ms) resolution
        frac = float(ah.get("expose_width_warn_fraction", 0.5))
        if width < frac * exposure_time_s:
            self._expose_width_warned = True
            msg = (f"ASI Tiger WARNING: Expose-Out was high for only ~{width*1000:.0f} ms (poll "
                   f"resolution ~25 ms) but the exposure is {exposure_time_s*1000:.0f} ms. This is NOT "
                   f"necessarily a wrong camera mode: in 'All Rows' mode (exp_out_mode=1) the signal is high "
                   f"only while EVERY row is exposing, i.e. about (exposure - rolling-shutter sweep time), and "
                   f"with a long camera_parameters['scan_line_delay'] (ASLM) the sweep can take most of the "
                   f"exposure, leaving a short pulse near its END. The ETL sweep (started by Expose-Out's "
                   f"rising edge) and the end-of-exposure/laser-blanking timing then start late / end early. "
                   f"Set exp_out_mode=2 (Any Row: high from the first row's start to the last row's end; "
                   f"the bench-confirmed setting for this backend). "
                   f"Shown once per live session; silence with asi_dac_parameters['expose_width_warn_fraction']=0.")
            logger.warning(msg)
            print(msg, flush=True)

    def stop_tasks(self):
        """No hardware action -- see start_tasks()'s docstring. The ETL
        stays armed for the whole row; close_tasks() stops/zeros it once,
        at the actual end. Writes the per-frame timing line: stop_tasks()
        runs once per frame on every path (snap/live and acquisition rows),
        whereas close_tasks() runs only once per ROW in an acquisition."""
        self._log_frame_timing()

    def _log_frame_timing(self):
        """One WARNING line per snap_image() cycle: how many serial
        commands it took, how long they spent on the wire, the slowest one,
        and how long run_tasks() waited for Expose-Out. Exists to show where
        a slow live() frame really goes. Silence with
        asi_dac_parameters['frame_timing_log'] = False."""
        if not self.cfg.asi_dac_parameters.get("frame_timing_log", True):
            return
        if self._tiger is None:
            return
        self._frame_logged = True
        n, wire_s, max_s, max_cmd = self._tiger.take_stats()
        now = time.perf_counter()
        wall = now - getattr(self, "_frame_prev_t", now)
        self._frame_prev_t = now
        hw = getattr(self, "_frame_expose_high_wait_s", None)
        hw_txt = "NEVER went high (timed out)" if hw is None else f"{hw*1000:.0f} ms"
        wd = getattr(self, "_frame_expose_high_width_s", None)
        if wd is not None:
            hw_txt += f", high for ~{wd*1000:.0f} ms (exposure {_state_get(self.state, 'camera_exposure_time', 0.0)*1000:.0f} ms)"
        msg = (
            f"ASI Tiger frame timing: {wall:.2f}s since last frame | {n} serial cmds, "
            f"{wire_s:.2f}s on wire (avg {1000*wire_s/max(n,1):.0f} ms, slowest "
            f"{max_s*1000:.0f} ms = {max_cmd!r}) | run_tasks {getattr(self, '_frame_run_tasks_s', 0.0):.2f}s, "
            f"Expose-Out high after: {hw_txt}"
        )
        logger.warning(msg)  # goes to mesoSPIM's log file (see mesoSPIM/log/)
        print(msg, flush=True)  # and the console, since mesoSPIM logs to a file only

    def _stop_axes(self):
        """Stop + zero both ETL axes and both galvo axes."""
        self._armed_key = None
        for name, axis in (("ETL L", self._etl_l), ("ETL R", self._etl_r),
                            ("galvo L", self._galvo_l), ("galvo R", self._galvo_r)):
            if axis is not None:
                try:
                    axis.stop_and_zero()
                except Exception as exc:
                    logger.error(f"ASI Tiger {name}: error stopping/zeroing on close: {exc}")

    def begin_live(self):
        """Called by mesoSPIM_Core.live() (see the Core patch) before its frame loop.
        While live, close_tasks() leaves the ETL/galvo running between frames and
        write_waveforms_to_tasks() only reconfigures them when a setting changes."""
        ah = getattr(self.cfg, "asi_dac_parameters", {})
        self._live_hold_dac = bool(ah.get("live_hold_laser_dac", True))
        track_ptr = bool(ah.get("live_track_plc_pointer", True))
        self._live_dac_levels = {}
        self._expose_width_warned = False
        self._live_mode = True
        self._armed_key = None
        # Start every live session from "unknown" PLC cell states / pointer so the
        # first frame re-establishes them for real (later frames skip redundant
        # writes and, if enabled, redundant pointer moves).
        if self._plc is not None:
            self._plc.invalidate_state_cache()
            self._plc.track_pointer = track_ptr
        logger.info(f"ASI Tiger live mode: hold laser DAC between frames={self._live_hold_dac}, "
                    f"track PLC pointer={track_ptr}")

    def end_live(self):
        """Called by mesoSPIM_Core.live() after its frame loop: back to the normal
        stop-everything-after-every-close behavior, and stop the ETL/galvo now."""
        was_holding = self._live_mode and self._live_hold_dac
        self._live_mode = False
        self._live_dac_levels = {}
        if self._plc is not None:
            self._plc.track_pointer = False
            self._plc.invalidate_state_cache()
        if was_holding and self._dac is not None:
            # live held the laser level between frames; put every laser channel
            # back to 0 V now (the L/R switch channel keeps its last side).
            try:
                for ch_name in self._dac.channel_names():
                    if self._lr_switch is not None and ch_name == self._lr_switch.channel_name:
                        continue
                    self._dac.set_voltage(ch_name, 0.0)
            except Exception as exc:
                logger.error(f"ASI Tiger DAC: error zeroing at end of live: {exc}")
        self._stop_axes()

    def safe_outputs(self):
        """Full teardown: reconfigure every PLC output this instance set up
        to constant-low. This DISCONNECTS the camera trigger / ETL sync /
        laser-enable wiring, so call it only when really done (e.g. a future
        app-exit hook); the next session must re-run create_tasks() setup."""
        if self._plc is not None:
            self._plc.safe_all_outputs()
            self._tiger = None  # force create_tasks() to rebuild wiring next time

    def close_tasks(self):
        """
        Returns all outputs to a safe idle state -- stops/zeros both ETL
        axes and both galvo axes, idles the camera-trigger cell, and zeros
        the DAC. (UPDATED: it no longer calls safe_all_outputs() -- that
        severed the PLC wiring after the first frame; see the comment in
        the body and PATCHNOTES_ASI_TIGER.md. Use safe_outputs() for a full
        teardown. The paragraphs below that describe safe_all_outputs() as
        part of close_tasks() predate this change.) Deliberately does NOT release/disconnect anything or reset
        any of this instance's device handles -- see the "REAL CALL
        FREQUENCY" note below for why that would be actively harmful
        here, unlike for the NI backend this class mirrors.

        REAL CALL FREQUENCY (confirmed by fetching the real
        mesoSPIM_Core.py directly, not assumed -- an earlier version of
        this docstring incorrectly assumed close_tasks() is called
        "once, at the actual end" of a whole acquisition list; it is
        NOT):
          - snap_image() (used by BOTH snap() and live()'s per-frame
            loop) calls create_tasks() -> write_waveforms_to_tasks() ->
            ... -> close_tasks(), ALL SIX METHODS, EVERY SINGLE CALL --
            once per snap, and once per frame of a live-preview loop.
          - close_acquisition() (-> close_image_series() ->
            close_tasks()) is called ONCE PER ROW inside
            run_acquisition_list()'s `for acq in acq_list:` loop, i.e.
            once per acquisition/row, NOT once at the end of a
            multi-row list.
        For the NI backend, this is fine: create_tasks()/close_tasks()
        build and tear down finite nidaqmx.Task() buffers every call by
        design (a fresh one-shot AO sweep has to be written before every
        trigger cycle regardless). This backend's hardware is the
        opposite: the Tiger connection, PLC cell config, and
        SingleAxisWaveform objects are meant to be set up ONCE and left
        alone -- create_tasks() is written to be a no-op on every call
        after the first (`if self._tiger is not None: return`). If
        close_tasks() nulled those handles (as an earlier version of
        this method did), every single live-preview frame -- and every
        single row of a multi-row acquisition -- would trigger a full
        re-setup (PLC clear_state, DAC channel re-add, ETL/galvo
        re-instantiate, backplane-trigger-mode re-enable, camera-trigger
        cell + Expose-Out wiring reconfigured from scratch) immediately
        followed by tearing it all back down again -- at minimum a lot
        of wasted serial round-trips, and for live() specifically, the
        free-running galvo would never actually run continuously: it
        would be started, then immediately stop_and_zero()'d, every
        single frame, rather than scanning smoothly for the whole
        preview. NOT yet confirmed on real hardware how visually bad
        that would have looked -- caught by re-reading the real
        mesoSPIM_Core.py before ever reaching that hardware test, so
        there's no real-hardware evidence of the broken behavior to
        report, only the source-level confirmation of why it would have
        happened.

        IMPORTANT (carried over from earlier drafts, still true): the
        PLC keeps running its programmed logic in hardware regardless of
        the host connection -- confirmed on real hardware that a
        configured toggle/gate kept switching well after the controlling
        process exited. plc.clear_state() alone does NOT stop this; only
        reconfiguring the physical outputs back to inputs (or, as this
        project's actual fix does, to a driven constant) does -- see
        asi_tiger/plc.py's docstrings. This method does that via
        safe_all_outputs(). Note this does NOT stop a free-running galvo
        by itself (galvo isn't a PLC-driven output) -- that's why both
        galvo axes get their own explicit stop_and_zero() below, same as
        the ETL axes. Since this now runs after every frame/row rather
        than once, this is actually MORE protective than before against
        anything being left mid-sweep if mesoSPIM stops an acquisition
        or a live preview abruptly.

        What this means for true application exit: there's no dedicated
        "app is closing for real" hook exposed to the waveformer at all
        (checked directly in mesoSPIM_Core.py -- no closeEvent/exit
        handler calls anything on self.waveformer beyond the same 6
        methods used throughout a normal run). A connection this
        instance opened itself (self._owns_tiger_connection) is
        therefore left open for the life of the mesoSPIM-control
        process, the same way mesoSPIM's own ASI stage driver
        (StageControlASI) already leaves ITS serial connection open with
        no explicit final close call either -- consistent with this
        codebase's existing convention, not a new risk this file
        introduces. The OS reclaims the port on process exit either way.
        """
        if not self._live_mode:
            self._stop_axes()
        if self._plc is not None:
            # NOT safe_all_outputs() here. REAL-HARDWARE FINDING (from the
            # frame-timing log): safe_all_outputs() reconfigures EVERY PLC
            # output (camera-trigger BNC, ETL backplane trigger-in lines,
            # laser-enable BNCs) to push-pull driven by constant-low --
            # i.e. it DISCONNECTS the wiring create_tasks() set up once.
            # Since create_tasks() never re-runs, every frame after the first
            # fired a camera-trigger cell that no longer reached the BNC, and
            # the laser-enable cells no longer reached their BNCs either. It
            # also cost ~20-30 serial commands per frame. Per-frame idle
            # safety is instead: camera-trigger cell low (below), laser
            # cells low (mesoSPIM calls laserenabler.disable_all() after each
            # frame), ETL/galvo stopped+zeroed (above), DAC zeroed (below).
            # Call safe_outputs() explicitly for a full teardown.
            try:
                self._plc.set_cell_state(self._camera_trigger_cell, False)
            except Exception as exc:
                logger.error(f"ASI Tiger PLC: error idling camera trigger on close: {exc}")
        if self._dac is not None and not (self._live_mode and self._live_hold_dac):
            try:
                # Zero the laser-intensity channels only. zero_all() would also drive
                # the L/R arm switch to 0 V after EVERY frame, so with Right selected
                # the switch would flip 5V -> 0V -> 5V each frame (and park on the
                # 0 V arm between frames). The switch just holds its last side;
                # write_waveforms_to_tasks() sets it again whenever the row changes.
                for ch_name in self._dac.channel_names():
                    if self._lr_switch is not None and ch_name == self._lr_switch.channel_name:
                        continue
                    self._dac.set_voltage(ch_name, 0.0)
            except Exception as exc:
                logger.error(f"ASI Tiger DAC: error zeroing on close: {exc}")
        if self._plc is not None and not self._live_mode:
            self._plc.track_pointer = False  # acq_track_plc_pointer is per row/snap only
        if not getattr(self, "_frame_logged", False):
            # Normally stop_tasks() already logged this frame; this covers a close
            # with no frame since (e.g. a row stopped before its first plane). The
            # close's own commands are otherwise counted in the next frame's line.
            self._log_frame_timing()
        # Deliberately NOT disconnecting or nulling self._tiger/_plc/_dac/etc here
        # -- see this method's docstring ("REAL CALL FREQUENCY") for why. create_tasks()'s
        # existing `if self._tiger is not None: return` guard is what actually makes
        # setup happen once, for the whole life of this backend instance.
