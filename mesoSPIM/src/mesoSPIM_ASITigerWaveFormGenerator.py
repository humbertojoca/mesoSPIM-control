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

  Call pattern (unchanged from before): prepare_acquisition() calls
  prepare_image_series() -- create_tasks() + write_waveforms_to_tasks(),
  ONCE per row. run_acquisition()'s `for i in range(steps)` loop then
  calls snap_image_in_series() EACH iteration (laser enable ->
  start_tasks() -> run_tasks() -> stop_tasks() -> laser disable) --
  once per frame. close_acquisition() calls close_image_series() --
  close_tasks() -- ONCE, at the end. Every run_tasks() call in THIS
  design does real, per-frame work (no "first call does everything,
  rest no-op" pattern -- that was specific to the zstack_chain design).

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

from .mesoSPIM_WaveFormGenerator import mesoSPIM_WaveFormGenerator

from .devices.asi_tiger import (
    TigerController, ASITigerDAC, PLCCard, SingleAxisWaveform,
    PATTERN_SAWTOOTH, PATTERN_TRIANGLE, enable_backplane_trigger_mode,
    axis_slot_index, trigger_in_backplane_addr,
    IO_TYPE_INPUT, IO_TYPE_PUSH_PULL_OUTPUT, cell_addr, bnc_addr,
    configure_lr_switch,
)

logger = logging.getLogger(__name__)


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
        different COM port than asi_dac_parameters['port']."""
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
            return TigerController.from_open_serial(ser)
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
        work the first time."""
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

        REAL-HARDWARE FINDING (from the user, after switching the camera's
        readout mode to "All Rows"): Expose-Out's duration now equals the
        REAL configured exposure time exactly, no longer the shorter,
        rolling-shutter-derived pulse documented earlier in this project
        ("one confirmed data point showed a 150ms exposure setting
        producing a ~120ms Expose-Out pulse" -- see
        asi_tiger_galvo_etl_demo.py's --etl-period-ms help text, which
        predates this readout-mode change and no longer reflects reality
        under "All Rows"). With Expose-Out now spanning the FULL exposure,
        the ETL sweep should track the real exposure time directly, not a
        separately hand-tuned constant that silently drifts out of sync
        whenever the exposure setting changes (including row to row, since
        mesoSPIM allows exposure to vary by channel).

        Derives the period from self.state['camera_exposure_time'] (the
        same key run_tasks() already uses for its own Expose-Out-low
        timeout) MINUS a small safety margin
        (asi_dac_parameters.get('etl_period_margin_ms', 0.0)) --
        CONFIRMED ON REAL HARDWARE, separately, earlier in this project:
        the ETL's period must be SHORTER than the camera's real trigger
        interval, not equal to it -- SAM=2 must finish its cycle before
        the next trigger edge arrives, or it misses every other trigger.
        That finding was from the OLD rolling-shutter timing, where
        Expose-Out was itself a separate, shorter, derived pulse with its
        own jitter relative to the actual exposure window -- some margin
        against IT made sense. Under "All Rows" (the mode now in use),
        Expose-Out spans the exposure window directly and the camera's
        own trigger-to-trigger interval already includes its readout
        overhead beyond the exposure time itself, so there is no known
        source of jitter left for a margin to protect against -- PER THE
        USER, directly: "in reality, margin period is negligible if the
        expose out is in 'all rows'". Default margin is therefore 0.0ms
        (derived period = the real exposure time, exactly). The key is
        left in place (not removed) as a manual escape hatch for a rack
        or readout mode where some margin genuinely is still needed --
        set asi_dac_parameters['etl_period_margin_ms'] explicitly if you
        find one.

        asi_dac_parameters['etl_period_ms'], if explicitly set, OVERRIDES
        this derivation entirely (manual escape hatch, e.g. for a fixed
        value independent of exposure time) -- the default (unset) is to
        auto-derive as described above.
        """
        explicit = ah.get("etl_period_ms")
        if explicit is not None:
            return explicit
        margin_ms = ah.get("etl_period_margin_ms", 0.0)
        exposure_s = self.state.get("camera_exposure_time", 0.5)
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
        side = self.state.get("shutterconfig", "Left")
        letter = "l" if side == "Left" else "r"
        other_letter = "r" if letter == "l" else "l"

        # Laser intensity: self.state['intensity'] is 0-100% (per mesoSPIM_Core.set_intensity()'s
        # own docstring), NOT a voltage -- convert using max_laser_voltage, already
        # validated/clamped in config_check(). laser_dac_channels is indexed the same
        # order as cfg.laserdict's keys -- confirm that ordering matches your config
        # (not independently verified here).
        laser_name = self.state.get("laser")
        laser_idx = list(self.cfg.laserdict.keys()).index(laser_name) if laser_name in self.cfg.laserdict else 0
        intensity_pct = self.state.get("intensity", 0)
        voltage = (intensity_pct / 100.0) * self.state["max_laser_voltage"]
        dac_ch = ah["laser_dac_channels"][laser_idx]["name"]
        self._dac.set_voltage(dac_ch, voltage)

        if self._lr_switch is not None:
            self._lr_switch.select(is_right=(side == "Right"))

        # --- Quiesce the INACTIVE side first (galvo AND ETL) -- a previous row may
        # have left it armed/running for the opposite side. ---
        etl_by_letter = {"l": self._etl_l, "r": self._etl_r}
        galvo_by_letter = {"l": self._galvo_l, "r": self._galvo_r}
        etl_by_letter[other_letter].stop_and_zero()
        galvo_by_letter[other_letter].stop_and_zero()

        # --- ETL: configure + arm the ACTIVE side only ---
        active_etl = etl_by_letter[letter]
        etl_amp = self.state.get(f"etl_{letter}_amplitude", 0.0)
        etl_off = self.state.get(f"etl_{letter}_offset", 0.0)
        etl_period_ms = self._etl_period_ms(ah)
        active_etl.configure(pattern=PATTERN_SAWTOOTH, amplitude_v=etl_amp, offset_v=etl_off,
                              period_ms=etl_period_ms, external_trigger=True)
        active_etl.arm_triggered(free_running=False)

        # --- Galvo: configure + start (free-running) the ACTIVE side only ---
        active_galvo = galvo_by_letter[letter]
        galvo_amp = self.state.get(f"galvo_{letter}_amplitude", 0.0)
        galvo_off = self.state.get(f"galvo_{letter}_offset", 0.0)
        galvo_freq = self.state.get(f"galvo_{letter}_frequency", 100.0)
        galvo_duty = self.state.get(f"galvo_{letter}_duty_cycle", 0.5)

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
        """
        ah = self.cfg.asi_dac_parameters
        pulse_ms = ah.get("camera_trigger_pulse_ms", 10.0)
        expose_bit = 1 << (self._camera_expose_bnc - 1)

        self._plc.set_cell_state(self._camera_trigger_cell, True)
        time.sleep(pulse_ms / 1000.0)
        self._plc.set_cell_state(self._camera_trigger_cell, False)

        start_timeout_s = ah.get("camera_expose_start_timeout_s", 2.0)
        start = time.perf_counter()
        while time.perf_counter() - start < start_timeout_s:
            if self._plc.read_bnc_inputs() & expose_bit:
                break
            time.sleep(0.001)
        else:
            logger.error(f"ASI Tiger: camera Expose-Out never went high within "
                          f"{start_timeout_s:.1f}s of triggering -- exposure may not have "
                          f"started. Proceeding anyway.")

        exposure_time_s = self.state.get("camera_exposure_time", 0.5)
        end_timeout_s = ah.get("camera_expose_end_timeout_margin_s", 2.0) + exposure_time_s
        start = time.perf_counter()
        while time.perf_counter() - start < end_timeout_s:
            if not (self._plc.read_bnc_inputs() & expose_bit):
                return
            time.sleep(0.001)
        logger.error(f"ASI Tiger: camera Expose-Out never went low within "
                      f"{end_timeout_s:.1f}s -- exposure may still be in progress. "
                      f"Proceeding anyway.")

    def stop_tasks(self):
        """No-op for this design -- see start_tasks()'s docstring. The
        ETL stays armed for the whole row; close_tasks() stops/zeros it
        once, at the actual end."""
        pass

    def close_tasks(self):
        """
        Stops/zeros both ETL axes and both galvo axes (whichever was
        active for the last row, plus the inactive one defensively),
        THEN safes all PLC outputs and zeros the DAC BEFORE
        disconnecting.

        IMPORTANT (carried over from earlier drafts, still true): the
        PLC keeps running its programmed logic in hardware regardless of
        the host connection -- confirmed on real hardware that a
        configured toggle/gate kept switching well after the controlling
        process exited. plc.clear_state() alone does NOT stop this; only
        reconfiguring the physical outputs back to inputs does (see
        asi_tiger/plc.py's docstrings). This method does that via
        safe_all_outputs() BEFORE zeroing the DAC and disconnecting, so a
        mesoSPIM session ending (normally or via a crash caught by
        whatever wraps this) actually leaves lasers/switch/camera-trigger
        lines quiet. Note this does NOT stop a free-running galvo by
        itself (galvo isn't a PLC-driven output) -- that's why both
        galvo axes get their own explicit stop_and_zero() below, same as
        the ETL axes.

        Does NOT close the shared serial connection if it's owned by the
        ASI stage driver -- only closes it if this instance opened it.
        """
        for name, axis in (("ETL L", self._etl_l), ("ETL R", self._etl_r),
                            ("galvo L", self._galvo_l), ("galvo R", self._galvo_r)):
            if axis is not None:
                try:
                    axis.stop_and_zero()
                except Exception as exc:
                    logger.error(f"ASI Tiger {name}: error stopping/zeroing on close: {exc}")
        if self._plc is not None:
            try:
                self._plc.safe_all_outputs()
            except Exception as exc:
                logger.error(f"ASI Tiger PLC: error safing outputs on close: {exc}")
        if self._dac is not None:
            try:
                self._dac.zero_all()
            except Exception as exc:
                logger.error(f"ASI Tiger DAC: error zeroing on close: {exc}")
        if self._owns_tiger_connection and self._tiger is not None:
            self._tiger.disconnect()
        self._tiger = None
        self._plc = None
        self._dac = None
        self._etl_l = None
        self._etl_r = None
        self._galvo_l = None
        self._galvo_r = None
        self._lr_switch = None
        self._camera_trigger_cell = None
        self._camera_expose_bnc = None
