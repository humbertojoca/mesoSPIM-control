"""
asi_tiger.singleaxis
======================
Wraps ASI Tiger's SINGLE_AXIS_FUNCTION firmware module (SAA/SAF/SAO/SAP/
SAM) -- genuine ON-CARD, hardware-timed waveform generation (sawtooth,
triangle, square, sine), running off a 4kHz internal clock (documented
as up to 40kHz on "fast DAC" axes on cards that have them -- see
https://asiimaging.com/docs/singleaxis).

This is categorically different from asi_tiger.waveform.WaveformStreamer:
there are NO per-sample serial commands here at all. You configure the
pattern once (SAA/SAO/SAF/SAP), start it once (SAM), and the card
generates the waveform completely autonomously in hardware -- the
"software-timed loop is ~1000x slower than NI" problem this was built
to work around simply does not apply to this path.

======================================================================
CONFIRMED WORKING on this firmware -- ASI's recommended architecture
======================================================================
Directly confirmed by ASI support (correcting an earlier, wrong reading
of theirs here: GALVO_SPIM is a SEPARATE, optional firmware they asked
about, NOT a prerequisite for this module -- single-axis function works
on the SIGNAL_DAC_4CH firmware already on these cards):

    "We can certainly trigger single-axis function waveforms with
    external TTL, particularly from the backplane. I think the idea is
    to use single-axis function on both the galvo and ETL DAC cards,
    where the galvo is free-running, and the etl sawtooth wave is
    triggered across the backplane, from the TGPLC card."

Concretely:
  - Galvo card (7 = A/B/C/D): free-running, internal clock -- configure()
    + start() (SAM=1), no external trigger needed.
  - ETL card (4 = H/I/J/K): configure(..., external_trigger=True) +
    arm_triggered(), so its sawtooth is stepped by a TTL pulse the PLC
    sends across the backplane -- keeping the ETL ramp synchronized to
    whatever the PLC is already gating (e.g. camera exposure), without
    the galvo and ETL needing to share a clock directly.

Hardware requirement (ASI, confirmed): a jumper on header SV9 on the
DAC card that needs to RECEIVE a backplane trigger (the ETL card in
this scheme) -- not needed on a card left free-running (the galvo card).

Backplane addresses per axis slot (0-3 = 1st-4th channel on the card,
e.g. H/I/J/K -> slots 0/1/2/3), confirmed by ASI and matching
SAP_TRIGGER_IN_ADDR/SAP_TTL_OUT_ADDR below exactly:

    Slot | Trigger IN (backplane addr) | TTL OUT (backplane addr)
    -----|------------------------------|---------------------------
      0  |             42               |            41
      1  |             44               |            43
      2  |             46               |            45
      3  |             48               |            47

To route a PLC signal onto a DAC axis's trigger-in line, use
trigger_in_backplane_addr(slot) with PLCCard.configure_io() -- e.g.
plc.configure_io(trigger_in_backplane_addr(1), IO_TYPE_PUSH_PULL_OUTPUT,
source_addr=<some PLC cell>) to trigger slot 1 (e.g. axis I on the ETL
card) from that cell's output. See PLCCard.route_to_dac_trigger() for a
convenience wrapper, and tools/asi_tiger_galvo_etl_demo.py for the full
worked example matching ASI's suggested architecture.
======================================================================

======================================================================
UPDATE: new firmware for card 37 (galvo), confirmed on real hardware
======================================================================
ASI provided a new firmware for card 37 that fixes the original galvo
speed limitation: 40kHz update rate on axes A and C, normal ~1kHz on
B/D -- the OPPOSITE of the original SIGNAL_DAC_4CH firmware, where B/D
were the fast pair. Bench-confirmed: 100Hz triangle waveforms on A/C
are visibly smooth (no stepping) with this firmware, vs. the visible
stepping seen at 50Hz on the original firmware's B/D axes.

CRITICAL: this firmware also changes SAA/SAO's units (confirmed by ASI
directly, not inferred): control range is -4000..4000 for the SAME
+/-10.24V that the original firmware represents as -10240..10240 (mV).
Sending amplitude/offset values computed for the old millivolt
convention would command roughly 2.56x the intended values. Pass
units_per_volt=asi_tiger.dac.UNITS_PER_VOLT_TGGALVO (~390.625, exactly
4000/10.24) to SingleAxisWaveform's constructor for any axis running
this firmware -- the default (1000.0) is only correct for the original
SIGNAL_DAC_4CH firmware, still running on cards 34 and 35. ASI confirmed
SAF/SAP/SAM and the BACKLASH (B) filter-cutoff command are unaffected --
only SAA/SAO's units changed. Plain M/W (asi_tiger.dac.ASITigerDAC) on
this same axis need the identical units_per_volt for the same reason --
see DacChannel.units_per_volt.

NOT YET CONFIRMED: whether PR/range_code (the SIGNAL_DAC_4CH card-wide
range selection) behaves the same way on this new firmware, which
appears to have a fixed native range rather than PR's original
selectable codes. range_code is still accepted for these channels but
treat it as unverified on this firmware specifically.
======================================================================

Units: SAA (amplitude) and SAO (offset) are documented as "axis units".
For SIGNAL_DAC_4CH, the MOVE/WHERE (M/W) commands use millivolts -- this
module assumes SAA/SAO follow that same convention and converts
volts<->mV accordingly. That assumption is a best guess by analogy, NOT
confirmed from ASI's docs for this specific card -- verify actual output
amplitude with a scope, don't trust the number blind.

SAFETY: like the PLC (see plc.py's clear_state() docstring), an active
single-axis pattern keeps running in hardware once started, independent
of the host connection. stop() (SAM=0) halts the pattern but does NOT
necessarily return the output to 0V -- always follow it with an M/
zero_all() to actually zero the physical output if that matters for
your setup. Also see dac.py's DacChannel.safety_limit_mv/max_step_v --
ASI's confirmed galvo-card voltage limit (+/-10.00V, not the card's full
+/-10.24V hardware range) and their warning about sudden voltage jumps
apply here too: amplitude_v/offset_v below should respect the same
bounds as plain M-command voltages on the same axis.
"""

