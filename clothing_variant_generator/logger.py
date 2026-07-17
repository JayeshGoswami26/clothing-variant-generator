# -*- coding: utf-8 -*-
"""
logger.py
---------
Central logging utility for the Clothing Variant Generator.

Provides a Qt-signal based logger so a live QTextEdit in the UI can
display progress as it happens, while every line is simultaneously
streamed to a log.txt file inside the chosen output folder.

Nothing is buffered in memory: the UI owns the on-screen history (with a
bounded document) and log.txt owns the permanent record, so a
thousand-asset batch adds no growing Python-side list.

Logging failures (e.g. a locked/unwritable output folder) are always
caught internally -- a broken log must never crash a batch job.
"""

import os
import datetime

try:
    # Maya 2022-2024 ship Qt5 / PySide2.
    from PySide2.QtCore import QObject, Signal
except ImportError:
    # Maya 2025+ ship Qt6 / PySide6 instead.
    from PySide6.QtCore import QObject, Signal

from . import config


class VariantLogger(QObject):
    """
    Batch-safe logger.

    Usage:
        logger = VariantLogger()
        logger.message_logged.connect(my_text_edit.append)
        logger.set_log_file(r"C:/exports")
        logger.info("Started...")
    """

    message_logged = Signal(str)

    LEVEL_INFO = "INFO"
    LEVEL_WARN = "WARN"
    LEVEL_ERROR = "ERROR"
    LEVEL_SUCCESS = "SUCCESS"

    def __init__(self, parent=None):
        super(VariantLogger, self).__init__(parent)
        self.log_file_path = None

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def set_log_file(self, folder_path, filename=config.LOG_FILENAME):
        """Point the logger at a folder, creating it if necessary."""
        if not folder_path:
            self.log_file_path = None
            return
        try:
            if not os.path.isdir(folder_path):
                os.makedirs(folder_path)
            self.log_file_path = os.path.join(folder_path, filename)
            with open(self.log_file_path, "w") as handle:
                handle.write("")
        except OSError as exc:
            self.log_file_path = None
            self._write(self.LEVEL_ERROR, "Could not create log file: %s" % exc)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _write(self, level, message):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = "[%s][%s] %s" % (timestamp, level, message)
        self.message_logged.emit(line)
        if self.log_file_path:
            try:
                with open(self.log_file_path, "a") as handle:
                    handle.write(line + "\n")
            except OSError:
                # Never let a logging failure interrupt processing.
                pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def info(self, message):
        self._write(self.LEVEL_INFO, message)

    def warning(self, message):
        self._write(self.LEVEL_WARN, message)

    def error(self, message):
        self._write(self.LEVEL_ERROR, message)

    def success(self, message):
        self._write(self.LEVEL_SUCCESS, message)
