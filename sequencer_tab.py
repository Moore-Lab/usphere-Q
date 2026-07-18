"""
sequencer_tab.py

GUI for the compound charge-control command sequencer (SequencerTab).

Build a list of steps — Discharge (flash lamp), Recharge (filament), or Wait —
each with its own actuator parameters and a signed charge-threshold stop
condition, then run the list (optionally repeating) driving the charge
actuators.  Modeled on the Electrodes Sweep tab's list-building pattern.

The ChargeSequencer engine and its actuators are attached by the ChargeWidget.
"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QComboBox, QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QListWidget, QPushButton, QSpinBox, QStackedWidget, QTextEdit, QVBoxLayout,
    QWidget,
)

from charge_sequence import ChargeSequencer, SeqStep, COMPARES

_ACTIONS = ["Discharge (flash lamp)", "Recharge (filament)",
            "Set electrode field", "Wait (delay)"]
_ACTION_KEYS = ["discharge", "recharge", "set_electrode", "wait"]


class SequencerTab(QWidget):
    """List-based charge/discharge command sequencer UI."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._seq: ChargeSequencer | None = None
        self._steps: list[SeqStep] = []
        self._build_ui()

    # ------------------------------------------------------------------
    # Engine wiring (called by ChargeWidget)
    # ------------------------------------------------------------------

    def set_sequencer(self, seq: ChargeSequencer):
        self._seq = seq
        seq.step_changed.connect(self._on_step_changed)
        seq.log_msg.connect(self._log)
        seq.state_changed.connect(self._on_state)
        seq.sequence_done.connect(self._on_done)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        outer.setContentsMargins(6, 6, 6, 6)

        # --- Global settings ---
        set_grp = QGroupBox("Sequence settings")
        sgl = QGridLayout(set_grp)
        sgl.addWidget(QLabel("Repeat list:"), 0, 0)
        self._repeat_spin = QSpinBox()
        self._repeat_spin.setRange(0, 100000)
        self._repeat_spin.setValue(1)
        self._repeat_spin.setMaximumWidth(90)
        self._repeat_spin.setToolTip("How many times to run the whole list. 0 = until Stopped.")
        sgl.addWidget(self._repeat_spin, 0, 1)
        sgl.addWidget(QLabel("(0 = until stopped)"), 0, 2)

        sgl.addWidget(QLabel("Safety charge limit (|e|):"), 1, 0)
        self._limit_spin = QDoubleSpinBox()
        self._limit_spin.setRange(1.0, 10000.0)
        self._limit_spin.setDecimals(1)
        self._limit_spin.setValue(30.0)
        self._limit_spin.setMaximumWidth(90)
        self._limit_spin.setToolTip("Any step stops immediately if |charge| reaches this ceiling.")
        sgl.addWidget(self._limit_spin, 1, 1)

        sgl.addWidget(QLabel("Poll interval (s):"), 1, 2)
        self._poll_spin = QDoubleSpinBox()
        self._poll_spin.setRange(0.02, 5.0)
        self._poll_spin.setDecimals(2)
        self._poll_spin.setValue(0.2)
        self._poll_spin.setMaximumWidth(80)
        sgl.addWidget(self._poll_spin, 1, 3)
        sgl.setColumnStretch(4, 1)
        outer.addWidget(set_grp)

        # --- Step editor ---
        ed_grp = QGroupBox("Step editor")
        edl = QVBoxLayout(ed_grp)
        arow = QHBoxLayout()
        arow.addWidget(QLabel("Action:"))
        self._action_combo = QComboBox()
        self._action_combo.addItems(_ACTIONS)
        self._action_combo.currentIndexChanged.connect(self._on_action_changed)
        arow.addWidget(self._action_combo)
        arow.addStretch()
        edl.addLayout(arow)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_discharge_page())
        self._stack.addWidget(self._build_recharge_page())
        self._stack.addWidget(self._build_setelec_page())
        self._stack.addWidget(self._build_wait_page())
        edl.addWidget(self._stack)

        btns = QHBoxLayout()
        add_btn = QPushButton("Add step")
        add_btn.clicked.connect(self._add_step)
        btns.addWidget(add_btn)
        upd_btn = QPushButton("Update selected")
        upd_btn.clicked.connect(self._update_selected)
        btns.addWidget(upd_btn)
        btns.addStretch()
        edl.addLayout(btns)
        outer.addWidget(ed_grp)

        # --- Step list ---
        list_grp = QGroupBox("Sequence")
        lgl = QVBoxLayout(list_grp)
        self._list = QListWidget()
        self._list.setMinimumHeight(120)
        self._list.currentRowChanged.connect(self._on_row_selected)
        lgl.addWidget(self._list)
        lb = QHBoxLayout()
        for text, fn in (("▲ Up", self._move_up), ("▼ Down", self._move_down),
                         ("Remove", self._remove), ("Clear", self._clear)):
            b = QPushButton(text); b.clicked.connect(fn); b.setMaximumWidth(90)
            lb.addWidget(b)
        lb.addStretch()
        lgl.addLayout(lb)
        outer.addWidget(list_grp)

        # --- Run controls ---
        run_row = QHBoxLayout()
        self._start_btn = QPushButton("Start sequence")
        self._start_btn.setStyleSheet("font-weight: bold;")
        self._start_btn.clicked.connect(self._on_start)
        run_row.addWidget(self._start_btn)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        run_row.addWidget(self._stop_btn)
        self._status = QLabel("Idle")
        self._status.setStyleSheet("color: gray;")
        run_row.addWidget(self._status)
        run_row.addStretch()
        outer.addLayout(run_row)

        # --- Log ---
        self._logbox = QTextEdit()
        self._logbox.setReadOnly(True)
        self._logbox.setMaximumHeight(150)
        outer.addWidget(self._logbox)

    def _stop_cond_row(self, grid, row):
        grid.addWidget(QLabel("Stop when:"), row, 0)
        cmp = QComboBox()
        for key, label in COMPARES.items():
            cmp.addItem(label, key)
        cmp.setMaximumWidth(100)
        grid.addWidget(cmp, row, 1)
        thr = QDoubleSpinBox(); thr.setRange(-100000, 100000); thr.setDecimals(2)
        thr.setValue(1.0); thr.setSuffix(" e"); thr.setMaximumWidth(100)
        grid.addWidget(thr, row, 2)
        grid.addWidget(QLabel("Timeout:"), row, 3)
        to = QDoubleSpinBox(); to.setRange(0.5, 100000); to.setDecimals(1)
        to.setValue(60.0); to.setSuffix(" s"); to.setMaximumWidth(90)
        grid.addWidget(to, row, 4)
        return cmp, thr, to

    def _build_discharge_page(self):
        w = QWidget(); g = QGridLayout(w); g.setColumnStretch(5, 1)
        g.addWidget(QLabel("Flash rate:"), 0, 0)
        self._d_rate = QDoubleSpinBox(); self._d_rate.setRange(0.001, 1e6)
        self._d_rate.setDecimals(3); self._d_rate.setValue(10.0); self._d_rate.setSuffix(" Hz")
        self._d_rate.setMaximumWidth(110); g.addWidget(self._d_rate, 0, 1)
        g.addWidget(QLabel("Flash control:"), 0, 2)
        self._d_ctrl = QDoubleSpinBox(); self._d_ctrl.setRange(0.0, 32.0)
        self._d_ctrl.setDecimals(3); self._d_ctrl.setValue(0.0); self._d_ctrl.setSuffix(" V")
        self._d_ctrl.setMaximumWidth(110); g.addWidget(self._d_ctrl, 0, 3)
        self._d_cmp, self._d_thr, self._d_to = self._stop_cond_row(g, 1)
        return w

    def _build_recharge_page(self):
        w = QWidget(); g = QGridLayout(w); g.setColumnStretch(6, 1)
        g.addWidget(QLabel("Start width:"), 0, 0)
        self._r_start = QDoubleSpinBox(); self._r_start.setRange(0.001, 1e5)
        self._r_start.setDecimals(3); self._r_start.setValue(5.0); self._r_start.setSuffix(" ms")
        self._r_start.setMaximumWidth(100); g.addWidget(self._r_start, 0, 1)
        g.addWidget(QLabel("Increment:"), 0, 2)
        self._r_inc = QDoubleSpinBox(); self._r_inc.setRange(0.001, 1e5)
        self._r_inc.setDecimals(3); self._r_inc.setValue(5.0); self._r_inc.setSuffix(" ms")
        self._r_inc.setMaximumWidth(100); g.addWidget(self._r_inc, 0, 3)
        g.addWidget(QLabel("Max width:"), 0, 4)
        self._r_max = QDoubleSpinBox(); self._r_max.setRange(0.001, 1e5)
        self._r_max.setDecimals(3); self._r_max.setValue(200.0); self._r_max.setSuffix(" ms")
        self._r_max.setMaximumWidth(100); g.addWidget(self._r_max, 0, 5)
        g.addWidget(QLabel("Timeout cycles:"), 1, 0)
        self._r_cycles = QSpinBox(); self._r_cycles.setRange(1, 1000)
        self._r_cycles.setValue(6); self._r_cycles.setMaximumWidth(100)
        self._r_cycles.setToolTip("Read cycles to wait (filament off) before reading.\n"
                                  "Here one cycle = one sequencer poll interval.")
        g.addWidget(self._r_cycles, 1, 1)
        g.addWidget(QLabel("Power (0 = keep):"), 1, 2)
        self._r_power = QDoubleSpinBox(); self._r_power.setRange(0.0, 32.0)
        self._r_power.setDecimals(3); self._r_power.setValue(0.0); self._r_power.setSuffix(" V")
        self._r_power.setMaximumWidth(100); g.addWidget(self._r_power, 1, 3)
        hint = QLabel(
            "Fires one pulse, waits N cycles with the filament off (clean read), "
            "reads,\nand increments the pulse width until the stop condition — "
            "immune to filament noise.")
        hint.setStyleSheet("color: #9E9E9E; font-size: 11px;")
        g.addWidget(hint, 2, 0, 1, 6)
        self._r_cmp, self._r_thr, self._r_to = self._stop_cond_row(g, 3)
        return w

    def _build_setelec_page(self):
        w = QWidget(); g = QGridLayout(w); g.setColumnStretch(4, 1)
        g.addWidget(QLabel("Drive amplitude:"), 0, 0)
        self._e_amp = QDoubleSpinBox(); self._e_amp.setRange(0.0, 20.0)
        self._e_amp.setDecimals(3); self._e_amp.setValue(0.5); self._e_amp.setSuffix(" Vpp")
        self._e_amp.setMaximumWidth(110); g.addWidget(self._e_amp, 0, 1)
        g.addWidget(QLabel("Settle:"), 0, 2)
        self._e_settle = QDoubleSpinBox(); self._e_settle.setRange(0.0, 100000)
        self._e_settle.setDecimals(2); self._e_settle.setValue(0.5); self._e_settle.setSuffix(" s")
        self._e_settle.setMaximumWidth(90); g.addWidget(self._e_settle, 0, 3)
        hint = QLabel(
            "Sets the drive amplitude on the monitored axis; the reported charge "
            "stays\nnormalized (quanta). Drop the field low before recharging so "
            "the filament\ndoesn't knock the sphere out; raise it for a "
            "better-SNR discharge measurement.")
        hint.setStyleSheet("color: #9E9E9E; font-size: 11px;")
        g.addWidget(hint, 1, 0, 1, 5)
        return w

    def _build_wait_page(self):
        w = QWidget(); g = QGridLayout(w); g.setColumnStretch(2, 1)
        g.addWidget(QLabel("Duration:"), 0, 0)
        self._w_dur = QDoubleSpinBox(); self._w_dur.setRange(0.1, 100000)
        self._w_dur.setDecimals(1); self._w_dur.setValue(2.0); self._w_dur.setSuffix(" s")
        self._w_dur.setMaximumWidth(110); g.addWidget(self._w_dur, 0, 1)
        return w

    def _on_action_changed(self, idx):
        self._stack.setCurrentIndex(idx)

    # ------------------------------------------------------------------
    # Step <-> editor
    # ------------------------------------------------------------------

    def _editor_to_step(self) -> SeqStep:
        action = _ACTION_KEYS[self._action_combo.currentIndex()]
        if action == "discharge":
            return SeqStep(action="discharge",
                           flash_rate_hz=self._d_rate.value(),
                           flash_ctrl_v=self._d_ctrl.value(),
                           compare=self._d_cmp.currentData(),
                           threshold_e=self._d_thr.value(),
                           timeout_s=self._d_to.value())
        if action == "recharge":
            return SeqStep(action="recharge",
                           fil_start_width_ms=self._r_start.value(),
                           fil_increment_ms=self._r_inc.value(),
                           fil_max_width_ms=self._r_max.value(),
                           fil_timeout_cycles=self._r_cycles.value(),
                           fil_power_v=self._r_power.value(),
                           compare=self._r_cmp.currentData(),
                           threshold_e=self._r_thr.value(),
                           timeout_s=self._r_to.value())
        if action == "set_electrode":
            return SeqStep(action="set_electrode",
                           electrode_amp_vpp=self._e_amp.value(),
                           electrode_settle_s=self._e_settle.value())
        return SeqStep(action="wait", wait_s=self._w_dur.value())

    def _step_to_editor(self, s: SeqStep):
        idx = _ACTION_KEYS.index(s.action) if s.action in _ACTION_KEYS else 0
        self._action_combo.setCurrentIndex(idx)
        if s.action == "discharge":
            self._d_rate.setValue(s.flash_rate_hz); self._d_ctrl.setValue(s.flash_ctrl_v)
            self._d_cmp.setCurrentIndex(max(0, self._d_cmp.findData(s.compare)))
            self._d_thr.setValue(s.threshold_e); self._d_to.setValue(s.timeout_s)
        elif s.action == "recharge":
            self._r_start.setValue(s.fil_start_width_ms)
            self._r_inc.setValue(s.fil_increment_ms)
            self._r_max.setValue(s.fil_max_width_ms)
            self._r_cycles.setValue(int(s.fil_timeout_cycles))
            self._r_power.setValue(s.fil_power_v)
            self._r_cmp.setCurrentIndex(max(0, self._r_cmp.findData(s.compare)))
            self._r_thr.setValue(s.threshold_e); self._r_to.setValue(s.timeout_s)
        elif s.action == "set_electrode":
            self._e_amp.setValue(s.electrode_amp_vpp)
            self._e_settle.setValue(s.electrode_settle_s)
        elif s.action == "wait":
            self._w_dur.setValue(s.wait_s)

    def _refresh_list(self, keep_row=None):
        self._list.clear()
        for i, s in enumerate(self._steps):
            self._list.addItem(f"{i + 1}. {s.summary()}")
        if keep_row is not None and 0 <= keep_row < len(self._steps):
            self._list.setCurrentRow(keep_row)

    def _add_step(self):
        self._steps.append(self._editor_to_step())
        self._refresh_list(keep_row=len(self._steps) - 1)

    def _update_selected(self):
        r = self._list.currentRow()
        if 0 <= r < len(self._steps):
            self._steps[r] = self._editor_to_step()
            self._refresh_list(keep_row=r)

    def _on_row_selected(self, r):
        if 0 <= r < len(self._steps):
            self._step_to_editor(self._steps[r])

    def _move_up(self):
        r = self._list.currentRow()
        if r > 0:
            self._steps[r - 1], self._steps[r] = self._steps[r], self._steps[r - 1]
            self._refresh_list(keep_row=r - 1)

    def _move_down(self):
        r = self._list.currentRow()
        if 0 <= r < len(self._steps) - 1:
            self._steps[r + 1], self._steps[r] = self._steps[r], self._steps[r + 1]
            self._refresh_list(keep_row=r + 1)

    def _remove(self):
        r = self._list.currentRow()
        if 0 <= r < len(self._steps):
            self._steps.pop(r)
            self._refresh_list(keep_row=min(r, len(self._steps) - 1))

    def _clear(self):
        self._steps.clear()
        self._refresh_list()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def _log(self, msg: str):
        self._logbox.append(msg)

    def _on_start(self):
        if self._seq is None:
            self._log("No sequencer engine attached."); return
        if not self._steps:
            self._log("Add at least one step first."); return
        self._seq.set_steps(list(self._steps))
        self._seq.set_repeat(self._repeat_spin.value())
        self._seq.set_charge_limit(self._limit_spin.value())
        self._seq.set_poll_interval(self._poll_spin.value())
        self._logbox.clear()
        try:
            self._seq.start()
        except Exception as e:
            self._log(f"Cannot start: {type(e).__name__}: {e}")
            return
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)

    def _on_stop(self):
        if self._seq is not None:
            self._seq.stop()

    def _on_step_changed(self, idx, n, summary):
        self._status.setText(f"Step {idx + 1}/{n}: {summary}")
        self._status.setStyleSheet("color: #1565C0;")
        self._list.setCurrentRow(idx)

    def _on_state(self, msg):
        self._log(f"— {msg} —")

    def _on_done(self, ok):
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._status.setText("Done" if ok else "Stopped")
        self._status.setStyleSheet("color: green;" if ok else "color: gray;")

    # ------------------------------------------------------------------
    # Config persistence
    # ------------------------------------------------------------------

    def get_config(self) -> dict:
        return {
            "repeat": self._repeat_spin.value(),
            "charge_limit": self._limit_spin.value(),
            "poll_s": self._poll_spin.value(),
            "steps": [s.to_dict() for s in self._steps],
        }

    def restore_config(self, cfg: dict):
        if "repeat" in cfg:
            self._repeat_spin.setValue(int(cfg["repeat"]))
        if "charge_limit" in cfg:
            self._limit_spin.setValue(float(cfg["charge_limit"]))
        if "poll_s" in cfg:
            self._poll_spin.setValue(float(cfg["poll_s"]))
        if "steps" in cfg:
            self._steps = [SeqStep.from_dict(d) for d in cfg["steps"]]
            self._refresh_list()
