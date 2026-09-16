"""
asi_tiger.stage_trigger
=========================
Wraps ASI Tiger's RING BUFFER mechanism (RM/LOAD/TTL X=1|12) for
TTL-triggered stage motion. Confirmed on real hardware for the Z/Theta
stage card (addr 32): a software-simulated trigger (bare RM) moved the
axis correctly, AND a real electrical pulse (PLC BNC jumpered to the
card's physical TRIG IN) also moved it correctly, using TTL X=12
(relative ring-buffer mode) -- the natural mode for Z-stack stepping.

IMPORTANT -- THE BUFFER WRAPS AROUND, IT DOES NOT EXHAUST AND STOP.
Confirmed from ASI's own ring buffer documentation
(https://asiimaging.com/docs/ring_buffer): "Each press of the @ button
causes the stage to advance to the next position. When you reach the
last position, the next press... will take you back to the first
position." An earlier assumption here that loading exactly N points
might make the buffer naturally stop after N triggers ("N then stop"
for free) was untested and turned out to be FALSE -- it's a genuine
circular buffer. This means: (1) the PLC pulse-pass-through counter
(asi_tiger.plc's configure_pulse_pass_through_counter_single()) is
what actually needs to gate "stop after N frames" in the acquisition
loop -- the ring buffer will not do this on its own; (2) usefully, for
UNIFORM stepping (the real Z-stack use case), loading exactly ONE
relative step means every trigger wraps to that same single entry --
so you never need more than 1 loaded point regardless of stack size,
which sidesteps the buffer's documented 50-position default (250 on
request from ASI) capacity limit entirely. Recommended architecture:
  - Row-level (once, slow): absolute MOVE to Z_start.
  - Row-level (once, slow): LOAD exactly one relative step, arm TTL X=12.
  - PLC pulse-pass-through counter gates the actual "stop after N".
See tools/asi_tiger_ring_buffer_wraparound_test.py, which tests this
directly (both single-entry consistency and multi-entry wraparound).

SCOPE -- READ BEFORE USING THIS ON A DAC AXIS:
This module is for STAGE axes (cards with STD_XY/STD_Z_ROT_SRS-type
firmware), not DAC axes. The DAC/ETL/galvo cards already have a
separate, extensively real-hardware-validated triggering mechanism --
asi_tiger.singleaxis's SAM/SAP (SINGLEAXIS_FUNCTION), including finding
a genuine per-axis hardware fault on one channel (see
PATCHNOTES_ASI_TIGER.md). A diagnostic script explored an alternative
ring-buffer-based approach (TTL X=1, absolute mode) for DAC cards too,
but never confirmed it working on DAC hardware -- its own code hedges
"skip if the DAC card has no separate TTL IN BNC" without a working
backplane-triggered result to point to. Deliberately NOT adopted here,
to avoid two competing mechanisms for the same job when only one is
actually proven. Use SingleAxisWaveform for ETL/galvo; use this module
for stage axes only.

Command reference:
  - LOAD <axis>=<value>: NOT card-addressed -- the axis letter alone
    identifies the card. Stores ONE ring-buffer position; does NOT
    move anything by itself. Units: tenths of a micron for stage axes.
  - [addr]RM X=0: clears the ring buffer for that card. Confirmed
    necessary before loading fresh points -- stale entries from a
    previous run/config can otherwise interfere.
  - [addr]RM Y=<mask>: selects which axes on that card respond to a
    ring-buffer trigger -- bit 0 is the FIRST axis listed on that card
    (e.g. bit0=Z on card 32, which lists axes as "Z,T"), bit1 the
    second, etc.
  - Bare [addr]RM (no arguments): simulates a TTL IN0 pulse purely in
    software. CONFIRMED ON REAL HARDWARE as a config-only self-test,
    independent of any wiring -- if this moves the axis but a real
    electrical/backplane trigger doesn't, the problem is wiring/
    routing, not configuration; if even this doesn't move it, the
    problem is in the RM/LOAD/TTL setup itself.
  - [addr]TTL X=<mode>: arms the ring-buffer trigger response mode --
    0=disarmed, 1=absolute (move to next stored position, NOT
    independently confirmed on real hardware in this module's own
    testing), 12=relative (move by the stored amount -- CONFIRMED for
    Z-stack stepping).
  - [addr]WHERE <axis>: reports current position -- used here for
    before/after verification, not a fully-typed general query API.
"""

from dataclasses import dataclass
from typing import Optional
import time

from .controller import TigerController