from dataclasses import dataclass
import time

from .controller import TigerController

PATTERN_SAWTOOTH = 0
PATTERN_TRIANGLE = 1           # period forced to an even number of ms
PATTERN_SQUARE = 2             # period forced to an even number of ms
PATTERN_SINE = 3
PATTERN_VARIABLE_TRIANGLE = 4  # Tiger firmware v3.55+ required

SAM_IDLE = 0                  # stop the pattern
SAM_ACTIVE = 1                 # start the pattern (internal clock, immediate)
SAM_ARM_TRIGGER_ONCE = 2       # Tiger 3.31+: wait for external TTL, cycle once
SAM_ACTIVE_SYNC = 3            # start + resync every other single-axis pattern on this card
SAM_ARM_TRIGGER_FREE_RUN = 4   # Tiger 3.41+: wait for external TTL, then free-run

# Backplane trigger in/out addresses per axis slot (0-3 = 1st-4th channel
# on a card, e.g. H/I/J/K -> 0/1/2/3), for external-clock/TTL-triggered
# single-axis mode. Confirmed directly by ASI support (see module
# docstring) -- matches command:sap's table exactly.
SAP_TRIGGER_IN_ADDR = {0: 42, 1: 44, 2: 46, 3: 48}
SAP_TTL_OUT_ADDR = {0: 41, 1: 43, 2: 45, 3: 47}


def trigger_in_backplane_addr(axis_slot: int) -> int:
    """Backplane address to DRIVE (from a PLC cell, typically) to trigger
    axis slot 0-3's single-axis waveform to advance/start."""
    if axis_slot not in SAP_TRIGGER_IN_ADDR:
        raise ValueError("axis_slot must be 0-3 (1st-4th channel on the card)")
    return SAP_TRIGGER_IN_ADDR[axis_slot]


