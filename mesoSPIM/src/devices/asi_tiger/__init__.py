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
from .dac import (
    ASITigerDAC, DacChannel, DAC_RANGE_CODES, DAC_RANGE_LIMITS_MV,
    UNITS_PER_VOLT_SIGNAL_DAC, UNITS_PER_VOLT_TGGALVO,
)
from .plc import (
    PLCCard, CELL_TYPE,
    IO_TYPE_INPUT, IO_TYPE_OPEN_DRAIN_OUTPUT, IO_TYPE_PUSH_PULL_OUTPUT,
    cell_addr, bnc_addr, backplane_addr, inverted, rising_edge, falling_edge,
)
from .singleaxis import (
    SingleAxisWaveform,
    PATTERN_SAWTOOTH, PATTERN_TRIANGLE, PATTERN_SQUARE, PATTERN_SINE, PATTERN_VARIABLE_TRIANGLE,
    SAM_IDLE, SAM_ACTIVE, SAM_ARM_TRIGGER_ONCE, SAM_ACTIVE_SYNC, SAM_ARM_TRIGGER_FREE_RUN,
    build_sap_code, trigger_in_backplane_addr, ttl_out_backplane_addr, axis_slot_index,
    enable_backplane_trigger_mode,
)

from .zstack_chain import configure_zstack_trigger_chain, ZStackTriggerChain
from .row_setup import configure_laser_enable_lines, LaserEnableLines, configure_lr_switch, LRSwitch

from .stage_trigger import (
    StageRingBuffer, load_ring_buffer_point,
    TTL_MODE_DISARMED, TTL_MODE_ABSOLUTE, TTL_MODE_RELATIVE,
    RB_MODE_CONSUME, RB_MODE_TTL_TRIGGERED, RB_MODE_ONESHOT_AUTOPLAY,
    RB_MODE_REPEAT_AUTOPLAY, RB_MODE_ONESHOT_AUTOPLAY_NO_RETURN,
    OUT0_MODE_LOW, OUT0_MODE_HIGH, OUT0_MODE_MOVE_COMPLETE,
)

__all__ = [
    "TigerController",
    "TigerError",
    "ASITigerDAC",
    "DacChannel",
    "DAC_RANGE_CODES",
    "DAC_RANGE_LIMITS_MV",
    "UNITS_PER_VOLT_SIGNAL_DAC",
    "UNITS_PER_VOLT_TGGALVO",
    "PLCCard",
    "CELL_TYPE",
    "IO_TYPE_INPUT",
    "IO_TYPE_OPEN_DRAIN_OUTPUT",
    "IO_TYPE_PUSH_PULL_OUTPUT",
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
    "StageRingBuffer",
    "load_ring_buffer_point",
    "TTL_MODE_DISARMED",
    "TTL_MODE_ABSOLUTE",
    "TTL_MODE_RELATIVE",
    "configure_zstack_trigger_chain",
    "ZStackTriggerChain",
    "configure_laser_enable_lines",
    "LaserEnableLines",
    "configure_lr_switch",
    "LRSwitch",
    "RB_MODE_CONSUME",
    "RB_MODE_TTL_TRIGGERED",
    "RB_MODE_ONESHOT_AUTOPLAY",
    "RB_MODE_REPEAT_AUTOPLAY",
    "RB_MODE_ONESHOT_AUTOPLAY_NO_RETURN",
    "OUT0_MODE_LOW",
    "OUT0_MODE_HIGH",
    "OUT0_MODE_MOVE_COMPLETE",
]
