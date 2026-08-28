"""
asi_tiger.dac
=============
High-level interface to ASI Tiger DAC cards (SIGNAL_DAC_4CH and its
TGGALVO-derived variant -- see units_per_volt below).

Hardware facts this module encodes (see ASI docs, tggalvo page, and
ASI support correspondence for the specific card ranges/units on this
rack):
  - SIGNAL_DAC_4CH firmware sets output voltage directly via the MOVE
    ('M') command, in MILLIVOLTS -- not the 1/10-micron units used by
    motor axes. This is the ORIGINAL firmware on all three DAC cards.
  - A NEW firmware ASI provided for card 37 (galvo) specifically --
    adapted from a MicroMirror control firmware whose native units were
    milliradians -- uses a DIFFERENT control range: -4000 to 4000
    representing the SAME +/-10.24V as the original firmware's -10240
    to 10240 (mV). This is NOT a simple unit rename: 4000/10240 =
    0.390625 raw-units-per-mV, i.e. sending a "millivolt-style" value
    on this firmware would command roughly 2.56x the intended voltage.
    See DacChannel.units_per_volt.
  - Output range per card is set with the PR command (SIGNAL_DAC_4CH
    firmware) and is a CARD-WIDE setting (shared by all 4 axes on that
    card), not per-channel. NOT CONFIRMED whether PR/range_code behaves
    the same way on the new TGGALVO-derived firmware, which has a fixed
    native range rather than the original's selectable PR codes --
    range_code is currently still accepted for channels on that
    firmware but should be treated as unverified until confirmed.
  - CAUTION (from ASI): on power-up, SIGNAL_DAC outputs briefly swing
    to -10V. Always zero_all() immediately after connect(), before
    anything downstream is powered/sensitive.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional
import math
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
# These are REAL VOLTAGE limits (expressed in mV units for historical/
# readability reasons) and stay firmware-agnostic -- what changes
# between firmwares is units_per_volt (see DacChannel), the encoding
# used to represent a given real voltage on the wire, not the safety
# bound itself.
DAC_RANGE_LIMITS_MV = {
    0: (0, 2048),
    1: (0, 4096),
    2: (0, 10240),
    3: (-1024, 1024),
    4: (-2048, 2048),
    5: (-5128, 5128),
    6: (-10240, 10240),
}

# Raw device units per volt, by firmware. SIGNAL_DAC_4CH's native units
# are literally millivolts (1000 units/volt). ASI's TGGALVO-derived
# firmware for card 37 uses -4000..4000 representing the same
# +/-10.24V, i.e. 4000/10.24 units/volt.
UNITS_PER_VOLT_SIGNAL_DAC = 1000.0
UNITS_PER_VOLT_TGGALVO = 4000 / 10.24  # == 390.625


@dataclass
class DacChannel:
    """
    One physical DAC channel: a card address + axis letter.

    units_per_volt: raw device units per volt on the WIRE PROTOCOL for
        this specific axis's card firmware. Defaults to 1000.0
        (SIGNAL_DAC_4CH's native millivolt units -- the original
        firmware on all three DAC cards as shipped). Use
        UNITS_PER_VOLT_TGGALVO (~390.625) for a channel on card 37 if
        it's been reflashed with ASI's TGGALVO-derived firmware --
        confirmed by ASI directly: that firmware's control range is
        -4000..4000 for the same +/-10.24V, NOT millivolts, and using
        the wrong constant here would command roughly 2.56x the
        intended voltage. This affects the wire ENCODING only --
        safety_limit_mv/limits_mv below remain expressed as real
        voltage bounds regardless of which firmware/encoding a channel
        uses.
    safety_limit_mv: an OPTIONAL hard clamp tighter than the card's
        hardware range_code. Use this when the card's electrical range
        is wider than what's actually safe for the downstream device --
        e.g. ASI's own hardware engineers specify the galvo card's range
        as +/-10.24V but require commands to stay within +/-10.00V
        ("Limit command voltage to + and - 10000 (+-10v) to guarantee
        galvo amplifier safety") -- range_code=6 alone does NOT enforce
        that tighter bound, safety_limit_mv does. Expressed in mV
        (real voltage), independent of units_per_volt.
    max_step_v: an OPTIONAL slew-rate limit, in volts per set_voltage()
        call. Per ASI: "Sudden jumps in command voltage that are faster
        then the inertial moment of the device can cause damage" to
        galvo/tunable-lens hardware -- their card-level analog Bessel
        low-pass filters (400Hz on ETL/laser cards, 1.6kHz -- or faster
        on the new TGGALVO firmware's fast axes -- on the galvo card)
        smooth normal waveform playback, but a single large jump
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
    units_per_volt: float = UNITS_PER_VOLT_SIGNAL_DAC

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
        units_per_volt: float = UNITS_PER_VOLT_SIGNAL_DAC,
    ):
        """
        Register a logical channel name -> (card_addr, axis letter).
        See DacChannel's docstring for safety_limit_mv / max_step_v /
        units_per_volt -- units_per_volt defaults to the standard
        SIGNAL_DAC_4CH millivolt encoding; pass UNITS_PER_VOLT_TGGALVO
        for a channel on card 37 if it's running ASI's new firmware.
        """
        self.channels[name] = DacChannel(
            name=name, card_addr=card_addr, axis=axis.upper(), range_code=range_code,
            safety_limit_mv=safety_limit_mv, max_step_v=max_step_v, max_step_delay_s=max_step_delay_s,
            units_per_volt=units_per_volt,
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
                units_per_volt=get("units_per_volt", UNITS_PER_VOLT_SIGNAL_DAC),
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

        Safety bounds (limits_mv/safety_limit_mv) are checked in REAL
        VOLTAGE terms, independent of the channel's units_per_volt --
        this stays correct regardless of which firmware/encoding a
        channel uses. Only the final wire encoding (the actual integer
        sent in the M command) uses units_per_volt.

        If the channel has max_step_v set (see DacChannel docstring --
        ASI's explicit warning that sudden jumps can damage galvo/lens
        hardware), a large change is broken into intermediate steps of
        at most max_step_v (interpolated in volts, then encoded per
        step), each separated by max_step_delay_s, instead of being
        commanded in a single jump. This makes set_voltage() take longer
        for large jumps on protected channels -- that's the point, not
        a bug.
        """
        ch = self._get(name)
        lo, hi = ch.limits_mv  # real mV bounds, firmware-agnostic
        target_mv = volts * 1000.0
        if not (lo <= target_mv <= hi):
            raise ValueError(
                f"{volts:.4f} V ({target_mv:.1f} mV) is outside the allowed range "
                f"[{lo/1000:.3f}, {hi/1000:.3f}] V for channel {name!r} "
                f"(hardware range_code plus any safety_limit_mv). "
                f"Use set_range() first if you need a wider hardware range."
            )

        def encode(v: float) -> int:
            return int(round(v * ch.units_per_volt))

        if ch.max_step_v is None:
            self.tiger.send_command(f"M {ch.axis}={encode(volts)}", card_addr=ch.card_addr)
            return

        try:
            current_v = self.get_voltage(name)
        except Exception:
            current_v = volts  # can't read back -- just command it directly, no ramp

        delta_v = volts - current_v
        if abs(delta_v) <= ch.max_step_v:
            self.tiger.send_command(f"M {ch.axis}={encode(volts)}", card_addr=ch.card_addr)
            return

        steps = max(1, math.ceil(abs(delta_v) / ch.max_step_v))
        for i in range(1, steps + 1):
            intermediate_v = current_v + delta_v * i / steps
            self.tiger.send_command(f"M {ch.axis}={encode(intermediate_v)}", card_addr=ch.card_addr)
            if i < steps:
                time.sleep(ch.max_step_delay_s)

    def get_voltage(self, name: str) -> float:
        """Query a channel's current output voltage, in volts (WHERE / 'W')."""
        ch = self._get(name)
        reply = self.tiger.send_command(f"W {ch.axis}", card_addr=ch.card_addr)
        try:
            raw = float(reply.replace(f"{ch.axis}=", "").split()[0])
        except (IndexError, ValueError) as exc:
            raise TigerError(-1, f"Could not parse voltage reply: {reply!r}") from exc
        return raw / ch.units_per_volt

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
