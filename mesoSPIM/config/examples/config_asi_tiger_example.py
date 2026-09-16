"""
config_asi_tiger_example.py
=============================
Example config additions for the ASI Tiger DAC/PLC waveform generation
backend (mesoSPIM_ASITigerWaveFormGenerator). This is NOT a complete
mesoSPIM config file -- copy the pieces below into your existing config
(alongside your laserdict, stage_parameters, camera_parameters, etc.),
which is exactly how demo_config.py and the other example configs in
this folder are meant to be used.

Card specs, ranges, and safety limits below are taken directly from
ASI's own confirmation email about this specific rack's build (firmware
v3.60 SIGNAL_DAC_4CH), not guessed -- see PATCHNOTES_ASI_TIGER.md,
"ASI-confirmed hardware facts" for the full email content. Re-verify
against your own rack's actual card addresses with the 'N' command
before trusting these numbers (see tools/asi_tiger_hardware_test.py).

See tools/asi_tiger_hardware_test.py to verify your DAC channels and
PLC wiring against this same channel layout BEFORE using it here.
"""

# Select the ASI Tiger backend instead of 'NI', 'cDAQ', or 'DemoWaveFormGeneration'.
# Requires the mesoSPIM_Core.py edit described in PATCHNOTES_ASI_TIGER.md.
waveformgeneration = 'ASI_Tiger'

