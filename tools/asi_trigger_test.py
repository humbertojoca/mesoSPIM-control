"""
ASI Tiger controller trigger-chain diagnostic script (v3)
===========================================================

Changes from v2
----------------
  * Z-stage test now uses TTL X=12 (relative ring-buffer mode) with LOAD,
    instead of TTL X=2 + a pre-emptive MOVREL. LOAD only stores a value in
    the ring buffer -- it never moves the stage. The stage only moves once
    a trigger (electrical or software) actually fires. This removes the
    v2 race condition by design rather than by reordering.
  * RM X=0 now clears the ring buffer before each LOAD, so stale entries
    from a previous run can't interfere.
  * NEW: a software self-test using the bare `RM` command (no arguments),
    which simulates a TTL IN0 pulse purely in software -- no electrical
    signal involved. Run this BEFORE the PLogic-driven electrical test.
    If the software self-test moves the stage/DAC but the electrical test
    doesn't, the problem is wiring/backplane routing, not configuration.
    If even the software self-test doesn't move it, the problem is in the
    RM/LOAD/TTL configuration itself.

Test sequence per card, in order
----------------------------------
  1. Clear ring buffer (RM X=0), load one relative step, arm TTL X=12,
     enable the right axis with RM Y=<mask>.
  2. Software self-test: bare `RM` command. Confirms config is correct
     independent of any external pulse.
  3. Electrical test: PLogic BNC1 -> jumper -> card's TTL IN. Confirms
     the physical/electrical path.
  4. Backplane sweep: PLogic drives each of the 8 backplane lines in
     turn, no jumper needed. Finds which line (if any) the card listens
     to natively.

Card addresses (confirm against your `who` output before running)
--------------------------------------------------------------------
  PLogic (TGPLC):     addr 36, axis E, 8 front-panel BNCs (addr 33-40),
                       8 backplane TTL lines (addr 41-48)
  DAC card:            addr 34, axis H (first channel), SIGNAL_DAC_4CH
  Z/Theta stage card:  addr 32, axis Z (bit0 of RM Y), axis T (bit1)

Requirements
------------
    pip install pyserial
"""

import sys
import time

import serial

# ---------------------------------------------------------------------
# Configuration -- EDIT THESE for your setup
# ---------------------------------------------------------------------
COM_PORT = "COM4"
BAUD_RATE = 115200
TIMEOUT_S = 2.0

PLOGIC_ADDR = 36
DAC_ADDR = 34
DAC_AXIS = "H"
Z_ADDR = 32
Z_AXIS = "Z"
Z_AXIS_MASK = 1     # bit0 = Z, since card 32 lists axes as "Z,T" in that order

# The other two DAC cards -- same TGGALVO/SIGNAL_DAC_4CH type as card 34, but
# in different slots. Their backplane address is likely NOT also 42; use
# backplane_sweep() on their first channel to find it (see main()).
DAC2_ADDR = 35
DAC2_AXIS = "P"
DAC3_ADDR = 37
DAC3_AXIS = "A"

TEST_SOURCE_BNC_ADDR = 33          # BNC1 on PLogic
BACKPLANE_ADDRS = list(range(41, 49))  # TTL0..TTL7, addresses 41-48

DAC_TEST_LOW_MV = 0
DAC_TEST_HIGH_MV = 2000
DAC_BACKPLANE_ADDR = 42     # confirmed: card 34 (H channel) listens on TTL1 / backplane addr 42.
                             # Cards 35 and 37 are the same TGGALVO/SIGNAL_DAC_4CH card type but
                             # in different slots -- their address is likely different. Find it
                             # with backplane_sweep() the same way, using their first channel.

Z_TEST_MOVE_UNITS = 10       # relative step, in tenths of a micron -> 1 micron

SETTLE_S = 0.3
PULSE_HOLD_S = 0.2


