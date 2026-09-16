"""
ASI DAC Controller + Waveform Creator
=======================================
A PyQt5 application for controlling ASI Tiger SIGNAL_DAC_4CH analog
outputs over serial:
  - Connection bar: pick a COM port, connect/disconnect (with the
    mandatory zero-all-on-connect safety step)
  - Section 1: Per-channel slider DC manual control
  - Section 2: Waveform Creator -- design, preview, and stream
    waveforms (software-timed; see asi_tiger.waveform docstring for
    the timing caveat vs. hardware-clocked NI-DAQ output)

Uses PyQt5 to match mesoSPIM-control's own dependencies
(requirements-conda-mamba.txt pins PyQt5==5.15.11 -- there's no PySide6
in that environment).

Loads DAC channels from a JSON file matching the shape of
mesoSPIM/config/examples/asi_dac_channels_example.json (top-level
"asi_dac.channels" or a bare "channels" list) -- same format read_ by
tools/asi_tiger_hardware_test.py, so both tools share one config file.
Pass --config to point at a different file; defaults to that example
file's path relative to this script.

Standalone: no mesoSPIM/nidaqmx dependency, only asi_tiger + PyQt5 +
numpy (already in mesoSPIM's requirements).
"""

import argparse
import json
import sys
import os
from pathlib import Path

os.environ["QT_LOGGING_RULES"] = "qt.core.qobject.connect=false"


from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QSlider, QPushButton, QDoubleSpinBox, QSpinBox,
    QGroupBox, QStatusBar, QComboBox, QMessageBox, QLineEdit,
    QGridLayout, QScrollArea,
)
from PyQt5.QtCore import Qt, pyqtSignal as Signal, QObject, QThread, pyqtSlot as Slot

# This script lives in tools/, the library lives in mesoSPIM/src/devices/asi_tiger/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "mesoSPIM" / "src" / "devices"))

from asi_tiger import ASITigerDAC, TigerError
from asi_tiger.waveform import generate_waveform, WaveformStreamer

_DEFAULT_CONFIG = _REPO_ROOT / "mesoSPIM" / "config" / "examples" / "asi_dac_channels_example.json"

_arg_parser = argparse.ArgumentParser(add_help=False)
_arg_parser.add_argument("--port", default=None, help="Override the serial port from --config")
_arg_parser.add_argument("--baudrate", type=int, default=None)
_arg_parser.add_argument("--config", default=str(_DEFAULT_CONFIG),
                          help="JSON file with DAC channel definitions")
_cli_args, _qt_args = _arg_parser.parse_known_args()
sys.argv = [sys.argv[0]] + _qt_args  # leave the rest for QApplication


