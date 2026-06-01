"""
Interactive first-time login helper.

Run this once on any machine to cache the iCloud session:
    uv run python -m app.twofactor

The session cookies are saved to ~/.pyicloud and must be copied to the Pi
(or the Pi used directly) before the service will run unattended.
"""

from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudFailedLoginException

from app.config import config


def main() -> None:
    print(f"Logging in as {config.icloud_username}…")
    try:
        api = PyiCloudService(config.icloud_username, config.icloud_password)
    except PyiCloudFailedLoginException as exc:
        print(f"Login failed: {exc}")
        return

    if not api.requires_2fa:
        print("Already authenticated — no 2FA needed.")
        return

    print("2FA required. Trusted devices:")
    devices = api.trusted_devices
    for i, device in enumerate(devices):
        print(f"  [{i}] {device.get('deviceName', 'SMS to') + ' ' + device.get('phoneNumber', '')}")

    index = int(input("Select device index to receive code: "))
    device = devices[index]

    if not api.send_verification_code(device):
        print("Failed to send verification code.")
        return

    code = input("Enter the 6-digit code: ")
    if api.validate_verification_code(device, code):
        print("2FA complete. Session cached — you can now start the service.")
    else:
        print("Incorrect code.")


if __name__ == "__main__":
    main()