# ---------------------------------------------------------------------
# Low-level serial wrapper
# ---------------------------------------------------------------------
class TigerSerial:
    def __init__(self, port, baud=BAUD_RATE, timeout=TIMEOUT_S):
        self.ser = serial.Serial(port, baud, timeout=timeout)
        time.sleep(0.3)
        self.ser.reset_input_buffer()

    def send(self, cmd: str) -> str:
        # Flush any stray leftover bytes before sending, and read until we
        # see a line terminator instead of a fixed sleep + single read.
        # The fixed-sleep approach let replies from back-to-back commands
        # bleed/concatenate together (e.g. ":A \n:A 4096"), which produced
        # false "RESPONSE DETECTED" results in the backplane sweep.
        self.ser.reset_input_buffer()
        line = cmd.strip() + "\r"
        self.ser.write(line.encode("ascii"))
        return self._read_reply().strip()

    def _read_reply(self, timeout=1.0) -> str:
        end_time = time.time() + timeout
        buf = b""
        while time.time() < end_time:
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if chunk:
                buf += chunk
                if buf.endswith(b"\r\n") or buf.endswith(b"\r"):
                    break
            else:
                time.sleep(0.01)
        return buf.decode("ascii", errors="replace")

    def close(self):
        self.ser.close()


def check(condition: bool, label: str) -> bool:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")
    return condition


# ---------------------------------------------------------------------
# PLogic helpers (confirmed syntax from ASI's TGPLC manual)
# ---------------------------------------------------------------------
def plc_set_physical_io(tiger, addr, source, io_type=None):
    tiger.send(f"{PLOGIC_ADDR}M E={addr}")
    if io_type is not None:
        tiger.send(f"{PLOGIC_ADDR}CCA Y={io_type}")
    tiger.send(f"{PLOGIC_ADDR}CCA Z={source}")


def plc_drive_bnc1(tiger, high: bool):
    plc_set_physical_io(tiger, TEST_SOURCE_BNC_ADDR, 64 if high else 0)


def plc_release_backplane(tiger, addr):
    tiger.send(f"{PLOGIC_ADDR}M E={addr}")
    tiger.send(f"{PLOGIC_ADDR}CCA Y=0")   # input, the documented default


# ---------------------------------------------------------------------
# Generic per-card ring-buffer relative-move test harness
# ---------------------------------------------------------------------
def arm_relative_ring_buffer(tiger, addr, axis, axis_mask, step):
    """Clear the buffer, queue one relative step, arm TTL X=12."""
    tiger.send(f"{addr}RM X=0")               # clear ring buffer
    tiger.send(f"LOAD {axis}={step}")          # NOT card-addressed; stores only, no move
    tiger.send(f"{addr}RM Y={axis_mask}")      # enable this axis for ring-buffer moves
    tiger.send(f"{addr}TTL X=12")              # arm: relative ring-buffer mode


def disarm(tiger, addr):
    tiger.send(f"{addr}TTL X=0")


def software_self_test(tiger, addr, axis, label):
    print(f"  -- software self-test ({label}) --")
    before = tiger.send(f"{addr}WHERE {axis}")
    tiger.send(f"{addr}RM")                    # bare RM = simulated TTL pulse, no wiring involved
    time.sleep(SETTLE_S)
    after = tiger.send(f"{addr}WHERE {axis}")
    print(f"    before={before}  after={after}")
    moved = before.strip() != after.strip() and before and after
    check(moved, f"{label} responds to a software-simulated trigger (config is correct)")
    return moved


def electrical_test(tiger, addr, axis, label):
    print(f"  -- electrical test via PLogic BNC1 jumper ({label}) --")
    before = tiger.send(f"{addr}WHERE {axis}")
    plc_drive_bnc1(tiger, high=True)
    time.sleep(PULSE_HOLD_S)
    plc_drive_bnc1(tiger, high=False)
    time.sleep(SETTLE_S)
    after = tiger.send(f"{addr}WHERE {axis}")
    print(f"    before={before}  after={after}")
    moved = before.strip() != after.strip() and before and after
    check(moved, f"{label} responds to the jumpered electrical pulse")
    return moved


def plc_pulse_backplane(tiger, addr):
    """Drive one backplane line high->low once, open-drain (safe alongside
    another card also driving it), then release it back to input."""
    plc_set_physical_io(tiger, addr, source=64, io_type=1)
    time.sleep(PULSE_HOLD_S)
    plc_set_physical_io(tiger, addr, source=0, io_type=1)
    time.sleep(SETTLE_S)
    plc_release_backplane(tiger, addr)


