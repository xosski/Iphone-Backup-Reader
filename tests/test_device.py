from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from iphone_backup_reader.device import ConnectedDevice, create_backup, get_debug_snapshot, list_media_files


class DeviceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.device = ConnectedDevice("device-id", "Test phone", "iPhone99,1", "18.0", "SERIAL")

    @patch("iphone_backup_reader.device.subprocess.Popen")
    @patch("iphone_backup_reader.device.device_support_installed", return_value=True)
    def test_full_backup_command(self, _installed: Mock, popen: Mock) -> None:
        process = Mock()
        process.stdout = io.StringIO("Starting\nFinished\n")
        process.wait.return_value = 0
        popen.return_value = process
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "backups"
            output: list[str] = []
            create_backup(self.device, destination, full=True, output=output.append)

        command = popen.call_args.args[0]
        self.assertEqual(
            [
                sys.executable,
                "-m",
                "pymobiledevice3",
                "--udid",
                "device-id",
                "backup2",
                "backup",
                "--full",
                str(destination),
            ],
            command,
        )
        self.assertEqual(["Starting", "Finished"], output)

    @patch("iphone_backup_reader.device._get_debug_snapshot", new_callable=AsyncMock)
    @patch("iphone_backup_reader.device.device_support_installed", return_value=True)
    def test_debug_snapshot_uses_selected_udid(self, _installed: Mock, snapshot: AsyncMock) -> None:
        snapshot.return_value = {"DeveloperModeEnabled": True}
        self.assertEqual({"DeveloperModeEnabled": True}, get_debug_snapshot(self.device))
        snapshot.assert_awaited_once_with("device-id")

    @patch("iphone_backup_reader.device._list_media_files", new_callable=AsyncMock)
    @patch("iphone_backup_reader.device.device_support_installed", return_value=True)
    def test_media_listing_uses_afc_path(self, _installed: Mock, listing: AsyncMock) -> None:
        listing.return_value = ["DCIM", "Downloads"]
        self.assertEqual(["DCIM", "Downloads"], list_media_files(self.device, "/"))
        listing.assert_awaited_once_with("device-id", "/")


if __name__ == "__main__":
    unittest.main()
