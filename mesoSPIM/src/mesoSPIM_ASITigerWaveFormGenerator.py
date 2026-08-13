"""
mesoSPIM_ASITigerWaveFormGenerator
====================================
A drop-in alternative to mesoSPIM_WaveFormGenerator (NI-DAQmx) backed by
the asi_tiger library (ASI Tiger DAC + PLC cards) instead of an NI card.

DESIGN: this subclasses mesoSPIM_WaveFormGenerator and overrides ONLY the
hardware I/O methods (config_check, create_tasks, write_waveforms_to_tasks,
start_tasks, run_tasks, stop_tasks, close_tasks). All of the actual
waveform MATH -- create_waveforms(), create_etl_waveforms(),
create_galvo_waveforms(), create_laser_waveforms(),
bundle_galvo_and_etl_waveforms(), state_request_handler() -- is inherited
unchanged from the parent class, since none of it touches nidaqmx. This
mirrors exactly how mesoSPIM_DemoWaveFormGenerator is already structured
in mesoSPIM_WaveFormGenerator.py -- look at that class alongside this one.

======================================================================
READ BEFORE USING ON REAL HARDWARE -- BANDWIDTH CAVEAT
======================================================================
mesoSPIM's default timing (samplerate=100000 Hz, sweeptime=0.2s) means
20,000 analog samples per frame on the NI card. ASI's SIGNAL_DAC_4CH
card has a hard 4 kHz refresh ceiling -- 800 samples/sweep even with a
confirmed hardware-triggered ring-buffer playback mode, which this
class does NOT implement yet (see _stream_dac_waveforms below). What
IS implemented is a software-timed serial loop, realistically ~100 Hz,
i.e. ~20 samples/sweep. That is fine for confirming wiring and trigger
logic, but is NOT adequate for real galvo/ETL scanning -- you will see
visible stepping/banding.

Recommended path, in order of effort:
  1. Use this class for the PLC-driven parts NOW (master trigger,
     camera trigger, stage trigger, laser gating) -- those are genuine,
     confident wins: hardware-deterministic, host out of the timing
     loop, already validated against ASI's documented command set.
  2. Keep a small/cheap NI multifunction card (e.g. USB-6001, 4 AO
     channels is overkill -- even a 2-channel card covers galvo L/R,
     add ETL via a 2nd cheap card or accept ETL on ASI DAC since its
     ramp is much less demanding than the galvo sawtooth) for the
     genuinely fast analog channels, OR
  3. Confirm ASI's ring-buffer/TTL-triggered playback mode for
     SIGNAL_DAC_4CH (test empirically -- see asi_tiger README) and
     implement _stream_dac_waveforms() properly with it. At 4 kHz
     that's still 25x fewer samples than NI, so validate on your
     actual optics before trusting it for production acquisitions.
======================================================================

Config: expects a new `cfg.asi_dac_parameters` dict (see
config_snippet.py in this folder) instead of (or alongside)
`cfg.acquisition_hardware`.
"""

import logging
import time
from typing import Optional

import numpy as np

from .mesoSPIM_WaveFormGenerator import mesoSPIM_WaveFormGenerator

from .devices.asi_tiger import TigerController, ASITigerDAC, PLCCard, bnc_addr, backplane_addr
from .devices.asi_tiger.waveform import WaveformStreamer

logger = logging.getLogger(__name__)


