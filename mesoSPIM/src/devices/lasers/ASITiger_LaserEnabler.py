"""
ASITiger_LaserEnabler
======================
A drop-in alternative to mesoSPIM_LaserEnabler (NI-DAQmx digital
enable lines), backed by independent PLC TTL lines on the ASI Tiger
controller instead of an NI card -- see
mesoSPIM.src.devices.asi_tiger.row_setup.configure_laser_enable_lines()
for the actual cell/BNC wiring this wraps.

WHY THIS IS A SEPARATE CLASS FROM mesoSPIM_ASITigerWaveFormGenerator:
mesoSPIM_Core.py instantiates `self.laserenabler` and `self.waveformer`
independently (`cfg.laser` and `cfg.waveformgeneration` are two
SEPARATE config fields -- confirmed directly from mesoSPIM_Core.py).
Before this file existed, `cfg.laser` had no ASI-backed option at all:
only 'NI'/'cDAQ' (mesoSPIM_LaserEnabler) or a string containing 'demo'
(Demo_LaserEnabler) were recognized, so `cfg.laser = 'ASI_Tiger'` would
silently leave `self.laserenabler` unset -- a guaranteed AttributeError
the first time Core.py called `self.laserenabler.enable(...)` (and it
is called constantly: snap_image(), snap_image_in_series(), live(),
and run_acquisition(), all confirmed directly from mesoSPIM_Core.py).

INTERFACE PARITY: matches mesoSPIM_LaserEnabler's public interface
exactly (`enable(laser)`, `disable_all()`, `state()`) so this is a
true drop-in -- Core.py's call sites need no changes beyond the one
new `elif` branch that constructs this class.

ORDERING HAZARD, SOLVED HERE: `self.laserenabler` is constructed in
Core.__init__, BEFORE any image has ever been taken -- at that point
mesoSPIM_ASITigerWaveFormGenerator.create_tasks() has NOT run yet, so
its `_plc` (the PLCCard instance) is still None. Unlike
mesoSPIM_LaserEnabler (whose NI DO lines need no "connection" object
up front) this class genuinely has nothing to configure yet at
__init__ time. Confirmed directly from mesoSPIM_Core.py: of the four
places `self.laserenabler.enable()` is called, THREE are always
preceded by a `create_tasks()` call earlier in the same call chain
(snap_image() calls it directly; prepare_acquisition() ->
prepare_image_series() calls it before run_acquisition() runs) -- but
live() is the exception: it calls `self.laserenabler.enable(laser)`
*before* entering its loop that calls snap_image() (which is what
would call create_tasks()). So on a bare first-ever `live()` click
(before any snap()), `_plc` would still be None at that exact moment.
Fixed by having `_ensure_lines()` below call
`self.parent.waveformer.create_tasks()` itself if `_plc` isn't ready
yet -- safe because create_tasks() is explicitly documented as
idempotent/safe to call multiple times (mesoSPIM_ASITigerWaveFormGenerator
.create_tasks()'s own docstring), so forcing it a few lines earlier
than it would have run anyway changes nothing else.

NOT YET REAL-HARDWARE-CONFIRMED: this class has only been mock-tested
(FakeSerial) as of this writing -- confirm on real hardware (watch the
PLC BNC5-8 outputs on a scope, or the real laser drivers if wired)
before trusting it for an actual acquisition. See PATCHNOTES_ASI_TIGER.md.
"""

import logging

logger = logging.getLogger(__name__)

from ..asi_tiger import configure_laser_enable_lines, bnc_addr


