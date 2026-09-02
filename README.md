# iPhone Backup Reader

A Windows desktop application that browses local, unencrypted iPhone backups. It can also detect a USB-connected iPhone and create a standard Apple backup, which then becomes readable in the application.

## Features

- Finds backups in `D:\MobileSync\Backup` by default
- Shows device name, iOS version, backup date, serial/UDID, and encryption status
- Searches the backup manifest by domain or original path
- Quick filters for photos, messages, contacts, and notes
- Previews text, plist, JSON, and SQLite files
- Exports selected files using their original names
- Detects paired USB iPhones and creates full or incremental backups
- Reads raw Lockdown properties, battery/Wi-Fi diagnostics, and Developer Mode status
- Streams the live iOS system console with text filtering
- Browses and exports the phone's AFC media area (`/var/mobile/Media`)
- Lists and exports device crash reports

All reading and exporting stays local. The application never modifies an existing backup.

The **Admin / Debug** tab uses Apple's trusted-device services. On stock iOS, Apple blocks arbitrary root-filesystem, memory, keychain, and private app-container access. Developer Mode does not bypass those protections; unrestricted raw access requires a separately jailbroken research device.

## Run

Python 3.11 or newer is required.

```powershell
py -m iphone_backup_reader
```

Or double-click `run.bat`.

## Connected iPhone setup

iOS does not expose all phone data as a browsable drive. The supported flow is to pair with the phone, create a standard backup, and browse that backup.

1. Install **Apple Devices** or **iTunes** so Windows has Apple Mobile Device Support.
2. Unlock the iPhone, connect it by USB, and tap **Trust** when prompted.
3. Install connected-device support:

   ```powershell
   py -m pip install -e ".[device]"
   ```

   Alternatively, double-click `setup_connected_phone.bat`.

4. Restart the app and select **Connected iPhone** → **Refresh devices**.

If the existing backup is encrypted, its manifest and files cannot be read without decrypting them first. Creating a new backup from a phone that has encrypted backups enabled is supported, but its contents remain encrypted.

## Tests

```powershell
py -m unittest discover -s tests -v
```