TTL_MODE_DISARMED = 0
TTL_MODE_ABSOLUTE = 1   # move to next stored ring-buffer position -- not independently confirmed here
TTL_MODE_RELATIVE = 12  # move by the stored relative amount -- CONFIRMED for Z-stack stepping

# RM F=<mode> -- the ring buffer's own OPERATING MODE, distinct from TTL X=
# above (which selects the AXIS CARD's response to an external trigger).
# CONFIRMED ON REAL HARDWARE (both the mode transitions and the resulting
# RM X? readings): this card's actual capacity is 50 positions (not the
# optional 250 -- contact ASI for that upgrade). Per ASI's command:rbmode
# docs:
#   F=1 (TTL_TRIGGERED, default): moves to the next position and WRAPS
#       around at the end -- this is the mode used throughout this
#       project (see the module docstring's wraparound architecture).
#       RM X? here reports the number of USED positions.
#   F=0 (CONSUME): a trigger CONSUMES a position if one exists -- genuine
#       hardware exhaust-and-stop behavior, unlike F=1. Confirmed on real
#       hardware: entering this mode reduces usable capacity to 49 (the
#       docs say "reduces the capacity of the ring buffer by 1"), and
#       switching F CLEARS the buffer and resets indices (both directions:
#       entering OR leaving consume mode). RM X? here reports the number
#       of OPEN (not used) positions. NOT used in this project's chosen
#       architecture -- 49 positions is too few for realistic Z-stack
#       sizes; the PLC pulse-pass-through counter (asi_tiger.plc) has no
#       such ceiling and is what this project uses for "stop after N"
#       instead. Documented here for completeness/future reference.
#   F=2/3/4: autoplay modes (one-shot, repeating, one-shot-no-return) --
#       not evaluated for this project, included for completeness.
RB_MODE_CONSUME = 0
RB_MODE_TTL_TRIGGERED = 1  # default
RB_MODE_ONESHOT_AUTOPLAY = 2
RB_MODE_REPEAT_AUTOPLAY = 3
RB_MODE_ONESHOT_AUTOPLAY_NO_RETURN = 4  # Tiger v3.45+

# TTL Y=<mode> -- this card's OUT0_mode, confirmed from ASI's command:ttl
# docs. OUT0_MODE_MOVE_COMPLETE (2) is what the design doc's self-
# sustaining loop needs -- see set_output_mode()'s docstring for the
# important caveat about WHERE this pulse actually appears (card's own
# physical OUT connector by default, not necessarily a backplane line).
OUT0_MODE_LOW = 0
OUT0_MODE_HIGH = 1
OUT0_MODE_MOVE_COMPLETE = 2  # pulse at end of MOVE/MOVREL/ring-buffer/array move


def load_ring_buffer_point(tiger: TigerController, axis: str, value: float):
    """
    LOAD <axis>=<value> -- NOT card-addressed, the axis letter alone
    identifies the card (confirmed real Tiger command quirk -- don't
    prefix this one with a card address the way most other commands
    need). Stores one position/step in the ring buffer. Does NOT move
    anything by itself -- only a trigger (electrical, backplane, or
    StageRingBuffer.software_trigger()) does that.
    """
    tiger.send_command(f"LOAD {axis}={value}")


