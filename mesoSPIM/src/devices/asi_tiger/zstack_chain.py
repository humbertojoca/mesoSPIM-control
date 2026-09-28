"""
asi_tiger.zstack_chain
=========================
configure_zstack_trigger_chain() -- wires the full self-sustaining
Z-stack acquisition loop from the design doc, built ONLY from pieces
individually validated on real hardware earlier in this project:

  - Camera Expose-Out: confirmed real TTL signal, both edges reliably
    detected (5/5 cycles) -- see tools/asi_tiger_camera_expose_test.py.
  - ETL triggered sweep (SAM=2): confirmed auto-rearm on every genuine
    edge, no host involvement needed between triggers.
  - Z ring buffer (TTL X=12, relative): confirmed wraparound behavior,
    single loaded step is sufficient regardless of stack size, correct
    indexing after the arm-time Z=0 fix.
  - Z's OUT0 (TTL Y=2, move-complete): confirmed on the physical OUT
    connector (NOT backplane) via oscilloscope + latching catcher.
  - PLC pulse-pass-through counter: confirmed to N=400 (two-counter
    form) / single-counter form avoids the two-counter form's prime-N
    gap, validated address-by-address against ASI's own documented
    example.
  - Zero-delay retriggering was isolated as the actual cause of the
    isolated Z-only loop test's glitching, NOT TTL Y=2/IN0 in general
    (confirmed via well-spaced host-paced retriggering with TTL Y=2
    active -- clean 5/5). The camera's own exposure duration provides
    the real loop's natural dead-time between Z retriggers, which the
    isolated test didn't have.

======================================================================
ONE GENUINELY NEW, UNTESTED PIECE: triggering the camera itself.
======================================================================
Everything above is a recombination of validated signals. This
function ALSO drives a BNC meant to be wired to the camera's physical
trigger input -- unlike Expose-Out, which has only ever been READ,
nothing in this project has yet driven the camera's trigger input.
Test this in isolation FIRST with
tools/asi_tiger_camera_trigger_test.py before trusting this function's
Global Trigger fan-out to actually start real exposures.
======================================================================

Signal plan (see module docstring's table for the full picture):
  - Expose-Out rising edge -> ETL's backplane trigger-in (starts ETL sweep)
  - Expose-Out falling edge -> Z's IN0 (starts next Z step)
  - Z's OUT0 -> counter's pulse_in (counts completed planes)
  - Counter output OR'd with a one-time host "kick" -> Global Trigger
    BNC -> camera trigger input (starts next exposure, gated by N)

Galvo is NOT part of this function -- it's free-running and
independent of the whole loop, started once per row via
SingleAxisWaveform.start() separately, matching the design doc.
"""

from dataclasses import dataclass
import time
from typing import Optional

from .controller import TigerController
from .plc import (
    PLCCard, bnc_addr, cell_addr, falling_edge,
    IO_TYPE_INPUT, IO_TYPE_PUSH_PULL_OUTPUT,
)
from .stage_trigger import StageRingBuffer, OUT0_MODE_MOVE_COMPLETE, OUT0_MODE_LOW
from .singleaxis import trigger_in_backplane_addr, axis_slot_index, enable_backplane_trigger_mode


