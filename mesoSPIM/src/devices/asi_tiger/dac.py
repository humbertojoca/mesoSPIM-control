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

# PR command range codes for SIGNAL_DAC_4CH / TGGALVO firmware.
# CORRECTED against ASI's currently-documented command:pr table
# (docs.asiimaging.com/commands/pr) -- an earlier version of this table
# had codes 3-6 shifted by one position relative to what ASI's docs
# actually say (e.g. this project's code claimed 6 = +/-10.24V, but
# ASI's own docs say 6 = +/-5.12V and 7 = +/-10.24V), and was missing
# code 7 entirely. Fixed to match ASI's current docs exactly. NOT
# independently verified against real hardware for every code -- see
# ASITigerDAC.query_range()'s docstring: confirm the REAL, currently-
# active range code via PR <axis>? before trusting any code's safety
# limits for something safety-relevant (e.g. a galvo scanner), the
# same as everything else in this project.
DAC_RANGE_CODES = {
    "0V_2.048V": 0,
    "0V_4.096V": 1,
    "0V_10.24V": 2,
    # code 3 is a special "restart-dependent default" selector per ASI's
    # docs, not a fixed range -- deliberately omitted from this
    # name-based lookup (use the numeric code 3 directly with set_range()
    # if you specifically want that behavior, understanding what it
    # resolves to depends on firmware version -- see command:pr docs)
    "-1.024V_1.024V": 4,
    "-2.048V_2.048V": 5,
    "-5.12V_5.12V": 6,  # firmware default
    "-10.24V_10.24V": 7,
}

