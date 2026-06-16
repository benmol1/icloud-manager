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

    if api.requires_2fa:
        _handle_2fa(api)
    elif api.requires_2sa:
        _handle_2sa(api)
    else:
        print("Already authenticated — no verification needed.")
        return

    _trust(api)


def _handle_2fa(api: PyiCloudService) -> None:
    """Modern two-factor auth: Apple auto-pushes a code to trusted devices."""
    print("Two-factor authentication required.")
    print("A 6-digit code has been sent to your Apple trusted devices.")
    code = input("Enter the 6-digit code: ").strip()
    if api.validate_2fa_code(code):
        print("2FA code accepted.")
    else:
        print("Incorrect or expired code. Re-run the script to try again.")
        raise SystemExit(1)


def _handle_2sa(api: PyiCloudService) -> None:
    """Legacy two-step auth fallback (SMS / device list)."""
    print("Two-step authentication required. Trusted devices:")
    devices = api.trusted_devices
    for i, device in enumerate(devices):
        label = device.get("deviceName") or f"SMS to {device.get('phoneNumber', '')}"
        print(f"  [{i}] {label}")

    index = int(input("Select device index to receive code: "))
    device = devices[index]
    if not api.send_verification_code(device):
        print("Failed to send verification code.")
        raise SystemExit(1)

    code = input("Enter the 6-digit code: ").strip()
    if not api.validate_verification_code(device, code):
        print("Incorrect code.")
        raise SystemExit(1)
    print("2SA code accepted.")


def _trust(api: PyiCloudService) -> None:
    """Establish a trust token so future logins skip verification."""
    if api.trust_session():
        print("Session trusted and cached — you can now start the service.")
    else:
        print(
            "Warning: could not establish a trusted session; you may be "
            "prompted again next run."
        )
    if not api.is_trusted_session:
        print("Note: session is not marked trusted yet.")


if __name__ == "__main__":
    main()