def _load_asi_dac_config(path: str) -> dict:
    """Returns {'port': ..., 'baudrate': ..., 'channels': [...]}."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"WARNING: could not load {path!r} ({exc}). Starting with no channels -- "
              f"pass --config or add channels manually.")
        return {"port": "COM5", "baudrate": 115200, "channels": []}

    section = data["asi_dac"] if "asi_dac" in data else data
    return {
        "port": section.get("port", "COM5"),
        "baudrate": section.get("baudrate", 115200),
        "channels": section.get("channels", []),
    }


_ASI_CONFIG = _load_asi_dac_config(_cli_args.config)
ASI_PORT = _cli_args.port or _ASI_CONFIG["port"]
ASI_BAUD = _cli_args.baudrate or _ASI_CONFIG["baudrate"]
ASI_CHANNELS = _ASI_CONFIG["channels"]

# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────
SLIDER_RESOLUTION = 1000
DEFAULT_SAMPLE_RATE = 20        # Hz -- realistic for a software-timed serial loop
DEFAULT_DURATION = 2.0          # seconds
MAX_RECOMMENDED_RATE = 50       # Hz -- above this, warn the user


def slider_to_voltage(val: int, v_min: float, v_max: float) -> float:
    return v_min + (val / SLIDER_RESOLUTION) * (v_max - v_min)


def voltage_to_slider(v: float, v_min: float, v_max: float) -> int:
    if v_max == v_min:
        return 0
    return int((v - v_min) / (v_max - v_min) * SLIDER_RESOLUTION)


# ──────────────────────────────────────────────────────────────────────
# Per-channel slider widget
# ──────────────────────────────────────────────────────────────────────
class DacSliderWidget(QWidget):
    voltage_changed = Signal(str, float)  # channel_name, volts

    def __init__(self, channel_name: str, v_min: float = -10.0, v_max: float = 10.0, parent=None):
        super().__init__(parent)
        self.channel_name = channel_name
        self.v_min, self.v_max = v_min, v_max

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{channel_name}</b>"))

        self.spin = QDoubleSpinBox()
        self.spin.setRange(v_min, v_max)
        self.spin.setDecimals(3)
        self.spin.setSingleStep(0.01)
        self.spin.valueChanged.connect(self._on_spin_changed)
        lay.addWidget(self.spin)

        self.slider = QSlider(Qt.Vertical)
        self.slider.setRange(0, SLIDER_RESOLUTION)
        self.slider.setValue(voltage_to_slider(0.0, v_min, v_max))
        self.slider.valueChanged.connect(self._on_slider_changed)
        lay.addWidget(self.slider, alignment=Qt.AlignHCenter)

        self._updating = False

    def _on_slider_changed(self, val: int):
        if self._updating:
            return
        v = slider_to_voltage(val, self.v_min, self.v_max)
        self._updating = True
        self.spin.setValue(v)
        self._updating = False
        self.voltage_changed.emit(self.channel_name, v)

    def _on_spin_changed(self, v: float):
        if self._updating:
            return
        self._updating = True
        self.slider.setValue(voltage_to_slider(v, self.v_min, self.v_max))
        self._updating = False
        self.voltage_changed.emit(self.channel_name, v)

    def set_voltage(self, v: float, silent: bool = False):
        self._updating = True
        self.spin.setValue(v)
        self.slider.setValue(voltage_to_slider(v, self.v_min, self.v_max))
        self._updating = False
        if not silent:
            self.voltage_changed.emit(self.channel_name, v)

    def set_enabled(self, enabled: bool):
        self.spin.setEnabled(enabled)
        self.slider.setEnabled(enabled)


# ──────────────────────────────────────────────────────────────────────
# Waveform streaming worker (QThread wrapper around WaveformStreamer)
# ──────────────────────────────────────────────────────────────────────
class WaveformWorker(QObject):
    finished = Signal()
    error = Signal(str)
    progress = Signal(int)

    def __init__(self, dac: ASITigerDAC, waveforms: dict, sample_rate: float, loop: bool):
        super().__init__()
        self._streamer = WaveformStreamer(dac, waveforms, sample_rate, loop)
        self._streamer.on_progress = self.progress.emit
        self._streamer.on_error = self.error.emit
        self._streamer.on_finished = self.finished.emit

    @Slot()
    def run(self):
        self._streamer.start()
        # Block this worker thread until the streamer thread completes,
        # so the QThread hosting us has a well-defined lifetime.
        while self._streamer.is_running:
            QThread.msleep(50)

    def abort(self):
        self._streamer.stop()

    @property
    def achieved_rate_hz(self) -> float:
        return self._streamer.achieved_rate_hz


# ──────────────────────────────────────────────────────────────────────
# Main window
# ──────────────────────────────────────────────────────────────────────
class ASIDACControllerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ASI Tiger DAC Controller")
        self.resize(1000, 700)

        self.dac = ASITigerDAC(ASI_PORT, baudrate=ASI_BAUD)
        for ch in ASI_CHANNELS:
            self.dac.add_channel(
                name=ch["name"], card_addr=ch["card_addr"], axis=ch["axis"],
                range_code=ch.get("range_code", 6),
            )

        self.sliders: dict[str, DacSliderWidget] = {}
        self._wave_thread: QThread | None = None
        self._wave_worker: WaveformWorker | None = None

        self._build_ui()
        self._apply_style()

    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Disconnected")

        # ── Connection bar ──────────────────────────────────────────
        conn_group = QGroupBox("Connection")
        conn_lay = QHBoxLayout(conn_group)
        conn_lay.addWidget(QLabel("Port:"))
        self.port_edit = QLineEdit(ASI_PORT)
        conn_lay.addWidget(self.port_edit)
        conn_lay.addWidget(QLabel("Baud:"))
        self.baud_spin = QSpinBox()
        self.baud_spin.setRange(9600, 115200)
        self.baud_spin.setValue(ASI_BAUD)
        conn_lay.addWidget(self.baud_spin)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.setCheckable(True)
        self.btn_connect.clicked.connect(self._on_connect_clicked)
        conn_lay.addWidget(self.btn_connect)
        self.btn_zero = QPushButton("ZERO ALL OUTPUTS")
        self.btn_zero.setObjectName("btn_zero")
        self.btn_zero.clicked.connect(self._zero_all)
        self.btn_zero.setEnabled(False)
        conn_lay.addWidget(self.btn_zero)
        conn_lay.addStretch()
        root.addWidget(conn_group)

        # ── Sliders ──────────────────────────────────────────────────
        slider_group = QGroupBox("Manual DC Output Control")
        slider_scroll = QScrollArea()
        slider_scroll.setWidgetResizable(True)
        slider_inner = QWidget()
        sliders_row = QHBoxLayout(slider_inner)

        for name, ch in self.dac.channels.items():
            v_min, v_max = ch.limits_mv[0] / 1000.0, ch.limits_mv[1] / 1000.0
            sw = DacSliderWidget(name, v_min, v_max)
            sw.voltage_changed.connect(self._on_slider_changed)
            sw.set_enabled(False)
            sliders_row.addWidget(sw)
            self.sliders[name] = sw

        slider_scroll.setWidget(slider_inner)
        gl = QVBoxLayout(slider_group)
        gl.addWidget(slider_scroll)
        root.addWidget(slider_group, 2)

        # ── Waveform creator ────────────────────────────────────────
        wave_group = QGroupBox("Waveform Creator (software-timed -- see notes below)")
        wave_lay = QGridLayout(wave_group)

        wave_lay.addWidget(QLabel("Channel:"), 0, 0)
        self.wave_channel_combo = QComboBox()
        self.wave_channel_combo.addItems(list(self.dac.channels.keys()))
        wave_lay.addWidget(self.wave_channel_combo, 0, 1)

        wave_lay.addWidget(QLabel("Type:"), 0, 2)
        self.wave_type_combo = QComboBox()
        self.wave_type_combo.addItems(
            ["Sine", "Square", "Triangle", "Sawtooth", "Pulse", "Noise (White)"]
        )
        wave_lay.addWidget(self.wave_type_combo, 0, 3)

        wave_lay.addWidget(QLabel("Amplitude (V):"), 1, 0)
        self.amp_spin = QDoubleSpinBox()
        self.amp_spin.setRange(0, 10)
        self.amp_spin.setValue(1.0)
        wave_lay.addWidget(self.amp_spin, 1, 1)

        wave_lay.addWidget(QLabel("Offset (V):"), 1, 2)
        self.offset_spin = QDoubleSpinBox()
        self.offset_spin.setRange(-10, 10)
        self.offset_spin.setValue(0.0)
        wave_lay.addWidget(self.offset_spin, 1, 3)

        wave_lay.addWidget(QLabel("Frequency (Hz):"), 2, 0)
        self.freq_spin = QDoubleSpinBox()
        self.freq_spin.setRange(0.01, 20.0)
        self.freq_spin.setValue(1.0)
        wave_lay.addWidget(self.freq_spin, 2, 1)

        wave_lay.addWidget(QLabel("Duty cycle (%):"), 2, 2)
        self.duty_spin = QDoubleSpinBox()
        self.duty_spin.setRange(1, 99)
        self.duty_spin.setValue(50)
        wave_lay.addWidget(self.duty_spin, 2, 3)

        wave_lay.addWidget(QLabel("Duration (s):"), 3, 0)
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(0.1, 3600)
        self.duration_spin.setValue(DEFAULT_DURATION)
        wave_lay.addWidget(self.duration_spin, 3, 1)

        wave_lay.addWidget(QLabel("Sample rate (Hz):"), 3, 2)
        self.rate_spin = QSpinBox()
        self.rate_spin.setRange(1, 1000)
        self.rate_spin.setValue(DEFAULT_SAMPLE_RATE)
        wave_lay.addWidget(self.rate_spin, 3, 3)

        btn_row = QHBoxLayout()
        self.btn_once = QPushButton("Play Once")
        self.btn_once.setObjectName("btn_once")
        self.btn_once.clicked.connect(lambda: self._start_waveform(loop=False))
        self.btn_loop = QPushButton("Loop")
        self.btn_loop.setObjectName("btn_loop")
        self.btn_loop.clicked.connect(lambda: self._start_waveform(loop=True))
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setObjectName("btn_stop")
        self.btn_stop.clicked.connect(self._stop_waveform)
        self.btn_stop.setEnabled(False)
        btn_row.addWidget(self.btn_once)
        btn_row.addWidget(self.btn_loop)
        btn_row.addWidget(self.btn_stop)
        wave_lay.addLayout(btn_row, 4, 0, 1, 4)

        note = QLabel(
            "Note: each sample is one full serial round trip to the Tiger controller "
            f"(no hardware sample clock like NI-DAQ). Rates above ~{MAX_RECOMMENDED_RATE} Hz "
            "will likely fall behind -- check the status bar after a run for the achieved rate."
        )
        note.setWordWrap(True)
        wave_lay.addWidget(note, 5, 0, 1, 4)

        root.addWidget(wave_group, 1)

    def _apply_style(self):
        self.setStyleSheet(
            """
            QMainWindow { background-color: #1e1e2e; }
            QGroupBox {
                color: #cdd6f4; border: 1px solid #45475a; border-radius: 6px;
                margin-top: 10px; font-weight: bold; padding-top: 8px;
            }
            QLabel { color: #cdd6f4; }
            QPushButton {
                background-color: #313244; color: #cdd6f4; border: 1px solid #45475a;
                border-radius: 6px; padding: 6px 12px; font-weight: bold;
            }
            QPushButton:checked { background-color: #a6e3a1; color: #1e1e2e; }
            QPushButton#btn_zero { background-color: #f38ba8; color: #1e1e2e; }
            QPushButton#btn_stop:enabled { background-color: #f38ba8; color: #1e1e2e; }
            QPushButton#btn_once { background-color: #89dceb; color: #1e1e2e; }
            QPushButton#btn_loop { background-color: #fab387; color: #1e1e2e; }
            QDoubleSpinBox, QSpinBox, QComboBox, QLineEdit {
                background-color: #313244; color: #cdd6f4; border: 1px solid #45475a;
                border-radius: 4px; padding: 3px 6px;
            }
            QStatusBar { background-color: #181825; color: #a6adc8; }
            """
        )

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------
    def _on_connect_clicked(self, checked: bool):
        if checked:
            self.dac.tiger.port = self.port_edit.text().strip()
            self.dac.tiger.baudrate = self.baud_spin.value()
            try:
                self.dac.connect()
                self.dac.zero_all()  # mandatory: SIGNAL_DAC can surge to -10V on power-up
            except Exception as e:
                self.btn_connect.setChecked(False)
                QMessageBox.critical(self, "Connection failed", str(e))
                return

            self.btn_connect.setText("Disconnect")
            self.btn_zero.setEnabled(True)
            for sw in self.sliders.values():
                sw.set_voltage(0.0, silent=True)
                sw.set_enabled(True)
            self.status_bar.showMessage(f"Connected to {self.dac.tiger.port} -- all channels zeroed")
        else:
            self._stop_waveform()
            try:
                self.dac.zero_all()
            except Exception:
                pass
            self.dac.disconnect()
            self.btn_connect.setText("Connect")
            self.btn_zero.setEnabled(False)
            for sw in self.sliders.values():
                sw.set_enabled(False)
            self.status_bar.showMessage("Disconnected")

    def _zero_all(self):
        try:
            self.dac.zero_all()
            for sw in self.sliders.values():
                sw.set_voltage(0.0, silent=True)
            self.status_bar.showMessage("All outputs zeroed.")
        except Exception as e:
            self.status_bar.showMessage(f"Zero error: {e}")

    # ------------------------------------------------------------------
    # Slider handling
    # ------------------------------------------------------------------
    @Slot(str, float)
    def _on_slider_changed(self, name: str, volts: float):
        if not self.dac.is_connected:
            return
        try:
            self.dac.set_voltage(name, volts)
            self.status_bar.showMessage(f"{name}: {volts:+.3f} V")
        except (TigerError, ValueError, RuntimeError) as e:
            self.status_bar.showMessage(f"Hardware error: {e}")

    # ------------------------------------------------------------------
    # Waveform handling
    # ------------------------------------------------------------------
    def _start_waveform(self, loop: bool):
        if not self.dac.is_connected:
            QMessageBox.warning(self, "Not connected", "Connect to the Tiger controller first.")
            return
        if self._wave_thread is not None:
            return

        rate = self.rate_spin.value()
        if rate > MAX_RECOMMENDED_RATE:
            resp = QMessageBox.question(
                self, "High sample rate",
                f"{rate} Hz over serial will likely fall behind on this hardware. Continue anyway?",
            )
            if resp != QMessageBox.Yes:
                return

        name = self.wave_channel_combo.currentText()
        ch = self.dac.channels[name]
        v_min, v_max = ch.limits_mv[0] / 1000.0, ch.limits_mv[1] / 1000.0

        wave = generate_waveform(
            waveform_type=self.wave_type_combo.currentText(),
            amplitude=self.amp_spin.value(),
            offset=self.offset_spin.value(),
            frequency=self.freq_spin.value(),
            duty_cycle=self.duty_spin.value(),
            duration=self.duration_spin.value(),
            sample_rate=rate,
            v_min=v_min,
            v_max=v_max,
        )

        self._wave_thread = QThread()
        self._wave_worker = WaveformWorker(self.dac, {name: wave}, rate, loop)
        self._wave_worker.moveToThread(self._wave_thread)
        self._wave_thread.started.connect(self._wave_worker.run)
        self._wave_worker.progress.connect(self._on_wave_progress)
        self._wave_worker.error.connect(self._on_wave_error)
        self._wave_worker.finished.connect(self._on_wave_finished)
        self._wave_thread.start()

        self.sliders[name].set_enabled(False)
        self.btn_once.setEnabled(False)
        self.btn_loop.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.status_bar.showMessage(f"Streaming {name} ({'loop' if loop else 'once'})...")

    def _stop_waveform(self):
        if self._wave_worker is not None:
            self._wave_worker.abort()
        if self._wave_thread is not None:
            self._wave_thread.quit()
            self._wave_thread.wait(3000)
        self._wave_thread = None
        self._wave_worker = None
        self.btn_once.setEnabled(True)
        self.btn_loop.setEnabled(True)
        self.btn_stop.setEnabled(False)
        for sw in self.sliders.values():
            sw.set_enabled(self.dac.is_connected)

    @Slot(int)
    def _on_wave_progress(self, pct: int):
        self.status_bar.showMessage(f"Streaming... {pct}%")

    @Slot(str)
    def _on_wave_error(self, msg: str):
        self.status_bar.showMessage(f"Waveform error: {msg}")
        self._stop_waveform()

    @Slot()
    def _on_wave_finished(self):
        rate = self._wave_worker.achieved_rate_hz if self._wave_worker else 0.0
        self.status_bar.showMessage(f"Waveform finished (achieved ~{rate:.1f} Hz).")
        self._stop_waveform()

    # ------------------------------------------------------------------
    def closeEvent(self, event):
        self._stop_waveform()
        if self.dac.is_connected:
            try:
                self.dac.zero_all()
            except Exception:
                pass
            self.dac.disconnect()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName("ASI Tiger DAC Controller")
    window = ASIDACControllerWindow()
    window.show()
    sys.exit(app.exec_())