class mesoSPIM_ASITigerWaveFormGenerator(mesoSPIM_WaveFormGenerator):
    """See module docstring -- read the bandwidth caveat before using this
    for real acquisitions, not just wiring/logic tests."""

    def __init__(self, parent):
        self._dac: Optional[ASITigerDAC] = None
        self._plc: Optional[PLCCard] = None
        self._owns_tiger_connection = False
        super().__init__(parent)  # base __init__ calls self.config_check() at the end

    # ------------------------------------------------------------------
    # Config validation (overrides the NI-specific version)
    # ------------------------------------------------------------------
    def config_check(self):
        """ASI equivalent of the base class's config_check(). Validates
        cfg.asi_dac_parameters instead of cfg.acquisition_hardware, but
        keeps the same voltage safety clamps."""
        assert hasattr(self.cfg, "asi_dac_parameters"), (
            "Config file must define 'asi_dac_parameters' when "
            "waveformgeneration == 'ASI_Tiger'. See config_snippet.py."
        )
        ah = self.cfg.asi_dac_parameters
        for key in ("port", "baudrate", "plc_card_addr", "galvo_etl_channels", "laser_channels"):
            if key not in ah:
                raise ValueError(f"Config file: 'asi_dac_parameters' must contain {key!r}")
        if len(ah["laser_channels"]) != len(self.cfg.laserdict):
            raise ValueError(
                f"Config file: number of DAC channels in 'asi_dac_parameters[\"laser_channels\"]' "
                f"({len(ah['laser_channels'])}) must equal num(lasers) in 'laserdict' "
                f"({len(self.cfg.laserdict)})."
            )

        # Same safety clamps as the NI version -- keep them, they matter.
        if self.state["max_laser_voltage"] > 10:
            self.state["max_laser_voltage"] = 10
            msg = f"Config parameter 'max_laser_voltage' ({self.state['max_laser_voltage']}) is > 10V. Setting to 10V."
            print(msg); logger.warning(msg)
        elif self.state["max_laser_voltage"] > 5:
            msg = f"Config parameter 'max_laser_voltage' ({self.state['max_laser_voltage']}) is > 5V, may damage the laser controller."
            print(msg); logger.warning(msg)

        logger.warning(
            "ASI Tiger backend: DAC playback is software-timed (~100 Hz effective), "
            "NOT hardware-clocked like NI. See this module's docstring before using "
            "for real acquisitions, not just wiring/logic tests."
        )

    # ------------------------------------------------------------------
    # Shared-connection lookup
    # ------------------------------------------------------------------
    def _get_shared_tiger_connection(self) -> Optional[TigerController]:
        """
        If mesoSPIM's own ASI stage driver is already connected (i.e. the
        stage_type in the config is an ASI type), reuse ITS serial
        connection rather than opening a second handle on the same COM
        port -- see asi_tiger.controller.TigerController.from_open_serial
        for why this matters.

        This assumes mesoSPIM's stage object exposes `.asi_stages.asi_connection`
        (true for StageControlASI as of the current mesoSPIM_Stages.py) and
        that the ASI stage and ASI DAC/PLC cards are on the same COM port.
        If your stage is NOT ASI, or is on a different COM port, this
        returns None and create_tasks() opens its own connection instead.
        """
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
                    f"{ah['port']} -- different ports, opening a separate connection "
                    f"for the DAC/PLC cards."
                )
                return None
            return TigerController.from_open_serial(ser)
        except AttributeError:
            return None

    # ------------------------------------------------------------------
    # PLC trigger/gating setup -- run ONCE, not per frame
    # ------------------------------------------------------------------
    def _configure_plc_triggers(self):
        """
        Programs the PLC's master/camera trigger and laser gating logic.
        Runs once when the connection is (re)established, not on every
        frame -- unlike write_waveforms_to_tasks(), which does run every
        frame with fresh voltage values.

        ADAPT THIS to your actual BNC/backplane wiring -- the addresses
        below (BNC1 in, BNC5/6 out) are placeholders matching the earlier
        two-laser-toggle example, not a universal default.
        """
        ah = self.cfg.asi_dac_parameters
        camera_trigger_in = ah.get("plc_camera_trigger_bnc")  # e.g. 1
        laser_bncs = ah.get("plc_laser_bncs")                  # e.g. (5, 6)

        if camera_trigger_in and laser_bncs and len(laser_bncs) == 2:
            self._plc.configure_two_laser_toggle(
                trigger_source_addr=bnc_addr(camera_trigger_in),
                laser0_bnc=laser_bncs[0],
                laser1_bnc=laser_bncs[1],
            )
            logger.info(
                f"PLC: 2-laser toggle configured (trigger BNC{camera_trigger_in} -> "
                f"laser0 BNC{laser_bncs[0]}, laser1 BNC{laser_bncs[1]}). "
                f"Verify with a scope before trusting with real laser hardware."
            )
        else:
            logger.info("PLC: no plc_camera_trigger_bnc/plc_laser_bncs configured -- skipping laser-toggle setup.")

    # ------------------------------------------------------------------
    # The 6 overridden hardware I/O methods
    # ------------------------------------------------------------------
    def create_tasks(self):
        """ASI equivalent of NI task creation. Opens (or reuses) the
        Tiger connection and registers DAC channels + PLC config. Safe
        to call multiple times -- only does real work the first time."""
        self.calculate_samples()
        ah = self.cfg.asi_dac_parameters

        if self._dac is not None:
            return  # already set up -- write_waveforms_to_tasks() handles per-frame updates

        shared = self._get_shared_tiger_connection()
        if shared is not None:
            tiger = shared
            self._owns_tiger_connection = False
            logger.info("ASI Tiger DAC/PLC: reusing the ASI stage driver's serial connection.")
        else:
            tiger = TigerController(ah["port"], baudrate=ah["baudrate"])
            tiger.connect()
            self._owns_tiger_connection = True
            logger.info(f"ASI Tiger DAC/PLC: opened its own connection on {ah['port']}.")

        self._dac = ASITigerDAC(tiger=tiger)
        for ch in ah["galvo_etl_channels"] + ah["laser_channels"]:
            self._dac.add_channel(**ch)
        self._dac.zero_all()  # mandatory: SIGNAL_DAC can surge to -10V on power-up

        self._plc = PLCCard(tiger, card_addr=ah["plc_card_addr"], axis=ah.get("plc_axis", "E"))
        self._plc.clear_state()
        self._configure_plc_triggers()

        self._galvo_etl_names = [ch["name"] for ch in ah["galvo_etl_channels"]]
        self._laser_names = [ch["name"] for ch in ah["laser_channels"]]

    def write_waveforms_to_tasks(self):
        """
        Downsamples the (already-computed, full-resolution) galvo/etl/
        laser numpy arrays from create_waveforms() to a rate the ASI
        DAC's software-timed loop can actually keep up with, and stages
        them for run_tasks() to stream. See module docstring: this is
        the part that's NOT hardware-clocked yet.
        """
        target_rate = self.cfg.asi_dac_parameters.get("software_stream_rate_hz", 50)
        samplerate = self.state["samplerate"]
        step = max(1, int(round(samplerate / target_rate)))

        arrays = {}
        for i, name in enumerate(self._galvo_etl_names):
            arrays[name] = self.galvo_and_etl_waveforms[i][::step]
        for i, name in enumerate(self._laser_names):
            arrays[name] = self.laser_waveforms[i][::step]

        self._staged_waveforms = arrays
        self._staged_rate = samplerate / step
        logger.debug(
            f"ASI Tiger DAC: staged {len(arrays)} channels at ~{self._staged_rate:.1f} Hz "
            f"({len(next(iter(arrays.values())))} samples) -- see bandwidth caveat in module docstring."
        )

    def start_tasks(self):
        """No separate 'arm' step needed for the software-timed path --
        streaming begins in run_tasks()."""
        pass

    def run_tasks(self):
        """
        Fires the camera trigger via the PLC, then streams the staged
        waveforms to the DAC channels (software-timed -- see caveat).
        Blocks until the sweep finishes, mirroring the NI version's
        wait_until_done() behavior.
        """
        ah = self.cfg.asi_dac_parameters
        camera_trigger_cell = ah.get("plc_camera_trigger_cell")
        if camera_trigger_cell is not None:
            self._plc.set_cell_state(camera_trigger_cell, True)
            time.sleep(0.001)
            self._plc.set_cell_state(camera_trigger_cell, False)

        streamer = WaveformStreamer(
            self._dac, self._staged_waveforms, sample_rate=self._staged_rate, loop=False
        )
        done = {"finished": False}
        streamer.on_finished = lambda: done.update(finished=True)
        streamer.on_error = lambda msg: logger.error(f"ASI Tiger DAC streaming error: {msg}")
        streamer.start()
        while not done["finished"]:
            time.sleep(0.01)
        self._last_achieved_rate = streamer.achieved_rate_hz

    def stop_tasks(self):
        """Nothing persistent to stop for the software-timed path (the
        streamer already finished in run_tasks())."""
        pass

    def close_tasks(self):
        """
        Zeros all DAC outputs AND safes all PLC outputs (laser toggle,
        camera trigger, etc. configured in _configure_plc_triggers()).

        IMPORTANT: the PLC keeps running its programmed logic in hardware
        regardless of the host connection -- confirmed on real hardware
        that a 2-laser toggle kept switching on every camera trigger well
        after the controlling process exited. plc.clear_state() (HOME)
        does NOT stop this; only reconfiguring the physical outputs back
        to inputs does (see asi_tiger/plc.py docstrings for the full
        explanation). This method does that via safe_all_outputs()
        BEFORE zeroing the DAC and disconnecting, so a mesoSPIM session
        ending (normally or via a crash caught by whatever wraps this)
        actually leaves lasers/shutter/camera-trigger lines quiet.

        Does NOT close the shared serial connection if it's owned by the
        ASI stage driver -- only closes it if this instance opened it
        itself.
        """
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
            if self._owns_tiger_connection:
                self._dac.tiger.disconnect()
            self._dac = None
            self._plc = None