# Read by mesoSPIM_ASITigerWaveFormGenerator.config_check() / create_tasks().
# Mirrors the shape of 'acquisition_hardware' but for ASI Tiger cards
# instead of NI channel strings.
asi_dac_parameters = {
    # Serial connection to the Tiger rack. If this matches the ASI stage's
    # COMport in asi_parameters below, the DAC/PLC backend automatically
    # reuses that connection instead of opening a 2nd handle -- REQUIRED
    # if the stage, DAC, and PLC cards share one physical rack/COM port.
    'port': 'COM5',
    'baudrate': 115200,

    # ------------------------------------------------------------------
    # Galvo + ETL channels, in the order [galvo_l, galvo_r, etl_l, etl_r]
    # -- must match bundle_galvo_and_etl_waveforms() in
    # mesoSPIM_WaveFormGenerator.py.
    #
    # Card 7 (A/B/C/D): -10.24 to +10.24V, intended for galvo control.
    #   UPDATED: ASI provided new firmware for this card (bench-confirmed
    #   working -- 100Hz triangle waveforms on A/C are visibly smooth,
    #   vs. audible/visible stepping at 50Hz on the original firmware's
    #   B/D). This new firmware REVERSES which axes are fast: A and C
    #   now run at 40kHz, B/D at the normal ~1kHz -- the opposite of the
    #   original SIGNAL_DAC_4CH firmware, where B/D were fastest. Hence
    #   galvo_l/galvo_r map to A/C below, not B/D.
    #
    #   CRITICAL UNITS CHANGE (confirmed by ASI directly): this new
    #   firmware's control range is -4000..4000 for the SAME +/-10.24V
    #   the original firmware represents as -10240..10240 (mV). That's
    #   units_per_volt = 4000/10.24 = ~390.625, NOT 1000 -- using the
    #   default here would command ~2.56x the intended voltage. Both
    #   'units_per_volt' below (for plain M/W via ASITigerDAC) AND the
    #   equivalent constructor arg on SingleAxisWaveform (for SAA/SAO)
    #   need this value for any axis on this firmware. See
    #   asi_tiger.dac.UNITS_PER_VOLT_TGGALVO for the exact constant.
    #
    #   SAFETY (ASI, verbatim, still applies regardless of firmware/units):
    #   "Limit command voltage to + and - 10000 (+-10v) to guarantee
    #   galvo amplifier safety." safety_limit_mv is expressed in REAL
    #   VOLTAGE (mV) terms and stays correct regardless of
    #   units_per_volt -- do not remove it.
    #
    # Card 4 (H/I/J/K): 0 to 4.096V, 400Hz LPF, intended for Tunable Lens
    #   control (into an EL-E-4 driver). Still running the ORIGINAL
    #   SIGNAL_DAC_4CH firmware (units_per_volt defaults to 1000, the
    #   standard millivolt convention -- no change needed here).
    #
    #   ASI's docs say I and K are the guaranteed-fastest axes on this
    #   card. CONFIRMED ON REAL HARDWARE, K specifically has a trigger
    #   fault on this card: with SAM=2 triggered externally, K only
    #   responds to every OTHER pulse (1st/3rd fire, 2nd/4th don't) --
    #   tested from a fresh controller reset with several seconds
    #   between manually-sent pulses (ruling out re-arm timing), correct
    #   jumper installed and verified. H, I, and J were all confirmed
    #   working correctly under the identical protocol -- this looks
    #   isolated to K's own trigger circuit on this specific card, not
    #   the general architecture. Confirmed working axes for this card
    #   are H, I, and J -- the actual production pair in use is H and J
    #   (K's own confirmed replacement is J; H is the other channel,
    #   not I -- corrected after initially defaulting to I in some
    #   scripts/config here, since I was also confirmed working and
    #   easy to mix up with H as "the other good axis").
    #
    # max_step_v on all four: ASI's own warning -- "Sudden jumps in
    # command voltage that are faster then the inertial moment of the
    # device can cause damage" to the galvo/lens. The analog LPFs smooth
    # normal waveform playback, but a single large jump (a GUI slider
    # dragged quickly, a bad script) can still exceed what they can
    # absorb. 0.5V/0.2V are conservative STARTING defaults, not numbers
    # ASI verified for your specific galvo/lens -- tune down if you have
    # datasheet numbers for actual safe slew rates, and test cautiously.
    #
    'galvo_etl_channels': [
        {'name': 'galvo_l', 'card_addr': 37, 'axis': 'A', 'range_code': 6,
         'safety_limit_mv': 10000, 'max_step_v': 0.5,
         'units_per_volt': 4000 / 10.24},  # new TGGALVO firmware -- see note above
        {'name': 'galvo_r', 'card_addr': 37, 'axis': 'C', 'range_code': 6,
         'safety_limit_mv': 10000, 'max_step_v': 0.5,
         'units_per_volt': 4000 / 10.24},
        {'name': 'etl_l',   'card_addr': 34, 'axis': 'H', 'range_code': 1,
         'max_step_v': 0.2},  # original firmware -- default units_per_volt (1000) is correct
        {'name': 'etl_r',   'card_addr': 34, 'axis': 'J', 'range_code': 1,
         'max_step_v': 0.2},  # H and J are the confirmed-good production pair -- NOT K (real hardware fault)
    ],

    # ------------------------------------------------------------------
    # Laser modulation channels, in the SAME increasing-wavelength order
    # as laserdict (same requirement the NI 'laser_task_line' has).
    #
    # Card 5 (P/Q/R/S): 0 to 4.096V, 400Hz LPF (adjustable via the
    #   BACKLASH command if you need a different cutoff), intended for
    #   analog laser intensity control. By the same "2nd/4th channel is
    #   fastest" pattern ASI described for cards 4 and 7, Q and S are
    #   likely the fastest pair here too (not explicitly confirmed by
    #   ASI for this card -- ask if sub-channel timing matters for your
    #   blanking scheme). All four are listed since most setups need up
    #   to 4 laser lines; reorder/swap to match your actual laserdict.
    'laser_channels': [
        {'name': 'laser_405', 'card_addr': 35, 'axis': 'P', 'range_code': 1},
        {'name': 'laser_488', 'card_addr': 35, 'axis': 'Q', 'range_code': 1},
        {'name': 'laser_561', 'card_addr': 35, 'axis': 'R', 'range_code': 1},
        {'name': 'laser_638', 'card_addr': 35, 'axis': 'S', 'range_code': 1},
    ],

    # PLC (Programmable Logic Card) address and axis letter.
    'plc_card_addr': 36,
    'plc_axis': 'E',

    # Optional: PLC-based 2-laser toggle wiring (see PLCCard.configure_two_laser_toggle
    # in mesoSPIM/src/devices/asi_tiger/plc.py). Leave as None to skip.
    'plc_camera_trigger_bnc': 1,      # camera TTL wired into PLC BNC1
    'plc_laser_bncs': (5, 6),         # PLC BNC5/6 -> laser0/laser1 TTL modulation in

    # Optional: which PLC cell to pulse from run_tasks() to fire the
    # camera trigger. Leave as None if the camera is triggered some other way.
    'plc_camera_trigger_cell': None,

    # Effective software-timed streaming rate for the DAC (Hz), used by
    # WaveformStreamer for anything NOT running through the (not yet
    # confirmed) on-card single-axis generator. NOT the NI-equivalent
    # samplerate -- ASI confirmed the digital update ceiling is 4kHz
    # total across a card's 4 channels (so up to ~1kHz/channel, with the
    # analog Bessel filters smoothing the resulting steps in hardware),
    # but the SOFTWARE-timed serial loop here is far below even that --
    # keep well under ~100 Hz. See PATCHNOTES_ASI_TIGER.md.
    'software_stream_rate_hz': 50,
}

# If your XYZ stage is ALSO an ASI Tiger stage (stage_parameters['stage_type']
# containing 'asi'), point it at the same COM port so the connection is
# shared automatically -- see PATCHNOTES_ASI_TIGER.md, "Connection sharing".
#
# stage_parameters = {
#     'stage_type': 'TigerASI',
#     ...
# }
# asi_parameters = {
#     'COMport': 'COM5',   # <-- same port as asi_dac_parameters['port'] above
#     'baudrate': 115200,
#     ...
# }