def backplane_test(tiger, addr, axis, bp_addr, label):
    """Direct test for cards with NO physical TTL IN BNC (e.g. TGGALVO /
    SIGNAL_DAC_4CH) -- the card must be triggered via its known backplane
    address, since there is nothing to jumper on the front panel."""
    print(f"  -- backplane test ({label}, backplane addr {bp_addr}) --")
    before = tiger.send(f"{addr}WHERE {axis}")
    plc_pulse_backplane(tiger, bp_addr)
    after = tiger.send(f"{addr}WHERE {axis}")
    print(f"    before={before}  after={after}")
    moved = before.strip() != after.strip() and before and after
    check(moved, f"{label} responds to backplane addr {bp_addr}")
    return moved


def backplane_sweep(tiger, addr, axis, axis_mask, arm_fn, label):
    """arm_fn: a zero-arg callable that (re)arms the ring buffer for this card."""
    print(f"\n=== Backplane sweep: {label} (addr {addr}) ===")
    print("  No jumper needed -- PLogic drives each backplane line in turn.")
    for bp_addr in BACKPLANE_ADDRS:
        ttl_num = bp_addr - 41
        arm_fn()
        before = tiger.send(f"{addr}WHERE {axis}")

        plc_set_physical_io(tiger, bp_addr, source=64, io_type=1)   # open-drain high
        time.sleep(PULSE_HOLD_S)
        plc_set_physical_io(tiger, bp_addr, source=0, io_type=1)    # open-drain low
        time.sleep(SETTLE_S)
        plc_release_backplane(tiger, bp_addr)                        # always release

        after = tiger.send(f"{addr}WHERE {axis}")
        moved = before.strip() != after.strip() and before and after
        flag = "  <-- RESPONSE DETECTED" if moved else ""
        print(f"  TTL{ttl_num} (addr {bp_addr}): before={before}  after={after}{flag}")
        disarm(tiger, addr)


# ---------------------------------------------------------------------
# Test 0: connection + firmware inventory
# ---------------------------------------------------------------------
def test_connection(tiger: TigerSerial) -> bool:
    print("\n=== Test 0: Connection & firmware inventory ===")
    reply = tiger.send("WHO")
    print(reply if reply else "  (no reply)")
    return check(len(reply) > 0, "Controller responded to WHO")


def test_plogic_output(tiger: TigerSerial):
    print("\n=== Test 1: PLogic BNC1 manual output sanity check ===")
    plc_drive_bnc1(tiger, high=True)
    input("  BNC1 forced HIGH -- confirm with a scope/multimeter, then press Enter... ")
    plc_drive_bnc1(tiger, high=False)
    input("  BNC1 forced LOW -- confirm, then press Enter... ")


def test_z_stage(tiger: TigerSerial):
    print("\n=== Test 2: Z-stage (relative ring buffer, TTL X=12) ===")
    arm_relative_ring_buffer(tiger, Z_ADDR, Z_AXIS, Z_AXIS_MASK, Z_TEST_MOVE_UNITS)

    sw_ok = software_self_test(tiger, Z_ADDR, Z_AXIS, "Z stage")
    if not sw_ok:
        print("  -> Config problem: fix RM/LOAD/TTL setup before testing wiring.")
        disarm(tiger, Z_ADDR)
        return

    arm_relative_ring_buffer(tiger, Z_ADDR, Z_AXIS, Z_AXIS_MASK, Z_TEST_MOVE_UNITS)
    print("  Jumper PLogic BNC1 -> Z card TRIG IN before continuing.")
    input("  Press Enter once connected... ")
    electrical_test(tiger, Z_ADDR, Z_AXIS, "Z stage")
    disarm(tiger, Z_ADDR)


