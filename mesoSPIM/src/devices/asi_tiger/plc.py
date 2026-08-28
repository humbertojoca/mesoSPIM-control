"""
asi_tiger.plc
==============
Driver for the ASI Tiger Programmable Logic Card (TGPLC) -- a 16-cell
mini-FPGA used to build hardware-timed trigger/gating logic (camera
sync, laser switching, shutters) without round-tripping through the
host PC.

Command reference this is built from: ASI's "Programmable Logic Card
User Guide" (not just the general Tiger serial command page -- the PLC
recycles CCA/CCB/PM for a completely different purpose than on motor
axes, so the general docs are not enough here).

Core model:
  - The PLC card occupies one Tiger axis (default 'E') and one card
    address (e.g. 36 on your rack).
  - `M <axis>=<addr>` moves an internal "pointer" to a cell or physical
    I/O. All subsequent CCA/CCB commands act on whatever the pointer
    currently points at.
  - CCA Y sets: cell TYPE (if pointer is on a logic cell 1-16) or I/O
    TYPE (if pointer is on a physical I/O, addr 33-48).
  - CCA Z sets: cell CONFIGURATION (LUT bits / duration, if on a logic
    cell) or I/O SOURCE ADDRESS (if on a physical I/O).
  - CCB X/Y/Z/F set a logic cell's 4 inputs, by address.
  - Address space: 0 = constant low, 1-16 = logic cells, 33-40 = front
    panel BNC 1-8, 41-48 = backplane TTL0-7. Add 64 to invert, 128 for
    rising edge, 192 for falling edge.

SAFETY: this module lets you route real TTL signals to laser and
shutter hardware. Verify logic behavior (e.g. with an oscilloscope on
the BNC outputs, laser disconnected/attenuated) before connecting to
live laser lines. The two-laser toggle helper below implements the
wiring ASI describes in their manual (Section 3.2/3.3, used by their
own diSPIM plugin), but always confirm on your actual firmware build
before trusting it with hardware.

CRITICAL -- the PLC keeps running once programmed, independent of the
host: this is a real-time hardware logic device, not something driven
by the serial link. Confirmed on real hardware: after
configure_two_laser_toggle() and disconnecting, the toggle kept
switching on every camera trigger indefinitely, because clear_state()
(HOME) only resets a flip-flop's stored bit -- it does NOT remove a
cell's programming or a physical I/O's output configuration. Always
call safe_all_outputs() (or the specific disable_*() helper) when
you're done with a configuration, not clear_state() alone. See
clear_state()'s docstring for the full explanation.
"""

from dataclasses import dataclass, field
from typing import Optional

from .controller import TigerController

# ---------------------------------------------------------------------
# Cell types (CCA Y code when pointer is on a logic cell, addr 1-16)
# ---------------------------------------------------------------------
CELL_TYPE = {
    "constant": 0,
    "d_flop": 1,
    "lut2": 2,
    "lut3": 3,
    "lut4": 4,
    "and2": 5,
    "or2": 6,
    "xor2": 7,
    "one_shot_retrig": 8,
    "delay_retrig": 9,
    "and4": 10,
    "or4": 11,
    "sync_d_flop": 12,
    "jk_flop": 13,
    "one_shot": 14,       # non-retriggerable
    "delay": 15,          # non-retriggerable
}

# Physical I/O type codes (CCA Y code when pointer is on addr 33-48)
IO_TYPE_INPUT = 0
IO_TYPE_OPEN_DRAIN_OUTPUT = 1
IO_TYPE_PUSH_PULL_OUTPUT = 2

# Trigger source codes for the PM command (evaluation clock source)
TRIGGER_INTERNAL_4KHZ = 0
TRIGGER_BACKPLANE_C7 = 1     # diSPIM: micro-mirror card master clock
TRIGGER_BACKPLANE_C13 = 2
TRIGGER_BACKPLANE_C14 = 3
TRIGGER_BNC1 = 4             # external clock/trigger wired into BNC 1


