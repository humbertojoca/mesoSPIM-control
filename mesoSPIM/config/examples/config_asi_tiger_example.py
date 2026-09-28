"""
config_asi_tiger_example.py
=============================
Example config additions for the ASI Tiger DAC/PLC waveform generation
backend (mesoSPIM_ASITigerWaveFormGenerator). This is NOT a complete
mesoSPIM config file -- copy the pieces below into your existing config
(alongside your laserdict, stage_parameters, camera_parameters, etc.),
which is exactly how demo_config.py and the other example configs in
this folder are meant to be used.

REWRITTEN to match the CURRENT adapter's config_check()/create_tasks()
key shape -- the previous version of this file (galvo_etl_channels,
laser_channels, plc_camera_trigger_bnc, plc_laser_bncs,
plc_camera_trigger_cell, software_stream_rate_hz) reflected an EARLIER
draft of the adapter (pre-per-frame-redesign) and would fail
config_check() immediately against the current file. See
PATCHNOTES_ASI_TIGER.md for the full history of why the design changed
(true per-frame camera triggering instead of the autonomous
zstack_chain loop; Z/F handled entirely by mesoSPIM's own existing
per-frame stage-stepping, not this file; two separate galvo/ETL pairs,
one per illumination arm, confirmed directly by the user -- not one
pair shared/switched optically).

Card specs, ranges, and safety limits below are taken directly from
ASI's own confirmation email about this specific rack's build (firmware
v3.60 SIGNAL_DAC_4CH plus a later TGGALVO firmware update for the
galvo card specifically), not guessed -- see PATCHNOTES_ASI_TIGER.md,
"ASI-confirmed hardware facts" for the full email content. Re-verify
against your own rack's actual card addresses with the 'N' command
before trusting these numbers (see tools/asi_tiger_hardware_test.py).

See tools/asi_tiger_hardware_test.py to verify your DAC channels and
PLC wiring, and tools/asi_tiger_galvo_etl_demo.py to bench-verify the
galvo/ETL pair for ONE side in isolation, BEFORE trusting this config
for a real acquisition.
"""

# Select the ASI Tiger backend instead of 'NI', 'cDAQ', or 'DemoWaveFormGeneration'.
# Requires the mesoSPIM_Core.py edit described in PATCHNOTES_ASI_TIGER.md.
waveformgeneration = 'ASI_Tiger'

