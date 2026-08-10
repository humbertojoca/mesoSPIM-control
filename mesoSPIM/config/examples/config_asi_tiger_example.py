"""
config_asi_tiger_example.py
=============================
Example config additions for the ASI Tiger DAC/PLC waveform generation
backend (mesoSPIM_ASITigerWaveFormGenerator). This is NOT a complete
mesoSPIM config file -- copy the pieces below into your existing config
(alongside your laserdict, stage_parameters, camera_parameters, etc.),
which is exactly how demo_config.py and the other example configs in
this folder are meant to be used.

See tools/asi_tiger_hardware_test.py to verify your DAC channels and
PLC wiring against this same channel layout BEFORE using it here.
"""

# Select the ASI Tiger backend instead of 'NI', 'cDAQ', or 'DemoWaveFormGeneration'.
# Requires the mesoSPIM_Core.py edit described in PATCHNOTES_ASI_TIGER.md.
waveformgeneration = 'ASI_Tiger'

# Read by mesoSPIM_ASITigerWaveFormGenerator.config_check() / create_tasks().
# Mirrors the shape of 'acquisition_hardware' but for ASI Tiger cards
# instead of NI channel strings. Card addresses/axes below match the
# example 7-card rack this was developed against -- adjust to yours
# (confirm with the Tiger 'N' command, see tools/asi_tiger_hardware_test.py).
asi_dac_parameters = {
    # Serial connection to the Tiger rack. If this matches the ASI stage's
    # COMport in asi_parameters below, the DAC/PLC backend automatically
    # reuses that connection instead of opening a 2nd handle -- REQUIRED
    # if the stage, DAC, and PLC cards share one physical rack/COM port.
    'port': 'COM5',
    'baudrate': 115200,

    # Galvo + ETL channels, in the order [galvo_l, galvo_r, etl_l, etl_r]
    # -- must match bundle_galvo_and_etl_waveforms() in
    # mesoSPIM_WaveFormGenerator.py.
    #
    # BANDWIDTH CAVEAT (see PATCHNOTES_ASI_TIGER.md): these are the
    # channels most affected by ASI's lower update rate vs. NI. Validate
    # scan quality on your actual optics before relying on this for
    # production acquisitions.
    'galvo_etl_channels': [
        {'name': 'galvo_l', 'card_addr': 37, 'axis': 'A', 'range_code': 4},  # +/-2.048V
        {'name': 'galvo_r', 'card_addr': 37, 'axis': 'B', 'range_code': 4},
        {'name': 'etl_l',   'card_addr': 37, 'axis': 'C', 'range_code': 4},
        {'name': 'etl_r',   'card_addr': 37, 'axis': 'D', 'range_code': 4},
    ],

    # Laser modulation channels, in the SAME increasing-wavelength order
    # as laserdict (same requirement the NI 'laser_task_line' has).
    'laser_channels': [
        {'name': 'laser_405', 'card_addr': 34, 'axis': 'H', 'range_code': 6},
        {'name': 'laser_488', 'card_addr': 34, 'axis': 'I', 'range_code': 6},
        {'name': 'laser_561', 'card_addr': 34, 'axis': 'J', 'range_code': 6},
        {'name': 'laser_638', 'card_addr': 34, 'axis': 'K', 'range_code': 6},
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

    # Effective software-timed streaming rate for the DAC (Hz). This is
    # NOT the NI-equivalent samplerate -- it's a much lower, best-effort
    # rate for the serial-command loop. Keep well under ~100 Hz. See
    # PATCHNOTES_ASI_TIGER.md for the full bandwidth comparison.
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
