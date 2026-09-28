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
    PATTERN_SAWTOOTH, enable_backplane_trigger_mode,
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
        self._etl: Optional[SingleAxisWaveform] = None
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
            "etl_card_addr", "etl_axis", "etl_card_first_axis",
            "camera_expose_bnc", "camera_trigger_bnc",
            "laser_dac_channels",
        )
        for key in required:
            if key not in ah:
                raise ValueError(f"Config file: 'asi_dac_parameters' must contain {key!r}")
        # NOT z_card_addr/z_axis/z_axis_mask/z_in0_bnc/z_out0_bnc/laser_bncs here --
        # this design touches neither Z (the existing stage driver's own per-frame
        # stepping handles it) nor laser enable (mesoSPIM Core's own job).
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

        self._etl = SingleAxisWaveform(self._tiger, card_addr=ah["etl_card_addr"], axis=ah["etl_axis"])
        self._etl.stop_and_zero()
        enable_backplane_trigger_mode(self._tiger, card_addr=ah["etl_card_addr"])

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
        self._camera_expose_bnc = ah["camera_expose_bnc"]
        expose_addr = bnc_addr(self._camera_expose_bnc)
        self._plc.configure_io(expose_addr, IO_TYPE_INPUT)
        etl_slot = axis_slot_index(ah["etl_axis"], ah["etl_card_first_axis"])
        etl_trigger_addr = trigger_in_backplane_addr(etl_slot)
        self._plc.configure_io(etl_trigger_addr, IO_TYPE_PUSH_PULL_OUTPUT, source_addr=expose_addr)

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

        CONFIGURES AND ARMS the ETL here, ONCE per row -- NOT per frame
        (an earlier version of this file armed in start_tasks() and
        stopped/zeroed in stop_tasks(), every frame). CONFIRMED ON REAL
        HARDWARE this was a real bug: external-trigger arming
        (SAM=2-style) auto-rearms on every subsequent trigger edge on
        its own -- already confirmed earlier in this project ("SAM=2
        DOES auto-rearm"), so re-arming every frame was both
        unnecessary and a real source of the intermittent "Expose-Out
        never went high" failures seen on real hardware -- re-arming
        via a serial command immediately before firing the next trigger
        pulse creates exactly the kind of race that would explain
        failures on a random frame, not a fixed one. Armed once here,
        left armed for the whole row, disarmed once in close_tasks().
        """
        ah = self.cfg.asi_dac_parameters
        side = self.state.get("shutterconfig", "Left")

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

        etl_amp = self.state.get(f"etl_{'l' if side == 'Left' else 'r'}_amplitude", 0.0)
        etl_off = self.state.get(f"etl_{'l' if side == 'Left' else 'r'}_offset", 0.0)
        etl_period_ms = ah.get("etl_period_ms", 120.0)
        self._etl.configure(pattern=PATTERN_SAWTOOTH, amplitude_v=etl_amp, offset_v=etl_off,
                             period_ms=etl_period_ms, external_trigger=True)
        self._etl.arm_triggered(free_running=False)

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
        Stops/zeros the ETL (armed once per row in
        write_waveforms_to_tasks(), see that method's docstring), THEN
        safes all PLC outputs and zeros the DAC BEFORE disconnecting.

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
        lines quiet.

        Does NOT close the shared serial connection if it's owned by the
        ASI stage driver -- only closes it if this instance opened it.
        """
        if self._etl is not None:
            try:
                self._etl.stop_and_zero()
            except Exception as exc:
                logger.error(f"ASI Tiger ETL: error stopping/zeroing on close: {exc}")
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
        self._etl = None
        self._lr_switch = None
        self._camera_trigger_cell = None
        self._camera_expose_bnc = None