class ASITiger_LaserEnabler:
    """ASI-Tiger-backed laser enable/disable, one independent PLC TTL
    line per laser (see row_setup.configure_laser_enable_lines()).

    Args:
        laserdict: same dict mesoSPIM_LaserEnabler/Demo_LaserEnabler
            take -- cfg.laserdict (keys are laser designations, e.g.
            '488 nm'; values are NI DO line strings for the NI backend,
            IGNORED here -- this class drives PLC cells instead, not
            those DO lines).
        parent: the mesoSPIM_Core instance (NOT laserdict's parent --
            Core.py must pass `self` here). Needed to reach
            `parent.waveformer._plc` lazily, and `parent.cfg` for the
            optional plc_laser_bncs/plc_laser_toggle_cells overrides.
    """

    def __init__(self, laserdict, parent):
        self.laserdict = laserdict
        self.parent = parent
        self.laser_keys_sorted = sorted(laserdict.keys())
        self.laserenablestate = "None"
        self._lines = None  # LaserEnableLines, built lazily -- see _ensure_lines()

    def _check_if_laser_in_laserdict(self, laser):
        """Mirrors mesoSPIM_LaserEnabler's own check/error exactly."""
        if laser in self.laserdict:
            return True
        else:
            raise ValueError("Laser not in the configuration")

    def _ensure_lines(self):
        """Builds the PLC laser-enable lines on first real use. See this
        module's docstring for why this can't happen at __init__ time,
        and why forcing create_tasks() here (if needed) is safe."""
        if self._lines is not None:
            return
        plc = getattr(self.parent.waveformer, "_plc", None)
        if plc is None:
            # Normally already true by this point (see docstring) --
            # this call is a no-op if create_tasks() already ran.
            self.parent.waveformer.create_tasks()
            plc = getattr(self.parent.waveformer, "_plc", None)
        if plc is None:
            raise RuntimeError(
                "ASITiger_LaserEnabler: self.parent.waveformer._plc is still None "
                "after calling create_tasks() -- is cfg.waveformgeneration actually "
                "'ASI_Tiger'? The PLC-backed laser enabler requires the ASI Tiger "
                "waveformer backend to supply its PLCCard instance."
            )
        ah = getattr(self.parent.cfg, "asi_dac_parameters", {})
        bncs = ah.get("plc_laser_bncs", (5, 6, 7, 8))
        cells = ah.get("plc_laser_toggle_cells", (12, 13, 14, 15))
        if len(bncs) != len(self.laserdict):
            raise ValueError(
                f"Config file: len(asi_dac_parameters['plc_laser_bncs']) ({len(bncs)}) "
                f"must equal num(lasers) in 'laserdict' ({len(self.laserdict)})."
            )
        gate_src, gate_cells = None, None
        if ah.get("laser_gate_with_expose_out", False):
            # Hardware blanking: each laser BNC = its enable cell AND camera Expose-Out
            # (BNC input level, the same signal that starts the ETL ramp).
            gate_src = bnc_addr(ah["camera_expose_bnc"])
            gate_cells = tuple(ah.get("plc_laser_gate_cells", (8, 9, 11, 16)))
            reserved = {ah.get("camera_trigger_cell", 10)}
            if reserved & set(gate_cells):
                raise ValueError(f"plc_laser_gate_cells {gate_cells} collide with camera_trigger_cell {reserved}")
        self._lines = configure_laser_enable_lines(plc, laser_bncs=bncs, toggle_cells=cells,
                                                   gate_source_addr=gate_src, gate_cells=gate_cells)
        logger.info(f"ASITiger_LaserEnabler: configured PLC BNCs {tuple(bncs)} for lasers "
                    f"{self.laser_keys_sorted} (in that order)"
                    + (f", gated by camera Expose-Out (BNC {ah['camera_expose_bnc']}) via cells {gate_cells}."
                       if gate_src is not None else "."))

    def enable(self, laser):
        """Enables a single laser line. All other lines are switched off
        first (matches mesoSPIM_LaserEnabler's "all other lines are
        switched off" semantics, though as sequential PLC writes here
        rather than one atomic NI multi-line write). The target line is
        NOT switched off first: Core calls enable() before the plane loop
        AND again for plane 1, and disable_all() here used to turn the
        already-on line off and on again (2 extra ~103 ms pointer moves
        per row, plus a short laser blink)."""
        if self._check_if_laser_in_laserdict(laser):
            self._ensure_lines()
            idx = self.laser_keys_sorted.index(laser)
            for i in range(len(self._lines.cells)):
                if i != idx:
                    self._lines.disable(i)
            self._lines.enable(idx)
            self.laserenablestate = laser

    def disable_all(self):
        """Disables all laser lines."""
        self._ensure_lines()
        self._lines.disable_all()
        self.laserenablestate = "off"

    def state(self):
        """Returns laserline if a laser is on, otherwise "off"/"None"."""
        return self.laserenablestate
