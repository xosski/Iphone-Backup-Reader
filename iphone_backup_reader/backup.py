from __future__ import annotations

import json
import plistlib
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any


DEFAULT_BACKUP_ROOT = Path(r"D:\MobileSync\Backup")


class BackupError(Exception):
    """Raised when a backup cannot be read safely."""


@dataclass(frozen=True)
class BackupFile:
    file_id: str
    domain: str
    relative_path: str
    flags: int
    size: int | None

    @property
    def display_path(self) -> str:
        return f"{self.domain}/{self.relative_path}".rstrip("/")


class Backup:
    def __init__(self, path: Path):
        self.path = path
        self.info = self._read_plist("Info.plist")
        self.manifest = self._read_plist("Manifest.plist")
        self.status = self._read_plist("Status.plist")

    def _read_plist(self, name: str) -> dict[str, Any]:
        path = self.path / name
        if not path.is_file():
            return {}
        try:
            with path.open("rb") as handle:
                value = plistlib.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, plistlib.InvalidFileException):
            return {}

    @property
    def udid(self) -> str:
        return str(self.info.get("Unique Identifier") or self.path.name)

    @property
    def device_name(self) -> str:
        return str(self.info.get("Device Name") or "Unknown iPhone")

    @property
    def product_version(self) -> str:
        return str(self.info.get("Product Version") or "Unknown")

    @property
    def product_type(self) -> str:
        return str(self.info.get("Product Type") or "Unknown")

    @property
    def serial_number(self) -> str:
        return str(self.info.get("Serial Number") or "Unknown")

    @property
    def backup_date(self) -> datetime | None:
        value = self.info.get("Last Backup Date") or self.status.get("Date")
        return value if isinstance(value, datetime) else None

    @property
    def is_encrypted(self) -> bool:
        return bool(self.manifest.get("IsEncrypted", False))

    def _database(self) -> sqlite3.Connection:
        if self.is_encrypted:
            raise BackupError("This backup is encrypted. Its file manifest cannot be browsed without decryption.")
        database = self.path / "Manifest.db"
        if not database.is_file():
            raise BackupError("Manifest.db is missing from this backup.")
        try:
            connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            return connection
        except sqlite3.Error as error:
            raise BackupError(f"The backup manifest could not be opened: {error}") from error

    def search_files(self, query: str = "", category: str = "All", limit: int = 5000) -> list[BackupFile]:
        clauses = ["flags = 1"]
        parameters: list[Any] = []

        category_clauses = {
            "Photos": "(domain = 'CameraRollDomain' OR relativePath LIKE 'Media/DCIM/%')",
            "Messages": "(relativePath LIKE '%sms.db%' OR relativePath LIKE '%Messages/%')",
            "Contacts": "(relativePath LIKE '%AddressBook%' OR relativePath LIKE '%Contacts%')",
            "Notes": "(relativePath LIKE '%NoteStore%' OR relativePath LIKE '%notes.sqlite%')",
        }
        if category in category_clauses:
            clauses.append(category_clauses[category])
        if query.strip():
            clauses.append("(domain LIKE ? OR relativePath LIKE ?)")
            pattern = f"%{query.strip()}%"
            parameters.extend((pattern, pattern))

        sql = f"""
            SELECT fileID, domain, relativePath, flags, file
            FROM Files
            WHERE {' AND '.join(clauses)}
            ORDER BY domain, relativePath
            LIMIT ?
        """
        parameters.append(limit)
        try:
            with closing(self._database()) as connection:
                rows = connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as error:
            raise BackupError(f"The backup manifest could not be searched: {error}") from error

        return [
            BackupFile(
                file_id=str(row["fileID"]),
                domain=str(row["domain"]),
                relative_path=str(row["relativePath"]),
                flags=int(row["flags"]),
                size=_extract_size(row["file"]),
            )
            for row in rows
        ]

    def stored_path(self, entry: BackupFile) -> Path:
        modern = self.path / entry.file_id[:2] / entry.file_id
        if modern.is_file():
            return modern
        legacy = self.path / entry.file_id
        if legacy.is_file():
            return legacy
        raise BackupError(f"The stored data for {entry.display_path} is missing.")

    def export(self, entry: BackupFile, destination: Path) -> Path:
        source = self.stored_path(entry)
        domain = _safe_component(entry.domain)
        path_parts = [
            _safe_component(part)
            for part in PurePosixPath(entry.relative_path).parts
            if part not in ("/", ".", "..")
        ]
        relative = Path(domain, *path_parts)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return target

    def preview(self, entry: BackupFile, max_bytes: int = 1_000_000) -> str:
        source = self.stored_path(entry)
        suffix = Path(entry.relative_path).suffix.lower()
        if suffix in {".db", ".sqlite", ".sqlite3"} or _is_sqlite(source):
            return _preview_sqlite(source)
        data = source.read_bytes()[:max_bytes]
        if suffix == ".plist" or data.startswith((b"bplist00", b"<?xml")):
            try:
                return plistlib.dumps(plistlib.loads(data), fmt=plistlib.FMT_XML).decode("utf-8")
            except (plistlib.InvalidFileException, ValueError, TypeError):
                pass
        if suffix == ".json":
            try:
                return json.dumps(json.loads(data), indent=2, ensure_ascii=False)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        if b"\x00" not in data[:4096]:
            for encoding in ("utf-8", "utf-16", "latin-1"):
                try:
                    text = data.decode(encoding)
                    if source.stat().st_size > max_bytes:
                        text += "\n\n[Preview truncated]"
                    return text
                except UnicodeDecodeError:
                    continue
        return f"Binary file\nStored size: {source.stat().st_size:,} bytes\nOriginal path: {entry.display_path}"


def discover_backups(root: Path) -> list[Backup]:
    if not root.is_dir():
        return []
    backups: list[Backup] = []
    for child in root.iterdir():
        if child.is_dir() and (child / "Info.plist").is_file():
            backups.append(Backup(child))
    return sorted(backups, key=lambda backup: backup.backup_date or datetime.min, reverse=True)


def _extract_size(metadata: bytes | None) -> int | None:
    if not metadata:
        return None
    try:
        value = plistlib.loads(metadata)
        if isinstance(value, dict):
            size = value.get("Size")
            return int(size) if size is not None else None
    except (plistlib.InvalidFileException, ValueError, TypeError):
        pass
    return None


def _safe_component(value: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if character in invalid or ord(character) < 32 else character for character in value)
    return cleaned.rstrip(". ") or "unnamed"


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _preview_sqlite(path: Path) -> str:
    lines = ["SQLite database", ""]
    try:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            if not tables:
                return "SQLite database with no user tables."
            for (table_name,) in tables[:50]:
                escaped = str(table_name).replace('"', '""')
                count = connection.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0]
                columns = connection.execute(f'PRAGMA table_info("{escaped}")').fetchall()
                names = ", ".join(str(column[1]) for column in columns)
                lines.append(f"{table_name} ({count:,} rows)")
                lines.append(f"  {names}")
    except sqlite3.Error as error:
        return f"SQLite database (could not inspect tables: {error})"
    if len(tables) > 50:
        lines.append(f"\n[Only the first 50 of {len(tables)} tables are shown]")
    return "\n".join(lines)
