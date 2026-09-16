"""
asi_tiger.waveform
===================
Waveform generation + software-timed streaming for ASI Tiger DAC
channels.

IMPORTANT CAVEAT vs. the NI-DAQ waveform path in ao_controller.py:
NI-DAQ streams a waveform with a hardware sample clock, so it can hit
kHz-range rates with rock-solid timing. The ASI Tiger DAC has no
equivalent from the host side over RS-232/USB-serial -- every sample
here means one command + one reply round trip (roughly single-digit
milliseconds on Tiger). This module is a software-timed loop: fine for
slow ramps, sweeps, and low-frequency (<= tens of Hz) waveforms, but it
will silently fall behind its target sample_rate for anything fast,
and it is NOT suitable for audio-rate or other high-fidelity signals.
Use `WaveformStreamer.achieved_rate_hz` to check what you actually got.

This module has no Qt dependency by design, so it can be used headless
or wrapped by any GUI/thread model.
"""

import threading
import time
from typing import Callable, Dict, Optional

import numpy as np

from .dac import ASITigerDAC


def generate_waveform(
    waveform_type: str,
    amplitude: float,
    offset: float,
    frequency: float,
    duty_cycle: float,
    duration: float,
    sample_rate: int,
    phase_deg: float = 0.0,
    v_min: float = -10.0,
    v_max: float = 10.0,
) -> np.ndarray:
    """
    Return a 1-D numpy array (volts) for the requested waveform.
    Same algorithm/parameter set as the NI-DAQ version in
    ao_controller.py, so behavior matches across both hardware paths.
    """
    n_samples = max(2, int(duration * sample_rate))
    t = np.linspace(0, duration, n_samples, endpoint=False)
    phase_rad = np.deg2rad(phase_deg)
    omega = 2 * np.pi * frequency
    wt = omega * t + phase_rad

    if waveform_type == "Sine":
        wave = amplitude * np.sin(wt)
    elif waveform_type == "Square":
        from scipy.signal import square as _square
        wave = amplitude * _square(wt, duty=duty_cycle / 100.0)
    elif waveform_type == "Triangle":
        from scipy.signal import sawtooth as _saw
        wave = amplitude * _saw(wt, width=0.5)
    elif waveform_type == "Sawtooth":
        from scipy.signal import sawtooth as _saw
        wave = amplitude * _saw(wt, width=1.0)
    elif waveform_type == "Pulse":
        from scipy.signal import square as _square
        wave = amplitude * np.clip(_square(wt, duty=duty_cycle / 100.0), 0, 1)
    elif waveform_type == "Noise (White)":
        rng = np.random.default_rng()
        wave = amplitude * (rng.random(n_samples) * 2 - 1)
    else:
        wave = np.zeros(n_samples)

    wave = wave + offset
    return np.clip(wave, v_min, v_max)


class WaveformStreamer:
    """
    Streams precomputed per-channel waveform arrays to ASI DAC channels
    using a background thread and best-effort software timing.

    Usage:
        streamer = WaveformStreamer(
            dac, waveforms={"scanner_x": wave_array}, sample_rate=50,
            loop=False,
        )
        streamer.on_progress = lambda pct: print(pct)
        streamer.on_error = lambda msg: print("error:", msg)
        streamer.start()
        ...
        streamer.stop()
    """

    def __init__(
        self,
        dac: ASITigerDAC,
        waveforms: Dict[str, np.ndarray],
        sample_rate: float,
        loop: bool = False,
    ):
        if not waveforms:
            raise ValueError("waveforms must contain at least one channel array")
        lengths = {len(arr) for arr in waveforms.values()}
        if len(lengths) != 1:
            raise ValueError("All channel waveform arrays must be the same length")

        self._dac = dac
        self._waveforms = waveforms
        self._n_samples = lengths.pop()
        self._sample_rate = sample_rate
        self._loop = loop

        self._thread: Optional[threading.Thread] = None
        self._abort_evt = threading.Event()

        self.achieved_rate_hz: float = 0.0
        self.on_progress: Optional[Callable[[int], None]] = None  # 0-100
        self.on_error: Optional[Callable[[str], None]] = None
        self.on_finished: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------------
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._abort_evt.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._abort_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    def _run(self):
        period = 1.0 / self._sample_rate
        channel_names = list(self._waveforms.keys())

        try:
            first_loop = True
            while first_loop or self._loop:
                first_loop = False
                loop_start = time.perf_counter()

                for i in range(self._n_samples):
                    if self._abort_evt.is_set():
                        return

                    t0 = time.perf_counter()
                    for name in channel_names:
                        self._dac.set_voltage(name, float(self._waveforms[name][i]))

                    if self.on_progress is not None:
                        self.on_progress(int(100 * (i + 1) / self._n_samples))

                    elapsed = time.perf_counter() - t0
                    sleep_for = period - elapsed
                    if sleep_for > 0:
                        time.sleep(sleep_for)

                actual_duration = time.perf_counter() - loop_start
                if actual_duration > 0:
                    self.achieved_rate_hz = self._n_samples / actual_duration

        except Exception as exc:  # surface hardware/serial errors to the caller
            if self.on_error is not None:
                self.on_error(str(exc))
            return
        finally:
            if self.on_finished is not None:
                self.on_finished()
