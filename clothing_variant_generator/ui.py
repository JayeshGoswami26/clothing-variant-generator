# -*- coding: utf-8 -*-
"""
ui.py
-----
Dockable Qt UI for the Clothing Variant Generator.
Works with PySide2 (Maya 2022-2024) and PySide6 (Maya 2025+).

The UI never touches Maya's scene graph directly for the heavy lifting --
it only gathers user intent (base body, target bodies, clothing list,
output folder, options, deformation sliders) and hands one task at a time
to processor.BatchRunner, driven by a QTimer so:

    * the Maya main thread (where all cmds calls must run) stays free,
    * the progress bar / log / ETA update between every asset,
    * Cancel takes effect immediately between tasks.

True background threading is intentionally NOT used: maya.cmds is not
thread-safe, so "UI-safe progress updates" here means "never block the
event loop for the whole batch", not "run Maya commands off-thread".

WINDOW BEHAVIOUR
----------------
The tool opens as a genuine FREE-FLOATING top-level window owned by
Maya's main window -- NOT a workspaceControl. It opens at 900x850,
resizes freely down to a narrow strip (380x300), and reopens wherever
the artist last left it.

That distinction is the whole point: a workspaceControl persists its
dock position in Maya's workspace prefs and restores it whenever a
control of the same name is recreated, overriding ``floating=True``.
That is how this tool ended up docked across the top of Maya eating
half the screen. A plain window cannot be re-docked behind the artist's
back.

Docking is opt-in via the "Open Docked" checkbox, the only path that
creates a workspaceControl (docked vertically, right-hand side). No
``uiScript`` is ever registered either way, so Maya never resurrects
this window on startup by itself.

The entire interface lives inside a QScrollArea so every section stays
reachable (and nothing is ever clipped) no matter how small the window
is made or how narrow a dock it sits in.
"""

import os
import json

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
    class MayaQWidgetDockableMixin(object):
        """
        Fallback so this module stays importable outside Maya (docs,
        linting, unit tests). Swallows the dock-specific keywords the
        real mixin understands so `show()` still works as a plain
        QWidget.
        """

        def show(self, *args, **kwargs):
            for key in ("dockable", "floating", "area", "restore",
                        "retain", "uiScript", "width", "height"):
                kwargs.pop(key, None)
            super(MayaQWidgetDockableMixin, self).show(*args, **kwargs)

from . import config
from . import utils
from .logger import VariantLogger
from .processor import ClothingVariantProcessor, BatchRunner
from .exporter import FBXExporter
from .deform_state import DeformationState


def _exec_dialog(dialog):
    """
    Run a modal dialog across Qt versions.

    PySide2 spells this `exec_()`; PySide6 renamed it to `exec()` and
    deprecated the underscore alias.
    """
    runner = getattr(dialog, "exec", None)
    if runner is None:
        runner = dialog.exec_
    return runner()


