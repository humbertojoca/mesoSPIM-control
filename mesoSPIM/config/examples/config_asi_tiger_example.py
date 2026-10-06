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

# 'laser' is a SEPARATE config field from 'waveformgeneration' above -- confirmed
# directly from mesoSPIM_Core.py: it picks self.laserenabler independently of
# self.waveformer. Setting 'waveformgeneration' to 'ASI_Tiger' WITHOUT also
# setting 'laser' to 'ASI_Tiger' leaves self.laserenabler unset entirely (neither
# the 'NI'/'cDAQ' nor the 'demo' branch matches) -- a guaranteed AttributeError
# the first time Core.py calls self.laserenabler.enable(), which happens on
# every single snap()/live()/acquisition call. Requires the SAME mesoSPIM_Core.py
# patch (it adds this branch alongside the waveformgeneration one) and the new
# devices/lasers/ASITiger_LaserEnabler.py file -- see PATCHNOTES_ASI_TIGER.md.
laser = 'ASI_Tiger'

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
    # Camera Expose-Out mode (camera_parameters['exp_out_mode']) decides what this backend sees:
    #   0 First Row  : high ~= exposure, from the first row's start
    #   1 All Rows   : high only while EVERY row exposes (~ exposure - sweep). With a long scan_line_delay (ASLM)
    #                  that is a short pulse near the END of the exposure -> ETL starts late, laser blanking ends early
    #   2 Any Row    : high from first row start to last row end (~ exposure + sweep); bench-confirmed on the
    #                  ASLM rig: rises ~8 ms after the trigger, ~390 ms wide for a 200 ms exposure. Recommended for ASLM.
    #   4 Line Output: short pulses per line -- not usable here
    'camera_expose_bnc': 3,     # PLC BNC wired FROM the camera's Expose-Out
    'camera_trigger_bnc': 4,    # PLC BNC wired TO the camera's trigger input
    'camera_trigger_cell': 10,  # optional -- PLC cell used as the manual trigger D-flop (default 10)
    'camera_trigger_pulse_ms': 10.0,             # optional, default 10.0
    'camera_expose_start_timeout_s': 2.0,        # optional, default 2.0
    # --- ETL amplitude / limits ---
    # Amplitudes are passed RAW from mesoSPIM's state (tune per system). Note the Tiger's SAA is TOTAL
    # peak-to-peak, whereas NI-era mesoSPIM swung offset +/- amplitude -- the same number is a half-size sweep here.
    'etl_follow_ramp_direction': False,          # optional, default False: True = a side whose ramp_falling_% > ramp_rising_%
                                                 # (normally Right) gets a NEGATIVE SAA (downward ramp). Bench-check first
    # ETL guard (opt-in; a swing outside these is refused, ETL left stopped and zeroed). 0-4.096 V is this rack's
    # DAC range for the ETL axes (user-confirmed). The real ceiling is the Optotune lens driver: its analog input is
    # specified 0-5 V, 10-bit, mapped LINEARLY onto the current range set by the driver's lower/upper software limits
    # (Lens Driver 4i manual) -- so the voltage window is effectively fixed and the focal sweep per volt is set by
    # those software limits. Keep the guard at/below the driver's 5 V.
    'etl_min_volts': 0.0, 'etl_max_volts': 4.096,
    'stage_poll_interval_ms_during_run': 1000,   # optional -- needs the updated mesoSPIM_Core.py patch. While live/snap/
                                                 # acquisition runs, keep polling stage positions every this many ms
                                                 # (one cheap 'W <axes>' query each) so the GUI shows correct positions
                                                 # after you move a stage. Omit / None / 0 = pause polling entirely
                                                 # (the earlier confirmed behaviour). Normal 100 ms rate returns when idle.
    'live_hold_laser_dac': True,                 # optional, default True -- LIVE ONLY: keep the laser DAC at its
                                                 # level between frames (PLC enable line does the blanking) instead
                                                 # of zeroing/re-setting it every frame; zeroed when live ends
    'live_track_plc_pointer': True,              # optional, default True -- LIVE ONLY: skip PLC `M E=` pointer moves
                                                 # that repeat the current position (~103 ms each on this rack)
    'acq_track_plc_pointer': True,               # optional, default True -- the same skip outside live: acquisition
                                                 # rows and single snaps. Hardware-confirmed: ~0.78 -> ~0.67 s per
                                                 # plane at 200 ms exposure, laser blanking intact
    'expose_width_warn_fraction': 0.5,           # optional, default 0.5: warn once per live session if Expose-Out is high for
                                                 # less than this fraction of the exposure (Any Row should be ~ exposure + sweep).
                                                 # 0 silences it. Only checked for exposures >= expose_width_check_min_exposure_s (0.1)
    'frame_timing_log': True,                    # optional, default True -- one timing line per frame (debugging)
    'camera_expose_poll_interval_s': 0.005,      # optional, default 0.005 (5 ms) -- delay between
                                                 # Expose-Out polls in run_tasks(); each poll is a
                                                 # full serial round-trip on the shared port, so
                                                 # don't go much below ~5 ms (1 ms stalled the port)
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
    # (120.0ms), so the ETL sweep tracks the exposure setting, not a
    # constant. Requires exp_out_mode=2 (Any Row): Expose-Out rises at the
    # start of the rolling-shutter sweep, which is when SAM=2 starts the
    # ramp. Whether exposure-length matches the real sweep in the images
    # is not yet bench-confirmed. See
    # mesoSPIM_ASITigerWaveFormGenerator._etl_period_ms() and
    # PATCHNOTES_ASI_TIGER.md for the full history.
    #
    # 'etl_period_ms': 120.0,     # uncomment to force a fixed period instead (bypasses auto-derivation)
    #
    # etl_period_margin_ms defaults to 0.0 (derived period = the real
    # exposure time, exactly). With per-frame triggering the trigger
    # interval (~0.66 s at 200 ms exposure) is far longer than the
    # period, so no margin is needed. Left
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
    # Card 5 (P/Q/R/S), 400Hz LPF (adjustable via BACKLASH if you need a
    # different cutoff), intended for analog laser intensity control.
    # Only 'name' (matching a DacChannel's own name, used by
    # write_waveforms_to_tasks() to look up which channel to drive for
    # the active laser) is required here; the actual channel is added
    # via ASITigerDAC.add_channel(**ch) in create_tasks(), so each
    # dict's other keys (card_addr, axis, range_code, ...) must match
    # DacChannel's constructor.
    #
    # range_code=2 (0-10.24V) -- REAL-HARDWARE FINDING with real Oxxius
    # L4Cc lasers (638/561/488/405): commanding up to ~4V (near
    # range_code=1's 4.096V ceiling) only reached ~70mW of the module's
    # rated 100mW. CONFIRMED directly from Oxxius's own L4Cc/L6Cc user
    # manual (not assumed): the analog modulation input is LINEAR 0V =
    # 0% power to 5V = 100% power -- so range_code=1 physically cannot
    # reach 100%, regardless of what 'intensity'/max_laser_voltage ask
    # for. range_code=2 gives headroom above the needed 5V.
    #
    # IMPORTANT: 'range_code' here is a CLIENT-SIDE label only -- it
    # does NOT itself change the card's real electrical output range.
    # The real range is a hardware setting (the 'PR' command) that
    # needs to be set ONCE and the controller power-cycled/reset
    # afterward before it's genuinely active (confirmed directly from
    # ASI's own command:pr docs -- see asi_tiger/dac.py's
    # ASITigerDAC.set_range() docstring). Run
    # tools/asi_tiger_laser_dac_range_setup.py ONCE against your real
    # rack (--set-range, power-cycle the Tiger controller, then
    # --verify) BEFORE trusting that this config's range_code=2 here
    # actually matches the hardware -- this project had never actually
    # sent that PR command before this finding, so don't assume any
    # rack already has it.
    'laser_dac_channels': [
        {'name': 'laser_405', 'card_addr': 35, 'axis': 'P', 'range_code': 2},
        {'name': 'laser_488', 'card_addr': 35, 'axis': 'Q', 'range_code': 2},
        {'name': 'laser_561', 'card_addr': 35, 'axis': 'R', 'range_code': 2},
        {'name': 'laser_638', 'card_addr': 35, 'axis': 'S', 'range_code': 2},
    ],

    # ------------------------------------------------------------------
    # Optional: which PLC BNCs / toggle cells drive the digital laser
    # ENABLE lines (separate from laser_dac_channels above, which is the
    # ANALOG intensity line) -- used by ASITiger_LaserEnabler (set
    # `laser = 'ASI_Tiger'` near the top of this file to enable it; see
    # PATCHNOTES_ASI_TIGER.md). Both default to (5,6,7,8)/(12,13,14,15)
    # if omitted -- this rack's convention, since BNCs 1-4 and cells 1-7
    # are already used by the Z/camera trigger chain (zstack_chain.py)
    # -- only override if that collides with something else on your PLC
    # card. Order must match laserdict's SORTED key order (same
    # convention as laser_dac_channels and mesoSPIM's own laserdict
    # handling elsewhere).
    'plc_laser_bncs': (5, 6, 7, 8),
    'plc_laser_toggle_cells': (12, 13, 14, 15),
    #
    # SEPARATELY, in your real mesoSPIM config file's `startup` dict
    # (NOT this asi_dac_parameters block -- 'max_laser_voltage' lives in
    # `cfg.startup`, read by the base mesoSPIM_WaveFormGenerator class
    # before this backend's own config_check() even runs -- confirmed
    # directly from the real mesoSPIM_Core.py/mesoSPIM_WaveFormGenerator.py):
    #   startup = {
    #       ...
    #       'max_laser_voltage': 5,   # Oxxius L4Cc/L6Cc: 5V = 100%, confirmed from its manual
    #       ...
    #   }
    # 100% intensity sends max_laser_voltage volts -- setting this to 5
    # (not the 4.096 the old range_code=1 silently implied) is what
    # actually lets 'intensity': 100 reach the lasers' true 100mW rating,
    # PROVIDED the DAC range has genuinely been raised per the note above
    # -- if it hasn't, set_voltage() will raise a clear ValueError at 5V
    # under the OLD unconfirmed range rather than silently underpowering,
    # which is a safer failure than what asi_tiger_laser_intensity_test.py's
    # bare voltage sweep could show you.

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
# == 'TigerASI'), point it at the same COM port so the connection is shared
# automatically -- CONFIRMED against the real mesoSPIM-control source
# (mesoSPIM_Stages.py's mesoSPIM_ASI_Stages + devices/stages/asi/asicontrol.py's
# StageControlASI, fetched directly, not assumed): StageControlASI opens its
# own raw serial.Serial and stores it as self.asi_stages.asi_connection, with
# NO built-in sharing mechanism of its own -- mesoSPIM_ASITigerWaveFormGenerator
# .create_tasks() is what does the actual sharing, by reaching
# self.parent.serial_worker.stage.asi_stages.asi_connection and wrapping that
# SAME already-open serial.Serial in a TigerController via
# TigerController.from_open_serial() (see asi_tiger/controller.py) instead of
# opening a second handle on the same COM port (which would fail or corrupt
# traffic -- only one process/handle can hold a COM port open on Windows).
# CONFIRMED SAFE from a threading standpoint too: mesoSPIM_Core.py keeps
# serial_worker on the Core thread (never moved to a QThread) and explicitly
# emits sig_polling_stage_position_stop before prepare_acquisition() (resumed
# only after the whole acquisition list finishes) -- so nothing else sends
# stage-position-poll serial traffic on this shared connection during an
# acquisition; the only other traffic on it (Z/F's per-frame move_relative()
# serial commands) is issued by the SAME thread, sequentially, interleaved
# with this backend's own per-frame commands by the run_acquisition() loop
# itself -- never concurrently.
#
# ALSO REQUIRED for this design (see mesoSPIM_ASITigerWaveFormGenerator's
# ARCHITECTURE NOTE): 'ttl_motion_enabled' must be False, since this backend
# relies entirely on mesoSPIM's own existing per-frame host-stepping loop
# for Z/F -- it does not touch Z/F itself at all.
#
# stage_parameters = {
#     'stage_type': 'TigerASI',   # NOT 'ASI' -- confirmed exact string from demo_config.py
#     'y_load_position': -6000,
#     'y_unload_position': 6000,
#     'x_center_position': 0,
#     'z_center_position': 0,
#     'x_max': 25000, 'x_min': -25000,
#     'y_max': 50000, 'y_min': -50000,
#     'z_max': 25000, 'z_min': -25000,
#     'f_max': 98000, 'f_min': 0,
#     'theta_max': 999, 'theta_min': -999,
#     # ^ soft-limit placeholders from demo_config.py -- replace with your
#     # rack's real travel ranges before trusting this for real moves.
# }
# asi_parameters = {
#     'COMport': 'COM5',            # <-- same port as asi_dac_parameters['port'] above, for connection sharing
#     'baudrate': 115200,
#     # stage_assignment: maps mesoSPIM's axis names to this rack's ASI stage
#     # letters -- REQUIRED by StageControlASI.__init__ (mesoSPIM2ASIdict),
#     # not optional. Replace values with your rack's real axis letters (from
#     # the 'N' command -- see tools/asi_tiger_hardware_test.py). Use None for
#     # an axis your rack doesn't have.
#     'stage_assignment': {'x': 'X', 'y': 'Y', 'z': 'Z', 'theta': 'T', 'f': 'F'},
#     # encoder_conversion: counts per micron (or degree, for theta) per axis
#     # letter used above -- REQUIRED by StageControlASI.__init__. Confirm
#     # your rack's real values (typically stage-model-dependent) before
#     # trusting reported/commanded positions.
#     'encoder_conversion': {'X': 10., 'Y': 10., 'Z': 10., 'T': 100., 'F': 10.},
#     'ttl_motion_enabled': False,  # REQUIRED -- see note above (opposite of vanilla ASI-stage configs)
#     'ttl_cards': None,            # None is correct for MS2000ASI; a tuple of card numbers for TigerASI
#                                    # is only meaningful when ttl_motion_enabled=True, which this design
#                                    # never sets -- left None here since it won't be used, but
#                                    # mesoSPIM_ASI_Stages.__init__ still reads this key unconditionally.
# }
