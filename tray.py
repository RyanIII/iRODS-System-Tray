"""System tray coordinator tying together config, monitoring, and the settings UI."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QStyle, QSystemTrayIcon

from config import ConfigStore, IRODSEnvironmentStore, normalize_directory
from irods_worker import IRODSUploadWorker
from monitor import MonitorManager
from ui import SettingsWindow


class TrayController(QObject):
    """Own the long-lived application state and system tray interactions.

    This controller is the central integration point for the app: it loads and saves
    configuration, updates the watchdog monitor, reacts to GUI events, and handles tray
    icon behavior so monitoring can continue while the window stays hidden.
    """

    queue_upload = Signal(str, str)

    def __init__(self, app: QApplication) -> None:
        """Build the tray icon, menu, monitor, and settings window for the app."""

        super().__init__()
        self.app = app
        self.app.setQuitOnLastWindowClosed(False)

        self.config_store = ConfigStore()
        self.irods_environment_store = IRODSEnvironmentStore()
        self.irods_environment_store.ensure_exists()
        self.config = self.config_store.load()
        self.monitor = MonitorManager()
        self.window = SettingsWindow()
        self._queued_uploads: set[str] = set()

        self.upload_thread = QThread(self)
        self.upload_worker = IRODSUploadWorker(self.irods_environment_store)
        self.upload_worker.moveToThread(self.upload_thread)
        self.upload_thread.start()

        self.monitor_toggle_action = QAction("Toggle Monitoring", self)
        self.monitor_toggle_action.setCheckable(True)
        self.monitor_toggle_action.toggled.connect(self.set_monitoring_active)
        self.menu = QMenu()

        self.tray_icon = QSystemTrayIcon(self._build_icon(), self)
        self.tray_icon.setToolTip("Directory Ingestion")
        self.tray_icon.activated.connect(self._handle_tray_activation)

        self._single_click_timer = QTimer(self)
        self._single_click_timer.setSingleShot(True)
        self._single_click_timer.timeout.connect(self.toggle_window)

        self._build_menu()
        self._connect_signals()
        self._sync_from_config()
        self.window.set_irods_environment(self.irods_environment_store.load())
        self.tray_icon.show()

    def show_window(self) -> None:
        """Show and focus the settings window from the tray or startup path."""

        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def toggle_window(self) -> None:
        """Hide the settings window if visible, otherwise show and focus it."""

        if self.window.isVisible():
            self.window.hide()
            return
        self.show_window()

    def prompt_add_directory(self) -> None:
        """Open a native folder picker and add the chosen directory if provided."""

        selected = QFileDialog.getExistingDirectory(
            self.window,
            "Select folder to monitor",
            str(Path.home()),
        )
        if selected:
            self.add_directory(selected)

    def add_directory(self, path: str) -> None:
        """Normalize and persist a new monitored directory from the UI."""

        normalized = normalize_directory(path)
        if normalized in self.config.monitored_directories:
            self.window.set_status_message(f"Already monitoring {normalized}")
            return

        self.config.monitored_directories.append(normalized)
        self._persist_and_sync()
        self.window.set_status_message(f"Added {normalized}")

    def remove_directory(self, path: str) -> None:
        """Remove a monitored directory, then persist and resync background watches."""

        self.config.monitored_directories = [
            directory for directory in self.config.monitored_directories if directory != path
        ]
        self._persist_and_sync()
        self.window.set_status_message(f"Removed {path}")

    def set_monitoring_active(self, is_active: bool) -> None:
        """Apply the global monitoring toggle from either the tray or the window."""

        self.config.is_monitoring_active = is_active
        self._persist_and_sync()

    def exit_application(self) -> None:
        """Save state, stop background monitoring, and quit the Qt application cleanly."""

        self.config_store.save(self.config)
        self.monitor.shutdown()
        self.upload_thread.quit()
        self.upload_thread.wait(5000)
        self.tray_icon.hide()
        self.app.quit()

    def save_irods_settings(self) -> None:
        """Persist the iRODS session settings entered in the settings window."""

        environment = self.window.get_irods_environment()
        if not all(
            [
                environment.irods_host,
                environment.irods_user_name,
                environment.irods_password,
                environment.irods_zone_name,
                environment.irods_default_vault,
            ]
        ):
            self.window.set_status_message(
                "Complete all iRODS fields before saving.",
                is_error=True,
            )
            return

        self.irods_environment_store.save(environment)
        self.window.set_irods_environment(self.irods_environment_store.load())
        self.window.set_status_message("Saved iRODS settings.")
        self.window.append_activity(
            f"saved iRODS settings for {environment.irods_user_name}@{environment.irods_host}:{environment.irods_port}"
        )

    def _build_icon(self):
        """Return a standard fallback icon so the tray works without bundled assets."""

        return self.app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)

    def _build_menu(self) -> None:
        """Create the tray context menu and wire actions to controller methods."""

        open_action = self.menu.addAction("Open Settings")
        open_action.triggered.connect(lambda _checked=False: self.show_window())
        self.menu.addAction(self.monitor_toggle_action)
        self.menu.addSeparator()
        exit_action = self.menu.addAction("Exit")
        exit_action.triggered.connect(lambda _checked=False: self.exit_application())
        self.tray_icon.setContextMenu(self.menu)

    def _connect_signals(self) -> None:
        """Connect UI and monitor signals so changes flow through one controller."""

        self.window.add_folder_requested.connect(self.prompt_add_directory)
        self.window.remove_folder_requested.connect(self.remove_directory)
        self.window.save_irods_requested.connect(self.save_irods_settings)
        self.window.monitoring_toggled.connect(self.set_monitoring_active)
        self.monitor.file_event.connect(self._handle_file_event)
        self.monitor.ingest_requested.connect(self._queue_ingestion)
        self.monitor.monitor_error.connect(self._handle_monitor_error)
        self.queue_upload.connect(self.upload_worker.upload_file)
        self.upload_worker.upload_started.connect(self._handle_upload_started)
        self.upload_worker.upload_progress.connect(self._handle_upload_progress)
        self.upload_worker.upload_finished.connect(self._handle_upload_finished)
        self.upload_worker.upload_failed.connect(self._handle_upload_failed)

    def _sync_from_config(self, *, show_status: bool = True) -> None:
        """Push the current config into the monitor, tray menu, and visible window.

        This keeps every surface of the app consistent after startup or after any user
        action that changes directories or the global enabled state.
        """

        self.monitor.sync(self.config.monitored_directories, self.config.is_monitoring_active)

        invalid_directories = {
            directory
            for directory in self.config.monitored_directories
            if not Path(directory).is_dir()
        }

        self.window.set_monitoring_active(self.config.is_monitoring_active)
        previous = self.monitor_toggle_action.blockSignals(True)
        self.monitor_toggle_action.setChecked(self.config.is_monitoring_active)
        self.monitor_toggle_action.blockSignals(previous)
        self.window.set_directories(self.config.monitored_directories, invalid_directories)

        if show_status:
            if not self.config.is_monitoring_active:
                self.window.set_status_message("Monitoring paused.")
            elif invalid_directories:
                self.window.set_status_message(
                    f"Monitoring active for available folders. {len(invalid_directories)} folder(s) are missing.",
                    is_error=True,
                )
            else:
                self.window.set_status_message("Monitoring active.")

    def _persist_and_sync(self) -> None:
        """Save the latest state and immediately refresh monitoring and UI widgets."""

        self.config.monitored_directories = list(dict.fromkeys(self.config.monitored_directories))
        self.config_store.save(self.config)
        self._sync_from_config()

    def _handle_tray_activation(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Interpret tray clicks so single and double clicks both toggle the window.

        A short timer delays the single-click action long enough to detect a possible
        double-click without triggering both behaviors.
        """

        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._single_click_timer.start(self.app.doubleClickInterval())
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._single_click_timer.stop()
            self.toggle_window()

    def _handle_file_event(self, event_type: str, path: str, is_directory: bool) -> None:
        """Format background file events into readable activity log entries."""

        entry_type = "folder" if is_directory else "file"
        self.window.append_activity(f"{event_type}: {entry_type} -> {path}")

    def _queue_ingestion(self, path: str) -> None:
        """Forward created and moved files to the iRODS worker thread once per path."""

        if not self.config.is_monitoring_active:
            return

        normalized_path = str(Path(path).expanduser().resolve(strict=False))
        monitored_root = self._match_monitored_directory(normalized_path)
        if monitored_root is None:
            return
        if normalized_path in self._queued_uploads:
            return

        self._queued_uploads.add(normalized_path)
        self.window.append_activity(f"queued upload -> {normalized_path}")
        self.queue_upload.emit(normalized_path, monitored_root)

    def _handle_monitor_error(self, message: str) -> None:
        """Surface monitoring failures in both the status area and activity log."""

        self.window.set_status_message(message, is_error=True)
        self.window.append_activity(f"warning: {message}")

    def _handle_upload_started(self, local_path: str, logical_path: str) -> None:
        """Surface the start of an iRODS upload in the tray window."""

        self.window.set_status_message(f"Uploading {Path(local_path).name} to iRODS...")
        self.window.append_activity(f"uploading -> {local_path} to {logical_path}")

    def _handle_upload_progress(
        self,
        local_path: str,
        _logical_path: str,
        bytes_sent: int,
        total_bytes: int,
    ) -> None:
        """Show coarse-grained upload progress without blocking the UI thread."""

        if total_bytes <= 0:
            self.window.set_status_message(f"Uploading {Path(local_path).name}...")
            return

        percent_complete = int((bytes_sent / total_bytes) * 100)
        self.window.set_status_message(
            f"Uploading {Path(local_path).name}: {percent_complete}%"
        )

    def _handle_upload_finished(self, local_path: str, logical_path: str) -> None:
        """Clear queue tracking and log successful background uploads."""

        self._queued_uploads.discard(local_path)
        self.window.set_status_message(f"Uploaded {Path(local_path).name} to iRODS.")
        self.window.append_activity(f"uploaded -> {local_path} to {logical_path}")

    def _handle_upload_failed(self, local_path: str, message: str) -> None:
        """Clear queue tracking and surface upload failures to the user."""

        self._queued_uploads.discard(local_path)
        self.window.set_status_message(message, is_error=True)
        self.window.append_activity(f"upload failed: {local_path} ({message})")

    def _match_monitored_directory(self, path: str) -> str | None:
        """Return the configured watch root that contains the given file path."""

        candidate = Path(path).expanduser().resolve(strict=False)
        best_match: str | None = None

        for directory in self.config.monitored_directories:
            directory_path = Path(directory).expanduser().resolve(strict=False)
            try:
                candidate.relative_to(directory_path)
            except ValueError:
                continue

            if best_match is None or len(directory) > len(best_match):
                best_match = directory

        return best_match
