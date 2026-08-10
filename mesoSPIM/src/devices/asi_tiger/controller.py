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
    def from_open_serial(cls, ser: "serial.Serial") -> "TigerController":
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
        """
        obj = cls(port=ser.port, baudrate=ser.baudrate)
        obj._ser = ser
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

        with self._lock:
            self._ser.reset_input_buffer()
            self._ser.write((full_cmd + "\r").encode("ascii"))
            self._ser.flush()
            raw = self._ser.read_until(b"\r\n")

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