@dataclass
class StageRingBuffer:
    """
    One card's ring-buffer trigger configuration. Confirmed on real
    hardware for the Z/Theta stage card (addr 32) via both a
    software-simulated trigger and a real electrical pulse.

    Usage (relative/Z-stepping, the confirmed real-hardware pattern):
        z = StageRingBuffer(tiger, card_addr=32, axis="Z", axis_mask=1)
        z.clear()
        z.load_relative_step(step_tenths_of_micron)
        z.arm_relative()
        # ... trigger arrives (electrical/backplane, or z.software_trigger()) ...
        z.disarm()

    axis_mask: bit0 = first axis listed on this card, bit1 = second,
        etc. -- e.g. 1 for Z on card 32 (axes listed "Z,T").
    """

    tiger: TigerController
    card_addr: int
    axis: str
    axis_mask: int

    def clear(self):
        """
        RM X=0 -- clears the ring buffer for this card, THEN explicitly
        resets the read index (RM Z=0). Do this before loading fresh
        points.

        FOUND ON REAL HARDWARE: ASI's docs only document the read/write
        indices being reset by an F-mode transition (see RB_MODE_*
        comment above), NOT by RM X=0 clearing. Without the explicit
        Z=0 reset here, a fresh clear()+load() sequence can start
        reading from wherever the pointer was left by a PREVIOUS
        session's triggers, not from the first newly-loaded point --
        confirmed via a real-hardware run where a second test's
        multi-entry sequence was offset by 2 positions relative to what
        was loaded, exactly consistent with a stale pointer carried
        over from an earlier single-entry test. Explicit Z=0 makes
        clear() actually mean "start fresh," not just "empty the values."
        """
        self.tiger.send_command("RM X=0", card_addr=self.card_addr)
        self.set_read_index(0)

    def set_read_index(self, index: int):
        """RM Z=<index> -- sets the read index (which loaded position
        the NEXT trigger will use), zero-indexed."""
        self.tiger.send_command(f"RM Z={index}", card_addr=self.card_addr)

    def query_read_index(self) -> str:
        """RM Z? -- reads back the current read index (raw reply string).
        Use this to directly observe which loaded position is about to
        fire, rather than inferring it from position deltas."""
        return self.tiger.send_command("RM Z?", card_addr=self.card_addr)

    def load_relative_step(self, step: float):
        """Queues one relative step (tenths of a micron for stage
        axes). Does not move anything until a trigger fires."""
        load_ring_buffer_point(self.tiger, self.axis, step)

    def load_absolute_point(self, position: float):
        """Queues one absolute position. Does not move anything until a
        trigger fires."""
        load_ring_buffer_point(self.tiger, self.axis, position)

    def set_axis_mask(self, mask: Optional[int] = None):
        """RM Y=<mask> -- which axes on this card respond to a trigger.
        Defaults to this instance's axis_mask if not overridden."""
        self.tiger.send_command(f"RM Y={mask if mask is not None else self.axis_mask}", card_addr=self.card_addr)

    def query_axis_mask(self) -> str:
        """RM Y? -- reads back the currently configured mask, as a raw
        reply string. Cheap sanity check (e.g. "did bit0 really land on
        the axis I expected"), not a fully-parsed query API."""
        return self.tiger.send_command("RM Y?", card_addr=self.card_addr)

    def arm_relative(self, settle_s: float = 0.0):
        """
        TTL X=12 -- CONFIRMED on real hardware as the relative
        ring-buffer trigger mode (the mode for Z-stack stepping). Sets
        the axis mask first (RM Y=<axis_mask>).

        CONFIRMED ON REAL HARDWARE AND FIXED: arming itself silently
        advances the read index by one position (Z=0 -> Z=1 observed
        with ZERO triggers sent in between, isolated by checking the
        index right after load vs. right after arm) -- an undocumented
        side effect, not something in this library's control to
        prevent. Compensated for by explicitly resetting the index to 0
        again immediately after arming. CONFIRMED FIXED on a follow-up
        real-hardware run: a 3-entry sequence that previously showed a
        systematic offset (Z starting at 1 instead of 0) now starts at
        Z=0 and cycles exactly as expected, with only ordinary +/-1
        (tenths-of-micron) settling noise remaining -- the same noise
        floor seen on single-entry runs, not a counting error.

        SEPARATE, benign observation from the same testing: arming can
        sometimes (not consistently) produce a small (~1 loaded-step
        magnitude) physical move on its own, before any real trigger --
        most likely ordinary backlash/static-friction take-up on first
        motor engagement after the stage sits idle, NOT a ring-buffer
        logic issue (the read index stays correct throughout regardless
        of whether this occurs). Since it was inconsistent between two
        otherwise-identical runs, this isn't something to "fix" in
        software -- settle_s is an optional, cheap hedge: pass a small
        value (e.g. 0.3-0.5) to sleep after arming, giving any residual
        settling time to finish before your acquisition loop's first
        real trigger. Default 0 (no delay) preserves prior behavior.
        """
        self.set_axis_mask()
        self.tiger.send_command(f"TTL X={TTL_MODE_RELATIVE}", card_addr=self.card_addr)
        self.set_read_index(0)
        if settle_s > 0:
            time.sleep(settle_s)

    def arm_absolute(self, settle_s: float = 0.0):
        """TTL X=1 -- absolute ring-buffer trigger mode. NOT
        independently confirmed on real hardware for stage axes in this
        module's own testing (the relative mode was what got validated)
        -- included for completeness/parity, verify before relying on
        it for anything real. Applies the same defensive read-index
        reset as arm_relative() as a precaution, since the underlying
        cause of that mode's index-advance-on-arm isn't confirmed to be
        specific to TTL X=12 vs. TTL X= in general. See arm_relative()'s
        docstring for the settle_s parameter's purpose."""
        self.set_axis_mask()
        self.tiger.send_command(f"TTL X={TTL_MODE_ABSOLUTE}", card_addr=self.card_addr)
        self.set_read_index(0)
        if settle_s > 0:
            time.sleep(settle_s)

    def disarm(self):
        """TTL X=0 -- returns the card to normal (non-ring-buffer)
        motion control."""
        self.tiger.send_command(f"TTL X={TTL_MODE_DISARMED}", card_addr=self.card_addr)

    def set_output_mode(self, mode: int, polarity: int = 1):
        """
        TTL Y=<mode> [F=<polarity>] -- configures this card's OUT0
        behavior. Confirmed from ASI's command:ttl docs: mode=2
        "generates TTL pulse at end of a commanded move (MOVE, MOVREL,
        move via ring buffer, or via array module)" -- this is the
        mechanism the design doc's self-sustaining loop depends on
        (Z-move-complete -> next frame's trigger).

        IMPORTANT, NOT YET CONFIRMED WHICH APPLIES ON THIS CARD: per
        ASI's docs, OUT0 is normally the card's own PHYSICAL OUT
        connector (paired with the IN0 connector the electrical ring-
        buffer test already jumpers), NOT automatically a numbered
        backplane line -- backplane routing for a completion pulse is
        only documented as a special case for a DIFFERENT mode (21,
        "requires MM_TARGET firmware", not the mode used here) and only
        since firmware v3.36. Use
        tools/asi_tiger_z_output_discovery_test.py to determine which
        applies to this specific card before wiring it into the full
        self-sustaining loop -- don't assume either possibility.

        polarity: TTL OUT0_polarity -- 1 (default) or -1 (inverted).
        """
        cmd = f"TTL Y={mode}"
        if polarity != 1:
            cmd += f" F={polarity}"
        self.tiger.send_command(cmd, card_addr=self.card_addr)

    def set_output_pulse_duration(self, duration: int):
        """
        RT Y=<duration> -- sets the OUT0 pulse duration used by
        set_output_mode(2) (and other timed-pulse OUT0 modes). Units
        NOT independently confirmed here -- ASI's docs reference this
        command without stating units explicitly in the TTL page;
        start with a generously long value for initial discovery
        testing (easier to observe/catch), narrow down once the
        pulse's existence and location are confirmed.
        """
        self.tiger.send_command(f"RT Y={duration}", card_addr=self.card_addr)

    def software_trigger(self):
        """
        Bare RM (no arguments) -- simulates a TTL IN0 pulse purely in
        software, no electrical signal involved. CONFIRMED ON REAL
        HARDWARE as a config-only self-test: if this moves the axis but
        a real electrical trigger doesn't, the problem is wiring/
        backplane routing, not configuration. If even this doesn't move
        it, the problem is in the RM/LOAD/TTL setup itself.
        """
        self.tiger.send_command("RM", card_addr=self.card_addr)

    def where(self) -> str:
        """
        WHERE <axis> -- current position, card-addressed. Returns the
        raw reply string; used here for before/after verification in
        tests, not a fully-typed general query API.
        """
        return self.tiger.send_command(f"WHERE {self.axis}", card_addr=self.card_addr)

    def set_mode(self, mode: int):
        """
        RM F=<mode> -- the ring buffer's own operating mode (see the
        RB_MODE_* constants and the module-level comment above them for
        what's confirmed on real hardware). Distinct from arm_relative()/
        arm_absolute() above, which set TTL X= (the axis card's response
        to an external trigger) -- both settings interact but are
        separate registers.

        CAUTION (confirmed on real hardware): switching F clears the
        ring buffer and resets its read/write indices, in BOTH
        directions (entering OR leaving consume mode) -- re-load your
        points after calling this, don't assume anything loaded before
        the mode switch survives it.
        """
        self.tiger.send_command(f"RM F={mode}", card_addr=self.card_addr)

    def query_mode(self) -> str:
        """RM F? -- reads back the current ring buffer mode (raw reply string)."""
        return self.tiger.send_command("RM F?", card_addr=self.card_addr)

    def query_position_count(self) -> str:
        """
        RM X? -- CONFIRMED ON REAL HARDWARE this is dual-meaning
        depending on the current mode (see RB_MODE_* comment above):
        number of USED positions in RB_MODE_TTL_TRIGGERED (F=1, the
        mode this project uses), but number of OPEN positions in
        RB_MODE_CONSUME (F=0). Returns the raw reply string -- check
        query_mode() first if you need to know which meaning applies.
        """
        return self.tiger.send_command("RM X?", card_addr=self.card_addr)
