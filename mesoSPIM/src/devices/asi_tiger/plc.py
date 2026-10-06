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
        plc.configure_io(bnc_addr(1), IO_TYPE_INPUT)   # e.g. external camera TTL on BNC1
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
    # Last state this instance wrote to each directly-driven (CCA F) cell,
    # used ONLY by set_cell_state() to skip redundant writes. REAL-HARDWARE
    # FINDING (live() frame-timing log): every set_cell_state() costs a
    # `M E=<cell>` pointer move that takes ~103 ms on this rack, and live()
    # was re-sending ~10 of them per frame for states that were already
    # set (laser lines re-cleared, camera cell cleared twice). The POINTER
    # position is deliberately NOT cached -- a skipped pointer move would
    # make a later CCA F land on the wrong cell (possibly the wrong laser);
    # skipping a write whose cell already holds the wanted state cannot.
    # Cleared on any error, any (re)configuration of that cell, and any
    # reset/clear/teardown call; see invalidate_state_cache().
    _cell_state_cache: dict = field(default_factory=dict, init=False, repr=False)

    # OPT-IN pointer tracking (default OFF). When True, _select() skips an
    # `M <axis>=<addr>` that repeats the last pointer position THIS instance
    # set, saving a ~103 ms serial round-trip on this rack. Only safe while
    # nothing else moves this card's pointer: the waveformer turns it on only
    # inside live() (stage polling paused, nothing else addresses this card)
    # and off again afterwards. Cleared on any error and by
    # invalidate_state_cache().
    track_pointer: bool = field(default=False, init=False)
    _ptr: Optional[int] = field(default=None, init=False, repr=False)

    def invalidate_state_cache(self):
        """Forget what we think every cell holds AND where the pointer is;
        the next set_cell_state() per cell / _select() is then always sent.
        Call after anything that may have changed cell state or the pointer
        behind this object's back."""
        self._cell_state_cache.clear()
        self._ptr = None

    def forget_pointer(self):
        """Forget only the pointer position (keep the cell-state cache), so
        the next _select() always sends its `M` move."""
        self._ptr = None

    # ------------------------------------------------------------------
    # Pointer + raw cell/IO programming
    # ------------------------------------------------------------------
    def _select(self, addr: int):
        """Move the card's internal pointer to a cell or I/O address.
        (Skipped when track_pointer is on and the pointer is already there.)"""
        if self.track_pointer and self._ptr == addr:
            return
        self._ptr = None  # unknown until the move is confirmed
        self.tiger.send_command(f"M {self.axis}={addr}", card_addr=self.card_addr)
        self._ptr = addr

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
        self._cell_state_cache.pop(cell_num, None)  # re-typing a cell resets its state
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

        ORDERING (real-hardware finding, see PATCHNOTES): CCA Y (I/O
        type) and CCA Z (source address) are two SEPARATE serial
        round-trips, not one atomic write. The previous version sent
        Y first, then Z -- meaning a pin transitioning to an output
        went briefly LIVE while CCA Z still held whatever source
        address was stored from the LAST time this address was
        configured (possibly a different script/session entirely,
        possibly hours earlier). If that stale source happened to be
        high at that exact moment, the pin would glitch high for the
        few-millisecond gap between the two commands -- a real,
        physically-driven edge, not just a logical inconsistency, and
        exactly the kind of thing a camera in external-trigger mode
        (which only needs a brief edge) would catch as a genuine
        trigger. Fixed by writing the source address FIRST (storing
        the register while the pin is still whatever it was before --
        doesn't yet drive anything) and the type LAST, so a pin only
        ever goes live already pointing at the intended source.
        Symmetrically, when reverting TO input, the source is also
        reset to constant-low (address 0) AFTER the type change, so a
        stale non-zero source can never again be inherited by some
        future configure_io() call on this same address that (for any
        reason) doesn't pass its own source_addr.
        """
        self._select(io_addr)
        if io_type != IO_TYPE_INPUT:
            self.tiger.send_command(
                f"CCA Z={source_addr if source_addr is not None else 0}", card_addr=self.card_addr
            )
            self.tiger.send_command(f"CCA Y={io_type}", card_addr=self.card_addr)
            self._output_addrs.add(io_addr)
        else:
            self.tiger.send_command(f"CCA Y={io_type}", card_addr=self.card_addr)
            self.tiger.send_command("CCA Z=0", card_addr=self.card_addr)
            self._output_addrs.discard(io_addr)

    # ------------------------------------------------------------------
    # Card-level operations
    # ------------------------------------------------------------------
    def reset_all_cells_and_io(self):
        """
        Unconditionally resets EVERY logic cell (1-16) to a harmless
        constant-0 type, and EVERY physical I/O (BNC 1-8, backplane 0-7)
        to IO_TYPE_INPUT -- regardless of what THIS PLCCard instance
        itself configured.

        This is different from safe_all_outputs(): that method only
        touches what this session's _output_addrs tracking set knows
        about (i.e. what this specific instance configured as an
        output), so it can't clean up leftover state from a DIFFERENT
        prior session/script/instance -- exactly the gap that caused a
        BNC to come back as an output after a real controller reset,
        traced to an earlier SS Z that saved whatever was live at the
        time, including cell config no session's tracking set knew
        about. This method has no such blind spot -- it touches every
        possible address regardless of history.

        Use this to establish a genuinely clean baseline BEFORE calling
        save() -- otherwise SS Z persists whatever happens to be live,
        cells included. This is deliberate and comprehensive: it WILL
        disrupt any currently running acquisition/logic on this card.
        Confirm nothing else depends on the current PLC state first.
        """
        self.invalidate_state_cache()
        for cell in range(1, 17):
            self.configure_cell(cell, "constant", config=0)
        for bnc in range(1, 9):
            self.configure_io(bnc_addr(bnc), IO_TYPE_INPUT)
        for bp in range(8):
            self.configure_io(backplane_addr(bp), IO_TYPE_INPUT)
        self._output_addrs.clear()

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
        self.invalidate_state_cache()
        self.tiger.send_command(f"! {self.axis}", card_addr=self.card_addr)

    def safe_all_outputs(self):
        """
        Reconfigures every physical I/O this PLCCard instance has set as
        an output (tracked automatically by configure_io()) to drive a
        hardwired constant LOW (CONST_LOW, address 0) -- NOT to
        IO_TYPE_INPUT as an earlier version of this method did.

        REAL-HARDWARE FINDING (root cause of a reported spurious camera
        exposure, order-dependent: only reproduced when a per-frame-test
        run preceded another run, never when a loop-test run did --
        traced to exactly this difference: ZStackTriggerChain.disarm()
        never changes any BNC's I/O TYPE at all, it only disarms Z's ring
        buffer and clears Z's OUT0 mode, so its BNCs stay live outputs the
        whole time; this method, by contrast, used to flip driven outputs
        straight to IO_TYPE_INPUT). Reverting a push-pull output that has
        been actively driving a line LOW straight to a floating,
        high-impedance INPUT can itself produce a brief glitch on some
        hardware -- the line is no longer being held low by anything for
        the instant between the old drive stopping and whatever the
        receiving device's own input biasing settles to, and a camera in
        external-trigger mode only needs a few microseconds of a rising
        edge to latch a real exposure. This is a plausible, electrically
        sound explanation for why ONLY this method's revert-to-input
        behavior (never ZStackTriggerChain.disarm()'s leave-as-output
        behavior) could produce a spurious trigger, independent of the
        separate CCA-Y/CCA-Z command-ordering race fixed in
        configure_io() (that fix alone did not resolve the report).

        Fixed: instead of releasing the line to a floating input, this
        method now keeps it as an OUTPUT but repoints its source to
        CONST_LOW (a hardwired constant-0 register, not any logic cell).
        This achieves the SAME original goal safe_all_outputs() exists
        for -- see clear_state()'s docstring: a live logic cell (e.g. a
        toggle that keeps switching on every camera trigger) must not be
        left driving a physical line after the host disconnects -- while
        also never producing a floating/undriven transition: the line
        stays continuously, actively driven low, permanently disconnected
        from any cell whose state could ever change again.

        Call this whenever you're done with a PLC configuration, not just
        clear_state() -- see clear_state()'s docstring for why. Safe to
        call even if nothing was ever configured as an output (no-op).
        """
        self.invalidate_state_cache()
        for addr in list(self._output_addrs):
            self.configure_io(addr, IO_TYPE_PUSH_PULL_OUTPUT, source_addr=CONST_LOW)

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
        """
        Persist cell config, I/O config, and trigger source to
        non-volatile memory (SS Z) -- CONFIRMED ON REAL HARDWARE this
        survives an actual controller reset/power cycle (not just a
        reconnected serial port -- that's a separate, much weaker kind
        of persistence covered by the safe_all_outputs()/clear_state()
        pattern used elsewhere in this module).

        CAUTION -- this saves the CARD'S ENTIRE CURRENT LIVE
        CONFIGURATION, not just whatever you were focused on changing.
        If any other cell/IO config happens to be live at the moment you
        call this (leftover test config, a different script's setup,
        etc.), that gets baked into the new power-on default too,
        possibly unintentionally and hard to notice until it resurfaces
        after some future reset. Confirmed as a real footgun: BNC I/O
        type reverted to a stale OUTPUT default after a real controller
        reset, traced to an earlier SS Z call that saved that state
        before anyone noticed it needed to be INPUT.

        Recommended safe pattern before calling this: clear_state() (or
        even better, safe_all_outputs() to release any live outputs)
        first, then explicitly (re)configure ONLY what you actually want
        persisted, THEN call save() -- so what gets written to
        non-volatile memory is deliberate, not whatever happened to be
        live at the time.

        Note the software-level defensive-reset pattern used in
        asi_tiger_galvo_etl_demo.py (forcing the trigger BNC to
        IO_TYPE_INPUT at the start of every run) does NOT depend on this
        at all -- it self-heals the live config every run regardless of
        the saved default, which is why that script kept working
        correctly even before this was traced to a stale saved default.
        save() is for making other tools/manual sessions against this
        controller start from a sane default, not a substitute for that
        defensive pattern.
        """
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

    def configure_pulse_catchers(self, addresses, reset_cell: int = 1, first_catcher_cell: int = 2):
        """
        Configures one "sticky latch" D-flop per address in `addresses`
        -- each catches ANY rising edge on that address and holds it
        high until reset. Lets you reliably detect a brief pulse (e.g.
        a TTL OUT0 move-complete pulse of unknown/short duration) via a
        slow serial RDADC poll afterward, regardless of how brief the
        real pulse was or how much serial round-trip latency sits
        between triggering it and reading back -- avoids needing a
        scope or precisely-timed polling for discovery/bench testing.

        reset_cell: a manually-controlled D-flop (D/clock tied low, the
            same pattern used for manual pulse sources elsewhere in
            this project) whose state you toggle via
            reset_pulse_catchers() to clear ALL catchers at once. Must
            not collide with the catcher cell range below.
        first_catcher_cell: first of len(addresses) CONSECUTIVE cells
            used as catchers, one per address in the given order.

        FIXED (found via real hardware testing): front-panel BNCs
        (addr 33-40) default to being configured as PUSH-PULL OUTPUTS,
        not inputs (backplane lines 41-48 default to input, which is
        why an earlier version of this method appeared to work for
        backplane addresses purely by accident of that different
        default -- it was never actually forcing input mode for
        either). A BNC address in `addresses` left at its default
        output type means the PLC is DRIVING that line instead of
        listening to it -- a real, confirmed pulse jumpered into such a
        BNC was never caught, not because of a timing issue, but
        because the input was never actually configured to listen.
        This method now explicitly forces every BNC address (33-40) in
        `addresses` to IO_TYPE_INPUT before wiring up its catcher.
        Backplane addresses are left alone (already default to input,
        and forcing them here would be harmless but redundant).

        Returns the list of catcher cell numbers, in the same order as
        `addresses` -- check these bits in read_cell_outputs()'s
        bitmask (bit = cell_num - 1) to see which address(es) caught a
        pulse since the last reset.
        """
        self.configure_cell(reset_cell, "d_flop", inputs={"a": 0, "b": 0, "c": 0})
        self.set_cell_state(reset_cell, False)

        catcher_cells = list(range(first_catcher_cell, first_catcher_cell + len(addresses)))
        for cell, addr in zip(catcher_cells, addresses):
            if 33 <= addr <= 40:
                self.configure_io(addr, IO_TYPE_INPUT)
            self.configure_cell(
                cell, "d_flop",
                inputs={"a": CONST_HIGH, "b": rising_edge(addr), "c": cell_addr(reset_cell)},
            )
        return catcher_cells

    def reset_pulse_catchers(self, reset_cell: int = 1):
        """Resets all catchers from configure_pulse_catchers() at once
        -- pulses the shared reset cell high then low via direct state
        control (CCA F), the same confirmed mechanism used for manual
        pulse sources elsewhere in this project."""
        self.set_cell_state(reset_cell, True)
        self.set_cell_state(reset_cell, False)

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

    def configure_pulse_pass_through_counter_single(
        self,
        pulse_in_addr: int,
        n_pulses: int,
        pulse_out_bnc: Optional[int] = None,
        cells=(1, 2, 3, 4, 5),
    ):
        """
        Passes an incoming pulse train through unchanged for exactly
        n_pulses, then blocks it, until reset_pulse_pass_through_counter()
        is called.

        Derived from ASI's own documented, customer-tested example --
        "Pass through pulse N*M times" on the TGPLC wiki page -- using a
        SINGLE one-shot counter instead of their original two cascaded
        ones. Their two-counter form multiplies two 16-bit counters to
        reach totals beyond 65535 (~65535^2); this project's realistic
        Z-stack plane counts fit comfortably within one counter's
        65535-pulse range, so the second stage was dropped -- fewer
        cells, and no prime-N gap (see CONSTRAINT below). The cell roles,
        addresses, and edge/invert combinations that remain are still
        cross-checked against ASI's literal example values, just with
        the outer-counter stage removed and the latch flop wired
        directly to this counter's own falling edge instead.

        CONSTRAINT (from how a one-shot's duration config works -- see
        command:sam/CCA Z docs: "If the duration is set to 0 then the
        output will never go high"): n_pulses must be >= 2, since the
        one-shot's config is (n_pulses-1), and a config of 0 breaks that
        cell's counting entirely.

        pulse_in_addr: address of the incoming pulse (e.g. bnc_addr(1)
                       or a backplane trigger address).
        pulse_out_bnc: OPTIONAL BNC number (1-8) for a physical monitor
                       of the gated output, purely for an optional scope
                       check -- the counter's REAL functional output
                       (e.g. driving a camera trigger via an OR gate, see
                       configure_zstack_trigger_chain()) already reads
                       cell_addr(cells[-1]) directly, not this BNC, so
                       nothing depends on it being wired. Default None:
                       no physical output configured at all -- pass an
                       explicit BNC only if you actually want to watch it
                       on a scope AND have confirmed that BNC isn't
                       already claimed by something else. FOUND ON REAL
                       PROJECT USE: an earlier default of 8 here silently
                       collided with row_setup.configure_laser_enable_lines()'s
                       default laser BNCs (5-8) once both were used
                       together -- with Z/camera (BNCs 1-4) and lasers
                       (5-8) now claiming all 8 physical BNCs on this
                       rack, there ISN'T a safe default BNC left for this
                       purely-optional feature, hence None.
        cells: which 5 cells to use, in role order (initialize, count,
               latch, delay, output-AND) -- defaults 1-5.
        """
        if n_pulses < 2:
            raise ValueError(
                f"n_pulses must be >= 2 (one-shot duration=0 never fires) -- got n_pulses={n_pulses}"
            )
        c_init, c_count, c_latch, c_delay, c_and = cells

        # cell 1: initialize/reset flag -- constant, held low during normal operation
        self.configure_cell(c_init, "constant", config=CONST_LOW)

        # cell 2: count -- one-shot (NRT), high for n_pulses then low.
        # Trigger and clock are the SAME signal (the incoming pulse's rising
        # edge); the clock input on the evaluation cycle when the trigger
        # fires is documented as ignored, so config=n_pulses-1 additional
        # edges are what's actually counted after the triggering one.
        self.configure_cell(
            c_count, "one_shot", config=n_pulses - 1,
            inputs={"a": rising_edge(pulse_in_addr), "b": rising_edge(pulse_in_addr), "c": cell_addr(c_init)},
        )

        # cell 3: latch flop -- latches high the first time the count is exhausted
        self.configure_cell(
            c_latch, "d_flop",
            inputs={"a": CONST_HIGH, "b": falling_edge(cell_addr(c_count)), "c": cell_addr(c_init)},
        )

        # cell 4: delay flop -- delays the stop signal by one pulse-in cycle so the
        # FINAL pulse still makes it through the output AND gate below
        self.configure_cell(
            c_delay, "d_flop",
            inputs={"a": cell_addr(c_latch), "b": falling_edge(pulse_in_addr), "c": cell_addr(c_init)},
        )

        # cell 5: output AND gate -- passes pulse_in through while not yet latched-stopped
        self.configure_cell(
            c_and, "and2",
            inputs={"a": pulse_in_addr, "b": inverted(cell_addr(c_delay))},
        )

        if pulse_out_bnc is not None:
            self.configure_io(bnc_addr(pulse_out_bnc), IO_TYPE_PUSH_PULL_OUTPUT, source_addr=cell_addr(c_and))
        if 33 <= pulse_in_addr <= 40:
            self.configure_io(pulse_in_addr, IO_TYPE_INPUT)

    def reset_pulse_pass_through_counter(self, init_cell: int = 1):
        """
        Re-arms configure_pulse_pass_through_counter() for another full
        count -- matches ASI's documented reset mechanism exactly: pulse
        the "initialize" cell's constant value low -> high -> low. Their
        script does this with 3 back-to-back raw CCA Z commands on the
        SAME already-selected cell (no re-selecting, no re-setting cell
        type in between) -- replicated here the same way rather than via
        3 separate configure_cell() calls, since re-sending CCA Y (even
        to the same type) is documented to clear the cell's prior
        config/state, which isn't the effect wanted for a clean pulse.
        """
        self.invalidate_state_cache()
        self._ptr = None
        self.tiger.send_command(f"M E={cell_addr(init_cell)}", card_addr=self.card_addr)
        self.tiger.send_command(f"CCA Z={CONST_LOW}", card_addr=self.card_addr)
        self.tiger.send_command(f"CCA Z={CONST_HIGH}", card_addr=self.card_addr)
        self.tiger.send_command(f"CCA Z={CONST_LOW}", card_addr=self.card_addr)

    def set_cell_state(self, cell_num: int, high: bool):
        """Directly set a stateful cell's (flip-flop) output (CCA F).

        Skipped entirely (no serial traffic) if this instance already wrote
        that same state to that cell and nothing has invalidated it since --
        see _cell_state_cache. Any failure invalidates the whole cache so
        the next call re-sends for real."""
        high = bool(high)
        if self._cell_state_cache.get(cell_num) is high:
            return
        try:
            self._select(cell_addr(cell_num))
            self.tiger.send_command(f"CCA F={1 if high else 0}", card_addr=self.card_addr)
        except BaseException:
            self._cell_state_cache.clear()
            self._ptr = None
            raise
        self._cell_state_cache[cell_num] = high

    def read_cell_state(self, cell_num: int) -> float:
        """
        CCA F? -- reads the currently-selected cell's STATE, which per
        ASI's own tiger_programmable_logic_card docs is NOT the same
        thing as its binary output (that's read_cell_outputs()/
        RDADC Z?). For a D-flop, state IS just the output (0 or 1). But
        for one-shots, delay cells, and counters, ASI's docs are
        explicit: state is "the current clock counter value... (counter
        decreases with each clock)" -- i.e. the internal countdown,
        readable directly, not just a derived done/not-done bit.

        Confirmed from ASI's own docs, NOT yet independently verified on
        real hardware in this project -- e.g. the exact starting value,
        direction, and value at exhaustion for a one-shot configured via
        configure_pulse_pass_through_counter_single() haven't been
        empirically confirmed here. See
        tools/asi_tiger_counter_state_test.py for that.
        """
        self._select(cell_addr(cell_num))
        reply = self.tiger.send_command("CCA F?", card_addr=self.card_addr)
        return float(reply.split("=")[-1].split()[0])