def maya_main_window():
    """
    Return Maya's main window as a QWidget, or None outside Maya.

    Used as the parent for the floating window so it behaves like a
    proper Maya tool: it stays in front of Maya, follows Maya's
    minimise/restore, and dies with Maya -- while still being freely
    movable and resizable anywhere on any monitor.
    """
    try:
        import maya.OpenMayaUI as omui
    except ImportError:
        return None

    try:
        from shiboken6 import wrapInstance      # Maya 2025+ (Qt6)
    except ImportError:
        try:
            from shiboken2 import wrapInstance  # Maya 2022-2024 (Qt5)
        except ImportError:
            return None

    try:
        pointer = omui.MQtUtil.mainWindow()
        if pointer is None:
            return None
        return wrapInstance(int(pointer), QtWidgets.QWidget)
    except (RuntimeError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Small reusable dialog: Add Target Body
# ---------------------------------------------------------------------------
class AddTargetBodyDialog(QtWidgets.QDialog):
    """Dialog for adding a new target body: pick a preset or type a custom
    display name, then assign a mesh from the current Maya selection."""

    NO_MESH_TEXT = "<no mesh selected>"

    def __init__(self, parent=None):
        super(AddTargetBodyDialog, self).__init__(parent)
        self.setWindowTitle("Add Target Body")
        self.setMinimumWidth(360)

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
        self.mesh_label = QtWidgets.QLabel(self.NO_MESH_TEXT)
        self.mesh_label.setEnabled(False)
        select_btn = QtWidgets.QPushButton("Use Selected Mesh")
        select_btn.setFixedHeight(28)
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
            QtWidgets.QMessageBox.warning(self, "No Selection",
                                          "Select a mesh in the scene first.")
            return
        candidate = selection[0]
        if not utils.node_exists_and_is_mesh(candidate):
            QtWidgets.QMessageBox.warning(self, "Invalid Selection",
                                          "'%s' is not a polygon mesh." % candidate)
            return
        self.mesh_label.setText(candidate)
        self.mesh_label.setEnabled(True)

    def _on_accept(self):
        is_custom = self.preset_combo.currentText() == "Custom..."
        display_name = (self.custom_name_edit.text().strip() if is_custom
                        else self.preset_combo.currentText())

        if not display_name:
            QtWidgets.QMessageBox.warning(self, "Missing Name",
                                          "Please provide a body type name.")
            return
        if self.mesh_label.text() == self.NO_MESH_TEXT:
            QtWidgets.QMessageBox.warning(self, "Missing Mesh",
                                          "Please assign a mesh for this body.")
            return

        self.result_display_name = display_name
        self.result_mesh_name = self.mesh_label.text()
        self.accept()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class ClothingVariantGeneratorWindow(MayaQWidgetDockableMixin, QtWidgets.QWidget):

    NO_BASE_TEXT = "<none selected>"

    def __init__(self, parent=None):
        super(ClothingVariantGeneratorWindow, self).__init__(parent=parent)
        self.setObjectName(config.PLUGIN_OBJECT_NAME)
        self.setWindowTitle("%s v%s" % (config.PLUGIN_NAME, config.PLUGIN_VERSION))
        self.setMinimumSize(config.WINDOW_MIN_WIDTH, config.WINDOW_MIN_HEIGHT)
        self.resize(config.WINDOW_DEFAULT_WIDTH, config.WINDOW_DEFAULT_HEIGHT)

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

        # Manual Deformation Controls. At their defaults these are an
        # exact no-op, so the automatic result is what an artist gets
        # unless they deliberately move a slider.
        self.deform_state = DeformationState()
        self.live_processor = None  # lazily (re)built in _ensure_processor()

        # Last known floating window geometry, persisted to settings.
        self._window_geometry = None

        # scriptJobs this window registers, so clean shutdown can remove
        # every one on close -- nothing persistent should survive this
        # window closing.
        self._script_job_ids = []
        self._cleaned_up = False

        self._build_ui()
        self._load_settings(silent=True)
        self._register_scene_scriptjobs()

    # ------------------------------------------------------------------
    # Startup stability, single-instance management, clean shutdown
    # ------------------------------------------------------------------
    def _register_scene_scriptjobs(self):
        """
        Track every scriptJob this window registers so shutdown can kill
        them all by id -- nothing should survive after the window is
        closed, and NOTHING here runs unless this window was actually
        constructed by an explicit `cvg_main.show()` call: this method is
        only ever invoked from __init__.
        """
        # If the scene is closed/opened out from under us, our cached
        # mesh names are no longer valid -- drop them defensively rather
        # than risk operating on stale/renamed nodes.
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
            # Let the timer tick notice and shut the batch down through
            # the normal path, so the viewport and buttons are restored.
            self.batch_runner.cancel()
        utils.resume_viewport(self.logger)

        if self.live_processor is not None:
            self.live_processor.release_all()
        self.live_processor = None

        self.base_body_mesh = None
        self.target_bodies = []
        self.clothing_items = []
        self.base_body_label.setText(self.NO_BASE_TEXT)
        self.base_body_label.setEnabled(False)
        self._refresh_target_list_widget()
        self._refresh_clothing_list_widget()
        self.logger.info("Scene changed -- body/clothing selections were cleared.")

    def dockCloseEventTriggered(self):
        """
        MayaQWidgetDockableMixin's documented hook for a DOCKED window
        being closed (the workspaceControl's [x], or
        `workspaceControl -close`). This is NOT a Qt Signal to
        `.connect()` to -- Maya calls this method directly on the
        instance. Overriding the method is the correct, version-proof
        mechanism; `QWidget.closeEvent` below covers the FLOATING-window
        case, and together the two ensure cleanup runs no matter how the
        artist closes the window.
        """
        self._cleanup()

    def closeEvent(self, event):
        self._cleanup()
        super(ClothingVariantGeneratorWindow, self).closeEvent(event)

    def _cleanup(self, *_args):
        """
        Release EVERYTHING this window could have created or registered --
        timers, scriptJobs, the batch runner, cached bind data, a
        suspended viewport -- so closing the window (or Maya quitting)
        never leaves a dangling callback/timer/frozen viewport behind.
        Idempotent: safe to call more than once (close + quit, etc).
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

        # A batch interrupted by the window closing must not leave the
        # viewport frozen for the rest of the Maya session.
        utils.resume_viewport()

        if self.batch_runner is not None and not self.batch_runner.is_finished():
            self.batch_runner.cancel()
        self.batch_runner = None

        if self.live_processor is not None:
            self.live_processor.release_all()
        self.live_processor = None

        for job_id in self._script_job_ids:
            try:
                if cmds.scriptJob(exists=job_id):
                    cmds.scriptJob(kill=job_id, force=True)
            except RuntimeError:
                pass
        self._script_job_ids = []

        # Remember where the artist left the window, plus their options
        # and sliders, without them having to press Save Settings.
        # Broadly guarded: this runs while Maya is quitting, where any
        # Qt/Maya call may find its underlying object already torn down,
        # and nothing here is worth interrupting a shutdown for.
        try:
            self._capture_window_geometry()
            self._save_settings(self._default_settings_path(), silent=True)
        except Exception:  # noqa: BLE001 - shutdown must never raise
            pass

        global _window_instance
        if _window_instance is self:
            _window_instance = None

    # ------------------------------------------------------------------
    # Window geometry / docking
    # ------------------------------------------------------------------
    @staticmethod
    def _workspace_control_exists():
        try:
            return bool(cmds.workspaceControl(config.WORKSPACE_CONTROL_NAME, exists=True))
        except RuntimeError:
            return False

    def is_floating(self):
        """
        True when the window is free-floating (the default).

        No workspaceControl means this is a plain top-level window, which
        is always floating -- that is the default path.

        Geometry is only meaningful -- and only saved -- while floating:
        when docked, `self.window()` is Maya's main window, and saving
        THAT geometry would restore the tool at Maya-main-window size.
        """
        if not self._workspace_control_exists():
            return True
        try:
            return bool(cmds.workspaceControl(
                config.WORKSPACE_CONTROL_NAME, query=True, floating=True))
        except RuntimeError:
            return True

    def open_docked_preference(self):
        return self.chk_open_docked.isChecked()

    def _capture_window_geometry(self):
        """Store the current floating geometry, or keep the last known one."""
        if not self.is_floating():
            return self._window_geometry
        try:
            geo = self.window().geometry()
            self._window_geometry = {
                "x": geo.x(), "y": geo.y(),
                "width": geo.width(), "height": geo.height(),
            }
        except RuntimeError:
            pass
        return self._window_geometry

    @staticmethod
    def _geometry_is_reachable(x, y, width):
        """
        True if a window placed here would have its title bar on a
        connected monitor.

        An artist who undocks a laptop, or changes their monitor layout,
        would otherwise reopen the tool at coordinates that no longer
        exist on any screen -- the window opens invisibly off-screen and
        looks like the plugin is broken.
        """
        try:
            screens = QtGui.QGuiApplication.screens()
        except (RuntimeError, AttributeError):
            return True
        if not screens:
            return True
        title_bar_point = QtCore.QPoint(int(x + width / 2), int(y + 12))
        return any(s.availableGeometry().contains(title_bar_point) for s in screens)

    def apply_saved_geometry(self):
        """
        Restore the saved floating position/size.

        Deferred by show() until after Maya has built the workspaceControl,
        because until then `self.window()` is not yet the floating panel
        we want to move.
        """
        geo = self._window_geometry
        if not geo or not self.is_floating():
            return
        try:
            width = max(int(geo.get("width", config.WINDOW_DEFAULT_WIDTH)),
                        config.WINDOW_MIN_WIDTH)
            height = max(int(geo.get("height", config.WINDOW_DEFAULT_HEIGHT)),
                         config.WINDOW_MIN_HEIGHT)
            x = int(geo.get("x", 0))
            y = int(geo.get("y", 0))
        except (TypeError, ValueError):
            return

        try:
            window = self.window()
            if self._geometry_is_reachable(x, y, width):
                window.setGeometry(x, y, width, height)
            else:
                # Keep the remembered size, drop the unreachable position.
                window.resize(width, height)
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        # Root layout holds nothing but the scroll area, so every section
        # below scrolls instead of being squeezed or clipped when the
        # window is small or docked into a narrow column.
        outer_layout = QtWidgets.QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll_area = QtWidgets.QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QtWidgets.QFrame.NoFrame)
        outer_layout.addWidget(scroll_area)

        scroll_content = QtWidgets.QWidget()
        main_layout = QtWidgets.QVBoxLayout(scroll_content)
        main_layout.setSpacing(8)

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

        self.chk_open_docked = QtWidgets.QCheckBox("Open Docked")
        self.chk_open_docked.setChecked(config.WINDOW_OPEN_DOCKED_DEFAULT)
        self.chk_open_docked.setToolTip(
            "Next time this window is opened, dock it into the right-hand side\n"
            "of Maya instead of opening it as a free-floating window.\n\n"
            "Off (default) is a normal movable, resizable window that never\n"
            "takes space away from the viewport."
        )
        row.addWidget(self.chk_open_docked)
        return row

    def _build_body_section(self):
        group = QtWidgets.QGroupBox("Body")
        layout = QtWidgets.QVBoxLayout(group)

        base_row = QtWidgets.QHBoxLayout()
        base_row.addWidget(QtWidgets.QLabel("Base Body:"))
        self.base_body_label = QtWidgets.QLabel(self.NO_BASE_TEXT)
        self.base_body_label.setEnabled(False)
        select_base_btn = QtWidgets.QPushButton("Select")
        select_base_btn.setFixedHeight(28)
        select_base_btn.setToolTip("Use the mesh currently selected in Maya as the Base body.")
        select_base_btn.clicked.connect(self._on_select_base_body)
        base_row.addWidget(self.base_body_label, 1)
        base_row.addWidget(select_base_btn)
        layout.addLayout(base_row)

        layout.addWidget(self._hline())

        layout.addWidget(QtWidgets.QLabel("Target Bodies:"))
        self.target_list_widget = QtWidgets.QListWidget()
        self.target_list_widget.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        # Modest minimum: enough to be usable, small enough that a shrunk
        # window scrolls rather than being forced tall by the lists.
        self.target_list_widget.setMinimumHeight(70)
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
        # Drag to reorder processing order. NOTE: dragging mesh nodes in
        # from Maya's Outliner is not supported by Maya's Qt/MEL bridge,
        # so this only reorders items already in the list.
        self.clothing_list_widget.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.clothing_list_widget.model().rowsMoved.connect(self._on_clothing_rows_moved)
        self.clothing_list_widget.setMinimumHeight(70)
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
        load_folder_btn.setToolTip("Import every .ma/.mb file in a folder and add its meshes.")
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
        self.output_folder_edit.setPlaceholderText(
            "Each body variant gets its own subfolder here")
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
        self.chk_delete_history.setToolTip("Bake the result down to plain vertex positions.")
        self.chk_freeze_transforms = QtWidgets.QCheckBox("Freeze Transforms")
        self.chk_center_pivot = QtWidgets.QCheckBox("Center Pivot")
        self.chk_transfer_skin = QtWidgets.QCheckBox("Transfer Skin Weights")
        self.chk_transfer_skin.setToolTip(
            "Copy the original clothing's skinCluster weights onto each variant.\n"
            "Requires the original clothing mesh to be skinned already."
        )
        self.chk_export_fbx = QtWidgets.QCheckBox("Export FBX")
        self.chk_overwrite = QtWidgets.QCheckBox("Overwrite Existing Files")
        self.chk_keep_clean = QtWidgets.QCheckBox("Keep Scene Clean")
        self.chk_keep_clean.setToolTip(
            "Delete temporary nodes and empty namespaces created during processing.")

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
    # Deformation Controls
    # ------------------------------------------------------------------
    def _make_float_slider_row(self, layout, label_text, tooltip, lo, hi,
                               default, decimals, on_change):
        """A labelled float slider + spin box, kept in sync both ways."""
        row = QtWidgets.QHBoxLayout()
        label = QtWidgets.QLabel(label_text)
        label.setMinimumWidth(120)
        label.setToolTip(tooltip)
        row.addWidget(label, 0)

        slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        slider.setRange(0, 1000)
        slider.setToolTip(tooltip)

        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(lo, hi)
        spin.setDecimals(decimals)
        spin.setSingleStep((hi - lo) / 100.0 or 0.01)
        spin.setValue(default)
        spin.setToolTip(tooltip)

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

    def _make_int_slider_row(self, layout, label_text, tooltip, lo, hi, default, on_change):
        """A labelled integer slider + spin box, kept in sync both ways."""
        row = QtWidgets.QHBoxLayout()
        label = QtWidgets.QLabel(label_text)
        label.setMinimumWidth(120)
        label.setToolTip(tooltip)
        row.addWidget(label, 0)

        slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(default)
        slider.setToolTip(tooltip)

        spin = QtWidgets.QSpinBox()
        spin.setRange(lo, hi)
        spin.setValue(default)
        spin.setToolTip(tooltip)

        def _on_slider_moved(v):
            spin.blockSignals(True)
            spin.setValue(v)
            spin.blockSignals(False)
            on_change(v)

        def _on_spin_changed(v):
            slider.blockSignals(True)
            slider.setValue(v)
            slider.blockSignals(False)
            on_change(v)

        slider.valueChanged.connect(_on_slider_moved)
        spin.valueChanged.connect(_on_spin_changed)

        row.addWidget(slider, 1)
        row.addWidget(spin, 0)
        layout.addLayout(row)
        return slider, spin

    def _build_deformation_controls_section(self):
        group = QtWidgets.QGroupBox("Deformation Controls")
        outer = QtWidgets.QVBoxLayout(group)

        hint = QtWidgets.QLabel(
            "Optional. At their default values the generated clothing is "
            "exactly the automatic fit."
        )
        hint.setWordWrap(True)
        hint.setEnabled(False)
        outer.addWidget(hint)

        self.deform_widgets = {}
        d = config.DEFORM_DEFAULTS

        lo, hi = config.DEFORM_RANGES["global_influence"]
        self.deform_widgets["global_influence"] = self._make_float_slider_row(
            outer, "Global Body Influence",
            "How strongly the clothing follows the target body's shape.\n"
            "1.0 = the automatic fit. Below 1.0 hugs the body more; above 1.0 exaggerates.",
            lo, hi, d["global_influence"], 2,
            lambda v: self.deform_state.set_global_influence(v))

        lo, hi = config.DEFORM_RANGES["surface_offset"]
        self.deform_widgets["surface_offset"] = self._make_float_slider_row(
            outer, "Surface Offset",
            "Push the clothing out along the body's surface normal.\n"
            "Use a small positive value to fix clothing poking through the body.",
            lo, hi, d["surface_offset"], 2,
            lambda v: self.deform_state.set_surface_offset(v))

        lo, hi = config.DEFORM_RANGES["smooth_iterations"]
        self.deform_widgets["smooth_iterations"] = self._make_int_slider_row(
            outer, "Smooth Iterations",
            "Relax passes applied to the fitted clothing.\n"
            "Softens pinching on extreme body shapes. 0 = no smoothing.",
            lo, hi, d["smooth_iterations"],
            lambda v: self.deform_state.set_smooth_iterations(v))

        reset_row = QtWidgets.QHBoxLayout()
        reset_btn = QtWidgets.QPushButton("Reset Sliders To Default")
        reset_btn.setFixedHeight(28)
        reset_btn.clicked.connect(self._on_reset_deform_clicked)
        reset_row.addWidget(reset_btn)
        reset_row.addStretch()
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
        status_row.addWidget(self.current_clothing_label, 1)
        status_row.addWidget(self.current_body_label, 1)
        layout.addLayout(status_row)

        stats_row = QtWidgets.QHBoxLayout()
        self.stats_label = QtWidgets.QLabel("Processed: 0 / 0")
        self.eta_label = QtWidgets.QLabel("ETA: -")
        self.elapsed_label = QtWidgets.QLabel("Elapsed: -")
        stats_row.addWidget(self.stats_label, 1)
        stats_row.addWidget(self.eta_label, 1)
        stats_row.addWidget(self.elapsed_label, 1)
        layout.addLayout(stats_row)

        button_row = QtWidgets.QHBoxLayout()
        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.pause_btn.setFixedHeight(28)
        self.pause_btn.clicked.connect(self._on_pause_clicked)
        self.resume_btn = QtWidgets.QPushButton("Resume")
        self.resume_btn.setFixedHeight(28)
        self.resume_btn.clicked.connect(self._on_resume_clicked)
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.setFixedHeight(28)
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.retry_failed_btn = QtWidgets.QPushButton("Retry Failed Items")
        self.retry_failed_btn.setFixedHeight(28)
        self.retry_failed_btn.clicked.connect(self._on_retry_failed_clicked)
        self.pause_btn.setEnabled(False)
        self.resume_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.retry_failed_btn.setEnabled(False)
        button_row.addWidget(self.pause_btn)
        button_row.addWidget(self.resume_btn)
        button_row.addWidget(self.cancel_btn)
        button_row.addWidget(self.retry_failed_btn)
        layout.addLayout(button_row)

        layout.addWidget(QtWidgets.QLabel("Log:"))
        self.log_view = QtWidgets.QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(90)
        self.log_view.setFontFamily("Courier")
        # Keep the widget's document bounded; log.txt on disk keeps every
        # line, but a huge in-memory document makes Maya feel sluggish.
        self.log_view.document().setMaximumBlockCount(config.LOG_VIEW_MAX_LINES)
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

    @staticmethod
    def _hline():
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
            QtWidgets.QMessageBox.warning(self, "No Selection",
                                          "Select the base body mesh first.")
            return
        candidate = selection[0]
        if not utils.node_exists_and_is_mesh(candidate):
            QtWidgets.QMessageBox.warning(self, "Invalid Selection",
                                          "'%s' is not a polygon mesh." % candidate)
            return
        self.base_body_mesh = candidate
        self.base_body_label.setText(candidate)
        self.base_body_label.setEnabled(True)
        # Bind data is relative to the base body, so it is now stale.
        if self.live_processor is not None:
            self.live_processor.release_all()
        self.live_processor = None
        self.logger.info("Base body set to '%s'." % candidate)

    # ------------------------------------------------------------------
    # Target bodies
    # ------------------------------------------------------------------
    def _on_add_target_body(self):
        dialog = AddTargetBodyDialog(self)
        if _exec_dialog(dialog) == QtWidgets.QDialog.Accepted:
            self.target_bodies.append({
                "display_name": dialog.result_display_name,
                "mesh": dialog.result_mesh_name,
            })
            self._refresh_target_list_widget()
            self.logger.info("Added target body '%s' (%s)."
                             % (dialog.result_display_name, dialog.result_mesh_name))

    def _on_remove_target_body(self):
        rows = sorted((i.row() for i in self.target_list_widget.selectedIndexes()),
                      reverse=True)
        for row in rows:
            del self.target_bodies[row]
        self._refresh_target_list_widget()

    def _refresh_target_list_widget(self):
        self.target_list_widget.clear()
        for entry in self.target_bodies:
            self.target_list_widget.addItem(
                "%s  (%s)" % (entry["display_name"], entry["mesh"]))

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
                self, "Nothing Added", "No new valid mesh selections found.")
            return
        self._refresh_clothing_list_widget()
        self.logger.info("Added %d clothing mesh(es)." % added)

    def _on_remove_selected_clothing(self):
        selected = {item.text() for item in self.clothing_list_widget.selectedItems()}
        self.clothing_items = [c for c in self.clothing_items if c not in selected]
        self._refresh_clothing_list_widget()

    def _on_clothing_rows_moved(self, *_args):
        """Keep the processing order in step with a drag-reorder."""
        self.clothing_items = [
            self.clothing_list_widget.item(i).text()
            for i in range(self.clothing_list_widget.count())
        ]

    def _on_load_clothing_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Clothing Reference Folder")
        if not folder:
            return

        supported_ext = (".ma", ".mb")
        scene_files = [f for f in sorted(os.listdir(folder))
                       if f.lower().endswith(supported_ext)]
        if not scene_files:
            QtWidgets.QMessageBox.information(
                self, "No Scene Files Found",
                "No .ma/.mb files were found in that folder.\n\n"
                "Select meshes in the scene and use 'Add Selected Clothing' instead."
            )
            return

        imported_files = 0
        added = 0
        for filename in scene_files:
            file_path = os.path.join(folder, filename)
            try:
                # returnNewNodes is what makes this precise: only meshes
                # that came out of THIS file are added. Scanning the whole
                # scene for meshes instead would sweep up the body meshes
                # and every unrelated prop already open.
                new_nodes = cmds.file(
                    file_path, i=True, returnNewNodes=True,
                    mergeNamespacesOnClash=True,
                    namespace=os.path.splitext(filename)[0],
                    preserveReferences=True,
                ) or []
            except RuntimeError as exc:
                self.logger.error("Could not import '%s': %s" % (file_path, exc))
                continue

            imported_files += 1
            for node in new_nodes:
                if not cmds.objExists(node) or cmds.nodeType(node) != "mesh":
                    continue
                try:
                    if cmds.getAttr(node + ".intermediateObject"):
                        continue
                except (RuntimeError, ValueError):
                    continue
                for parent in cmds.listRelatives(node, parent=True) or []:
                    if parent not in self.clothing_items:
                        self.clothing_items.append(parent)
                        added += 1

        self._refresh_clothing_list_widget()
        self.logger.info("Imported %d file(s) from '%s'; added %d clothing mesh(es)."
                         % (imported_files, folder, added))
        if added == 0:
            QtWidgets.QMessageBox.information(
                self, "No Meshes Added",
                "Those files imported, but contained no polygon meshes to add."
            )

    def _on_clear_clothing_list(self):
        self.clothing_items = []
        self._refresh_clothing_list_widget()

    def _filter_clothing_list(self, text):
        text = (text or "").lower()
        for i in range(self.clothing_list_widget.count()):
            item = self.clothing_list_widget.item(i)
            item.setHidden(text not in item.text().lower())

    def _refresh_clothing_list_widget(self):
        self.clothing_list_widget.clear()
        for name in self.clothing_items:
            self.clothing_list_widget.addItem(name)
        # Re-apply any active search, otherwise refreshing the list
        # silently resurrects items the artist has filtered out.
        self._filter_clothing_list(self.clothing_search.text())

    # ------------------------------------------------------------------
    # Output folder
    # ------------------------------------------------------------------
    def _on_browse_output_folder(self):
        start_dir = self.output_folder_edit.text().strip() or ""
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Output Folder", start_dir)
        if folder:
            self.output_folder_edit.setText(folder)

    # ------------------------------------------------------------------
    # Settings persistence
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

    def _gather_settings(self):
        settings = {
            "version": config.SETTINGS_VERSION,
            "base_body": self.base_body_mesh,
            "target_bodies": self.target_bodies,
            "clothing_items": self.clothing_items,
            "output_folder": self.output_folder_edit.text(),
            "options": self._gather_options(),
            "deformation": self.deform_state.to_dict(),
        }
        window = dict(self._window_geometry or {})
        window["open_docked"] = self.chk_open_docked.isChecked()
        settings["window"] = window
        return settings

    def _apply_settings(self, data):
        self.base_body_mesh = data.get("base_body")
        if self.base_body_mesh:
            self.base_body_label.setText(self.base_body_mesh)
            self.base_body_label.setEnabled(True)
        else:
            self.base_body_label.setText(self.NO_BASE_TEXT)
            self.base_body_label.setEnabled(False)

        self.target_bodies = data.get("target_bodies") or []
        self.clothing_items = data.get("clothing_items") or []
        self.output_folder_edit.setText(data.get("output_folder") or "")

        options = data.get("options") or {}
        defaults = config.DEFAULT_OPTIONS
        self.chk_delete_history.setChecked(
            options.get("delete_history", defaults["delete_history"]))
        self.chk_freeze_transforms.setChecked(
            options.get("freeze_transforms", defaults["freeze_transforms"]))
        self.chk_center_pivot.setChecked(
            options.get("center_pivot", defaults["center_pivot"]))
        self.chk_transfer_skin.setChecked(
            options.get("transfer_skin_weights", defaults["transfer_skin_weights"]))
        self.chk_export_fbx.setChecked(
            options.get("export_fbx", defaults["export_fbx"]))
        self.chk_overwrite.setChecked(
            options.get("overwrite_existing", defaults["overwrite_existing"]))
        self.chk_keep_clean.setChecked(
            options.get("keep_scene_clean", defaults["keep_scene_clean"]))

        self.deform_state.from_dict(data.get("deformation") or {})
        self._push_deform_state_to_widgets()

        window = data.get("window") or {}
        self.chk_open_docked.setChecked(
            bool(window.get("open_docked", config.WINDOW_OPEN_DOCKED_DEFAULT)))
        if all(k in window for k in ("x", "y", "width", "height")):
            self._window_geometry = {
                "x": window["x"], "y": window["y"],
                "width": window["width"], "height": window["height"],
            }

        self._refresh_target_list_widget()
        self._refresh_clothing_list_widget()

    def _push_deform_state_to_widgets(self):
        """Mirror the DeformationState onto the sliders (settings load / reset)."""
        for key, value in (
            ("global_influence", self.deform_state.global_influence),
            ("surface_offset", self.deform_state.surface_offset),
            ("smooth_iterations", self.deform_state.smooth_iterations),
        ):
            spin = self.deform_widgets[key][1]
            spin.setValue(value)

    @staticmethod
    def _default_settings_path():
        return os.path.join(cmds.internalVar(userAppDir=True),
                            config.DEFAULT_SETTINGS_FILENAME)

    def _save_settings_dialog(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save Settings", self._default_settings_path(),
            config.SETTINGS_FILE_FILTER)
        if not path:
            return
        self._capture_window_geometry()
        if self._save_settings(path, silent=False):
            self.logger.info("Settings saved to %s" % path)

    def _save_settings(self, path, silent=False):
        try:
            folder = os.path.dirname(path)
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)
            with open(path, "w") as handle:
                json.dump(self._gather_settings(), handle, indent=2)
            return True
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            # Auto-save on close must never raise a dialog at an artist
            # who is just closing the window, nor block Maya from quitting.
            if not silent:
                QtWidgets.QMessageBox.critical(self, "Save Failed", str(exc))
            return False

    def _load_settings_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load Settings", self._default_settings_path(),
            config.SETTINGS_FILE_FILTER)
        if not path:
            return
        if self._load_settings(path=path, silent=False):
            self.logger.info("Settings loaded from %s" % path)

    def _load_settings(self, path=None, silent=False):
        path = path or self._default_settings_path()
        if not os.path.isfile(path):
            return False
        try:
            with open(path, "r") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("Settings file is not a JSON object.")
            self._apply_settings(data)
            return True
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if not silent:
                QtWidgets.QMessageBox.critical(self, "Load Failed", str(exc))
            return False

    # ------------------------------------------------------------------
    # Deformation Controls callbacks
    # ------------------------------------------------------------------
    def _on_reset_deform_clicked(self):
        self.deform_state.reset_to_defaults()
        self._push_deform_state_to_widgets()

    # ------------------------------------------------------------------
    # Processor
    # ------------------------------------------------------------------
    def _ensure_processor(self):
        if (self.live_processor is None
                or self.live_processor.base_body != self.base_body_mesh):
            self.live_processor = ClothingVariantProcessor(
                self.base_body_mesh, self.logger, self.exporter)
        self.live_processor.deform_state = self.deform_state
        return self.live_processor

    # ------------------------------------------------------------------
    # Generate / batch processing
    # ------------------------------------------------------------------
    def _validate_inputs(self):
        errors = []
        if not self.base_body_mesh or not utils.node_exists_and_is_mesh(self.base_body_mesh):
            errors.append("Select a valid Base Body mesh.")
        if not self.target_bodies:
            errors.append("Add at least one Target Body.")
        elif not any(utils.node_exists_and_is_mesh(t["mesh"]) for t in self.target_bodies):
            errors.append("None of the Target Body meshes exist in this scene.")
        if not self.clothing_items:
            errors.append("Add at least one Clothing mesh.")
        elif not any(utils.node_exists_and_is_mesh(c) for c in self.clothing_items):
            errors.append("None of the Clothing meshes exist in this scene.")
        return errors

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
                "Export FBX is enabled but no output folder is set.")
            return

        if (options["export_fbx"] and not options["overwrite_existing"]
                and output_folder and self._any_export_would_collide(output_folder)):
            proceed = QtWidgets.QMessageBox.question(
                self, "Files Already Exist",
                "Some output files already exist and 'Overwrite Existing Files' "
                "is disabled. Those files will be left alone and their variants "
                "will not be exported. Continue?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
            )
            if proceed != QtWidgets.QMessageBox.Yes:
                return

        self.log_view.clear()
        if output_folder:
            self.logger.set_log_file(output_folder)
        self.logger.info("Started...")

        # Individually missing meshes are logged, not blocked: one stale
        # entry in a 50-item list must not stop the other 49 from running.
        for item in self.clothing_items:
            if not utils.node_exists_and_is_mesh(item):
                self.logger.warning("Clothing '%s' is missing from the scene; "
                                    "it will be skipped." % item)
        for target in self.target_bodies:
            if not utils.node_exists_and_is_mesh(target["mesh"]):
                self.logger.warning("Target body '%s' (%s) is missing from the scene; "
                                    "it will be skipped."
                                    % (target["display_name"], target["mesh"]))

        if options["keep_scene_clean"]:
            removed = utils.delete_leftover_temp_nodes(self.logger)
            if removed:
                self.logger.info(
                    "Removed %d leftover temporary node(s) from an interrupted run."
                    % removed)

        processor = self._ensure_processor()
        self.batch_runner = BatchRunner(
            processor, list(self.clothing_items), list(self.target_bodies),
            options, output_folder, self.logger)

        if not self.deform_state.is_identity():
            self.logger.info(
                "Deformation Controls active -- influence %.2f, offset %.2f, "
                "smooth iterations %d."
                % (self.deform_state.global_influence,
                   self.deform_state.surface_offset,
                   self.deform_state.smooth_iterations))

        self.logger.info("Processing %d clothing item(s) x %d body variant(s) = %d task(s)."
                         % (len(self.clothing_items), len(self.target_bodies),
                            self.batch_runner.total))

        self._set_running_ui(True)
        self.progress_bar.setValue(0)
        # Redrawing the viewport after every duplicate/rename/delete costs
        # far more than the geometry work itself. Always restored in
        # _finish_batch / _cleanup.
        utils.suspend_viewport(self.logger)
        self.process_timer.start()

    def _any_export_would_collide(self, output_folder):
        for clothing in self.clothing_items:
            for target in self.target_bodies:
                variant_folder = utils.sanitize_folder_component(target["display_name"])
                # Mirrors processor._rename_result + exporter's file naming.
                candidate = "%s_%s" % (utils.sanitize_node_name(clothing), variant_folder)
                path = os.path.join(output_folder, variant_folder,
                                    candidate + config.EXPORT_EXTENSION)
                if os.path.exists(path):
                    return True
        return False

    def _set_running_ui(self, running):
        self.generate_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        self.pause_btn.setEnabled(running)
        self.resume_btn.setEnabled(False)
        if running:
            self.retry_failed_btn.setEnabled(False)

    def _on_timer_tick(self):
        """
        One task per tick. Wrapped so that an unexpected failure anywhere
        in the UI update path shuts the batch down cleanly (and unfreezes
        the viewport) instead of leaving a timer spinning forever.
        """
        try:
            self._process_one_tick()
        except Exception as exc:  # noqa: BLE001 - must never crash Maya
            self.logger.error("Batch stopped by an unexpected error: %s" % exc)
            self._finish_batch(cancelled=True)

    def _process_one_tick(self):
        runner = self.batch_runner
        if runner is None:
            self.process_timer.stop()
            return

        if runner.cancelled:
            self._finish_batch(cancelled=True)
            return

        if runner.is_paused():
            self.elapsed_label.setText(
                "Elapsed: %s (paused)" % self._format_seconds(runner.elapsed_seconds()))
            return

        result = runner.run_next()
        if result is not None:
            self.current_clothing_label.setText("Clothing: %s" % result.clothing)
            self.current_body_label.setText("Body Variant: %s" % result.target_display_name)

        self._refresh_progress()

        if runner.is_finished():
            self._finish_batch(cancelled=False)

    def _refresh_progress(self):
        runner = self.batch_runner
        if runner is None:
            return
        self.progress_bar.setValue(int(runner.progress_fraction() * 100))
        eta = runner.eta_seconds()
        self.eta_label.setText(
            "ETA: %s" % (self._format_seconds(eta) if eta is not None else "-"))
        stats = runner.stats_summary()
        self.stats_label.setText("Processed: %d / %d" % (stats["processed"], stats["total"]))
        self.elapsed_label.setText(
            "Elapsed: %s" % self._format_seconds(runner.elapsed_seconds()))

    def _finish_batch(self, cancelled):
        self.process_timer.stop()
        utils.resume_viewport(self.logger)

        stats = self.batch_runner.stats_summary() if self.batch_runner else {}
        if cancelled:
            self.logger.warning("Cancelled. Processed %d / %d."
                                % (stats.get("processed", 0), stats.get("total", 0)))
        else:
            self.logger.info(
                "Completed. Succeeded: %d, Failed: %d, Total time: %.1fs"
                % (stats.get("succeeded", 0), stats.get("failed", 0),
                   stats.get("total_time", 0.0)))

        if self._gather_options().get("keep_scene_clean", True):
            utils.remove_empty_namespaces()

        if not cancelled:
            self.progress_bar.setValue(100)

        self._set_running_ui(False)
        self.retry_failed_btn.setEnabled(bool(stats.get("failed", 0)))
        self.current_clothing_label.setText("Clothing: -")
        self.current_body_label.setText("Body Variant: -")
        self.eta_label.setText("ETA: -")

    def _on_pause_clicked(self):
        if self.batch_runner:
            self.batch_runner.pause()
            self.pause_btn.setEnabled(False)
            self.resume_btn.setEnabled(True)
            self.logger.info("Paused -- the current item finished first.")

    def _on_resume_clicked(self):
        if self.batch_runner:
            self.batch_runner.resume()
            self.pause_btn.setEnabled(True)
            self.resume_btn.setEnabled(False)
            self.logger.info("Resumed.")

    def _on_retry_failed_clicked(self):
        if not self.batch_runner:
            return
        count = self.batch_runner.retry_failed()
        if count == 0:
            QtWidgets.QMessageBox.information(self, "Retry Failed Items",
                                              "No failed items to retry.")
            return
        self.logger.info("Retrying %d failed item(s)..." % count)
        self._set_running_ui(True)
        self.progress_bar.setValue(0)
        utils.suspend_viewport(self.logger)
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


# ---------------------------------------------------------------------------
# Show helper
# ---------------------------------------------------------------------------
_window_instance = None


def _delete_leftover_workspace_control():
    """
    Remove any workspaceControl left over from a previous session.

    This is what stops the tool reappearing docked across the top of Maya.
    A workspaceControl stores its dock position in the workspace prefs, and
    Maya restores that position when a control of the same name is
    recreated -- overriding `floating=True`. Deleting the control also
    discards the remembered docking, so the next open honours the artist's
    actual preference.
    """
    try:
        if cmds.workspaceControl(config.WORKSPACE_CONTROL_NAME, exists=True):
            cmds.deleteUI(config.WORKSPACE_CONTROL_NAME)
    except RuntimeError:
        pass


def show():
    """
    Open (or bring forward) the Clothing Variant Generator window.

    By default this is a plain, free-floating top-level window owned by
    Maya's main window -- NOT a workspaceControl. That is deliberate: a
    workspaceControl remembers where it was docked and will re-dock itself
    there on the next launch no matter what `floating` says, which is how
    the tool ended up parked across the top of Maya taking half the
    screen. A plain window can't do that: it is freely movable, freely
    resizable, and shrinks down to a narrow strip beside the viewport.

    Tick "Open Docked" to get the dockable workspaceControl instead, which
    docks vertically down the right-hand side.

    No ``uiScript`` is ever registered either way, so nothing is
    auto-restored by Maya's saved UI layout on startup -- the ONLY way to
    open this window is calling this function:

        import clothing_variant_generator.main as cvg_main
        cvg_main.show()

    Running this again does not tear down and rebuild the window: a live
    instance is brought to front instead of duplicated.
    """
    global _window_instance

    if _window_instance is not None:
        try:
            _window_instance.raise_()
            _window_instance.activateWindow()
            if _window_instance.isMinimized():
                _window_instance.showNormal()
            return _window_instance
        except RuntimeError:
            # The Python object survived but its Qt/Maya side didn't --
            # clean up and rebuild rather than return a dead window.
            _window_instance._cleanup()
            _window_instance = None

    _delete_leftover_workspace_control()

    window = ClothingVariantGeneratorWindow(parent=maya_main_window())
    _window_instance = window

    if window.open_docked_preference():
        # Opt-in dock: this is the only path that creates a
        # workspaceControl, and it docks vertically on the right.
        window.show(dockable=True, floating=False, area=config.WINDOW_DOCK_AREA)
    else:
        # Qt.Window promotes the Maya-parented widget to a real top-level
        # window with its own title bar and minimise/maximise/close, while
        # still being owned by Maya. QWidget.show bypasses the dockable
        # mixin entirely so no workspaceControl is ever created.
        window.setWindowFlags(QtCore.Qt.Window)
        QtWidgets.QWidget.show(window)

    # Restore position/size once the event loop turns: when docked, Maya
    # is still building the control at this point.
    QtCore.QTimer.singleShot(0, window.apply_saved_geometry)
    return window
