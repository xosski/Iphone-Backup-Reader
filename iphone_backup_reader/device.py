from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class DeviceSupportError(Exception):
    """Raised when connected-device support is unavailable."""


@dataclass(frozen=True)
class ConnectedDevice:
    udid: str
    name: str
    product_type: str
    ios_version: str
    serial_number: str


def device_support_installed() -> bool:
    return importlib.util.find_spec("pymobiledevice3") is not None


def list_connected_devices() -> list[ConnectedDevice]:
    if not device_support_installed():
        raise DeviceSupportError(
            'Connected-device support is not installed. Run: py -m pip install -e ".[device]"'
        )
    try:
        return asyncio.run(_list_connected_devices())
    except Exception as error:
        detail = f" Details: {error}" if str(error) else ""
        raise DeviceSupportError(
            "Could not communicate with an iPhone. Install Apple Devices or iTunes, unlock the phone, "
            f"connect it by USB, and tap Trust.{detail}"
        ) from error


async def _list_connected_devices() -> list[ConnectedDevice]:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.usbmux import list_devices

    result: list[ConnectedDevice] = []
    for device in await list_devices():
        if not device.is_usb:
            continue
        lockdown = await create_using_usbmux(
            serial=device.serial,
            connection_type="USB",
            autopair=True,
            pair_timeout=15,
        )
        async with lockdown:
            info = lockdown.all_values
            result.append(
                ConnectedDevice(
                    udid=device.serial,
                    name=str(info.get("DeviceName") or "iPhone"),
                    product_type=str(info.get("ProductType") or "Unknown"),
                    ios_version=str(info.get("ProductVersion") or "Unknown"),
                    serial_number=str(info.get("SerialNumber") or "Unknown"),
                )
            )
    return result


def create_backup(
    device: ConnectedDevice,
    destination: Path,
    full: bool,
    output: Callable[[str], None],
) -> None:
    if not device_support_installed():
        raise DeviceSupportError("Connected-device support is not installed.")
    destination.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pymobiledevice3",
        "--udid",
        device.udid,
        "backup2",
        "backup",
    ]
    if full:
        command.append("--full")
    command.append(str(destination))

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as error:
        raise DeviceSupportError(f"Could not start the backup command: {error}") from error
    assert process.stdout is not None
    for line in process.stdout:
        output(line.rstrip())
    return_code = process.wait()
    if return_code:
        raise DeviceSupportError(f"Backup command failed with exit code {return_code}.")


def get_debug_snapshot(device: ConnectedDevice) -> dict[str, Any]:
    _require_device_support()
    try:
        return asyncio.run(_get_debug_snapshot(device.udid))
    except Exception as error:
        raise DeviceSupportError(f"Could not read device diagnostics: {error}") from error


async def _get_debug_snapshot(udid: str) -> dict[str, Any]:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.diagnostics import DiagnosticsService

    async with await create_using_usbmux(serial=udid, connection_type="USB") as lockdown:
        snapshot: dict[str, Any] = {"Lockdown": lockdown.all_values}
        try:
            snapshot["DeveloperModeEnabled"] = await lockdown.get_developer_mode_status()
        except Exception as error:
            snapshot["DeveloperModeError"] = str(error)
        async with DiagnosticsService(lockdown=lockdown) as diagnostics:
            for name, operation in (
                ("Diagnostics", diagnostics.info),
                ("Battery", diagnostics.get_battery),
                ("WiFi", diagnostics.get_wifi),
            ):
                try:
                    snapshot[name] = await operation()
                except Exception as error:
                    snapshot[f"{name}Error"] = str(error)
        return snapshot


def list_media_files(device: ConnectedDevice, path: str) -> list[str]:
    _require_device_support()
    try:
        return asyncio.run(_list_media_files(device.udid, path))
    except Exception as error:
        raise DeviceSupportError(f"Could not list the phone's media storage: {error}") from error


async def _list_media_files(udid: str, path: str) -> list[str]:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService

    async with await create_using_usbmux(serial=udid, connection_type="USB") as lockdown:
        async with AfcService(lockdown=lockdown) as afc:
            return sorted(name for name in await afc.listdir(path) if name not in (".", ".."))


def export_media_path(device: ConnectedDevice, remote_path: str, destination: Path) -> None:
    _require_device_support()
    destination.mkdir(parents=True, exist_ok=True)
    try:
        asyncio.run(_export_media_path(device.udid, remote_path, destination))
    except Exception as error:
        raise DeviceSupportError(f"Could not export media data: {error}") from error


async def _export_media_path(udid: str, remote_path: str, destination: Path) -> None:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService

    async with await create_using_usbmux(serial=udid, connection_type="USB") as lockdown:
        async with AfcService(lockdown=lockdown) as afc:
            await afc.pull(remote_path, str(destination), ignore_errors=False, progress_bar=False)


def list_crash_reports(device: ConnectedDevice) -> list[str]:
    _require_device_support()
    try:
        return asyncio.run(_list_crash_reports(device.udid))
    except Exception as error:
        raise DeviceSupportError(f"Could not list crash reports: {error}") from error


async def _list_crash_reports(udid: str) -> list[str]:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.crash_reports import CrashReportsManager

    async with await create_using_usbmux(serial=udid, connection_type="USB") as lockdown:
        async with CrashReportsManager(lockdown) as crashes:
            await crashes.flush()
            return await crashes.ls("/", depth=-1)


def export_crash_report(device: ConnectedDevice, remote_path: str, destination: Path) -> None:
    _require_device_support()
    destination.mkdir(parents=True, exist_ok=True)
    try:
        asyncio.run(_export_crash_report(device.udid, remote_path, destination))
    except Exception as error:
        raise DeviceSupportError(f"Could not export crash report: {error}") from error


async def _export_crash_report(udid: str, remote_path: str, destination: Path) -> None:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.crash_reports import CrashReportsManager

    async with await create_using_usbmux(serial=udid, connection_type="USB") as lockdown:
        async with CrashReportsManager(lockdown) as crashes:
            await crashes.pull(str(destination), entry=remote_path, erase=False, progress_bar=False)


class DeviceLogStream:
    def __init__(
        self,
        device: ConnectedDevice,
        output: Callable[[str], None],
        stopped: Callable[[str | None], None],
    ) -> None:
        self.device = device
        self.output = output
        self.stopped = stopped
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_requested = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        _require_device_support()
        self._thread.start()

    def stop(self) -> None:
        self._stop_requested.set()
        if self._loop is not None and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)

    def _run(self) -> None:
        error: str | None = None
        try:
            asyncio.run(self._run_async())
        except Exception as exception:
            error = str(exception)
        self.stopped(error)

    async def _run_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._stream())
        if self._stop_requested.is_set():
            self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def _stream(self) -> None:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.syslog import SyslogService

        async with await create_using_usbmux(serial=self.device.udid, connection_type="USB") as lockdown:
            async with SyslogService(service_provider=lockdown) as service:
                async for line in service.watch():
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="replace")
                    self.output(line.rstrip())


def _require_device_support() -> None:
    if not device_support_installed():
        raise DeviceSupportError(
            'Connected-device support is not installed. Run: py -m pip install -e ".[device]"'
        )
