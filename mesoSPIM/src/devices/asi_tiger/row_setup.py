"""
asi_tiger.row_setup
======================
Row-level (slow, once-per-row) setup helpers -- laser enable and the
L/R illumination-arm switch -- as distinct from the fast, per-frame
trigger chain in zstack_chain.py. These get called once at the start
of each row's acquisition, before the fast per-frame Z-stack loop
begins, not per-frame.

Laser intensity and absolute Z positioning are NOT built here:
  - Laser intensity is already fully covered by ASITigerDAC.set_voltage()
    (this rack: card_addr 35, axes P/Q/R/S) -- no new code needed, just
    instantiate ASITigerDAC against that card and use its existing,
    already-validated voltage-control API.
  - Absolute Z positioning is StageRingBuffer.move_absolute() (in
    stage_trigger.py) -- a direct, host-commanded move, distinct from
    that same class's TTL-triggered ring-buffer mechanism.

L/R illumination-arm switch: DECIDED via real-hardware testing. ASI TTL
OUT0_mode=21 (documented as routing to backplane TTL1, addr 42, "as of
firmware v3.36") was tested on this rack's Z/Theta card using
tools/asi_tiger_ttl_mode21_discovery_test.py -- the command was
accepted without error (so the MM_TARGET firmware module it requires
is likely present), a genuine ring-buffer move was confirmed to occur
(position changed correctly), but NOTHING was detected across all 8
backplane lines. This rules out the documented behavior on this card/
firmware combination -- not confirmed reliable enough to build on, so
NOT used. Went with the free-DAC-axis approach instead (below): lower
risk regardless, since it touches nothing in the currently-working
trigger chain, at the cost of one DAC channel that isn't otherwise a
scarce resource on this rack (this rack: I or K free on card 34, or B
or D free on card 37 -- K has a known, separate hardware trigger fault
unrelated to this simple DC-voltage use, so I is the safer default of
the two free channels on card 34).

SIGNAL SHAPE CONFIRMED: mesoSPIM-control's own existing NI-card
implementation (mesoSPIM/config/demo_config.py, shutterdict/
shutterswitch) drives this via a plain NI digital-output line, with the
comment "'shutter_right' is the left/right switch (Right==True)" --
level-defined (a persistent HIGH/LOW state, matching how NI DO lines
inherently work), NOT edge/toggle-based. LRSwitch below already uses
this same level-defined approach (an explicit voltage per position,
not a flip-from-current-state pulse) -- confirmed consistent with
mesoSPIM's own convention, no design change needed.
"""

from dataclasses import dataclass
from typing import Optional, Sequence

from .plc import PLCCard, bnc_addr, cell_addr, IO_TYPE_PUSH_PULL_OUTPUT
from .dac import ASITigerDAC


@dataclass
class LaserEnableLines:
    """
    Handle for a set of independent, directly-toggleable laser enable
    lines, one per PLC BNC, returned by configure_laser_enable_lines().
    Each line is driven by its own manually-controlled D-flop cell
    (D/clock tied low, so it never changes on its own -- the same
    confirmed pattern used throughout this project for manual pulse/
    toggle sources, e.g. the camera trigger test's kick cell), toggled
    directly via CCA F.

    This is a plain, persistent ON/OFF line -- NOT a triggered/gated
    mechanism (unlike PLCCard.configure_shutter_gate(), which ANDs an
    enable bit with some OTHER trigger source). That distinction
    matters: "laser enable" here is a row-level setting (set once per
    row, stays as configured until explicitly changed), not something
    re-evaluated per frame.
    """
    plc: PLCCard
    bncs: Sequence[int]   # PLC BNC numbers, in laser-index order
    cells: Sequence[int]  # corresponding manual-toggle cell numbers, same order

    def enable(self, index: int):
        """Turns ON the laser at this index (0-based, into bncs/cells)."""
        self.plc.set_cell_state(self.cells[index], True)

    def disable(self, index: int):
        """Turns OFF the laser at this index."""
        self.plc.set_cell_state(self.cells[index], False)

    def disable_all(self):
        """Turns OFF every configured laser line at once."""
        for cell in self.cells:
            self.plc.set_cell_state(cell, False)


def configure_laser_enable_lines(
    plc: PLCCard,
    laser_bncs: Sequence[int] = (5, 6, 7, 8),
    toggle_cells: Sequence[int] = (12, 13, 14, 15),
) -> LaserEnableLines:
    """
    Wires one independent, directly-toggleable enable line per BNC in
    laser_bncs, each starting OFF. Returns a LaserEnableLines handle
    for toggling them from Python.

    laser_bncs: PLC BNC numbers, in laser-index order. Default (5,6,7,8)
        matches this rack's convention (BNCs 1-4 already used for Z
        control and camera -- see zstack_chain.py).
    toggle_cells: which cells to use, same order as laser_bncs. Default
        12-15, chosen to avoid the trigger chain's own cells (1-7, see
        configure_zstack_trigger_chain()) if both are configured on the
        same PLC card at once -- override if those collide with
        something else already configured on this card.
    """
    if len(laser_bncs) != len(toggle_cells):
        raise ValueError(
            f"laser_bncs and toggle_cells must be the same length -- "
            f"got {len(laser_bncs)} and {len(toggle_cells)}"
        )
    for bnc, cell in zip(laser_bncs, toggle_cells):
        plc.configure_cell(cell, "d_flop", inputs={"a": 0, "b": 0, "c": 0})
        plc.set_cell_state(cell, False)
        plc.configure_io(bnc_addr(bnc), IO_TYPE_PUSH_PULL_OUTPUT, source_addr=cell_addr(cell))
    return LaserEnableLines(plc=plc, bncs=tuple(laser_bncs), cells=tuple(toggle_cells))


