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
from typing import Optional

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
    Sends 'TTL X=30' -- the card-wide input mode ASI's command:sam docs
    say TTL-triggered single-axis modes (SAM=2 or 4) require. This is a
    CARD-WIDE setting: calling it affects every axis on that card, not
    just the one you're about to trigger -- call it once per card, not
    once per axis, and be aware of what else on that card might depend
    on the previous TTL input mode.
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
    """

    tiger: TigerController
    card_addr: int
    axis: str

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

        amplitude_v: peak-to-peak amplitude in volts (converted to mV for SAA).
                     Negative values reverse ramp direction (see command:saa).
        offset_v: center position in volts (converted to mV for SAO) --
                  output will swing between offset-amplitude/2 and
                  offset+amplitude/2.
        period_ms: waveform period in milliseconds (internal clock).
                   Triangle/square patterns force this to an even number
                   of ms automatically (per command:saf). 1ms is
                   documented as undefined behavior -- avoid it.
        """
        amp_mv = int(round(amplitude_v * 1000))
        off_mv = int(round(offset_v * 1000))
        sap_code = build_sap_code(pattern, external_trigger=external_trigger, ttl_out=ttl_out)

        self.tiger.send_command(f"SAA {self.axis}={amp_mv}", card_addr=self.card_addr)
        self.tiger.send_command(f"SAO {self.axis}={off_mv}", card_addr=self.card_addr)
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
        Follow with an explicit M {axis}=0 (or ASITigerDAC.zero_all())
        if you need the physical output at 0V.
        """
        self.tiger.send_command(f"SAM {self.axis}=0", card_addr=self.card_addr)

    def arm_triggered(self, free_running: bool = False):
        """
        Arm for external TTL triggering instead of starting immediately.
        Requires configure(..., external_trigger=True) first (sets SAP
        bit 7) AND enable_backplane_trigger_mode(tiger, card_addr) called
        once for this card (module-level function above) -- both are
        prerequisites this method does NOT do for you, since the latter
        is a card-wide setting affecting every axis on the card.
        """
        mode = SAM_ARM_TRIGGER_FREE_RUN if free_running else SAM_ARM_TRIGGER_ONCE
        self.tiger.send_command(f"SAM {self.axis}={mode}", card_addr=self.card_addr)

    def is_active(self) -> bool:
        """
        Best-effort check via RDSTAT: while single-axis mode is active,
        the axis move-status character is 'A'. Returns False on any
        parse failure rather than raising, since exact RDSTAT reply
        formatting can vary.
        """
        try:
            reply = self.tiger.send_command(f"RDSTAT {self.axis}", card_addr=self.card_addr)
            return "A" in reply
        except Exception:
            return False
