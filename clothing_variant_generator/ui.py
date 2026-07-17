# -*- coding: utf-8 -*-
"""
ui.py
-----
Dockable Qt UI for the Clothing Variant Generator.
Works with PySide2 (Maya 2022-2024) and PySide6 (Maya 2025+).

The UI never touches Maya's scene graph directly for the heavy lifting --
it only gathers user intent (base body, target bodies, clothing list,
output folder, options) and hands one task at a time to
processor.BatchRunner, driven by a QTimer so:

    * the Maya main thread (where all cmds calls must run) stays free,
    * the progress bar / log / ETA update between every asset,
    * Cancel takes effect immediately between tasks.

True background threading is intentionally NOT used: maya.cmds is not
thread-safe, so "UI-safe progress updates" here means "never block the
event loop for the whole batch", not "run Maya commands off-thread".

The entire interface lives inside a QScrollArea so every section stays
reachable (and nothing is ever clipped) regardless of how small the
window is docked.
"""

import os
import json
import time

import maya.cmds as cmds

try:
    # Maya 2022-2024 ship Qt5 / PySide2.
    from PySide2 import QtWidgets, QtCore, QtGui
except ImportError:
    # Maya 2025+ ship Qt6 / PySide6 instead. The subset of the API used
    # in this file (QWidget, layouts, QListWidget, QTextEdit, QTimer,
    # Signal-based logging, etc.) is source-compatible between the two.
    from PySide6 import QtWidgets, QtCore, QtGui

try:
    from maya.app.general.mayaMixin import MayaQWidgetDockableMixin
except ImportError:
    # Allows the module to be imported (e.g. for docs/tests) outside Maya.
    class MayaQWidgetDockableMixin(object):
        pass

from . import config
from . import utils
from .logger import VariantLogger
from .processor import ClothingVariantProcessor, BatchRunner
from .exporter import FBXExporter
from .deform_state import DeformationState