@dataclass
class LRSwitch:
    """
    Handle for the L/R illumination-arm switch, driven by a free DAC
    axis as a binary control voltage -- chosen over ASI TTL
    OUT0_mode=21 after real-hardware testing showed no detectable
    backplane routing on this card/firmware (see this module's
    docstring and tools/asi_tiger_ttl_mode21_discovery_test.py).

    Not new hardware-control logic -- just picks LEFT/RIGHT voltage
    levels for you on top of the already-validated
    ASITigerDAC.set_voltage(), including that method's existing safety
    bounds and optional slew-rate protection (max_step_v, set when the
    channel was added to the ASITigerDAC instance) if your switch
    hardware needs it.

    left_v / right_v are NOT assumed here -- they depend entirely on
    what your actual switch hardware expects as a control voltage
    (e.g. 0V/5V for a simple TTL-level input, or some other pair for an
    analog-driven mechanism). Pass the real values for your hardware
    when constructing this via configure_lr_switch().
    """
    dac: ASITigerDAC
    channel_name: str
    left_v: float
    right_v: float

    def select_left(self):
        """Drives the switch to its LEFT position."""
        self.dac.set_voltage(self.channel_name, self.left_v)

    def select_right(self):
        """Drives the switch to its RIGHT position."""
        self.dac.set_voltage(self.channel_name, self.right_v)

    def select(self, is_right: bool):
        """
        Drives the switch using mesoSPIM's own convention directly
        (confirmed from mesoSPIM-control's demo_config.py: "'shutter_right'
        is the left/right switch (Right==True)") -- convenient when
        wiring this up against mesoSPIM's existing state representation,
        which already uses a Right==True boolean rather than separate
        left/right calls.
        """
        self.select_right() if is_right else self.select_left()


def configure_lr_switch(
    dac: ASITigerDAC,
    card_addr: int = 34,
    axis: str = "I",
    left_v: float = 0.0,
    right_v: float = 5.0,
    range_code: int = 2,
    safety_limit_mv: Optional[float] = None,
    max_step_v: Optional[float] = None,
    channel_name: str = "lr_switch",
) -> LRSwitch:
    """
    Registers a DAC channel for the L/R switch (card_addr/axis default
    to this rack's free axis I on card 34, the ETL card -- K is also
    free there but has a known, unrelated hardware trigger fault,
    avoided here even though it likely wouldn't matter for a simple DC
    voltage output) and returns an LRSwitch handle.

    left_v / right_v: 0V/5V -- 5V is the confirmed level expected for
    this rack's real laser-switching hardware. Requires range_code=2
    (0-10.24V) to be ACTUALLY ACTIVE on the real card, not just assumed
    in software -- see the range_code note below before trusting this
    on a fresh session.

    range_code: defaults to 2 (0-10.24V), needed for the 5V right_v
    default above -- range_code=1 (0-4.096V, this rack's ETL channels'
    range) cannot represent 5V at all.

    IMPORTANT -- this default assumes the REAL hardware range has
    ALREADY been changed to match, via the confirmed sequence: send
    `PR I=2` on card 34, then a CARD-ADDRESSED reset (`34~`, NOT a bare
    `~` -- that would broadcast-reset the whole rack, zeroing every
    axis's position, including Z's), then confirm with `PR I?` that it
    reads back `I=2`. Unlike ASITigerDAC.set_range() (which deliberately
    does NOT update its cached range_code until confirm_range_change()
    is called, specifically to avoid this exact problem), THIS function
    registers a BRAND NEW channel -- there's no "old, trusted" cached
    value to protect, so it trusts range_code=2 immediately whether or
    not the real hardware has actually been reconfigured yet. If you're
    calling this for the first time in a fresh session before running
    that hardware sequence, the cached range won't match reality and
    set_voltage() calls could silently encode a value for the wrong
    range. Run dac.query_range(channel_name) right after this call to
    verify before trusting it.
    """
    dac.add_channel(
        channel_name, card_addr=card_addr, axis=axis, range_code=range_code,
        safety_limit_mv=safety_limit_mv, max_step_v=max_step_v,
    )
    return LRSwitch(dac=dac, channel_name=channel_name, left_v=left_v, right_v=right_v)