@dataclass
class ZStackTriggerChain:
    """
    Handle returned by configure_zstack_trigger_chain(). Call kick() to
    start a row's acquisition (after row-level setup: laser enable,
    L/R switch, absolute Z move to Z_start, galvo started separately),
    reset() to re-arm for another row's plane count, disarm() when done
    with this row entirely.
    """

    plc: PLCCard
    z: StageRingBuffer
    kick_cell: int
    counter_reset_cell: int
    counter_cells: tuple = (1, 2, 3, 4, 5)

    def reset(self):
        """Re-arms the plane counter for a fresh count of N. Call this
        once per row, before kick(), after any earlier row's loop has
        finished or been disarmed."""
        self.plc.reset_pulse_pass_through_counter(init_cell=self.counter_reset_cell)

    def kick(self):
        """Fires the one-time host pulse that starts the self-sustaining
        loop (first Global Trigger -> first camera exposure). Everything
        after this is autonomous in hardware until the counter blocks."""
        self.plc.set_cell_state(self.kick_cell, True)
        self.plc.set_cell_state(self.kick_cell, False)

    def disarm(self):
        """Stops the loop: disarms Z's ring buffer trigger response and
        clears OUT0 back to a quiet state. Does NOT touch ETL/galvo --
        disarm those separately if needed."""
        self.z.disarm()
        self.z.set_output_mode(OUT0_MODE_LOW)

    def is_complete(self) -> bool:
        """
        Checks whether the current row's plane count has been fully
        exhausted -- i.e. the self-sustaining loop has genuinely
        stopped on its own, not just paused between planes.

        Reads the counter's LATCH cell (the 3rd of the 5 counter cells,
        index [2] of counter_cells) via read_cell_outputs()/RDADC Z?,
        NOT the AND-gate output cell and NOT an exact countdown value.
        Deliberately: the AND-gate output only pulses briefly during an
        actual passing pulse, so a single read of it can't tell
        "between planes, still counting" apart from "genuinely done" --
        the same ambiguity that made a single WHERE read unreliable
        during active Z triggering. The latch cell is designed
        differently: it goes high once and STAYS high until the next
        reset(), which is the unambiguous signal this needs. RDADC Z?
        (logic cell outputs) has not shown any of WHERE's OWN reliability
        issues anywhere in this project, unlike querying a busy axis.

        CONFIRMED ON REAL HARDWARE: a malformed/empty serial reply to
        RDADC Z? CAN happen -- not previously observed, but also never
        previously polled this intensively (wait_until_complete()'s
        default poll_interval_s=0.1 over a timeout_s=60 default means up
        to ~600 reads in a single call). A real-hardware run crashed with
        an unhandled IndexError parsing an empty reply. Treats a failed
        read the same way move_absolute() treats a failed RDSTAT read --
        as "not confirmed done" (returns False), letting the caller's
        own polling loop simply retry on the next interval, rather than
        crash the whole wait.

        Safe to poll repeatedly during an active, autonomous loop --
        this reads PLC logic state, not an axis's busy status.
        """
        latch_cell = self.counter_cells[2]
        try:
            bitmask = self.plc.read_cell_outputs()
        except Exception:
            return False
        return bool(bitmask & (1 << (latch_cell - 1)))

    def wait_until_complete(self, timeout_s: float = 60.0, poll_interval_s: float = 0.1,
                             final_frame_settle_s: float = 1.0) -> bool:
        """
        Polls is_complete() until it's True, THEN waits an additional
        final_frame_settle_s before returning -- see CONFIRMED ON REAL
        HARDWARE below for why this extra wait is NOT optional.

        CONFIRMED ON REAL HARDWARE: is_complete()'s own latch goes high
        ONE FRAME TOO EARLY relative to "the whole acquisition,
        including the final frame, is genuinely done" -- a real bug in
        this method's earlier version, found from a real-hardware
        report (oscilloscope showing the ETL waveform dropping
        mid-sweep, before the FINAL frame's own Expose-Out had even
        gone low) and inconsistent final-position mismatches (missing
        exactly one step's worth on some runs, matching exactly on
        others). Traced the exact mechanism: the latch cell clocks on
        the count cell's falling edge, which happens the moment the
        one-shot's internal countdown reaches zero -- DURING the
        (n_planes-1)th Z-move-complete pulse, the SAME pulse that
        passes through the AND-gate (the delay-cell mechanism
        deliberately lets the triggering pulse itself through, "so the
        FINAL pulse still makes it through") and starts the FINAL
        frame's own exposure. So the latch reads "complete" at the
        exact moment the final frame BEGINS, not when it finishes.
        Whether the earlier version (no extra wait) happened to look
        correct depended entirely on whether poll_interval_s's own
        polling delay happened to exceed the final frame's real
        duration by chance -- explaining why some runs matched exactly
        and others were short by one step, unpredictably.

        final_frame_settle_s: MUST cover at least the final frame's own
        full cycle (camera exposure + ETL sweep + Z move + Z settle) --
        NOT a cosmetic buffer. The default (1.0s) is a starting point,
        not a measured value for any specific hardware -- set this
        based on your real camera exposure time and ETL period, the
        same way move_absolute()'s settle_tolerance needed real
        characterization rather than a guessed default.
        """
        start = time.perf_counter()
        while time.perf_counter() - start < timeout_s:
            if self.is_complete():
                if final_frame_settle_s > 0:
                    time.sleep(final_frame_settle_s)
                return True
            time.sleep(poll_interval_s)
        return False

    def planes_remaining(self) -> Optional[int]:
        """
        Reads the counter's internal countdown directly (CCA F? on the
        count cell, index [1] of counter_cells) -- confirmed from ASI's
        own docs that a one-shot's "state" is its internal countdown,
        separate from its binary output. Useful for a PROGRESS
        indicator ("N planes remaining") -- NOT the mechanism
        is_complete()/wait_until_complete() rely on for the actual
        completion decision.

        CONFIRMED ON REAL HARDWARE (asi_tiger_counter_state_test.py):
        the countdown equals real planes-remaining ONLY while the row
        is still actively passing pulses. Once blocked (is_complete()
        is True), the one-shot's own trigger input keeps re-arming on
        every further incoming pulse -- it doesn't know the downstream
        latch has already blocked the output -- so its countdown keeps
        CYCLING (observed: 0, then back up to 4, counting down again,
        repeating) even though the row is genuinely finished. Checking
        is_complete() first and short-circuiting to 0 avoids ever
        returning a stale, misleadingly-nonzero "remaining" count for a
        row that's actually done.

        CAVEAT (see is_complete()/wait_until_complete()'s own docs for
        the full explanation): is_complete() itself goes True one full
        frame before the acquisition is REALLY finished -- the latch
        fires the moment the final frame BEGINS, not when it ends. This
        method short-circuits to 0 at that same, slightly-early moment,
        so "0 remaining" can appear while the final frame is still
        physically running. Fine for a progress indicator; don't treat
        0 here as "safe to disarm now" without also respecting
        wait_until_complete()'s final_frame_settle_s.

        Returns None if the read fails.
        """
        if self.is_complete():
            return 0
        count_cell = self.counter_cells[1]
        try:
            return int(self.plc.read_cell_state(count_cell))
        except Exception:
            return None