def arm_absolute_ring_buffer(tiger, addr, axis, axis_mask, low_val, high_val):
    """DAC cards are voltage outputs, not stages -- TTL X=12 (relative move)
    is documented in stage-centric language ("drive the stage to the
    appropriate starting position") and likely isn't implemented for a
    SIGNAL_DAC axis. Use absolute ring buffer (mode 1) instead: load two
    explicit voltage points and step between them."""
    tiger.send(f"{addr}RM X=0")
    tiger.send(f"LOAD {axis}={low_val}")
    tiger.send(f"LOAD {axis}={high_val}")
    tiger.send(f"{addr}RM Y={axis_mask}")
    tiger.send(f"{addr}TTL X=1")   # absolute ring buffer, move-to-next-position


def test_dac(tiger: TigerSerial):
    print("\n=== Test 3: DAC card (absolute ring buffer, TTL X=1) ===")
    print("  Skip if the DAC card has no separate TTL IN BNC (only 4 channel outputs) --")
    print("  go straight to its backplane sweep instead.")

    # Confirm what axis mask the card actually reports before/after we set it --
    # cheap sanity check on whether bit0 really corresponds to H on this card.
    tiger.send(f"{DAC_ADDR}RM Y={1}")
    reported = tiger.send(f"{DAC_ADDR}RM Y?")
    print(f"  RM Y? reports: {reported}  (expect this to reflect mask=1)")

    arm_absolute_ring_buffer(tiger, DAC_ADDR, DAC_AXIS, 1, DAC_TEST_LOW_MV, DAC_TEST_HIGH_MV)

    sw_ok = software_self_test(tiger, DAC_ADDR, DAC_AXIS, "DAC card")
    if not sw_ok:
        print("  -> Still failing? Try RM Y=15 (all 4 channels enabled) as a broader")
        print("     sanity check, and confirm with WHERE H that the axis letter is right.")
        disarm(tiger, DAC_ADDR)
        return

    # No physical TTL IN BNC on this card type (TGGALVO / SIGNAL_DAC_4CH) --
    # confirmed on your hardware that H listens on backplane addr 42 (TTL1).
    # No jumper needed; PLogic drives the backplane line directly.
    arm_absolute_ring_buffer(tiger, DAC_ADDR, DAC_AXIS, 1, DAC_TEST_LOW_MV, DAC_TEST_HIGH_MV)
    backplane_test(tiger, DAC_ADDR, DAC_AXIS, DAC_BACKPLANE_ADDR, "DAC card")
    disarm(tiger, DAC_ADDR)


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
def main():
    print(f"Connecting to Tiger controller on {COM_PORT} @ {BAUD_RATE} baud...")
    tiger = TigerSerial(COM_PORT)

    try:
        if not test_connection(tiger):
            print("\nCould not confirm connection -- aborting further tests.")
            sys.exit(1)

        test_plogic_output(tiger)
        test_z_stage(tiger)
        test_dac(tiger)

        # Z card's backplane address is still unknown (test 4 was unreliable
        # before the send() fix) -- discover it now that replies are clean.
        backplane_sweep(
            tiger, Z_ADDR, Z_AXIS, Z_AXIS_MASK,
            lambda: arm_relative_ring_buffer(tiger, Z_ADDR, Z_AXIS, Z_AXIS_MASK, Z_TEST_MOVE_UNITS),
            "Z/Theta card",
        )

        # DAC card 34's backplane address is already known (42). Discover the
        # other two DAC cards' addresses the same way -- likely different,
        # since they're in different physical slots.
        for addr, axis, label in ((DAC2_ADDR, DAC2_AXIS, "DAC card 2"),
                                   (DAC3_ADDR, DAC3_AXIS, "DAC card 3")):
            backplane_sweep(
                tiger, addr, axis, 1,
                lambda a=addr, x=axis: arm_absolute_ring_buffer(tiger, a, x, 1, DAC_TEST_LOW_MV, DAC_TEST_HIGH_MV),
                label,
            )

        print("\n=== Summary ===")
        print("Software self-test FAIL  -> fix RM/LOAD/TTL configuration.")
        print("Software self-test PASS, electrical test FAIL -> wiring/jumper issue.")
        print("Backplane sweep RESPONSE DETECTED -> that TTL line is what the card")
        print("listens to internally; use it directly in the real acquisition logic.")

    except KeyboardInterrupt:
        print("\nAborted by user.")
    finally:
        tiger.close()


if __name__ == "__main__":
    main()