# ---------------------------------------------------------------------------
# Small reusable dialog: Add Target Body
# ---------------------------------------------------------------------------
class AddTargetBodyDialog(QtWidgets.QDialog):
    """Dialog for adding a new target body: pick a preset or type a custom
    display name, then assign a mesh from the current Maya selection."""

    def __init__(self, parent=None):
        super(AddTargetBodyDialog, self).__init__(parent)
        self.setWindowTitle("Add Target Body")
        self.setMinimumWidth(320)

        self.result_display_name = None
        self.result_mesh_name = None

        layout = QtWidgets.QVBoxLayout(self)

        form = QtWidgets.QFormLayout()
        self.preset_combo = QtWidgets.QComboBox()
        self.preset_combo.addItems(config.DEFAULT_BODY_VARIANT_PRESETS + ["Custom..."])
        self.preset_combo.currentTextChanged.connect(self._on_preset_changed)
        form.addRow("Body Type:", self.preset_combo)

        self.custom_name_edit = QtWidgets.QLineEdit()
        self.custom_name_edit.setPlaceholderText("e.g. Monster, Alien, Hero...")
        self.custom_name_edit.setVisible(False)
        form.addRow("Custom Name:", self.custom_name_edit)

        layout.addLayout(form)

        mesh_row = QtWidgets.QHBoxLayout()
        self.mesh_label = QtWidgets.QLabel("<no mesh selected>")
        self.mesh_label.setStyleSheet("color: gray;")
        select_btn = QtWidgets.QPushButton("Use Selected Mesh")
        select_btn.clicked.connect(self._grab_selection)
        mesh_row.addWidget(self.mesh_label, 1)
        mesh_row.addWidget(select_btn)
        layout.addLayout(mesh_row)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_preset_changed(self, text):
        self.custom_name_edit.setVisible(text == "Custom...")

    def _grab_selection(self):
        selection = cmds.ls(selection=True, long=False) or []
        if not selection:
            QtWidgets.QMessageBox.warning(self, "No Selection", "Select a mesh in the scene first.")
            return
        candidate = selection[0]
        if not utils.node_exists_and_is_mesh(candidate):
            QtWidgets.QMessageBox.warning(self, "Invalid Selection", "'%s' is not a polygon mesh." % candidate)
            return
        self.mesh_label.setText(candidate)
        self.mesh_label.setStyleSheet("color: black;")

    def _on_accept(self):
        display_name = self.custom_name_edit.text().strip() \
            if self.preset_combo.currentText() == "Custom..." \
            else self.preset_combo.currentText()

        if not display_name:
            QtWidgets.QMessageBox.warning(self, "Missing Name", "Please provide a body type name.")
            return
        if self.mesh_label.text() == "<no mesh selected>":
            QtWidgets.QMessageBox.warning(self, "Missing Mesh", "Please assign a mesh for this body.")
            return

        self.result_display_name = display_name
        self.result_mesh_name = self.mesh_label.text()
        self.accept()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class ClothingVariantGeneratorWindow(MayaQWidgetDockableMixin, QtWidgets.QWidget):

    def __init__(self, parent=None):
        super(ClothingVariantGeneratorWindow, self).__init__(parent=parent)
        self.setObjectName(config.PLUGIN_OBJECT_NAME)
        self.setWindowTitle("%s v%s" % (config.PLUGIN_NAME, config.PLUGIN_VERSION))
        self.setMinimumSize(700, 700)
        self.resize(900, 850)

        # ---- state -------------------------------------------------
        self.base_body_mesh = None
        self.target_bodies = []   # list of {"display_name": str, "mesh": str}
        self.clothing_items = []  # list of mesh names

        self.logger = VariantLogger(self)
        self.logger.message_logged.connect(self._append_log_line)
        self.exporter = FBXExporter(logger=self.logger)

        self.batch_runner = None
        self.process_timer = QtCore.QTimer(self)
        self.process_timer.setInterval(0)
        self.process_timer.timeout.connect(self._on_timer_tick)
        self.batch_start_time = None

        # Manual Deformation Controls (Global Body Influence, Global
        # Surface Offset, Smoothing, Falloff). Only applied during
        # Generate if the artist opts in via the "Apply these
        # Deformation Controls to Generate Variants" checkbox.
        self.deform_state = DeformationState()
        self.live_processor = None  # lazily (re)built in _ensure_live_processor()

        # scriptJobs / callbacks this window registers, so Clean
        # Shutdown can remove every single one on close -- nothing
        # persistent should survive this window closing.
        self._script_job_ids = []
        self._cleaned_up = False

        self._build_ui()
        self._load_settings(silent=True)
        self._register_scene_scriptjobs()

    # ------------------------------------------------------------------
    # Startup stability, single-instance window management, and clean
    # shutdown.
    # ------------------------------------------------------------------
    def _register_scene_scriptjobs(self):
        """
        Track every scriptJob this window registers so Clean Shutdown
        can kill them all by id -- nothing should survive after the
        window is closed, and NOTHING here runs unless this window was
        actually constructed by an explicit `cvg_main.show()` call:
        this method is only ever invoked from __init__.
        """
        # If the scene is closed/opened out from under us, our cached
        # mesh names are no longer valid -- drop them defensively
        # rather than risk operating on stale/renamed nodes.
        self._script_job_ids.append(
            cmds.scriptJob(event=["SceneOpened", self._on_scene_changed], protected=True)
        )
        self._script_job_ids.append(
            cmds.scriptJob(event=["NewSceneOpened", self._on_scene_changed], protected=True)
        )
        # Belt-and-braces: if Maya quits without our closeEvent firing
        # (e.g. a forced quit), still release timers/callbacks first.
        self._script_job_ids.append(
            cmds.scriptJob(event=["quitApplication", self._cleanup], protected=True)
        )

    def _on_scene_changed(self, *_args):
        if self.batch_runner is not None and not self.batch_runner.is_finished():
            self.batch_runner.cancel()
        self.live_processor = None
        self.base_body_mesh = None
        self.target_bodies = []
        self.clothing_items = []
        if hasattr(self, "base_body_label"):
            self.base_body_label.setText("<none selected>")
            self.base_body_label.setStyleSheet("color: gray;")
            self._refresh_target_list_widget()
            self._refresh_clothing_list_widget()

    def dockCloseEventTriggered(self):
        """
        MayaQWidgetDockableMixin's documented hook for a DOCKED window
        being closed (the workspaceControl's [x], or
        `workspaceControl -close`). This is NOT a Qt Signal to
        `.connect()` to -- Maya calls this method directly on the
        instance. That has been true since the mixin was introduced
        (Maya 2015+/PySide) and remains true in Maya 2025's PySide6
        build, where the attribute is a plain bound method rather than
        a Signal; attempting `.connect()` on it raises AttributeError
        there. Overriding the method is the correct, version-proof
        mechanism (this is also what Autodesk's own examples and the
        wider Maya scripting community have used for this hook for
        years). QWidget.closeEvent below covers the FLOATING-window
        case; together the two ensure cleanup runs no matter how the
        artist closes the window.
        """
        self._cleanup()

    def closeEvent(self, event):
        self._cleanup()
        super(ClothingVariantGeneratorWindow, self).closeEvent(event)

    def _cleanup(self, *_args):
        """
        Release EVERYTHING this window could have created or
        registered -- timers, scriptJobs, the batch runner -- so
        closing the window (or Maya quitting) never leaves a dangling
        callback/timer/temp-node behind. Idempotent: safe to call more
        than once (close + quit, etc).
        """
        if self._cleaned_up:
            return
        self._cleaned_up = True

        timer = getattr(self, "process_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except RuntimeError:
                pass  # underlying Qt object may already be gone

        if self.batch_runner is not None and not self.batch_runner.is_finished():
            self.batch_runner.cancel()
        self.batch_runner = None

        for job_id in self._script_job_ids:
            try:
                if cmds.scriptJob(exists=job_id):
                    cmds.scriptJob(kill=job_id, force=True)
            except RuntimeError:
                pass
        self._script_job_ids = []

        self.live_processor = None

        global _window_instance
        if _window_instance is self:
            _window_instance = None

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        outer_layout = QtWidgets.QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll_area = QtWidgets.QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QtWidgets.QFrame.NoFrame)
        outer_layout.addWidget(scroll_area)

        scroll_content = QtWidgets.QWidget()
        main_layout = QtWidgets.QVBoxLayout(scroll_content)

        main_layout.addLayout(self._build_settings_bar())
        main_layout.addWidget(self._build_body_section())
        main_layout.addWidget(self._build_clothing_section())
        main_layout.addWidget(self._build_output_section())
        main_layout.addWidget(self._build_options_section())
        main_layout.addWidget(self._build_deformation_controls_section())
        main_layout.addWidget(self._build_processing_section())
        main_layout.addLayout(self._build_bottom_buttons())

        scroll_area.setWidget(scroll_content)

    def _build_settings_bar(self):
        row = QtWidgets.QHBoxLayout()
        save_btn = QtWidgets.QPushButton("Save Settings...")
        save_btn.setFixedHeight(28)
        save_btn.clicked.connect(self._save_settings_dialog)
        load_btn = QtWidgets.QPushButton("Load Settings...")
        load_btn.setFixedHeight(28)
        load_btn.clicked.connect(self._load_settings_dialog)
        row.addWidget(save_btn)
        row.addWidget(load_btn)
        row.addStretch()
        return row

    def _build_body_section(self):
        group = QtWidgets.QGroupBox("Body")
        layout = QtWidgets.QVBoxLayout(group)

        base_row = QtWidgets.QHBoxLayout()
        base_row.addWidget(QtWidgets.QLabel("Base Body:"))
        self.base_body_label = QtWidgets.QLabel("<none selected>")
        self.base_body_label.setStyleSheet("color: gray;")
        select_base_btn = QtWidgets.QPushButton("Select")
        select_base_btn.setFixedHeight(28)
        select_base_btn.clicked.connect(self._on_select_base_body)
        base_row.addWidget(self.base_body_label, 1)
        base_row.addWidget(select_base_btn)
        layout.addLayout(base_row)

        layout.addWidget(self._hline())

        layout.addWidget(QtWidgets.QLabel("Target Bodies:"))
        self.target_list_widget = QtWidgets.QListWidget()
        self.target_list_widget.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        layout.addWidget(self.target_list_widget)

        target_btn_row = QtWidgets.QHBoxLayout()
        add_target_btn = QtWidgets.QPushButton("Add Target Body")
        add_target_btn.setFixedHeight(28)
        add_target_btn.clicked.connect(self._on_add_target_body)
        remove_target_btn = QtWidgets.QPushButton("Remove Target Body")
        remove_target_btn.setFixedHeight(28)
        remove_target_btn.clicked.connect(self._on_remove_target_body)
        target_btn_row.addWidget(add_target_btn)
        target_btn_row.addWidget(remove_target_btn)
        layout.addLayout(target_btn_row)

        return group

    def _build_clothing_section(self):
        group = QtWidgets.QGroupBox("Clothing")
        layout = QtWidgets.QVBoxLayout(group)

        self.clothing_search = QtWidgets.QLineEdit()
        self.clothing_search.setPlaceholderText("Search clothing list...")
        self.clothing_search.textChanged.connect(self._filter_clothing_list)
        layout.addWidget(self.clothing_search)

        self.clothing_list_widget = QtWidgets.QListWidget()
        self.clothing_list_widget.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        # Nice-to-have: drag-and-drop reordering within the list.
        # NOTE: dragging mesh nodes directly from Maya's Outliner into a
        # Qt list is not supported by Maya's Qt/MEL bridge, so this only
        # supports re-ordering items already in the list.
        self.clothing_list_widget.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        layout.addWidget(self.clothing_list_widget)

        btn_row1 = QtWidgets.QHBoxLayout()
        add_selected_btn = QtWidgets.QPushButton("Add Selected Clothing")
        add_selected_btn.setFixedHeight(28)
        add_selected_btn.clicked.connect(self._on_add_selected_clothing)
        remove_selected_btn = QtWidgets.QPushButton("Remove Selected")
        remove_selected_btn.setFixedHeight(28)
        remove_selected_btn.clicked.connect(self._on_remove_selected_clothing)
        btn_row1.addWidget(add_selected_btn)
        btn_row1.addWidget(remove_selected_btn)
        layout.addLayout(btn_row1)

        btn_row2 = QtWidgets.QHBoxLayout()
        load_folder_btn = QtWidgets.QPushButton("Load Clothing Folder")
        load_folder_btn.setFixedHeight(28)
        load_folder_btn.clicked.connect(self._on_load_clothing_folder)
        clear_btn = QtWidgets.QPushButton("Clear List")
        clear_btn.setFixedHeight(28)
        clear_btn.clicked.connect(self._on_clear_clothing_list)
        btn_row2.addWidget(load_folder_btn)
        btn_row2.addWidget(clear_btn)
        layout.addLayout(btn_row2)

        return group

    def _build_output_section(self):
        group = QtWidgets.QGroupBox("Output")
        layout = QtWidgets.QHBoxLayout(group)
        layout.addWidget(QtWidgets.QLabel("Output Folder:"))
        self.output_folder_edit = QtWidgets.QLineEdit()
        browse_btn = QtWidgets.QPushButton("Browse...")
        browse_btn.setFixedHeight(28)
        browse_btn.clicked.connect(self._on_browse_output_folder)
        layout.addWidget(self.output_folder_edit, 1)
        layout.addWidget(browse_btn)
        return group

    def _build_options_section(self):
        group = QtWidgets.QGroupBox("Options")
        layout = QtWidgets.QGridLayout(group)

        self.chk_delete_history = QtWidgets.QCheckBox("Delete Construction History")
        self.chk_freeze_transforms = QtWidgets.QCheckBox("Freeze Transforms")
        self.chk_center_pivot = QtWidgets.QCheckBox("Center Pivot")
        self.chk_transfer_skin = QtWidgets.QCheckBox("Transfer Skin Weights (optional)")
        self.chk_export_fbx = QtWidgets.QCheckBox("Export FBX")
        self.chk_overwrite = QtWidgets.QCheckBox("Overwrite Existing Files")
        self.chk_keep_clean = QtWidgets.QCheckBox("Keep Scene Clean")

        defaults = config.DEFAULT_OPTIONS
        self.chk_delete_history.setChecked(defaults["delete_history"])
        self.chk_freeze_transforms.setChecked(defaults["freeze_transforms"])
        self.chk_center_pivot.setChecked(defaults["center_pivot"])
        self.chk_transfer_skin.setChecked(defaults["transfer_skin_weights"])
        self.chk_export_fbx.setChecked(defaults["export_fbx"])
        self.chk_overwrite.setChecked(defaults["overwrite_existing"])
        self.chk_keep_clean.setChecked(defaults["keep_scene_clean"])

        layout.addWidget(self.chk_delete_history, 0, 0)
        layout.addWidget(self.chk_freeze_transforms, 0, 1)
        layout.addWidget(self.chk_center_pivot, 1, 0)
        layout.addWidget(self.chk_transfer_skin, 1, 1)
        layout.addWidget(self.chk_export_fbx, 2, 0)
        layout.addWidget(self.chk_overwrite, 2, 1)
        layout.addWidget(self.chk_keep_clean, 3, 0)

        return group

    # ------------------------------------------------------------------
    # Deformation Controls panel: Global Body Influence, Global Surface
    # Offset, and Smoothing/Falloff (feeding Maya's Laplacian smoothing
    # pass). Applied during Generate only if the artist opts in.
    # ------------------------------------------------------------------
    def _make_slider_row(self, layout, label_text, lo, hi, default, decimals, on_change):
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel(label_text), 0)

        slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        slider.setRange(0, 1000)

        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(lo, hi)
        spin.setDecimals(decimals)
        spin.setSingleStep((hi - lo) / 100.0 or 0.01)
        spin.setValue(default)

        def _slider_to_value(v):
            return lo + (hi - lo) * (v / 1000.0)

        def _value_to_slider(val):
            if hi == lo:
                return 0
            return int(round((val - lo) / (hi - lo) * 1000))

        slider.setValue(_value_to_slider(default))

        def _on_slider_moved(v):
            spin.blockSignals(True)
            spin.setValue(_slider_to_value(v))
            spin.blockSignals(False)
            on_change(_slider_to_value(v))

        def _on_spin_changed(val):
            slider.blockSignals(True)
            slider.setValue(_value_to_slider(val))
            slider.blockSignals(False)
            on_change(val)

        slider.valueChanged.connect(_on_slider_moved)
        spin.valueChanged.connect(_on_spin_changed)

        row.addWidget(slider, 1)
        row.addWidget(spin, 0)
        layout.addLayout(row)
        return slider, spin

    def _build_deformation_controls_section(self):
        group = QtWidgets.QGroupBox("Deformation Controls")
        outer = QtWidgets.QVBoxLayout(group)

        self.chk_apply_deform_to_generate = QtWidgets.QCheckBox(
            "Apply these Deformation Controls to Generate Variants "
            "(otherwise Generate stays fully automatic, as before)"
        )
        self.chk_apply_deform_to_generate.setChecked(False)
        outer.addWidget(self.chk_apply_deform_to_generate)
        outer.addWidget(self._hline())

        # -- Global sliders ---------------------------------------------
        self.deform_widgets = {}
        d = config.DEFORM_DEFAULTS
        lo, hi = config.DEFORM_RANGES["global_influence"]
        self.deform_widgets["global_influence"] = self._make_slider_row(
            outer, "Global Body Influence", lo, hi, d["global_influence"], 2,
            lambda v: self._on_deform_value_changed("global_influence", v))

        lo, hi = config.DEFORM_RANGES["surface_offset"]
        self.deform_widgets["surface_offset"] = self._make_slider_row(
            outer, "Surface Offset (cm)", lo, hi, d["surface_offset"], 2,
            lambda v: self._on_deform_value_changed("surface_offset", v))

        lo, hi = config.DEFORM_RANGES["smoothing"]
        self.deform_widgets["smoothing"] = self._make_slider_row(
            outer, "Smoothing", lo, hi, d["smoothing"], 1,
            lambda v: self._on_deform_value_changed("smoothing", v))

        lo, hi = config.DEFORM_RANGES["falloff"]
        self.deform_widgets["falloff"] = self._make_slider_row(
            outer, "Falloff", lo, hi, d["falloff"], 2,
            lambda v: self._on_deform_value_changed("falloff", v))

        reset_row = QtWidgets.QHBoxLayout()
        reset_btn = QtWidgets.QPushButton("Reset Sliders To Default")
        reset_btn.setFixedHeight(28)
        reset_btn.clicked.connect(self._on_reset_deform_clicked)
        reset_row.addWidget(reset_btn)
        outer.addLayout(reset_row)

        return group

    def _build_processing_section(self):
        group = QtWidgets.QGroupBox("Processing")
        layout = QtWidgets.QVBoxLayout(group)

        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        layout.addWidget(self.progress_bar)

        status_row = QtWidgets.QHBoxLayout()
        self.current_clothing_label = QtWidgets.QLabel("Clothing: -")
        self.current_body_label = QtWidgets.QLabel("Body Variant: -")
        status_row.addWidget(self.current_clothing_label)
        status_row.addWidget(self.current_body_label)
        layout.addLayout(status_row)

        stats_row = QtWidgets.QHBoxLayout()
        self.eta_label = QtWidgets.QLabel("ETA: -")
        self.stats_label = QtWidgets.QLabel("Processed: 0 / 0")
        stats_row.addWidget(self.eta_label)
        stats_row.addWidget(self.stats_label)
        layout.addLayout(stats_row)

        stats_row2 = QtWidgets.QHBoxLayout()
        self.vertex_count_label = QtWidgets.QLabel("Vertices: -")
        self.speed_label = QtWidgets.QLabel("Speed: -")
        self.elapsed_label = QtWidgets.QLabel("Elapsed: -")
        stats_row2.addWidget(self.vertex_count_label)
        stats_row2.addWidget(self.speed_label)
        stats_row2.addWidget(self.elapsed_label)
        layout.addLayout(stats_row2)

        button_row = QtWidgets.QHBoxLayout()
        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.pause_btn.setFixedHeight(28)
        self.resume_btn = QtWidgets.QPushButton("Resume")
        self.resume_btn.setFixedHeight(28)
        self.retry_failed_btn = QtWidgets.QPushButton("Retry Failed Items")
        self.retry_failed_btn.setFixedHeight(28)
        self.pause_btn.setEnabled(False)
        self.resume_btn.setEnabled(False)
        self.retry_failed_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._on_pause_clicked)
        self.resume_btn.clicked.connect(self._on_resume_clicked)
        self.retry_failed_btn.clicked.connect(self._on_retry_failed_clicked)
        button_row.addWidget(self.pause_btn)
        button_row.addWidget(self.resume_btn)
        button_row.addWidget(self.retry_failed_btn)
        layout.addLayout(button_row)

        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.setFixedHeight(28)
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.cancel_btn.setEnabled(False)
        layout.addWidget(self.cancel_btn)

        layout.addWidget(QtWidgets.QLabel("Log:"))
        self.log_view = QtWidgets.QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(140)
        self.log_view.setFontFamily("Courier")
        layout.addWidget(self.log_view)

        return group

    def _build_bottom_buttons(self):
        row = QtWidgets.QHBoxLayout()
        self.generate_btn = QtWidgets.QPushButton("Generate Variants")
        self.generate_btn.setFixedHeight(32)
        self.generate_btn.setStyleSheet("font-weight: bold; padding: 6px;")
        self.generate_btn.clicked.connect(self._on_generate_clicked)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.setFixedHeight(32)
        close_btn.clicked.connect(self.close)
        row.addWidget(self.generate_btn, 1)
        row.addWidget(close_btn)
        return row

    def _hline(self):
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Sunken)
        return line

    # ------------------------------------------------------------------
    # Base body
    # ------------------------------------------------------------------
    def _on_select_base_body(self):
        selection = cmds.ls(selection=True) or []
        if not selection:
            QtWidgets.QMessageBox.warning(self, "No Selection", "Select the base body mesh first.")
            return
        candidate = selection[0]
        if not utils.node_exists_and_is_mesh(candidate):
            QtWidgets.QMessageBox.warning(self, "Invalid Selection", "'%s' is not a polygon mesh." % candidate)
            return
        self.base_body_mesh = candidate
        self.base_body_label.setText(candidate)
        self.base_body_label.setStyleSheet("color: black;")
        self.live_processor = None  # forces a fresh processor bound to the new base body

    # ------------------------------------------------------------------
    # Target bodies
    # ------------------------------------------------------------------
    def _on_add_target_body(self):
        dialog = AddTargetBodyDialog(self)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            entry = {"display_name": dialog.result_display_name, "mesh": dialog.result_mesh_name}
            self.target_bodies.append(entry)
            self._refresh_target_list_widget()

    def _on_remove_target_body(self):
        selected_rows = sorted((i.row() for i in self.target_list_widget.selectedIndexes()), reverse=True)
        for row in selected_rows:
            del self.target_bodies[row]
        self._refresh_target_list_widget()

    def _refresh_target_list_widget(self):
        self.target_list_widget.clear()
        for entry in self.target_bodies:
            self.target_list_widget.addItem("%s  (%s)" % (entry["display_name"], entry["mesh"]))

    # ------------------------------------------------------------------
    # Clothing list
    # ------------------------------------------------------------------
    def _on_add_selected_clothing(self):
        selection = cmds.ls(selection=True) or []
        added = 0
        for node in selection:
            if utils.node_exists_and_is_mesh(node) and node not in self.clothing_items:
                self.clothing_items.append(node)
                added += 1
        if added == 0:
            QtWidgets.QMessageBox.information(
                self, "Nothing Added", "No new valid mesh selections found."
            )
        self._refresh_clothing_list_widget()

    def _on_remove_selected_clothing(self):
        selected_names = {item.text() for item in self.clothing_list_widget.selectedItems()}
        self.clothing_items = [c for c in self.clothing_items if c not in selected_names]
        self._refresh_clothing_list_widget()

    def _on_load_clothing_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Clothing Reference Folder")
        if not folder:
            return
        # A "clothing folder" typically holds .ma/.mb reference files to
        # import, OR the user may simply want every top-level mesh
        # transform whose scene file lives in that folder already
        # referenced into the scene. We support both: if the folder
        # contains Maya scene files, they are imported; any resulting
        # (or already-existing) top-level meshes are added to the list.
        supported_ext = (".ma", ".mb")
        imported_any = False
        for filename in sorted(os.listdir(folder)):
            if filename.lower().endswith(supported_ext):
                file_path = os.path.join(folder, filename)
                try:
                    cmds.file(file_path, i=True, mergeNamespacesOnClash=True,
                              namespace=os.path.splitext(filename)[0],
                              preserveReferences=True)
                    imported_any = True
                except RuntimeError as exc:
                    self.logger.error("Could not import '%s': %s" % (file_path, exc))

        if imported_any:
            all_meshes = cmds.ls(type="mesh", long=False) or []
            transforms = set()
            for shape in all_meshes:
                parents = cmds.listRelatives(shape, parent=True) or []
                transforms.update(parents)
            for t in sorted(transforms):
                if t not in self.clothing_items:
                    self.clothing_items.append(t)
        else:
            QtWidgets.QMessageBox.information(
                self, "No Scene Files Found",
                "No .ma/.mb files were found in that folder. "
                "Select meshes in the scene and use 'Add Selected Clothing' instead."
            )
        self._refresh_clothing_list_widget()

    def _on_clear_clothing_list(self):
        self.clothing_items = []
        self._refresh_clothing_list_widget()

    def _filter_clothing_list(self, text):
        text = text.lower()
        for i in range(self.clothing_list_widget.count()):
            item = self.clothing_list_widget.item(i)
            item.setHidden(text not in item.text().lower())

    def _refresh_clothing_list_widget(self):
        self.clothing_list_widget.clear()
        for name in self.clothing_items:
            self.clothing_list_widget.addItem(name)

    # ------------------------------------------------------------------
    # Output folder
    # ------------------------------------------------------------------
    def _on_browse_output_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if folder:
            self.output_folder_edit.setText(folder)

    # ------------------------------------------------------------------
    # Settings persistence (Save/Load project settings as JSON)
    # ------------------------------------------------------------------
    def _gather_settings(self):
        return {
            "base_body": self.base_body_mesh,
            "target_bodies": self.target_bodies,
            "clothing_items": self.clothing_items,
            "output_folder": self.output_folder_edit.text(),
            "options": self._gather_options(),
        }

    def _apply_settings(self, data):
        self.base_body_mesh = data.get("base_body")
        if self.base_body_mesh:
            self.base_body_label.setText(self.base_body_mesh)
            self.base_body_label.setStyleSheet("color: black;")
        self.target_bodies = data.get("target_bodies", [])
        self.clothing_items = data.get("clothing_items", [])
        self.output_folder_edit.setText(data.get("output_folder", "") or "")

        options = data.get("options", {})
        self.chk_delete_history.setChecked(options.get("delete_history", True))
        self.chk_freeze_transforms.setChecked(options.get("freeze_transforms", True))
        self.chk_center_pivot.setChecked(options.get("center_pivot", True))
        self.chk_transfer_skin.setChecked(options.get("transfer_skin_weights", False))
        self.chk_export_fbx.setChecked(options.get("export_fbx", True))
        self.chk_overwrite.setChecked(options.get("overwrite_existing", False))
        self.chk_keep_clean.setChecked(options.get("keep_scene_clean", True))

        self._refresh_target_list_widget()
        self._refresh_clothing_list_widget()

    def _default_settings_path(self):
        user_dir = cmds.internalVar(userAppDir=True)
        return os.path.join(user_dir, config.DEFAULT_SETTINGS_FILENAME)

    def _save_settings_dialog(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save Settings", self._default_settings_path(), config.SETTINGS_FILE_FILTER
        )
        if not path:
            return
        try:
            with open(path, "w") as handle:
                json.dump(self._gather_settings(), handle, indent=2)
            self.logger.info("Settings saved to %s" % path)
        except OSError as exc:
            QtWidgets.QMessageBox.critical(self, "Save Failed", str(exc))

    def _load_settings_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load Settings", self._default_settings_path(), config.SETTINGS_FILE_FILTER
        )
        if not path:
            return
        self._load_settings(path=path)

    def _load_settings(self, path=None, silent=False):
        path = path or self._default_settings_path()
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r") as handle:
                data = json.load(handle)
            self._apply_settings(data)
            if not silent:
                self.logger.info("Settings loaded from %s" % path)
        except (OSError, ValueError) as exc:
            if not silent:
                QtWidgets.QMessageBox.critical(self, "Load Failed", str(exc))

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------
    def _gather_options(self):
        return {
            "delete_history": self.chk_delete_history.isChecked(),
            "freeze_transforms": self.chk_freeze_transforms.isChecked(),
            "center_pivot": self.chk_center_pivot.isChecked(),
            "transfer_skin_weights": self.chk_transfer_skin.isChecked(),
            "export_fbx": self.chk_export_fbx.isChecked(),
            "overwrite_existing": self.chk_overwrite.isChecked(),
            "keep_scene_clean": self.chk_keep_clean.isChecked(),
        }

    # ------------------------------------------------------------------
    # Live processor (shared by manual Deformation Controls and the
    # batch Generate button).
    # ------------------------------------------------------------------
    def _ensure_live_processor(self, for_generate=False):
        if self.live_processor is None or self.live_processor.base_body != self.base_body_mesh:
            self.live_processor = ClothingVariantProcessor(self.base_body_mesh, self.logger, self.exporter)

        if for_generate:
            # Batch Generate stays byte-for-byte identical to the fully
            # automatic behaviour UNLESS the artist explicitly opts in
            # via this checkbox.
            self.live_processor.deform_state = (
                self.deform_state if self.chk_apply_deform_to_generate.isChecked() else None
            )
        else:
            self.live_processor.deform_state = self.deform_state
        return self.live_processor

    # ------------------------------------------------------------------
    # Generate / batch processing
    # ------------------------------------------------------------------
    def _on_generate_clicked(self):
        if self.batch_runner is not None and not self.batch_runner.is_finished():
            return  # already running

        errors = self._validate_inputs()
        if errors:
            QtWidgets.QMessageBox.warning(self, "Cannot Start", "\n".join(errors))
            return

        options = self._gather_options()
        output_folder = self.output_folder_edit.text().strip() or None

        if options["export_fbx"] and not output_folder:
            QtWidgets.QMessageBox.warning(
                self, "Missing Output Folder",
                "Export FBX is enabled but no output folder is set."
            )
            return

        if options["export_fbx"] and not options["overwrite_existing"] and output_folder:
            if self._any_export_would_collide(output_folder):
                proceed = QtWidgets.QMessageBox.question(
                    self, "Files Already Exist",
                    "Some output files already exist and 'Overwrite Existing Files' "
                    "is disabled. Those clothing/body combinations will be skipped. Continue?",
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
                )
                if proceed != QtWidgets.QMessageBox.Yes:
                    return

        self.logger.clear()
        self.log_view.clear()
        if output_folder:
            self.logger.set_log_file(output_folder)
        self.logger.info("Started...")

        processor = self._ensure_live_processor(for_generate=True)
        self.batch_runner = BatchRunner(
            processor, list(self.clothing_items), list(self.target_bodies),
            options, output_folder, self.logger
        )

        self.generate_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.pause_btn.setEnabled(True)
        self.resume_btn.setEnabled(False)
        self.retry_failed_btn.setEnabled(False)
        self.progress_bar.setValue(0)
        self.batch_start_time = time.time()
        self.process_timer.start()

    def _any_export_would_collide(self, output_folder):
        for clothing in self.clothing_items:
            for target in self.target_bodies:
                variant_folder = utils.sanitize_folder_component(target["display_name"])
                base_name = utils.strip_namespace(clothing)
                candidate = "%s_%s" % (base_name, variant_folder)
                path = os.path.join(output_folder, variant_folder, candidate + config.EXPORT_EXTENSION)
                if os.path.exists(path):
                    return True
        return False

    def _validate_inputs(self):
        errors = []
        if not self.base_body_mesh or not cmds.objExists(self.base_body_mesh):
            errors.append("Select a valid Base Body mesh.")
        if not self.target_bodies:
            errors.append("Add at least one Target Body.")
        if not self.clothing_items:
            errors.append("Add at least one Clothing mesh.")
        return errors

    def _on_timer_tick(self):
        if self.batch_runner is None:
            self.process_timer.stop()
            return

        if self.batch_runner.cancelled:
            self._finish_batch(cancelled=True)
            return

        if self.batch_runner.is_paused():
            self.elapsed_label.setText("Elapsed: %s (paused)" % self._format_seconds(self.batch_runner.elapsed_seconds()))
            return

        result = self.batch_runner.run_next()
        if result is not None:
            self.current_clothing_label.setText("Clothing: %s" % result.clothing)
            self.current_body_label.setText("Body Variant: %s" % result.target_display_name)
            self.vertex_count_label.setText("Vertices: %d" % self.batch_runner.current_vertex_count())

        if self.batch_runner.is_finished():
            self._finish_batch(cancelled=False)
            return

        fraction = self.batch_runner.progress_fraction()
        self.progress_bar.setValue(int(fraction * 100))
        eta = self.batch_runner.eta_seconds()
        self.eta_label.setText("ETA: %s" % self._format_seconds(eta) if eta is not None else "ETA: -")
        stats = self.batch_runner.stats_summary()
        self.stats_label.setText("Processed: %d / %d" % (stats["processed"], stats["total"]))
        self.speed_label.setText("Speed: %.0f verts/s" % self.batch_runner.processing_speed())
        self.elapsed_label.setText("Elapsed: %s" % self._format_seconds(self.batch_runner.elapsed_seconds()))

    def _finish_batch(self, cancelled):
        self.process_timer.stop()
        stats = self.batch_runner.stats_summary() if self.batch_runner else {}
        if cancelled:
            self.logger.warning("Cancelled by user. Processed %d / %d." %
                                 (stats.get("processed", 0), stats.get("total", 0)))
        else:
            self.logger.info(
                "Completed. Succeeded: %d, Failed: %d, Total time: %.1fs" % (
                    stats.get("succeeded", 0), stats.get("failed", 0), stats.get("total_time", 0.0)
                )
            )
        if self._gather_options().get("keep_scene_clean", True):
            utils.remove_empty_namespaces()

        self.progress_bar.setValue(100 if not cancelled else self.progress_bar.value())
        self.generate_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.resume_btn.setEnabled(False)
        self.retry_failed_btn.setEnabled(bool(stats.get("failed", 0)))
        self.current_clothing_label.setText("Clothing: -")
        self.current_body_label.setText("Body Variant: -")
        self.eta_label.setText("ETA: -")

    def _on_pause_clicked(self):
        if self.batch_runner:
            self.batch_runner.pause()
            self.pause_btn.setEnabled(False)
            self.resume_btn.setEnabled(True)

    def _on_resume_clicked(self):
        if self.batch_runner:
            self.batch_runner.resume()
            self.pause_btn.setEnabled(True)
            self.resume_btn.setEnabled(False)

    def _on_retry_failed_clicked(self):
        if not self.batch_runner:
            return
        count = self.batch_runner.retry_failed()
        if count == 0:
            QtWidgets.QMessageBox.information(self, "Retry Failed Items", "No failed items to retry.")
            return
        self.logger.info("Retrying %d failed item(s)..." % count)
        self.generate_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.pause_btn.setEnabled(True)
        self.retry_failed_btn.setEnabled(False)
        self.batch_start_time = time.time()
        self.process_timer.start()

    def _on_cancel(self):
        if self.batch_runner:
            self.batch_runner.cancel()
            self.cancel_btn.setEnabled(False)

    @staticmethod
    def _format_seconds(seconds):
        if seconds is None:
            return "-"
        seconds = int(max(0, seconds))
        minutes, secs = divmod(seconds, 60)
        return "%dm %02ds" % (minutes, secs) if minutes else "%ds" % secs

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def _append_log_line(self, line):
        self.log_view.append(line)
        cursor = self.log_view.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        self.log_view.setTextCursor(cursor)

    # ------------------------------------------------------------------
    # Deformation Controls: sliders -> DeformationState. Values are
    # only ever evaluated by Generate (if the artist opts in via the
    # checkbox above), so there is no live recompute to debounce.
    # ------------------------------------------------------------------
    def _on_deform_value_changed(self, key, value):
        setter = {
            "global_influence": self.deform_state.set_global_influence,
            "surface_offset": self.deform_state.set_surface_offset,
            "smoothing": self.deform_state.set_smoothing,
            "falloff": self.deform_state.set_falloff,
        }[key]
        setter(value)

    def _on_reset_deform_clicked(self):
        self.deform_state.reset_to_defaults()
        d = config.DEFORM_DEFAULTS
        self.deform_widgets["global_influence"][1].setValue(d["global_influence"])
        self.deform_widgets["surface_offset"][1].setValue(d["surface_offset"])
        self.deform_widgets["smoothing"][1].setValue(d["smoothing"])
        self.deform_widgets["falloff"][1].setValue(d["falloff"])


