"""
asi_tiger
=========
A small, dependency-light (pyserial-only) Python library for talking to
ASI Tiger (TG-1000) controllers, with a focus on SIGNAL_DAC_4CH analog
output cards.

    from asi_tiger import ASITigerDAC

    dac = ASITigerDAC("COM4")
    dac.connect()
    dac.add_channel("scanner_x", card_addr=37, axis="A")
    dac.zero_all()
    dac.set_voltage("scanner_x", 2.5)
    dac.disconnect()
"""

from .controller import TigerController, TigerError
from .dac import ASITigerDAC, DacChannel, DAC_RANGE_CODES, DAC_RANGE_LIMITS_MV
from .plc import (
    PLCCard, CELL_TYPE,
    IO_TYPE_INPUT, IO_TYPE_OPEN_DRAIN_OUTPUT, IO_TYPE_PUSH_PULL_OUTPUT,
    TRIGGER_INTERNAL_4KHZ, TRIGGER_BACKPLANE_C7, TRIGGER_BACKPLANE_C13,
    TRIGGER_BACKPLANE_C14, TRIGGER_BNC1,
    cell_addr, bnc_addr, backplane_addr, inverted, rising_edge, falling_edge,
)
from .singleaxis import (
    SingleAxisWaveform,
    PATTERN_SAWTOOTH, PATTERN_TRIANGLE, PATTERN_SQUARE, PATTERN_SINE, PATTERN_VARIABLE_TRIANGLE,
    SAM_IDLE, SAM_ACTIVE, SAM_ARM_TRIGGER_ONCE, SAM_ACTIVE_SYNC, SAM_ARM_TRIGGER_FREE_RUN,
    build_sap_code, trigger_in_backplane_addr, ttl_out_backplane_addr, axis_slot_index,
    enable_backplane_trigger_mode,
)

__all__ = [
    "TigerController",
    "TigerError",
    "ASITigerDAC",
    "DacChannel",
    "DAC_RANGE_CODES",
    "DAC_RANGE_LIMITS_MV",
    "PLCCard",
    "CELL_TYPE",
    "IO_TYPE_INPUT",
    "IO_TYPE_OPEN_DRAIN_OUTPUT",
    "IO_TYPE_PUSH_PULL_OUTPUT",
    "TRIGGER_INTERNAL_4KHZ",
    "TRIGGER_BACKPLANE_C7",
    "TRIGGER_BACKPLANE_C13",
    "TRIGGER_BACKPLANE_C14",
    "TRIGGER_BNC1",
    "cell_addr",
    "bnc_addr",
    "backplane_addr",
    "inverted",
    "rising_edge",
    "falling_edge",
    "SingleAxisWaveform",
    "PATTERN_SAWTOOTH",
    "PATTERN_TRIANGLE",
    "PATTERN_SQUARE",
    "PATTERN_SINE",
    "PATTERN_VARIABLE_TRIANGLE",
    "SAM_IDLE",
    "SAM_ACTIVE",
    "SAM_ARM_TRIGGER_ONCE",
    "SAM_ACTIVE_SYNC",
    "SAM_ARM_TRIGGER_FREE_RUN",
    "build_sap_code",
    "trigger_in_backplane_addr",
    "ttl_out_backplane_addr",
    "axis_slot_index",
    "enable_backplane_trigger_mode",
]