# (min_mV, max_mV) per range code -- used for client-side clamping so we
# reject an out-of-range voltage before it ever hits the serial link.
# These are REAL VOLTAGE limits (expressed in mV units for historical/
# readability reasons) and stay firmware-agnostic -- what changes
# between firmwares is units_per_volt (see DacChannel), the encoding
# used to represent a given real voltage on the wire, not the safety
# bound itself. CORRECTED -- see DAC_RANGE_CODES' comment above for why.
# Code 3 omitted deliberately (restart-dependent default, not a fixed
# range -- see command:pr docs before using set_range(name, 3)).
DAC_RANGE_LIMITS_MV = {
    0: (0, 2048),
    1: (0, 4096),
    2: (0, 10240),
    4: (-1024, 1024),
    5: (-2048, 2048),
    6: (-5120, 5120),
    7: (-10240, 10240),
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
    range_code: int = 6  # firmware's factory default per ASI's docs (+/-5.12V,
    # corrected from an earlier wrong table -- see DAC_RANGE_CODES' comment).
    # NEVER independently verified against real hardware for ANY channel in
    # this project -- set_range()/PR has never actually been called anywhere
    # here, so this is a software-side label only, not confirmed to match
    # whatever range the real hardware is actually running. Use
    # ASITigerDAC.query_range() to check before trusting this for anything
    # safety-relevant.
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
        self._pending_range_changes: Dict[int, int] = {}  # card_addr -> requested range_code

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
        Sends the PR command to change a channel's card's output range.

        IMPORTANT -- CONFIRMED FROM ASI'S OWN command:pr DOCS: this
        setting does NOT take effect until the controller is reset or
        restarted ("Controller reset or restart is needed for setting
        to take effect"). Sending PR alone changes nothing about the
        ACTUAL hardware output range yet.

        FURTHER CONFIRMED ON REAL HARDWARE (by the user, on card 35 --
        NOT found in any ASI documentation this project has managed to
        fetch; several attempts to fetch ASI's own SS/PR doc pages
        returned errors, so this is real-hardware-confirmed only, not
        doc-confirmed): sending PR alone was NOT enough to survive an
        actual power cycle -- the range reverted. Explicitly sending
        `SS Z` (card-addressed -- see save_settings() below) AFTER PR
        is what made the new range genuinely persist across a real
        power-down/power-up cycle. Always call save_settings(card_addr)
        after set_range() if you need the new range to survive a real
        power cycle, not just a soft reset/restart -- this method does
        NOT do that for you automatically, since PR+SS changes real,
        persistent hardware state and shouldn't happen silently as a
        side effect of a method whose name doesn't say so.

        Because of that, this method deliberately does NOT update this
        channel's cached range_code (and thus limits_mv, which
        set_voltage()'s safety check relies on) immediately -- doing so
        would create a dangerous window where software believes a wider
        (or narrower) range is already active and permits/computes
        set_voltage() calls accordingly, while the hardware is still
        actually operating on the OLD range. Given ASI's own docs note
        the axis's raw millivolt encoding is range-code-dependent (e.g.
        "PR H=2, the maximum axis value of H is 10240" vs "PR H=0...is
        2048"), a value that's valid under the NEW range but sent before
        the hardware has actually switched could be badly wrong, not
        just clipped.

        Instead, this records the requested range_code as PENDING for
        this channel's card_addr. Every OTHER channel on the same
        card_addr keeps using its OLD cached range_code (and thus old,
        safe limits_mv) until you explicitly call
        confirm_range_change(card_addr) -- which you should only do
        AFTER actually resetting/restarting the controller and
        confirming the new range is genuinely active.

        NOTE: PR is a CARD-WIDE setting on SIGNAL_DAC_4CH cards -- the
        pending change (and later, confirm_range_change()) affects
        every registered channel that shares the same card_addr as
        `name`, not just `name` itself.
        """
        if range_code not in DAC_RANGE_LIMITS_MV:
            raise ValueError(f"Unknown range_code {range_code}; valid: {list(DAC_RANGE_LIMITS_MV)}")
        ch = self._get(name)
        self.tiger.send_command(f"PR {ch.axis}={range_code}", card_addr=ch.card_addr)
        self._pending_range_changes[ch.card_addr] = range_code

    def confirm_range_change(self, card_addr: int):
        """
        Call this ONLY AFTER you have actually reset or restarted the
        controller following set_range(), and confirmed (e.g. by
        checking PR <axis>? or a real voltage measurement) that the new
        range is genuinely active. Applies the pending range_code from
        set_range() to every registered channel on card_addr, updating
        their cached range_code (and thus limits_mv) so set_voltage()'s
        safety check reflects the hardware's real, now-active range.

        Raises ValueError if no pending change was recorded for this
        card_addr (i.e. set_range() was never called, or this was
        already confirmed once).
        """
        if card_addr not in self._pending_range_changes:
            raise ValueError(
                f"No pending range change recorded for card_addr={card_addr} -- "
                f"did you call set_range() first? (Or this was already confirmed.)"
            )
        range_code = self._pending_range_changes.pop(card_addr)
        for other in self.channels.values():
            if other.card_addr == card_addr:
                other.range_code = range_code

    def save_settings(self, card_addr: int):
        """
        Sends `SS Z` (card-addressed) to commit the card's current
        settings -- including a just-sent PR range change -- to
        non-volatile flash, so they survive a real power cycle.

        CONFIRMED ON REAL HARDWARE by the user (card 35): after
        set_range() alone, the new PR range reverted on a real power
        cycle; sending `SS Z` afterward made it persist correctly.
        This is the user's own direct finding, not something
        independently confirmed against ASI's written documentation --
        several attempts to fetch ASI's own SS/PR doc pages from this
        project returned errors (provenance/rate-limit issues, not a
        content lookup that came back empty), so the exact general
        semantics of `SS <param>` (whether `Z` specifically means "all
        current settings" vs. something narrower, and whether other
        param values exist) are UNCONFIRMED beyond this one
        real-hardware data point. Treat `Z` as the one value this
        project has actually confirmed works for this purpose; don't
        assume other values without checking ASI's docs directly or
        testing on real hardware first.

        Call this ONCE per card_addr, AFTER set_range() -- it is a
        card-wide save, not per-axis. Does NOT itself wait for or
        confirm a power cycle; pair with a real power-cycle and a
        query_range() check afterward (see
        tools/asi_tiger_laser_dac_range_setup.py) before trusting the
        new range as genuinely persistent.
        """
        self.tiger.send_command("SS Z", card_addr=card_addr)

    def pending_range_change(self, card_addr: int) -> Optional[int]:
        """Returns the range_code requested via set_range() for this
        card_addr that hasn't been confirmed yet via
        confirm_range_change(), or None if there's nothing pending."""
        return self._pending_range_changes.get(card_addr)

    def query_range(self, name: str) -> str:
        """
        PR <axis>? -- queries the REAL, currently-active range code
        directly from the hardware, as a raw reply string. Read-only,
        safe to call anytime.

        Use this to check whether a channel's cached range_code
        actually matches reality -- set_range()/PR has never been
        called anywhere in this project as of this writing, so every
        channel's range_code (including the default) is a software-side
        label only, never confirmed against the real hardware. Worth
        checking before trusting limits_mv for anything safety-relevant
        (e.g. a galvo scanner) -- if the raw reply's code doesn't match
        this channel's cached range_code, the safety bounds set_voltage()
        is enforcing don't reflect the hardware's real, actual range.
        """
        ch = self._get(name)
        return self.tiger.send_command(f"PR {ch.axis}?", card_addr=ch.card_addr)

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
