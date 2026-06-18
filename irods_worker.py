"""Background iRODS upload worker that runs on a dedicated Qt thread."""

from __future__ import annotations

import time
from pathlib import Path, PurePosixPath

from PySide6.QtCore import QObject, Signal, Slot

from config import IRODSEnvironment, IRODSEnvironmentStore, normalize_irods_collection


class IRODSUploadWorker(QObject):
    """Perform blocking iRODS authentication and uploads away from the GUI thread."""

    upload_started = Signal(str, str)
    upload_progress = Signal(str, str, int, int)
    upload_finished = Signal(str, str)
    upload_failed = Signal(str, str)

    def __init__(self, environment_store: IRODSEnvironmentStore) -> None:
        super().__init__()
        self._environment_store = environment_store

    @Slot(str, str)
    def upload_file(self, local_path: str, monitored_root: str) -> None:
        """Upload a created or moved file into the configured iRODS collection."""

        local_file = Path(local_path).expanduser().resolve(strict=False)
        monitored_directory = Path(monitored_root).expanduser().resolve(strict=False)
        environment = self._environment_store.load()

        try:
            self._validate_environment(environment)
            self._wait_for_stable_file(local_file)
            total_bytes = local_file.stat().st_size
            logical_path = self._build_logical_path(
                local_file,
                monitored_directory,
                environment,
            )
            self.upload_started.emit(str(local_file), logical_path)
            self._stream_upload(local_file, logical_path, total_bytes, environment)
        except Exception as exc:  # noqa: BLE001
            self.upload_failed.emit(str(local_file), str(exc))
            return

        self.upload_finished.emit(str(local_file), logical_path)

    def _validate_environment(self, environment: IRODSEnvironment) -> None:
        """Reject incomplete iRODS settings before attempting a network connection."""

        required_values = {
            "irods_host": environment.irods_host,
            "irods_user_name": environment.irods_user_name,
            "irods_password": environment.irods_password,
            "irods_zone_name": environment.irods_zone_name,
            "irods_default_vault": environment.irods_default_vault,
        }
        missing = [name for name, value in required_values.items() if not str(value).strip()]
        if missing:
            missing_values = ", ".join(missing)
            raise ValueError(f"Missing iRODS settings: {missing_values}")

    def _wait_for_stable_file(self, local_file: Path) -> None:
        """Give newly created files a short window to settle before reading them."""

        previous_size: int | None = None
        stable_reads = 0

        for _attempt in range(10):
            if not local_file.exists():
                raise FileNotFoundError(f"File no longer exists: {local_file}")
            if not local_file.is_file():
                raise ValueError(f"Not a regular file: {local_file}")

            current_size = local_file.stat().st_size
            if current_size == previous_size:
                stable_reads += 1
                if stable_reads >= 2:
                    return
            else:
                stable_reads = 0

            previous_size = current_size
            time.sleep(0.3)

    def _build_logical_path(
        self,
        local_file: Path,
        monitored_directory: Path,
        environment: IRODSEnvironment,
    ) -> str:
        """Map a local file into the configured iRODS collection root."""

        try:
            relative_path = local_file.relative_to(monitored_directory)
        except ValueError:
            relative_path = Path(local_file.name)

        logical_root = PurePosixPath(normalize_irods_collection(environment.irods_default_vault))
        return str(logical_root.joinpath(*relative_path.parts))

    def _stream_upload(
        self,
        local_file: Path,
        logical_path: str,
        total_bytes: int,
        environment: IRODSEnvironment,
    ) -> None:
        """Authenticate to iRODS and upload the file in chunks for progress updates."""

        try:
            from irods.session import iRODSSession
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "python-irodsclient is not installed. Add it to the environment to enable uploads."
            ) from exc

        with iRODSSession(
            host=environment.irods_host,
            port=environment.irods_port,
            user=environment.irods_user_name,
            password=environment.irods_password,
            zone=environment.irods_zone_name,
        ) as session:
            self._ensure_collection(session, str(PurePosixPath(logical_path).parent))

            bytes_sent = 0
            chunk_size = 1024 * 1024
            self.upload_progress.emit(str(local_file), logical_path, 0, total_bytes)

            with local_file.open("rb") as local_stream, session.data_objects.open(
                logical_path,
                "w",
            ) as remote_stream:
                while True:
                    chunk = local_stream.read(chunk_size)
                    if not chunk:
                        break
                    remote_stream.write(chunk)
                    bytes_sent += len(chunk)
                    self.upload_progress.emit(
                        str(local_file),
                        logical_path,
                        bytes_sent,
                        total_bytes,
                    )

    def _ensure_collection(self, session, logical_collection: str) -> None:
        """Create missing iRODS collections one path segment at a time."""

        normalized_collection = normalize_irods_collection(logical_collection)
        current = PurePosixPath("/")
        for part in PurePosixPath(normalized_collection).parts:
            if part == "/":
                continue
            current = current.joinpath(part)
            current_path = str(current)
            try:
                session.collections.get(current_path)
            except Exception:  # noqa: BLE001
                session.collections.create(current_path)
