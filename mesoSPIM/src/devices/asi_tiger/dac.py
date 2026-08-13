"""
asi_tiger.dac
=============
High-level interface to ASI Tiger SIGNAL_DAC_4CH cards.

Wraps TigerController with volts-based semantics and per-axis card
addressing, so it drops into the same mental model as the existing
NI-DAQ AO controller (set_voltage() / zero_all()).

Hardware facts this module encodes (see ASI docs, tggalvo page):
  - SIGNAL_DAC_4CH firmware sets output voltage directly via the MOVE
    ('M') command, in MILLIVOLTS -- not the 1/10-micron units used by
    motor axes.
  - Output range per card is set with the PR command and is a
    CARD-WIDE setting (shared by all 4 axes on that card), not
    per-channel.
  - CAUTION (from ASI): on power-up, SIGNAL_DAC outputs briefly swing
    to -10V. Always zero_all() immediately after connect(), before
    anything downstream is powered/sensitive.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional
import time

from .controller import TigerController, TigerError

# PR command range codes for SIGNAL_DAC_4CH firmware.
DAC_RANGE_CODES = {
    "0V_2.048V": 0,
    "0V_4.096V": 1,
    "0V_10.24V": 2,
    "-1.024V_1.024V": 3,
    "-2.048V_2.048V": 4,
    "-5.128V_5.128V": 5,
    "-10.24V_10.24V": 6,  # firmware default
}

# (min_mV, max_mV) per range code -- used for client-side clamping so we
# reject an out-of-range voltage before it ever hits the serial link.
DAC_RANGE_LIMITS_MV = {
    0: (0, 2048),
    1: (0, 4096),
    2: (0, 10240),
    3: (-1024, 1024),
    4: (-2048, 2048),
    5: (-5128, 5128),
    6: (-10240, 10240),
}


@dataclass
class DacChannel:
    """
    One physical DAC channel: a card address + axis letter.

    safety_limit_mv: an OPTIONAL hard clamp tighter than the card's
        hardware range_code. Use this when the card's electrical range
        is wider than what's actually safe for the downstream device --
        e.g. ASI's own hardware engineers specify the galvo card's range
        as +/-10.24V but require commands to stay within +/-10.00V
        ("Limit command voltage to + and - 10000 (+-10v) to guarantee
        galvo amplifier safety") -- range_code=6 alone does NOT enforce
        that tighter bound, safety_limit_mv does.
    max_step_v: an OPTIONAL slew-rate limit, in volts per set_voltage()
        call. Per ASI: "Sudden jumps in command voltage that are faster
        then the inertial moment of the device can cause damage" to
        galvo/tunable-lens hardware -- their card-level analog Bessel
        low-pass filters (400Hz on ETL/laser cards, 1.6kHz on the galvo
        card) smooth normal waveform playback, but a single large jump
        (e.g. a GUI slider dragged quickly, or a bad script) can still
        exceed what the filter/mechanism can absorb safely. When set,
        set_voltage() breaks a large jump into intermediate steps of at
        most this size instead of commanding it in one jump. This is a
        conservative software safeguard, not a substitute for asking
        ASI/the device datasheet what an actually-safe step size is for
        your specific galvo/lens.
    """

    name: str
    card_addr: int
    axis: str
    range_code: int = 6  # default +/-10.24V
    safety_limit_mv: Optional[float] = None
    max_step_v: Optional[float] = None
    max_step_delay_s: float = 0.005

    @property
    def limits_mv(self):
        lo, hi = DAC_RANGE_LIMITS_MV[self.range_code]
        if self.safety_limit_mv is not None:
            lo = max(lo, -self.safety_limit_mv)
            hi = min(hi, self.safety_limit_mv)
        return lo, hi


class ASITigerDAC:
    """
    High-level ASI Tiger DAC controller.

    Usage:
        dac = ASITigerDAC("COM4")
        dac.connect()
        dac.add_channel("scanner_x", card_addr=37, axis="A")
        dac.add_channel("scanner_y", card_addr=37, axis="B")
        dac.zero_all()                  # do this before anything else
        dac.set_voltage("scanner_x", 2.5)
        dac.disconnect()
    """

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = 115200,
        tiger: Optional[TigerController] = None,
    ):
        """
        Either give it a port to open its own connection (typical
        standalone use), or pass an existing tiger= TigerController to
        SHARE a connection that something else (e.g. a stage driver)
        already opened -- required when the DAC and stage cards live on
        the same physical Tiger rack/COM port. See
        TigerController.from_open_serial().
        """
        if tiger is not None:
            self.tiger = tiger
            self._owns_connection = False
        elif port is not None:
            self.tiger = TigerController(port, baudrate=baudrate)
            self._owns_connection = True
        else:
            raise ValueError("Provide either port= or an existing tiger=TigerController")
        self.channels: Dict[str, DacChannel] = {}

    # ------------------------------------------------------------------
    def connect(self):
        """No-op if using a shared connection that's already open (or
        owned/opened elsewhere) -- only opens a port this instance owns."""
        if self._owns_connection:
            self.tiger.connect()

    def disconnect(self):
        """No-op on a shared connection -- never closes a port this
        instance doesn't own, since something else may still be using it."""
        if self._owns_connection:
            self.tiger.disconnect()

    @property
    def is_connected(self) -> bool:
        return self.tiger.is_connected

    @property
    def owns_connection(self) -> bool:
        return self._owns_connection

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.disconnect()

    # ------------------------------------------------------------------
    # Channel registration
    # ------------------------------------------------------------------
    def add_channel(
        self, name: str, card_addr: int, axis: str, range_code: int = 6,
        safety_limit_mv: Optional[float] = None,
        max_step_v: Optional[float] = None,
        max_step_delay_s: float = 0.005,
    ):
        """
        Register a logical channel name -> (card_addr, axis letter).
        See DacChannel's docstring for safety_limit_mv / max_step_v.
        """
        self.channels[name] = DacChannel(
            name=name, card_addr=card_addr, axis=axis.upper(), range_code=range_code,
            safety_limit_mv=safety_limit_mv, max_step_v=max_step_v, max_step_delay_s=max_step_delay_s,
        )

    def add_channels_from_config(self, cfg_section) -> None:
        """
        Bulk-register channels from a config dict/DotDict shaped like:

        {
            "device": "COM4",
            "baudrate": 115200,
            "channels": [
                {"name": "scanner_x", "card_addr": 37, "axis": "A", "range_code": 6},
                {"name": "scanner_y", "card_addr": 37, "axis": "B", "range_code": 6}
            ]
        }

        Works with plain dicts and with config_manager.DotDict alike,
        since DotDict subclasses dict.
        """
        for ch in cfg_section["channels"]:
            get = ch.get if hasattr(ch, "get") else (lambda k, d=None: ch[k] if k in ch else d)
            self.add_channel(
                name=ch["name"],
                card_addr=ch["card_addr"],
                axis=ch["axis"],
                range_code=get("range_code", 6),
                safety_limit_mv=get("safety_limit_mv", None),
                max_step_v=get("max_step_v", None),
                max_step_delay_s=get("max_step_delay_s", 0.005),
            )

    def channel_names(self) -> List[str]:
        return list(self.channels.keys())

    # ------------------------------------------------------------------
    # Range configuration
    # ------------------------------------------------------------------
    def set_range(self, name: str, range_code: int):
        """
        Set the output voltage range for a channel's card (PR command).

        NOTE: PR is a CARD-WIDE setting on SIGNAL_DAC_4CH cards -- this
        will affect every registered channel that shares the same
        card_addr, not just `name`. Their cached range_code is updated
        to match so limits_mv stays accurate for all of them.
        """
        if range_code not in DAC_RANGE_LIMITS_MV:
            raise ValueError(f"Unknown range_code {range_code}; valid: {list(DAC_RANGE_LIMITS_MV)}")
        ch = self._get(name)
        self.tiger.send_command(f"PR {ch.axis}={range_code}", card_addr=ch.card_addr)
        for other in self.channels.values():
            if other.card_addr == ch.card_addr:
                other.range_code = range_code

    # ------------------------------------------------------------------
    # Voltage set/get
    # ------------------------------------------------------------------
    def set_voltage(self, name: str, volts: float):
        """
        Set one channel's output voltage, in volts.

        If the channel has max_step_v set (see DacChannel docstring --
        ASI's explicit warning that sudden jumps can damage galvo/lens
        hardware), a large change is broken into intermediate steps of
        at most max_step_v, each separated by max_step_delay_s, instead
        of being commanded in a single jump. This makes set_voltage()
        take longer for large jumps on protected channels -- that's the
        point, not a bug.
        """
        ch = self._get(name)
        lo, hi = ch.limits_mv
        target_mv = int(round(volts * 1000))
        if not (lo <= target_mv <= hi):
            raise ValueError(
                f"{volts:.4f} V ({target_mv} mV) is outside the allowed range "
                f"[{lo/1000:.3f}, {hi/1000:.3f}] V for channel {name!r} "
                f"(hardware range_code plus any safety_limit_mv). "
                f"Use set_range() first if you need a wider hardware range."
            )

        if ch.max_step_v is None:
            self.tiger.send_command(f"M {ch.axis}={target_mv}", card_addr=ch.card_addr)
            return

        max_step_mv = max(1, int(round(ch.max_step_v * 1000)))
        try:
            current_mv = int(round(self.get_voltage(name) * 1000))
        except Exception:
            current_mv = target_mv  # can't read back -- just command it directly, no ramp

        delta = target_mv - current_mv
        if abs(delta) <= max_step_mv:
            self.tiger.send_command(f"M {ch.axis}={target_mv}", card_addr=ch.card_addr)
            return

        steps = -(-abs(delta) // max_step_mv)  # ceil division
        for i in range(1, steps + 1):
            intermediate_mv = current_mv + round(delta * i / steps)
            self.tiger.send_command(f"M {ch.axis}={intermediate_mv}", card_addr=ch.card_addr)
            if i < steps:
                time.sleep(ch.max_step_delay_s)

    def get_voltage(self, name: str) -> float:
        """Query a channel's current output voltage, in volts (WHERE / 'W')."""
        ch = self._get(name)
        reply = self.tiger.send_command(f"W {ch.axis}", card_addr=ch.card_addr)
        try:
            mv = float(reply.replace(f"{ch.axis}=", "").split()[0])
        except (IndexError, ValueError) as exc:
            raise TigerError(-1, f"Could not parse voltage reply: {reply!r}") from exc
        return mv / 1000.0

    def zero_all(self):
        """
        Set every registered channel to 0 V.

        Call this immediately after connect(), before relying on any
        downstream device -- SIGNAL_DAC outputs can briefly swing to
        -10V on controller power-up.
        """
        for name in self.channels:
            self.set_voltage(name, 0.0)

    # ------------------------------------------------------------------
    def _get(self, name: str) -> DacChannel:
        if name not in self.channels:
            raise KeyError(f"Unknown DAC channel {name!r}. Registered: {self.channel_names()}")
        return self.channels[name]