def ttl_out_backplane_addr(axis_slot: int) -> int:
    """Backplane address that axis slot 0-3 pulses on its own single-axis
    cycle completion, when configure(..., ttl_out=True) is used."""
    if axis_slot not in SAP_TTL_OUT_ADDR:
        raise ValueError("axis_slot must be 0-3 (1st-4th channel on the card)")
    return SAP_TTL_OUT_ADDR[axis_slot]


def axis_slot_index(axis: str, card_first_axis: str) -> int:
    """
    Compute an axis's slot index (0-3) from its letter and the card's
    first axis letter (e.g. axis_slot_index('K', 'H') -> 3, since H/I/J/K
    are slots 0/1/2/3). Axis letters are assumed consecutive, which holds
    for every card on this rack (A-D, H-K, P-S) -- verify with the 'N'
    command if you're using a different card layout.
    """
    slot = ord(axis.upper()) - ord(card_first_axis.upper())
    if not 0 <= slot <= 3:
        raise ValueError(f"{axis!r} is not within 4 letters of {card_first_axis!r}")
    return slot


def enable_backplane_trigger_mode(tiger: TigerController, card_addr: int):
    """
    Sends 'TTL X=30' -- the card-wide input mode required for
    TTL-triggered single-axis modes (SAM=2 or 4). Confirmed from ASI's
    command:ttl docs (mode 30's description): "Mode 2: On the rising
    edge of a TTL pulse, the routine is performed once. Mode 4: ...runs
    continuously" -- and confirmed on real hardware that mode 2 responds
    to EVERY genuine rising edge while armed, not just the first (see
    SingleAxisWaveform.arm_triggered()'s docstring for the full story,
    including an earlier wrong finding here that's now corrected).

    This is a CARD-WIDE setting: calling it affects every axis on that
    card, not just the one you're about to trigger -- call it once per
    card, not once per axis, and be aware of what else on that card
    might depend on the previous TTL input mode.
    """
    tiger.send_command("TTL X=30", card_addr=card_addr)


def build_sap_code(
    pattern: int,
    external_trigger: bool = False,
    trigger_negative_edge: bool = False,
    ttl_out: bool = False,
    ttl_out_active_low: bool = False,
) -> int:
    """
    Build the SAP bit-mapped configuration code (see command:sap):
      bit 7: 0=internal trigger, 1=external trigger on backplane TTL in
      bit 6: 0=positive edge, 1=negative edge (trigger polarity)
      bit 5: 0=no TTL out, 1=TTL out pulse at start of each cycle
      bit 4: 0=TTL out active high, 1=active low
      bits 2-0: pattern type (see PATTERN_* constants)
    """
    if not 0 <= pattern <= 4:
        raise ValueError("pattern must be 0-4 (sawtooth/triangle/square/sine/variable-triangle)")
    code = pattern
    if external_trigger:
        code |= (1 << 7)
    if trigger_negative_edge:
        code |= (1 << 6)
    if ttl_out:
        code |= (1 << 5)
    if ttl_out_active_low:
        code |= (1 << 4)
    return code


