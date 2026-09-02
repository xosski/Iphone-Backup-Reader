from __future__ import annotations

import plistlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path

from iphone_backup_reader.backup import Backup, BackupError, discover_backups


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.backup_path = self.root / "test-udid"
        self.backup_path.mkdir()
        self._write_plist(
            "Info.plist",
            {
                "Device Name": "Test iPhone",
                "Product Version": "18.0",
                "Product Type": "iPhone99,1",
                "Serial Number": "SERIAL",
                "Unique Identifier": "test-udid",
                "Last Backup Date": datetime(2025, 1, 2, 3, 4, 5),
            },
        )
        self._write_plist("Manifest.plist", {"IsEncrypted": False})
        self._write_plist("Status.plist", {"SnapshotState": "finished"})
        with closing(sqlite3.connect(self.backup_path / "Manifest.db")) as connection:
            connection.execute(
                "CREATE TABLE Files (fileID TEXT, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB)"
            )
            connection.commit()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_plist(self, name: str, value: dict) -> None:
        with (self.backup_path / name).open("wb") as handle:
            plistlib.dump(value, handle)

    def _add_file(self, file_id: str, domain: str, relative_path: str, content: bytes) -> None:
        metadata = plistlib.dumps({"Size": len(content)}, fmt=plistlib.FMT_BINARY)
        with closing(sqlite3.connect(self.backup_path / "Manifest.db")) as connection:
            connection.execute(
                "INSERT INTO Files VALUES (?, ?, ?, 1, ?)",
                (file_id, domain, relative_path, metadata),
            )
            connection.commit()
        stored = self.backup_path / file_id[:2] / file_id
        stored.parent.mkdir(exist_ok=True)
        stored.write_bytes(content)

    def test_discovers_and_reads_metadata(self) -> None:
        backups = discover_backups(self.root)
        self.assertEqual(1, len(backups))
        self.assertEqual("Test iPhone", backups[0].device_name)
        self.assertEqual("18.0", backups[0].product_version)
        self.assertFalse(backups[0].is_encrypted)

    def test_search_preview_and_export(self) -> None:
        file_id = "ab" + "1" * 38
        self._add_file(file_id, "HomeDomain", "Library/example.txt", b"hello backup")
        backup = Backup(self.backup_path)

        entries = backup.search_files("example")
        self.assertEqual(1, len(entries))
        self.assertEqual(12, entries[0].size)
        self.assertEqual("hello backup", backup.preview(entries[0]))

        destination = self.root / "export"
        exported = backup.export(entries[0], destination)
        self.assertEqual(b"hello backup", exported.read_bytes())
        self.assertEqual(destination / "HomeDomain" / "Library" / "example.txt", exported)

    def test_category_filter(self) -> None:
        self._add_file("aa" + "1" * 38, "CameraRollDomain", "Media/DCIM/100APPLE/IMG_1.JPG", b"image")
        self._add_file("bb" + "2" * 38, "HomeDomain", "Library/other.txt", b"other")
        entries = Backup(self.backup_path).search_files(category="Photos")
        self.assertEqual(["Media/DCIM/100APPLE/IMG_1.JPG"], [entry.relative_path for entry in entries])

    def test_export_cannot_escape_destination(self) -> None:
        file_id = "cc" + "3" * 38
        self._add_file(file_id, "Home/Domain", "../../outside.txt", b"safe")
        backup = Backup(self.backup_path)
        exported = backup.export(backup.search_files("outside")[0], self.root / "export")
        self.assertEqual(self.root / "export" / "Home_Domain" / "outside.txt", exported)
        self.assertEqual(b"safe", exported.read_bytes())

    def test_encrypted_backup_is_reported(self) -> None:
        self._write_plist("Manifest.plist", {"IsEncrypted": True})
        backup = Backup(self.backup_path)
        with self.assertRaisesRegex(BackupError, "encrypted"):
            backup.search_files()


if __name__ == "__main__":
    unittest.main()
