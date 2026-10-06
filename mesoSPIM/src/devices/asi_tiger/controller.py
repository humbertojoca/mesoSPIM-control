"""
asi_tiger.controller
=====================
Thin pyserial wrapper for ASI Tiger (TG-1000) controllers.

Implements the raw command/reply protocol described in ASI's serial
command documentation:
    https://asiimaging.com/docs/products/serial_commands

No dependency beyond pyserial. This module knows nothing about DAC
cards specifically -- see dac.py for that.
"""

import threading
import time
from typing import Optional

import serial


class TigerError(Exception):
    """Raised when the controller replies with an error (":N-#")."""

    def __init__(self, code: int, command: str):
        self.code = code
        self.command = command
        super().__init__(f"Tiger error {code} for command: {command!r}")


class TigerController:
    """
    Low-level serial interface to an ASI Tiger controller.

    One instance owns one serial port. Commands are plain ASCII lines
    terminated with '\\r'. Replies start with ':A' (success, may carry
    data) or ':N-<code>' (error).

    Thread-safe: a lock serializes command/reply pairs, since the
    controller is a single half-duplex serial device and interleaved
    commands from multiple threads would corrupt replies.
    """

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 0.5):
        self.port = port
        self.baudrate = baudrate
        self._timeout = timeout
        self._ser: Optional[serial.Serial] = None
        self._lock = threading.Lock()
        # Lightweight per-command timing, read/reset via take_stats() --
        # added to find out where a slow live() frame actually spends its
        # time (see PATCHNOTES_ASI_TIGER.md).
        self._stat_n = 0
        self._stat_t = 0.0
        self._stat_max = 0.0
        self._stat_max_cmd = ""

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------
    def connect(self):
        """Open the serial port. Recommended baud rate is 115200."""
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=self._timeout,
            write_timeout=self._timeout,
        )
        # Let the OS-level buffers settle, then discard anything stale
        # left over from a previous session before we start talking.
        time.sleep(0.1)
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()

    def disconnect(self):
        if self._ser is not None and self._ser.is_open:
            self._ser.close()
        self._ser = None

    @property
    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    @classmethod
    def from_open_serial(cls, ser: "serial.Serial", lock: Optional[threading.Lock] = None) -> "TigerController":
        """
        Wrap an ALREADY-OPEN serial.Serial connection instead of opening a
        new one.

        Why this exists: a Tiger rack is ONE physical device behind ONE
        serial port, even though it hosts many logical cards (stage, DAC,
        PLC...). Only one process/handle can hold a COM port open at a
        time on Windows, so if something else (e.g. mesoSPIM's own ASI
        stage driver) already has the port open, you MUST reuse that
        connection rather than calling connect() again -- a second
        serial.Serial(same_port) will fail or silently corrupt traffic.

        Use this together with card_addr= on send_command() to safely
        multiplex stage, DAC, and PLC commands over the one shared link.

        REAL-HARDWARE BUG, caught by the user: reusing the connection
        object alone is NOT enough. This class's own `self._lock` only
        serializes OUR OWN commands against each other -- it does
        nothing to stop a DIFFERENT thread (mesoSPIM's own stage-
        position-polling timer, running in the GUI thread, confirmed
        directly from mesoSPIM_Core.py's "Position polling runs in
        MainWindow GUI thread, not in Core thread!" comment) from
        writing/reading the SAME physical serial port at the same time
        as us, via mesoSPIM's own StageControlASI._send_command() --
        which, confirmed directly from asicontrol.py, had NO lock of
        its own at all. Two unsynchronized readers/writers on one
        half-duplex serial line interleave, producing exactly the
        garbled reply the user hit (mostly null bytes with one stray
        surviving character) -- intermittent, not every call, matching
        a genuine race rather than a deterministic parsing bug.

        Pass `lock=` here with the SAME lock object StageControlASI
        now also guards its own `_send_command()` with (its new
        `serial_lock` attribute -- see PATCHNOTES_ASI_TIGER.md) so
        BOTH sides of the shared connection serialize against each
        other, not just against themselves. If you don't have access
        to that lock (e.g. an older, unpatched StageControlASI),
        omitting `lock` falls back to a lock private to this
        TigerController instance -- which does NOT protect against the
        stage driver's own concurrent I/O, so this is a real, not
        theoretical, risk if you skip it.

        REAL-HARDWARE BUG #2, caught by the user after the lock fix
        above: locking alone stopped the CORRUPTION, but a THIRD
        hang_dump.txt then showed a genuine, non-recovering hang --
        stuck for 60+ seconds straight on the exact same line, first
        in serialwin32.flush(), then in serialwin32.read() -- not
        cycling through different call sites the way the earlier
        "slow but progressing" contention did. Root cause, confirmed
        by reading asicontrol.py's own serial.Serial(...) call
        directly: StageControlASI opens the shared port with
        `timeout=5` but NO `write_timeout` at all, so pyserial
        defaults it to None -- an UNBOUNDED, can-block-forever write.
        Because from_open_serial() reuses that exact Serial instance
        rather than opening its own, our writes/flushes inherit that
        same no-timeout setting. Under the much heavier command
        traffic this backend generates (run_tasks() polls
        read_bnc_inputs() in a tight loop -- see
        mesoSPIM_ASITigerWaveFormGenerator.py), a write that stalls
        for any reason (device-side command queue backed up, a
        transient USB/serial hiccup) had no way to ever time out and
        return control to Python -- it just sat there, exactly
        matching the dump.

        Fix: explicitly (re)apply finite timeout/write_timeout values
        to the shared Serial instance here, so neither side of the
        shared connection can block forever. This DOES change
        StageControlASI's own read timeout too (since it's the same
        instance) -- from 5s down to whichever timeout this
        TigerController was constructed with (default 0.5s via
        cls(...) above, i.e. NOT configurable through this
        classmethod's own arguments). That's a deliberate, acceptable
        trade: a short stage command that doesn't get a reply within
        0.5s almost certainly isn't coming at all on this hardware,
        and a bounded TimeoutError the stage driver can catch (or at
        worst a slightly premature timeout, logged, and retried by
        whatever called it) is strictly better than the connection
        wedging solid with no exception at all.
        """
        obj = cls(port=ser.port, baudrate=ser.baudrate)
        obj._ser = ser
        obj._ser.timeout = obj._timeout
        obj._ser.write_timeout = obj._timeout
        if lock is not None:
            obj._lock = lock
        return obj

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.disconnect()

    # ------------------------------------------------------------------
    # Command / reply
    # ------------------------------------------------------------------
    def send_command(self, command: str, card_addr: Optional[int] = None) -> str:
        """
        Send a raw command string and return the decoded reply, with the
        leading ':A' marker and surrounding whitespace stripped.

        card_addr: if given, prefixes the command with the card address
                   using Tiger's card-addressed syntax, e.g.
                   send_command("M A=1500", card_addr=37) sends "37M A=1500".

        Raises TigerError if the controller replies with ':N-<code>',
        and TimeoutError if nothing comes back within `timeout` seconds.
        """
        if not self.is_connected:
            raise RuntimeError("Not connected -- call connect() first")

        full_cmd = f"{card_addr}{command}" if card_addr is not None else command

        t_locked = None
        try:
            with self._lock:
                t_locked = time.perf_counter()
                self._ser.reset_input_buffer()
                self._ser.write((full_cmd + "\r").encode("ascii"))
                self._flush_bounded()
                raw = self._read_reply_lenient()
        finally:
            # Time spent on the wire only (excludes waiting for the lock).
            dt = (time.perf_counter() - t_locked) if t_locked is not None else 0.0
            self._stat_n += 1
            self._stat_t += dt
            if dt > self._stat_max:
                self._stat_max, self._stat_max_cmd = dt, full_cmd

        if not raw:
            raise TimeoutError(f"No reply from controller for: {full_cmd!r}")

        reply = raw.decode("ascii", errors="replace").strip()

        if reply.startswith(":N"):
            try:
                code = int(reply.split("-", 1)[1])
            except (IndexError, ValueError):
                code = -1
            raise TigerError(code, full_cmd)

        if reply.startswith(":A"):
            reply = reply[2:].strip()

        return reply

    def take_stats(self):
        """Return (n_commands, total_wire_seconds, max_seconds, max_command)
        since the last call, then reset. Wire time excludes time spent
        waiting for the shared lock."""
        out = (self._stat_n, self._stat_t, self._stat_max, self._stat_max_cmd)
        self._stat_n, self._stat_t, self._stat_max, self._stat_max_cmd = 0, 0.0, 0.0, ""
        return out

    def _flush_bounded(self):
        """
        Bounded replacement for serial.Serial.flush().

        REAL-HARDWARE BUG #3, found from a third hang_dump.txt stuck
        for 30+ seconds on serialwin32.flush(): pyserial's own flush()
        on Windows is `while self.out_waiting: time.sleep(0.05)` --
        confirmed directly from serialwin32.py's source -- a loop with
        NO timeout of any kind. It does not honor write_timeout (that
        only bounds write() itself) and cannot be stopped by setting
        any attribute on the Serial object. If out_waiting ever gets
        stuck non-zero -- the OS-level output buffer stops draining,
        which is exactly what sustained command flood from the
        unthrottled read_bnc_inputs() polling loop risked (see
        mesoSPIM_ASITigerWaveFormGenerator.py's run_tasks(), also
        fixed this round) -- real pyserial flush() hangs forever, no
        exception, nothing to catch.

        This does the same thing flush() does (wait for out_waiting to
        reach 0) but gives up after self._timeout and raises
        TimeoutError instead of looping forever, so a stalled write
        becomes a normal, catchable failure for this one command
        rather than a wedged connection for every future command too.
        """
        end_time = time.time() + self._timeout
        while self._ser.out_waiting:
            if time.time() >= end_time:
                raise TimeoutError(
                    f"Serial write never drained (out_waiting={self._ser.out_waiting}) "
                    f"within {self._timeout}s -- port may be stalled."
                )
            time.sleep(0.01)

    def _read_reply_lenient(self) -> bytes:
        """
        Polls for a reply, accepting either '\\r\\n' OR a bare '\\r' as a
        valid terminator (pyserial's read_until only accepts an exact
        byte sequence match). Adopted after a separate diagnostic script
        (asi_trigger_test.py) found this necessary for reliable framing
        on some command types -- a strict '\\r\\n'-only read_until would
        still eventually return the right data on a bare-'\\r' reply
        (pyserial returns whatever's buffered once the port timeout
        elapses), just after stalling for the full per-command timeout
        first. This returns as soon as either terminator is seen.
        """
        end_time = time.time() + self._timeout
        buf = b""
        while time.time() < end_time:
            chunk = self._ser.read(self._ser.in_waiting or 1)
            if chunk:
                buf += chunk
                if buf.endswith(b"\r\n") or buf.endswith(b"\r"):
                    break
            else:
                time.sleep(0.01)
        return buf