def cell_addr(n: int) -> int:
    """Address of logic cell n (1-16)."""
    if not 1 <= n <= 16:
        raise ValueError("cell number must be 1-16")
    return n


def bnc_addr(n: int) -> int:
    """Address of front-panel BNC connector n (1-8)."""
    if not 1 <= n <= 8:
        raise ValueError("BNC number must be 1-8")
    return 32 + n


def backplane_addr(n: int) -> int:
    """Address of backplane TTL line n (0-7)."""
    if not 0 <= n <= 7:
        raise ValueError("backplane TTL number must be 0-7")
    return 41 + n


def inverted(addr: int) -> int:
    """Logical NOT of a signal address."""
    return addr + 64


def rising_edge(addr: int) -> int:
    """Rising-edge-sensitive version of an address (for clock/trigger inputs)."""
    return (addr % 64) + 128


def falling_edge(addr: int) -> int:
    """Falling-edge-sensitive version of an address."""
    return (addr % 64) + 192


CONST_LOW = 0
CONST_HIGH = inverted(0)  # 64 == NOT(constant 0) == constant 1
EVERY_CYCLE_RISING_EDGE = 192  # per manual: address 192 (or 64) toggles every eval cycle


@dataclass
class PLCCard:
    """
    High-level interface to one Tiger Programmable Logic Card.

    Usage:
        plc = PLCCard(tiger_controller, card_addr=36, axis="E")
        plc.set_trigger_source(TRIGGER_BNC1)   # external camera TTL on BNC1
        ...
        plc.safe_all_outputs()   # NOT clear_state() -- see clear_state()'s docstring
    """

    tiger: TigerController
    card_addr: int
    axis: str = "E"
    # Tracks every physical I/O address (BNC/backplane) that has been
    # configured as an output on THIS card during this session -- used
    # by safe_all_outputs() to actually stop driving them on teardown.
    # See clear_state()'s docstring for why this tracking exists: HOME
    # does NOT do this for you.
    _output_addrs: set = field(default_factory=set, init=False, repr=False)

    # ------------------------------------------------------------------
    # Pointer + raw cell/IO programming
    # ------------------------------------------------------------------
    def _select(self, addr: int):
        """Move the card's internal pointer to a cell or I/O address."""
        self.tiger.send_command(f"M {self.axis}={addr}", card_addr=self.card_addr)

    def configure_cell(
        self,
        cell_num: int,
        cell_type: str,
        config: Optional[int] = None,
        inputs: Optional[dict] = None,
    ):
        """
        Program one logic cell.

        cell_type: a key from CELL_TYPE (e.g. "jk_flop", "lut4", "and2").
        config: the CCA Z value -- LUT bits, one-shot/delay duration in
                clock pulses, etc. Meaning depends on cell_type; see
                CELL_TYPE table / ASI's PLC manual Table 1.
        inputs: dict with any of keys 'a','b','c','d' -> address (int).
                Use bnc_addr()/backplane_addr()/cell_addr()/inverted()/
                rising_edge()/falling_edge() to build addresses.
        """
        if cell_type not in CELL_TYPE:
            raise ValueError(f"Unknown cell_type {cell_type!r}; valid: {list(CELL_TYPE)}")
        self._select(cell_addr(cell_num))
        self.tiger.send_command(f"CCA Y={CELL_TYPE[cell_type]}", card_addr=self.card_addr)
        if config is not None:
            self.tiger.send_command(f"CCA Z={config}", card_addr=self.card_addr)
        if inputs:
            parts = []
            key_map = {"a": "X", "b": "Y", "c": "Z", "d": "F"}
            for k, addr in inputs.items():
                if k not in key_map:
                    raise ValueError(f"input key must be one of a,b,c,d, got {k!r}")
                parts.append(f"{key_map[k]}={addr}")
            self.tiger.send_command("CCB " + " ".join(parts), card_addr=self.card_addr)

    def configure_io(self, io_addr: int, io_type: int, source_addr: Optional[int] = None):
        """
        Configure a physical I/O (BNC or backplane line).

        io_addr: from bnc_addr(n) or backplane_addr(n).
        io_type: IO_TYPE_INPUT / IO_TYPE_OPEN_DRAIN_OUTPUT / IO_TYPE_PUSH_PULL_OUTPUT.
        source_addr: for outputs, which cell/I/O drives this line
                     (ignored for inputs).
        """
        self._select(io_addr)
        self.tiger.send_command(f"CCA Y={io_type}", card_addr=self.card_addr)
        if io_type != IO_TYPE_INPUT and source_addr is not None:
            self.tiger.send_command(f"CCA Z={source_addr}", card_addr=self.card_addr)

        if io_type == IO_TYPE_INPUT:
            self._output_addrs.discard(io_addr)
        else:
            self._output_addrs.add(io_addr)

    # ------------------------------------------------------------------
    # Card-level operations
    # ------------------------------------------------------------------
    def set_trigger_source(self, code: int):
        """Set the evaluation-cycle clock source (PM command). See TRIGGER_* constants."""
        self.tiger.send_command(f"PM {self.axis}={code}", card_addr=self.card_addr)

    def load_preset(self, preset_num: int):
        """Load one of ASI's built-in card presets (see PLC manual Tables 4-5)."""
        self.tiger.send_command(f"CCA X={preset_num}", card_addr=self.card_addr)

    def clear_state(self):
        """
        Resets stateful cells' STORED VALUE (a flip-flop's current output
        bit, a one-shot/delay's in-progress timer) via HOME on the PLC axis.

        IMPORTANT -- this does NOT stop live logic. HOME does not remove a
        cell's TYPE/config/input wiring (CCA Y/Z, CCB), and does not touch
        physical I/O configuration (a BNC still configured as an output,
        still sourced from a cell, keeps being driven). The PLC evaluates
        its programmed logic continuously in dedicated hardware, entirely
        independent of whether anything is connected to the serial port --
        confirmed on real hardware: a 2-laser toggle kept switching on
        every camera trigger well after the controlling script exited,
        because clear_state() alone was (wrongly) assumed to make things
        safe. Use safe_all_outputs() (or disable_two_laser_toggle() for
        that specific helper) to ACTUALLY stop driving physical outputs.

        (Also fixed here: this previously sent card_addr=None, inconsistent
        with every other method in this class, and was missing the space
        HOME's documented syntax uses -- caused a real TimeoutError. Fixed
        to '{card_addr}! {axis}'.)
        """
        self.tiger.send_command(f"! {self.axis}", card_addr=self.card_addr)

    def safe_all_outputs(self):
        """
        Reconfigures every physical I/O this PLCCard instance has set as
        an output (tracked automatically by configure_io()) back to
        IO_TYPE_INPUT -- the one thing that actually stops a BNC/backplane
        line from being driven, regardless of what logic is still
        programmed into the cells behind it.

        Call this whenever you're done with a PLC configuration, not just
        clear_state() -- see clear_state()'s docstring for why. Safe to
        call even if nothing was ever configured as an output (no-op).
        """
        for addr in list(self._output_addrs):
            self.configure_io(addr, IO_TYPE_INPUT)

    def disable_two_laser_toggle(
        self,
        laser0_bnc: int,
        laser1_bnc: int,
        toggle_cell: int = 1,
        gate0_cell: int = 2,
        gate1_cell: int = 3,
    ):
        """
        Reverses configure_two_laser_toggle(): sets both laser BNCs back to
        inputs FIRST (immediately stops driving them, regardless of cell
        state), then reprograms the toggle/gate cells to a harmless
        constant-0 output. Call with the SAME arguments used to configure
        it. safe_all_outputs() also covers the BNC side generically if you
        don't remember the exact cell numbers used.
        """
        self.configure_io(bnc_addr(laser0_bnc), IO_TYPE_INPUT)
        self.configure_io(bnc_addr(laser1_bnc), IO_TYPE_INPUT)
        for cell in (toggle_cell, gate0_cell, gate1_cell):
            self.configure_cell(cell, "constant", config=0)

    def save(self):
        """Persist cell config, I/O config, and trigger source to non-volatile memory."""
        self.tiger.send_command("SS Z", card_addr=self.card_addr)

    def read_cell_outputs(self) -> int:
        """16-bit int, bit n-1 = output of logic cell n (cell 1 = LSB)."""
        reply = self.tiger.send_command("RDADC Z?", card_addr=self.card_addr)
        return int(float(reply.split("=")[-1].split()[0]))

    def read_bnc_inputs(self) -> int:
        """8-bit int, bit n-1 = state of front-panel BNC n (BNC 1 = LSB)."""
        reply = self.tiger.send_command("RDADC X?", card_addr=self.card_addr)
        return int(float(reply.split("=")[-1].split()[0]))

    def read_backplane(self) -> int:
        """8-bit int, bit n = state of backplane TTL line n (TTL0 = LSB)."""
        reply = self.tiger.send_command("RDADC Y?", card_addr=self.card_addr)
        return int(float(reply.split("=")[-1].split()[0]))

    # ------------------------------------------------------------------
    # Ready-made helper: 2-laser toggle, gated by an external trigger
    # ------------------------------------------------------------------
    def configure_two_laser_toggle(
        self,
        trigger_source_addr: int,
        laser0_bnc: int,
        laser1_bnc: int,
        toggle_cell: int = 1,
        gate0_cell: int = 2,
        gate1_cell: int = 3,
    ):
        """
        Build the 2-laser alternation logic from ASI's PLC manual
        (Section 3.2/3.3): a toggle flip-flop advances one bit on every
        rising edge of `trigger_source_addr` (e.g. a camera frame or
        volume trigger); two AND gates then route the *same* trigger
        pulse to laser0 while the toggle is low, and to laser1 while
        the toggle is high -- so lasers alternate every trigger pulse
        with no host software in the loop.

        trigger_source_addr: address of your camera TTL, e.g.
            bnc_addr(1) if wired to BNC1, or backplane_addr(n) if it
            arrives via another card's backplane line.
        laser0_bnc / laser1_bnc: BNC connector numbers (1-8) to drive
            each laser's TTL modulation/shutter input.
        toggle_cell/gate0_cell/gate1_cell: which of the 16 logic cells
            to use (defaults to the first three, adjust if those are
            already in use by other logic you've built).

        CAUTION: verify the two BNC outputs with a scope (lasers
        disconnected or heavily attenuated) before wiring to real
        laser hardware -- confirm polarity and that only one output is
        ever high at a time before trusting this with optics.
        """
        trig_rising = rising_edge(trigger_source_addr)

        # Toggle flip-flop: JK with J=K=1 (always toggle), clocked by
        # the trigger's rising edge. CONST_HIGH = inverted(constant 0).
        self.configure_cell(
            toggle_cell, "jk_flop",
            inputs={"a": CONST_HIGH, "b": CONST_HIGH, "c": trig_rising},
        )
        toggle_out = cell_addr(toggle_cell)

        # gate0 = trigger AND (NOT toggle)  -> fires while toggle==0
        self.configure_cell(
            gate0_cell, "and2",
            inputs={"a": trig_rising, "b": inverted(toggle_out)},
        )
        # gate1 = trigger AND toggle        -> fires while toggle==1
        self.configure_cell(
            gate1_cell, "and2",
            inputs={"a": trig_rising, "b": toggle_out},
        )

        self.configure_io(bnc_addr(laser0_bnc), IO_TYPE_PUSH_PULL_OUTPUT, source_addr=cell_addr(gate0_cell))
        self.configure_io(bnc_addr(laser1_bnc), IO_TYPE_PUSH_PULL_OUTPUT, source_addr=cell_addr(gate1_cell))

    def route_to_dac_trigger(self, backplane_addr: int, source_addr: int):
        """
        Route a signal (typically another cell's output) onto a backplane
        line that a DAC card's single-axis function is listening to as
        its external trigger input -- ASI's confirmed architecture for
        e.g. triggering an ETL sawtooth from the PLC while a galvo runs
        free-running (see asi_tiger.singleaxis module docstring).

        backplane_addr: from asi_tiger.singleaxis.trigger_in_backplane_addr(axis_slot) --
                        NOT a bnc_addr()/cell_addr(), this is specifically
                        one of the four backplane trigger-in addresses
                        (42/44/46/48) documented for SAP.
        source_addr: whatever should drive the trigger -- another cell's
                     cell_addr(), a bnc_addr() input, rising_edge()/
                     falling_edge() of either, etc.

        Requires a physical jumper on the DAC card's SV9 header (per ASI)
        for that card to actually listen to the backplane line -- this
        method only handles the PLC side of the wiring.
        """
        self.configure_io(backplane_addr, IO_TYPE_PUSH_PULL_OUTPUT, source_addr=source_addr)

    def fan_out_trigger(self, source_addr: int, output_addrs):
        """
        Route ONE signal onto MULTIPLE physical outputs simultaneously --
        e.g. a single PLC-generated pulse feeding both a camera's
        external trigger input (a BNC) AND a DAC axis's backplane
        trigger-in line at once, so both branches share the exact same
        output transition rather than one relaying/re-deriving from the
        other. See the "shared trigger source" discussion in
        PATCHNOTES_ASI_TIGER.md for why this matters: two branches fed
        from the same edge have no timing uncertainty RELATIVE TO EACH
        OTHER, even though each branch's own absolute response latency
        (camera trigger-to-exposure, DAC trigger-to-ramp) still exists
        independently.

        source_addr: the trigger source -- typically a cell you pulse
                     from the host (e.g. via set_cell_state()), or an
                     external input.
        output_addrs: iterable of physical output addresses to drive
                      from source_addr simultaneously -- mix bnc_addr()
                      (e.g. camera trigger) and
                      asi_tiger.singleaxis.trigger_in_backplane_addr()
                      (e.g. an armed DAC axis) freely.
        """
        for addr in output_addrs:
            self.configure_io(addr, IO_TYPE_PUSH_PULL_OUTPUT, source_addr=source_addr)

    def configure_shutter_gate(
        self,
        trigger_source_addr: int,
        shutter_bnc: int,
        enable_cell: int = 4,
    ):
        """
        Simple AND-gated shutter/laser-enable line: output on
        `shutter_bnc` follows `trigger_source_addr` only while a
        software-controlled enable bit is set (via CCA F on
        `enable_cell`, a constant/D-flop cell).

        Toggle it from Python with:
            plc.set_cell_state(enable_cell, True)   # arm
            plc.set_cell_state(enable_cell, False)  # disarm
        """
        gate_cell = enable_cell + 1
        self.configure_cell(enable_cell, "d_flop", inputs={"a": CONST_HIGH, "b": CONST_LOW})
        self.configure_cell(
            gate_cell, "and2",
            inputs={"a": trigger_source_addr, "b": cell_addr(enable_cell)},
        )
        self.configure_io(bnc_addr(shutter_bnc), IO_TYPE_PUSH_PULL_OUTPUT, source_addr=cell_addr(gate_cell))

    def disable_shutter_gate(self, shutter_bnc: int, enable_cell: int = 4):
        """
        Reverses configure_shutter_gate(): shutter BNC back to input first
        (stops driving it immediately), then the enable/gate cells to a
        harmless constant-0. Same "clear_state() doesn't do this for you"
        reasoning as disable_two_laser_toggle() -- see clear_state()'s docstring.
        """
        gate_cell = enable_cell + 1
        self.configure_io(bnc_addr(shutter_bnc), IO_TYPE_INPUT)
        self.configure_cell(enable_cell, "constant", config=0)
        self.configure_cell(gate_cell, "constant", config=0)

    def set_cell_state(self, cell_num: int, high: bool):
        """Directly set a stateful cell's (flip-flop) output (CCA F)."""
        self._select(cell_addr(cell_num))
        self.tiger.send_command(f"CCA F={1 if high else 0}", card_addr=self.card_addr)