# Read by mesoSPIM_ASITigerWaveFormGenerator.config_check() / create_tasks() /
# write_waveforms_to_tasks(). See that file's ARCHITECTURE NOTE for the full
# design rationale -- this is NOT a 1:1 mirror of the NI 'acquisition_hardware'
# shape, since the hardware split is genuinely different (PLC-driven camera
# trigger + per-frame Expose-Out polling, hardware-timed single-axis
# waveforms for galvo/ETL instead of NI analog output tasks).
asi_dac_parameters = {
    # Serial connection to the Tiger rack. If this matches the ASI stage's
    # COMport in asi_parameters below, the DAC/PLC backend automatically
    # reuses that connection instead of opening a 2nd handle -- REQUIRED
    # if the stage, DAC, and PLC cards share one physical rack/COM port.
    'port': 'COM5',
    'baudrate': 115200,

    # ------------------------------------------------------------------
    # PLC (Programmable Logic Card): manual camera-trigger cell + camera
    # Expose-Out readback + ETL sync. See mesoSPIM_ASITigerWaveFormGenerator
    # .create_tasks() for the exact wiring.
    'plc_card_addr': 36,
    'plc_axis': 'E',
    'camera_expose_bnc': 3,     # PLC BNC wired FROM the camera's Expose-Out
    'camera_trigger_bnc': 4,    # PLC BNC wired TO the camera's trigger input
    'camera_trigger_cell': 10,  # optional -- PLC cell used as the manual trigger D-flop (default 10)
    'camera_trigger_pulse_ms': 10.0,             # optional, default 10.0
    'camera_expose_start_timeout_s': 2.0,        # optional, default 2.0
    'camera_expose_end_timeout_margin_s': 2.0,   # optional, default 2.0 (added to state['camera_exposure_time'])

    # ------------------------------------------------------------------
    # ETL (Tunable Lens control, into an EL-E-4 driver): TWO separate axes,
    # one per illumination arm -- confirmed directly (not one axis shared/
    # switched optically). Both on card 4 (H/I/J/K), 0-4.096V, 400Hz LPF,
    # ORIGINAL SIGNAL_DAC_4CH firmware (units_per_volt defaults to 1000,
    # the standard millivolt convention -- no override needed).
    #
    # CONFIRMED ON REAL HARDWARE: K has a trigger fault on this card --
    # with SAM=2 triggered externally, K only responds to every OTHER
    # pulse (tested from a fresh controller reset, several seconds
    # between manual pulses, ruling out re-arm timing; correct jumper
    # installed and verified). H, I, and J were all confirmed working
    # correctly under the identical protocol. The production pair is H
    # (etl_l) and J (etl_r) -- NOT K, and NOT I (I is also confirmed
    # working but isn't the axis actually wired/in use).
    'etl_card_addr': 34,
    'etl_card_first_axis': 'H',   # first axis letter on this card, for backplane-trigger slot math
    'etl_l_axis': 'H',
    'etl_r_axis': 'J',

    # etl_period_ms is now OPTIONAL -- leave it unset (as below) and the
    # adapter derives it automatically, every row, from the real camera
    # exposure time: self.state['camera_exposure_time']*1000 -
    # etl_period_margin_ms. This replaced an earlier flat guessed value
    # (120.0ms) after the user switched the camera's readout mode to
    # "All Rows", which made Expose-Out span the FULL configured exposure
    # duration instead of a shorter rolling-shutter-derived pulse -- so
    # the ETL sweep should track the real exposure time, not a constant.
    # See mesoSPIM_ASITigerWaveFormGenerator._etl_period_ms() and
    # PATCHNOTES_ASI_TIGER.md for the full history.
    #
    # 'etl_period_ms': 120.0,     # uncomment to force a fixed period instead (bypasses auto-derivation)
    #
    # etl_period_margin_ms defaults to 0.0 (derived period = the real
    # exposure time, exactly) -- per the user, directly: "in reality,
    # margin period is negligible if the expose out is in 'all rows'",
    # since under that readout mode Expose-Out spans the real exposure
    # window with no known extra jitter to margin against. Left
    # commented out here since 0.0 is already the default; uncomment
    # and set a positive value only if your rack/readout mode is found
    # to still need some margin (the ETL's SAM=2 period must stay
    # SHORTER than the camera's real trigger interval, or the axis
    # misses every other trigger edge).
    # 'etl_period_margin_ms': 0.0,

    # ------------------------------------------------------------------
    # Galvo (scan mirror): TWO separate axes, one per illumination arm --
    # same confirmed topology as the ETL above. Card 7 (A/B/C/D),
    # -10.24 to +10.24V.
    #
    # UPDATED FIRMWARE: ASI provided new TGGALVO firmware for this card
    # (bench-confirmed working -- 100Hz triangle waveforms on A/C are
    # visibly smooth, vs. audible/visible stepping at 50Hz on the
    # original firmware's B/D). This new firmware REVERSES which axes
    # are fast: A and C now run at 40kHz, B/D at the normal ~1kHz -- the
    # OPPOSITE of the original SIGNAL_DAC_4CH firmware. Hence galvo_l/
    # galvo_r map to A/C below, not B/D.
    #
    # CRITICAL UNITS CHANGE (confirmed by ASI directly): this new
    # firmware's control range is -4000..4000 for the SAME +/-10.24V the
    # original firmware represents as -10240..10240 (mV) -- that's
    # units_per_volt = 4000/10.24 = ~390.625, NOT the library default of
    # 1000. Using the wrong value here would command ~2.56x the intended
    # voltage. See asi_tiger.dac.UNITS_PER_VOLT_TGGALVO for the exact
    # constant (galvo_units_per_volt below uses the same value).
    'galvo_card_addr': 37,
    'galvo_l_axis': 'A',
    'galvo_r_axis': 'C',
    'galvo_units_per_volt': 4000 / 10.24,  # new TGGALVO firmware -- see note above; pass 1000.0 instead
                                            # if your card is still on the original SIGNAL_DAC_4CH firmware
    # SAFETY (ASI, verbatim, still applies regardless of firmware/units):
    # "Limit command voltage to + and - 10000 (+-10v) to guarantee galvo
    # amplifier safety." mesoSPIM_ASITigerWaveFormGenerator.write_waveforms_to_tasks()
    # checks amplitude/2 + abs(offset) against this EVERY row (parameters can
    # change row to row) and refuses to drive the galvo (leaving it stopped
    # and zeroed, logging an error) rather than exceed it.
    'galvo_max_volts': 10.0,

    # ------------------------------------------------------------------
    # Laser modulation channels, in the SAME order as laserdict's keys
    # (same requirement the NI 'laser_task_line' has) -- config_check()
    # validates len(laser_dac_channels) == len(laserdict).
    #
    # Card 5 (P/Q/R/S): 0 to 4.096V, 400Hz LPF (adjustable via BACKLASH
    # if you need a different cutoff), intended for analog laser
    # intensity control. Only 'name' (matching a DacChannel's own name,
    # used by write_waveforms_to_tasks() to look up which channel to
    # drive for the active laser) is required here; the actual channel
    # is added via ASITigerDAC.add_channel(**ch) in create_tasks(), so
    # each dict's other keys (card_addr, axis, range_code, ...) must
    # match DacChannel's constructor.
    'laser_dac_channels': [
        {'name': 'laser_405', 'card_addr': 35, 'axis': 'P', 'range_code': 1},
        {'name': 'laser_488', 'card_addr': 35, 'axis': 'Q', 'range_code': 1},
        {'name': 'laser_561', 'card_addr': 35, 'axis': 'R', 'range_code': 1},
        {'name': 'laser_638', 'card_addr': 35, 'axis': 'S', 'range_code': 1},
    ],

    # ------------------------------------------------------------------
    # Optional: PLC-based DAC voltage range switch selecting which
    # illumination arm receives the laser (tested and confirmed working
    # at 5V high level -- see PATCHNOTES_ASI_TIGER.md). Leave
    # lr_switch_card_addr unset/None to skip if your rack doesn't have
    # this switch.
    'lr_switch_card_addr': 34,
    'lr_switch_axis': 'I',
    'lr_switch_left_v': 0.0,
    'lr_switch_right_v': 5.0,
    'lr_switch_range_code': 2,   # 0-10.24V range -- see PATCHNOTES_ASI_TIGER.md for why
}

# If your XYZ stage is ALSO an ASI Tiger stage (stage_parameters['stage_type']
# containing 'asi'), point it at the same COM port so the connection is
# shared automatically -- see PATCHNOTES_ASI_TIGER.md, "Connection sharing".
# ALSO REQUIRED for this design (see mesoSPIM_ASITigerWaveFormGenerator's
# ARCHITECTURE NOTE): 'ttl_motion_enabled' must be False, since this backend
# relies entirely on mesoSPIM's own existing per-frame host-stepping loop
# for Z/F -- it does not touch Z/F itself at all.
#
# stage_parameters = {
#     'stage_type': 'TigerASI',
#     ...
# }
# asi_parameters = {
#     'COMport': 'COM5',   # <-- same port as asi_dac_parameters['port'] above
#     'baudrate': 115200,
#     'ttl_motion_enabled': False,   # REQUIRED -- see note above
#     ...
# }