# ---------------------------------------------------------------------------
# Show helper
# ---------------------------------------------------------------------------
_window_instance = None


def show():
    """
    Open (or bring forward) the dockable Clothing Variant Generator
    window.

    No ``uiScript`` is ever passed to ``self.show(dockable=True, ...)``,
    so nothing is auto-restored by Maya's saved UI layout on startup --
    the ONLY way to open this window is calling this function yourself:

        import clothing_variant_generator.main as cvg_main
        cvg_main.show()

    Running this multiple times does not tear down and rebuild the
    window every time. If a live window instance already exists, it is
    simply brought to front / restored (un-minimized) instead of
    creating a duplicate.
    """
    global _window_instance

    workspace_control = config.PLUGIN_OBJECT_NAME + "WorkspaceControl"

    if _window_instance is not None:
        try:
            if cmds.workspaceControl(workspace_control, exists=True):
                cmds.workspaceControl(workspace_control, edit=True, restore=True)
                return _window_instance
        except RuntimeError:
            pass
        # The Python object survived but its Maya-side control didn't
        # (e.g. someone force-deleted it) -- clean up and rebuild below
        # rather than return a window with no backing UI.
        _window_instance._cleanup()
        _window_instance = None

    if cmds.workspaceControl(workspace_control, exists=True):
        # A leftover, ownerless control (e.g. from a Maya UI-layout
        # restore that recreated the empty control shell but has no
        # Python object behind it since we never register a uiScript
        # anymore) -- remove it before building a fresh, fully wired-up
        # window rather than adopt an orphaned one.
        cmds.deleteUI(workspace_control)

    _window_instance = ClothingVariantGeneratorWindow()
    # floating=True opens it as a normal standalone window (its own
    # title bar, draggable/resizable independently of Maya's main
    # window). dockable=True still lets the artist drag it onto the
    # right dock area later if they want it tabbed there -- but it
    # will not auto-attach to whatever top/side strip Maya guesses at
    # on first launch, which is what floating=False was doing.
    _window_instance.show(dockable=True, floating=True, area="right")
    workspace_control = config.PLUGIN_OBJECT_NAME + "WorkspaceControl"
    try:
        cmds.workspaceControl(
            workspace_control, edit=True,
            initialWidth=900, initialHeight=850,
            minimumWidth=700,
        )
    except RuntimeError:
        pass
    return _window_instance