@dataclass
class SingleAxisWaveform:
    """
    One axis's single-axis (on-card, hardware-timed) waveform generator.

    Usage:
        saw = SingleAxisWaveform(tiger, card_addr=37, axis="A")
        saw.configure(pattern=PATTERN_SAWTOOTH, amplitude_v=2.0,
                      offset_v=0.0, period_ms=20)   # 50 Hz sawtooth
        saw.start()
        ...
        saw.stop()   # halts the pattern -- does NOT zero the output, see module docstring

    units_per_volt: raw device units per volt for SAA/SAO, matching
        asi_tiger.dac.DacChannel.units_per_volt's meaning exactly.
        Defaults to 1000.0 (SIGNAL_DAC_4CH's native millivolt units).
        CONFIRMED BY ASI: their new TGGALVO-derived firmware for card 37
        uses a control range of -4000..4000 for the SAME +/-10.24V that
        the original firmware represents as -10240..10240 (mV) -- i.e.
        UNITS_PER_VOLT_TGGALVO (~390.625), NOT 1000. Using the wrong
        value here would command roughly 2.56x the intended
        amplitude/offset on that firmware -- pass
        asi_tiger.dac.UNITS_PER_VOLT_TGGALVO explicitly for any axis on
        that firmware. ASI confirmed SAA/SAO are the only single-axis
        commands affected by this unit change; SAF/SAP/SAM are unchanged.
    """

    tiger: TigerController
    card_addr: int
    axis: str
    units_per_volt: float = 1000.0

    def configure(
        self,
        pattern: int,
        amplitude_v: float,
        offset_v: float,
        period_ms: float,
        external_trigger: bool = False,
        ttl_out: bool = False,
    ):
        """
        Configure (but do not start) the waveform. Call start() separately.

        amplitude_v: peak-to-peak amplitude in volts (converted to raw
                     units via units_per_volt for SAA). Negative values
                     reverse ramp direction (see command:saa).
        offset_v: center position in volts (converted to raw units via
                  units_per_volt for SAO) -- output will swing between
                  offset-amplitude/2 and offset+amplitude/2.
        period_ms: waveform period in milliseconds (internal clock).
                   Triangle/square patterns force this to an even number
                   of ms automatically (per command:saf). 1ms is
                   documented as undefined behavior -- avoid it.
        """
        amp_raw = int(round(amplitude_v * self.units_per_volt))
        off_raw = int(round(offset_v * self.units_per_volt))
        sap_code = build_sap_code(pattern, external_trigger=external_trigger, ttl_out=ttl_out)

        self.tiger.send_command(f"SAA {self.axis}={amp_raw}", card_addr=self.card_addr)
        self.tiger.send_command(f"SAO {self.axis}={off_raw}", card_addr=self.card_addr)
        self.tiger.send_command(f"SAF {self.axis}={period_ms:.0f}", card_addr=self.card_addr)
        self.tiger.send_command(f"SAP {self.axis}={sap_code}", card_addr=self.card_addr)

    def start(self, sync: bool = False):
        """
        Start the pattern (SAM=1). sync=True (SAM=3) also restarts every
        other active single-axis pattern on the SAME card, so multiple
        axes stay phase-synchronized (e.g. galvo + a second scan axis).
        """
        mode = SAM_ACTIVE_SYNC if sync else SAM_ACTIVE
        self.tiger.send_command(f"SAM {self.axis}={mode}", card_addr=self.card_addr)

    def stop(self):
        """
        Stop the pattern (SAM=0) -- returns to idle. Does NOT zero the
        output voltage; the axis holds wherever the waveform left it.

        CONFIRMED ON REAL HARDWARE: SAM=0 is not optional cleanup, it's a
        hard prerequisite for M to work on this axis AT ALL. While SAM!=0
        (including after a one-shot SAM=2 cycle finishes -- it does NOT
        return to SAM=0 on its own), a plain M command is silently
        ACCEPTED (':A') but has no effect on the output -- no error, no
        indication anything was ignored. Always call stop() (or
        stop_and_zero() below) before M, not the other way around, and
        do this EVEN IF you don't believe single-axis mode was ever
        started on this axis in the current process/session -- a leftover
        state from a previous run/process has exactly this effect, and
        there's no way to query it that's simpler than just calling
        stop() defensively before your first M command on any axis you
        haven't explicitly zeroed-and-confirmed already.
        """
        self.tiger.send_command(f"SAM {self.axis}=0", card_addr=self.card_addr)

    def stop_and_zero(self, settle_s: float = 0.05):
        """
        stop() followed by M {axis}=0 -- the safe sequence, in the
        correct order. Use this instead of hand-rolling stop()+M=0
        wherever you need an axis at a known-safe 0V state, including
        defensively at the START of a script (see stop()'s docstring for
        why "M=0 first, just in case" does NOT work if the axis is
        already in single-axis mode from a previous session).

        settle_s: delay between SAM=0 and M=0. UNCONFIRMED whether this
        is actually needed -- added defensively in case the card needs
        time to internally finish releasing single-axis control before
        accepting a move command; harmless if it turns out not to be
        necessary. If M still doesn't take effect even with this delay,
        the cause is something other than a settle-time race -- see
        stop()'s docstring and check RDSTAT/W after SAM=0 rather than
        assuming a longer delay would help.
        """
        self.stop()
        if settle_s > 0:
            time.sleep(settle_s)
        self.tiger.send_command(f"M {self.axis}=0", card_addr=self.card_addr)

    def arm_triggered(self, free_running: bool = False):
        """
        Arm for external TTL triggering instead of starting immediately.
        Requires configure(..., external_trigger=True) first (sets SAP
        bit 7) AND enable_backplane_trigger_mode(tiger, card_addr) called
        once for this card (module-level function above) -- both are
        prerequisites this method does NOT do for you, since the latter
        is a card-wide setting affecting every axis on the card.

        free_running=False (SAM=2, "once"): CONFIRMED on real hardware
        that this DOES auto-rearm -- one cycle per genuine rising edge,
        repeatedly, with no host intervention needed between triggers.
        ASI's own TTL X=30 docs say this plainly ("on the rising edge of
        a TTL pulse, the routine is performed once" -- describing each
        edge, not just the first), and this was independently confirmed
        by testing: arm ONCE with arm_triggered(), then send 3 separate
        clean low->high->low pulses -- all 3 produced a fresh ramp, each
        one starting right at its pulse's rising edge.

        AN EARLIER VERSION OF THIS DOCSTRING CLAIMED THE OPPOSITE (did
        not auto-rearm) -- that was wrong, and traced to a testing
        artifact: holding the trigger line high (a level) rather than
        producing a genuine edge each time only ever generates ONE
        rising edge no matter how long you hold it, which looks
        identical to "no auto-rearm" on a scope but isn't the same
        thing. Pulse cleanly (low->high->low) if you're testing this
        yourself, not by holding a line at a fixed level.

        Practical implication: arm ONCE at acquisition start, and every
        subsequent real camera trigger (or PLC-generated pulse) drives a
        correctly-timed cycle autonomously in hardware -- no per-frame
        host round-trip required, no serial-latency budget to worry
        about. This is the mode to use for real per-frame ETL sync, not
        free_running=True below.

        free_running=True (SAM=4): runs continuously after the FIRST
        trigger, on its OWN internal clock -- it does not re-sync to
        subsequent triggers at all. Because it and the trigger source
        (e.g. a camera) are independent clocks, their relative phase
        will drift continuously and non-monotonically over time, even if
        both are nominally set to the same period -- this looks like
        "jitter" on a scope across many cycles, but it's clock-drift
        beat frequency, not trigger-response jitter, and is not fixable
        by anything in software. Given free_running=False now confirmed
        to auto-rearm correctly, there's little reason to prefer this
        mode for camera-synced scanning.
        """
        mode = SAM_ARM_TRIGGER_FREE_RUN if free_running else SAM_ARM_TRIGGER_ONCE
        self.tiger.send_command(f"SAM {self.axis}={mode}", card_addr=self.card_addr)

    def is_active(self) -> bool:
        """
        Checks via RDSTAT [axis]+ (the '+' qualifier is required -- see
        command:rdstat: without it, RDSTAT returns a raw numeric status
        code, not the move-status character. Confirmed on real hardware
        that the bare 'RDSTAT {axis}' form used here previously returned
        a plain number like '130', making the old "'A' in reply" check
        silently useless -- it would never correctly detect active
        single-axis mode, just always return False without erroring).

        With '+', the reply is the single right-side status character
        shown on ASI's LCD: 'A' = single axis move (what we're checking
        for), 'M'/'B'/'K'/'S'/'T'/'P'/'E' = other move states, or a SPACE
        for idle/no event -- confirmed on real hardware that this shows
        up here as an EMPTY string, not a literal " ", because
        TigerController.send_command() strips whitespace from every
        reply. That's expected and correct: "A" in "" is already False,
        so idle is correctly detected as not-active. Just don't mistake
        the empty reply for a communication failure if you're debugging
        this by hand -- it's the confirmed-idle signal, not an error.
        """
        try:
            reply = self.tiger.send_command(f"RDSTAT {self.axis}+", card_addr=self.card_addr)
            return "A" in reply
        except Exception:
            return False
