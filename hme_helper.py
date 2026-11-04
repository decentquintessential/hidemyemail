#!/usr/bin/env python3
"""Backend for the Hide My Email Mac app.

Speaks JSON lines over stdin/stdout: each request is
{"id": 1, "cmd": "list", ...} and each reply is
{"id": 1, "ok": true, "result": ...} or {"id": 1, "ok": false, "error": "..."}.
The iCloud session lives in this process, so 2FA can be a two-step exchange.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudFailedLoginException
from pyicloud.utils import (
    delete_password_in_keyring,
    get_password_from_keyring,
    store_password_in_keyring,
)

# Shared with the hme CLI, so logging in with one logs in the other.
if sys.platform == "win32":
    APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "HideMyEmail"
else:
    APP_DIR = Path.home() / "Library" / "Application Support" / "hme"
SESSION_DIR = APP_DIR / "session"
CONFIG_FILE = APP_DIR / "config.json"

# Set in main(): the real stdout, kept free of stray library prints.
PROTOCOL_OUT = sys.stdout


class HelperError(Exception):
    pass


def with_retry(fn, attempts: int = 4):
    """Call fn, retrying iCloud's transient "Something went wrong. Try again later" errors.

    iCloud sometimes answers a Hide My Email change with errorCode 500 and a
    retryAfter hint (seconds); the same call then succeeds a moment later.
    """
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            text = str(exc)
            transient = '"errorCode":"500"' in text or "Try again later" in text
            if not transient or attempt == attempts - 1:
                raise
            match = re.search(r'"retryAfter":\s*(\d+)', text)
            time.sleep(max(int(match.group(1)) if match else 2, 2) * (attempt + 1))


class Backend:
    def __init__(self) -> None:
        self.api: PyiCloudService | None = None
        self.apple_id: str | None = None
        self.password: str | None = None

    # ------------------------------------------------------------ auth

    def config(self) -> dict[str, Any]:
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def cmd_status(self, req) -> dict[str, Any]:
        apple_id = self.config().get("apple_id")
        has_password = bool(apple_id and get_password_from_keyring(apple_id))
        return {"apple_id": apple_id, "has_password": has_password}

    def cmd_login(self, req) -> dict[str, Any]:
        apple_id = (req.get("apple_id") or self.config().get("apple_id") or "").strip()
        if not apple_id:
            raise HelperError("Enter your Apple ID.")
        password = req.get("password") or get_password_from_keyring(apple_id)
        if not password:
            raise HelperError("Enter your password.")
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        try:
            api = PyiCloudService(apple_id, password, cookie_directory=str(SESSION_DIR))
        except PyiCloudFailedLoginException as exc:
            raise HelperError(f"Login failed: {exc}") from exc
        self.api, self.apple_id, self.password = api, apple_id, password

        if api.requires_2fa:
            if list(getattr(api, "fido2_devices", []) or []):
                return {"state": "security_key"}
            try:
                api.request_2fa_code()
            except Exception:
                # A code may already be showing on a trusted device.
                api.use_existing_trusted_device_code()
            return {"state": "2fa"}
        self._finish_login()
        return {"state": "ok"}

    def cmd_verify(self, req) -> dict[str, Any]:
        api = self._require_api(allow_pending=True)
        if req.get("security_key"):
            api.confirm_security_key(list(api.fido2_devices)[0])
        elif not api.validate_2fa_code(str(req.get("code", "")).strip()):
            raise HelperError("That code is not valid.")
        if not api.is_trusted_session:
            api.trust_session()
        self._finish_login()
        return {"state": "ok"}

    def _finish_login(self) -> None:
        store_password_in_keyring(self.apple_id, self.password)
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps({"apple_id": self.apple_id}, indent=2))

    def cmd_logout(self, req) -> dict[str, Any]:
        apple_id = self.config().get("apple_id")
        if apple_id:
            try:
                delete_password_in_keyring(apple_id)
            except Exception:
                pass
        shutil.rmtree(SESSION_DIR, ignore_errors=True)
        CONFIG_FILE.unlink(missing_ok=True)
        self.api = None
        return {}

    def _require_api(self, allow_pending: bool = False) -> PyiCloudService:
        if self.api is None:
            raise HelperError("Not logged in.")
        if not allow_pending and self.api.requires_2fa:
            raise HelperError("Two-factor verification is not finished.")
        return self.api

    # ------------------------------------------------------------ aliases

    def cmd_list(self, req) -> list[dict[str, Any]]:
        return with_retry(lambda: list(self._require_api().hidemyemail))

    def cmd_generate(self, req) -> dict[str, Any]:
        email = with_retry(self._require_api().hidemyemail.generate)
        if not email:
            raise HelperError("iCloud did not generate an address. You may have hit Apple's hourly limit.")
        return {"hme": email}

    def cmd_reserve(self, req) -> dict[str, Any]:
        hme = self._require_api().hidemyemail
        return self._checked(
            lambda: hme.reserve(req["hme"], req["label"], req.get("note") or ""), "Creating the address"
        )

    def cmd_update(self, req) -> dict[str, Any]:
        hme = self._require_api().hidemyemail
        return self._checked(lambda: hme.update_metadata(req["id"], req["label"], req.get("note")), "Saving")

    def cmd_deactivate(self, req) -> dict[str, Any]:
        hme = self._require_api().hidemyemail
        return self._checked(lambda: hme.deactivate(req["id"]), "Deactivating")

    def cmd_reactivate(self, req) -> dict[str, Any]:
        hme = self._require_api().hidemyemail
        return self._checked(lambda: hme.reactivate(req["id"]), "Reactivating")

    def cmd_delete(self, req) -> dict[str, Any]:
        hme = self._require_api().hidemyemail
        # iCloud only deletes addresses that are already deactivated.
        if req.get("active"):
            self._checked(lambda: hme.deactivate(req["id"]), "Deactivating")
        return self._checked(lambda: hme.delete(req["id"]), "Deleting")

    @staticmethod
    def _checked(call, what: str) -> dict[str, Any]:
        try:
            result = with_retry(call)
        except Exception as exc:
            if "Try again later" not in str(exc):
                raise
            raise HelperError(
                f"{what} failed: iCloud kept answering \"try again later\". "
                "Wait a minute and try again."
            ) from exc
        if not result:
            raise HelperError(f"{what} failed (iCloud returned no result).")
        return result


def reply(payload: dict[str, Any]) -> None:
    PROTOCOL_OUT.write(json.dumps(payload) + "\n")
    PROTOCOL_OUT.flush()


def main() -> None:
    sys.stdout = sys.stderr
    backend = Backend()
    for line in sys.stdin:
        if not line.strip():
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            handler = getattr(backend, f"cmd_{req.get('cmd')}", None)
            if handler is None:
                raise HelperError(f"Unknown command {req.get('cmd')!r}.")
            reply({"id": req_id, "ok": True, "result": handler(req)})
        except HelperError as exc:
            reply({"id": req_id, "ok": False, "error": str(exc)})
        except Exception as exc:  # surface anything from pyicloud to the UI
            reply({"id": req_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    main()