def configure_zstack_trigger_chain(
    tiger: TigerController,
    plc_card_addr: int,
    plc_axis: str,
    z_card_addr: int,
    z_axis: str,
    z_axis_mask: int,
    etl_card_addr: int,
    etl_axis: str,
    etl_card_first_axis: str,
    n_planes: int,
    z_step: float,
    camera_expose_bnc: int,
    camera_trigger_bnc: int,
    z_in0_bnc: int,
    z_out0_bnc: int,
    counter_monitor_bnc: Optional[int] = None,
    z_out0_pulse_duration_ms: int = 50,
    kick_cell: int = 6,
    or_gate_cell: int = 7,
    counter_cells=(1, 2, 3, 4, 5),
) -> ZStackTriggerChain:
    """
    Wires the full self-sustaining Z-stack loop. See module docstring
    for the signal plan and what's validated vs. new.

    n_planes: target plane count for this row.

    CONFIRMED ON REAL HARDWARE: must be >= 3, not >= 2. Every plane's
    Z-move-complete (OUT0) pulse that gets passed through by the
    counter ALSO re-triggers the camera for the NEXT plane -- correct
    for planes 1..N-1, but the counter passing through the Nth pulse
    (the LAST plane's completion) would incorrectly trigger an (N+1)th
    plane that shouldn't exist. Confirmed exactly this failure mode on
    real hardware at n_planes=3 (oscilloscope showed 4 camera exposures
    and 4 ETL sweeps, position showed +1 extra step) with the counter's
    own pass/block logic independently confirmed correct in isolation
    (3 pass, rest blocked, exactly as configured) -- the bug was using
    "pass-through" to mean "trigger the next plane" for ALL N passes,
    when it should only do that for the first N-1. Fixed by configuring
    the underlying counter for n_planes-1, not n_planes -- the Nth
    plane's completion is now a dead end, not a retrigger. Since the
    single-counter mechanism itself requires n_pulses>=2 (see
    PLCCard.configure_pulse_pass_through_counter_single()'s docstring),
    n_planes-1>=2 means n_planes must be >= 3.
    z_step: relative Z step per plane, tenths of a micron.
    camera_expose_bnc: PLC BNC wired FROM the camera's Expose-Out.
    camera_trigger_bnc: PLC BNC wired TO the camera's trigger input --
        NOT YET VALIDATED, see module docstring's warning.
    z_in0_bnc: PLC BNC wired TO the Z card's IN0.
    z_out0_bnc: PLC BNC wired FROM the Z card's OUT0.
    counter_monitor_bnc: OPTIONAL separate spare BNC the counter's own
        output also drives, purely as an optional scope-visible
        "counter fired" indicator -- the counter's real functional
        output (driving the camera trigger via the OR gate) already
        reads the cell directly, nothing depends on this BNC being
        wired. Default None: no physical monitor output configured at
        all. FOUND ON REAL PROJECT USE: an earlier default of 8 here
        silently collided with row_setup.configure_laser_enable_lines()'s
        default laser BNCs (5-8) once both were used together --
        z_in0_bnc/z_out0_bnc/camera_expose_bnc/camera_trigger_bnc (1-4)
        plus the default laser BNCs (5-8) now claim all 8 physical BNCs
        on this rack, so there ISN'T a safe default left for this
        purely-optional feature. Pass an explicit BNC only if you
        actually want a scope-visible indicator AND have separately
        confirmed that BNC isn't already claimed by something else (this
        function only validates against the OTHER BNCs it knows about
        below -- it has no visibility into what row_setup.py or your own
        code has claimed, so that check is yours to do).
    z_out0_pulse_duration_ms: RT Y=<value> for Z's OUT0 -- confirmed
        RT Y=1000 gives a 1-second pulse on real hardware, so this is
        assumed to be milliseconds. Default kept short since the real
        loop's dead-time comes from camera exposure duration, not this.

    Does NOT start the galvo (free-running, independent, started
    separately) or move Z to its row's starting position (a plain
    absolute MOVE, done at the row level before calling this).

    Returns a ZStackTriggerChain -- call .reset() then .kick() to start
    a row's acquisition.
    """
    if n_planes < 3:
        raise ValueError(
            f"n_planes must be >= 3 for this architecture -- got n_planes={n_planes}. Every "
            f"plane's Z-move-complete pulse that passes the counter ALSO retriggers the camera "
            f"for the next plane; the counter is configured for n_planes-1 (not n_planes) so the "
            f"kick's own first frame plus n_planes-1 retriggers gives exactly n_planes total. "
            f"Since the underlying counter itself requires n_pulses>=2, n_planes-1>=2 means "
            f"n_planes must be >= 3 -- see this function's docstring for the full explanation."
        )
    if counter_monitor_bnc is not None and counter_monitor_bnc == z_out0_bnc:
        raise ValueError(
            f"counter_monitor_bnc ({counter_monitor_bnc}) must differ from z_out0_bnc "
            f"({z_out0_bnc}) -- z_out0_bnc must stay an input reading Z's real OUT0 signal, "
            f"not also be reconfigured as an output by the counter."
        )
    if counter_monitor_bnc is not None and counter_monitor_bnc in (camera_expose_bnc, camera_trigger_bnc, z_in0_bnc):
        raise ValueError(
            f"counter_monitor_bnc ({counter_monitor_bnc}) collides with another BNC already "
            f"used in this chain -- pick a genuinely spare one. Note this check only knows "
            f"about the BNCs THIS function uses (1-4 by convention) -- it can't see BNCs "
            f"claimed elsewhere, e.g. row_setup.configure_laser_enable_lines()'s default 5-8."
        )

    plc = PLCCard(tiger, card_addr=plc_card_addr, axis=plc_axis)
    z = StageRingBuffer(tiger, card_addr=z_card_addr, axis=z_axis, axis_mask=z_axis_mask)

    # --- Z: single relative step, wraps forever, gated externally by the counter ---
    z.clear()
    z.load_relative_step(z_step)
    z.arm_relative(settle_s=0.3)
    z.set_output_mode(OUT0_MODE_MOVE_COMPLETE)
    z.set_output_pulse_duration(z_out0_pulse_duration_ms)

    # --- ETL: armed for external trigger, auto-rearms on every genuine edge ---
    enable_backplane_trigger_mode(tiger, card_addr=etl_card_addr)
    etl_slot = axis_slot_index(etl_axis, etl_card_first_axis)
    etl_trigger_addr = trigger_in_backplane_addr(etl_slot)

    # --- Counter: gates the loop to exactly n_planes ---
    # n_planes-1, NOT n_planes -- see this function's docstring for the full explanation. The
    # kick independently starts frame 1 before the counter has seen anything; the counter only
    # needs to gate the REMAINING n_planes-1 retriggers to reach exactly n_planes total frames.
    # CONFIRMED ON REAL HARDWARE that using n_planes here (the bug) produces n_planes+1 total
    # frames -- the counter's own pass/block logic was independently verified correct in
    # isolation, this was purely a wiring/usage mistake in this function, not a counter bug.
    plc.configure_pulse_pass_through_counter_single(
        pulse_in_addr=bnc_addr(z_out0_bnc),
        pulse_out_bnc=counter_monitor_bnc,
        n_pulses=n_planes - 1,
        cells=counter_cells,
    )
    counter_and_cell = counter_cells[-1]

    # --- Signal routing ---
    expose_addr = bnc_addr(camera_expose_bnc)
    plc.configure_io(expose_addr, IO_TYPE_INPUT)  # Expose-Out is an input to the PLC

    # Expose-Out's RAW LEVEL -> ETL's backplane trigger-in. NOT rising_edge(expose_addr) --
    # confirmed on real hardware that a PLC-computed edge pulse (~250us, one evaluation cycle)
    # is too brief for the ETL axis's own SAM=2 trigger detection to reliably catch. Every
    # confirmed-working ETL trigger in this project used a genuine, SUSTAINED level change on
    # the backplane line, letting the axis card's own hardware do the edge detection -- not a
    # PLC-precomputed pulse. Expose-Out is already high for the whole exposure, a real sustained
    # transition the ETL card can actually catch.
    plc.configure_io(etl_trigger_addr, IO_TYPE_PUSH_PULL_OUTPUT, source_addr=expose_addr)

    # Expose-Out's FALLING edge (brief pulse) -> Z's IN0 (via z_in0_bnc). NOT
    # inverted(expose_addr) -- CONFIRMED ON REAL HARDWARE that a sustained level (which is what
    # inverted() produces: high for the ENTIRE inter-frame gap, confirmed on a scope) causes Z's
    # TTL X=12 ring-buffer trigger to misbehave -- a real shortfall in cumulative position (not a
    # polling artifact -- sum of observed deltas was measurably less than expected) plus
    # occasional oversized deltas, consistent with spurious/interfering retriggers while the
    # level stayed high. This is DIFFERENT from ETL: Z's ring-buffer trigger is a different
    # subsystem (stage motion control, not the DAC waveform engine) and evidently does NOT
    # tolerate a sustained level the way ETL's SAM=2 does. Generalizing the ETL fix to Z by
    # analogy (rather than testing it independently) was the actual mistake here -- a brief pulse
    # is the correct signal for Z specifically.
    plc.configure_io(bnc_addr(z_in0_bnc), IO_TYPE_PUSH_PULL_OUTPUT, source_addr=falling_edge(expose_addr))

    # Kick + counter output -> Global Trigger (camera trigger input)
    plc.configure_cell(kick_cell, "d_flop", inputs={"a": 0, "b": 0, "c": 0})
    plc.set_cell_state(kick_cell, False)
    plc.configure_cell(or_gate_cell, "or2", inputs={"a": cell_addr(kick_cell), "b": cell_addr(counter_and_cell)})
    plc.configure_io(bnc_addr(camera_trigger_bnc), IO_TYPE_PUSH_PULL_OUTPUT, source_addr=cell_addr(or_gate_cell))

    plc.reset_pulse_pass_through_counter(init_cell=counter_cells[0])

    return ZStackTriggerChain(plc=plc, z=z, kick_cell=kick_cell, counter_reset_cell=counter_cells[0],
                              counter_cells=counter_cells)
