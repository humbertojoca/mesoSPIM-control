#!/usr/bin/env python3
"""
tiger_terminal.py

A simple interactive serial terminal for ASI Tiger (and MS2000/TG-1000)
controllers. Lets you type commands and see raw responses, just like
using a terminal program (e.g. PuTTY / TeraTerm) against the controller.

ASI Tiger serial protocol basics (from asiimaging.com/docs/products/serial_commands):
  - Commands are plain ASCII text terminated with a carriage return '\r'.
  - Successful replies start with ':A' (sometimes with data after it).
  - Error replies start with ':N-<code>' (e.g. ':N-1' unknown command).
  - In a multi-card Tiger rack, prefix a command with the card address
    number to target that card, e.g. "1BU" or "2AA X=85". No prefix
    addresses the Tiger Comm card / whole system for global commands.
  - Baud rate: 115200 8-N-1 is standard for Tiger over USB (the common
    case). Older RS-232 setups often use 9600 8-N-1. Change --baud if
    your controller doesn't respond.

Usage:
    python tiger_terminal.py --port COM4
    python tiger_terminal.py --port /dev/ttyUSB0 --baud 115200
    python tiger_terminal.py --list                 # list available ports

Once connected, type commands and press Enter. Type 'exit' or 'quit' to
leave, or press Ctrl+C / Ctrl+D.
"""

import argparse
import sys
import time

import serial
import serial.tools.list_ports


def list_ports() -> None:
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No serial ports found.")
        return
    print("Available serial ports:")
    for p in ports:
        print(f"  {p.device:<15} {p.description}")


class TigerController:
    """Thin wrapper around a pyserial connection to an ASI Tiger controller."""

    def __init__(self, port: str, baud: int = 115200, timeout: float = 1.0):
        self.ser = serial.Serial(port=port, baudrate=baud, timeout=timeout)
        # Give the controller a moment after opening the port.
        time.sleep(0.3)
        self.ser.reset_input_buffer()

    def send(self, command: str) -> str:
        """
        Send a single command line to the controller and return the raw
        response (with trailing CR/LF stripped). Blank input sends nothing.
        """
        command = command.strip()
        if not command:
            return ""
        self.ser.write((command + "\r").encode("ascii"))
        return self._read_response()

    def _read_response(self) -> str:
        """
        Read a response. Most Tiger replies are a single line ending in
        <CR><LF>, but a few commands (e.g. BU X, AZ) reply with multiple
        lines. We keep reading lines until the port times out (no more
        data arrives within `timeout` seconds), which naturally captures
        multi-line replies too.
        """
        lines = []
        while True:
            raw = self.ser.readline()
            if not raw:
                break  # timed out waiting for more data
            lines.append(raw.decode("ascii", errors="replace").rstrip("\r\n"))
        return "\n".join(lines)

    def close(self) -> None:
        if self.ser.is_open:
            self.ser.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def run_terminal(port: str, baud: int) -> None:
    print(f"Connecting to {port} at {baud} baud...")
    try:
        tiger = TigerController(port, baud)
    except serial.SerialException as e:
        print(f"Could not open port {port}: {e}")
        sys.exit(1)

    print("Connected. Type a command and press Enter (e.g. 'BU' or '1WHERE X').")
    print("Type 'exit' or 'quit' to leave.\n")

    try:
        with tiger:
            while True:
                try:
                    cmd = input("> ").strip()
                except EOFError:
                    print()
                    break

                if cmd.lower() in ("exit", "quit"):
                    break

                response = tiger.send(cmd)
                if response:
                    print(response)
    except KeyboardInterrupt:
        print("\nExiting.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Serial terminal for ASI Tiger controllers.")
    parser.add_argument("--port", "-p", help="Serial port, e.g. COM4 or /dev/ttyUSB0")
    parser.add_argument("--baud", "-b", type=int, default=115200,
                         help="Baud rate (default 115200; try 9600 for older RS-232 setups)")
    parser.add_argument("--list", "-l", action="store_true", help="List available serial ports and exit")
    args = parser.parse_args()

    if args.list:
        list_ports()
        return

    if not args.port:
        parser.error("--port is required (use --list to see available ports)")

    run_terminal(args.port, args.baud)


if __name__ == "__main__":
    main()
