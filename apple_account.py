"""Read-only access to the Hide My Email list on the Apple Account site (account.apple.com).

iCloud's Hide My Email list (what pyicloud reads) only has addresses made in iCloud,
Safari and apps. Addresses made with "Sign in with Apple" are listed by the Apple
Account site instead, which this module reads the same way that site does:

  1. sign in at idmsa.apple.com with the site's public widget key (SRP, like pyicloud),
     answering a 2FA code if Apple asks and keeping the trust token so it asks rarely;
  2. open an appleid.apple.com session (GET /account/manage/gs/ws/token);
  3. GET /account/manage/email/private  -> privateEmailList / inactivePrivateEmailList.

Apple doesn't document any of this; it mirrors what the site's own JavaScript does.
Nothing here changes the account.
"""

from __future__ import annotations

import base64
import json
import logging
import pickle
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
import srp
from pyicloud.base import SrpPassword, SrpProtocolType, b64_encode

# Published by https://appleid.apple.com/bootstrap/portal as "serviceKey".
WIDGET_KEY = "af1139274f266b22b68c2a3e7ad932cb3c0bbe854e13a79af78dcc73136882c3"
AUTH = "https://idmsa.apple.com/appleauth/auth"
API = "https://appleid.apple.com"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/18.3.1 Safari/605.1.15")
RELAY_DOMAINS = ("privaterelay.appleid.com", "private.icloud.com")

log = logging.getLogger("hme")


class AccountError(Exception):
    """Something went wrong talking to the Apple Account site; the message is for the user."""


class CodeRequired(Exception):
    """Apple wants a 2FA code before it shows the list."""


class AppleAccount:
    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.http = requests.Session()
        self.http.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/javascript",
            "Content-Type": "application/json",
            "X-Apple-Widget-Key": WIDGET_KEY,
            "X-Requested-With": "XMLHttpRequest",
        })
        self.trust_token: str | None = None
        self._load()

    # ------------------------------------------------------------ state

    def _load(self) -> None:
        try:
            state = pickle.loads(self.state_file.read_bytes())
            self.http.cookies.update(state["cookies"])
            self.trust_token = state.get("trust_token")
            for header in ("scnt", "X-Apple-ID-Session-Id"):
                if state.get(header):
                    self.http.headers[header] = state[header]
        except (FileNotFoundError, KeyError, pickle.UnpicklingError, EOFError):
            pass

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        state = {"cookies": self.http.cookies, "trust_token": self.trust_token,
                 "scnt": self.http.headers.get("scnt"),
                 "X-Apple-ID-Session-Id": self.http.headers.get("X-Apple-ID-Session-Id")}
        self.state_file.write_bytes(pickle.dumps(state))
        self.state_file.chmod(0o600)

    def forget(self) -> None:
        self.state_file.unlink(missing_ok=True)
        self.http.cookies.clear()
        self.trust_token = None

    def _keep(self, response: requests.Response) -> requests.Response:
        for header in ("scnt", "X-Apple-ID-Session-Id"):
            if response.headers.get(header):
                self.http.headers[header] = response.headers[header]
        if response.headers.get("X-Apple-TwoSV-Trust-Token"):
            self.trust_token = response.headers["X-Apple-TwoSV-Trust-Token"]
        return response

    def _auth_headers(self) -> dict[str, str]:
        return {"Origin": "https://idmsa.apple.com", "Referer": "https://idmsa.apple.com/"}

    def _api_headers(self) -> dict[str, str]:
        return {"Origin": API, "Referer": f"{API}/", "X-Apple-I-TimeZone-Offset": "0"}

    # ------------------------------------------------------------ sign-in

    def sign_in(self, apple_id: str, password: str) -> None:
        """Sign in; raises CodeRequired when Apple wants a 2FA code (it's sent to your devices)."""
        srp_password = SrpPassword(password)
        srp.rfc5054_enable()
        srp.no_username_in_x()
        user = srp.User(apple_id, srp_password, hash_alg=srp.SHA256, ng_type=srp.NG_2048)
        name, a = user.start_authentication()
        r = self._keep(self.http.post(f"{AUTH}/signin/init", headers=self._auth_headers(), json={
            "a": b64_encode(a), "accountName": name, "protocols": [p.value for p in SrpProtocolType]}))
        if r.status_code != 200:
            log.warning("apple account signin/init: HTTP %s", r.status_code)
            raise AccountError(f"Apple didn't accept the sign-in (step 1, HTTP {r.status_code}).")
        body = r.json()
        salt, b = base64.b64decode(body["salt"]), base64.b64decode(body["b"])
        srp_password.set_encrypt_info(salt, body["iteration"], 32, SrpProtocolType(body["protocol"]))
        m1 = user.process_challenge(salt, b)
        r = self._keep(self.http.post(
            f"{AUTH}/signin/complete", params={"isRememberMeEnabled": "true"}, headers=self._auth_headers(),
            json={"accountName": name, "c": body["c"], "m1": b64_encode(m1), "m2": b64_encode(user.H_AMK),
                  "rememberMe": True, "trustTokens": [self.trust_token] if self.trust_token else []}))
        log.info("apple account signin/complete: HTTP %s", r.status_code)
        if r.status_code == 409:
            # Ask Apple to send a code to the trusted devices.
            self._keep(self.http.get(AUTH, headers=self._auth_headers()))
            self._save()
            raise CodeRequired()
        if r.status_code != 200:
            raise AccountError(f"Apple didn't accept the sign-in (HTTP {r.status_code}). "
                               "If you changed your Apple ID password, sign out and in again.")
        self._open_api()

    def verify_code(self, code: str, apple_id: str, password: str) -> None:
        r = self._keep(self.http.post(f"{AUTH}/verify/trusteddevice/securitycode", headers=self._auth_headers(),
                                      json={"securityCode": {"code": code.strip()}}))
        log.info("apple account verify code: HTTP %s", r.status_code)
        if r.status_code not in (200, 204):
            raise AccountError("That code wasn't accepted. Check the code and try again.")
        r = self._keep(self.http.get(f"{AUTH}/2sv/trust", headers=self._auth_headers()))
        log.info("apple account trust: HTTP %s, trust token: %s", r.status_code, bool(self.trust_token))
        self._save()
        if self._open_api(raise_on_failure=False):
            return
        # The code makes this device trusted, but the account session only opens after a
        # fresh sign-in that presents the new trust token (Apple doesn't ask for a code again).
        log.info("apple account: signing in again with the trust token")
        try:
            self.sign_in(apple_id, password)
        except CodeRequired:
            raise AccountError("Apple asked for another verification code. Wait a minute, then try "
                               "Connect Apple Account again.") from None

    def _open_api(self, raise_on_failure: bool = True) -> bool:
        r = self._keep(self.http.get(f"{API}/account/manage/gs/ws/token", headers=self._api_headers()))
        log.info("apple account session: HTTP %s", r.status_code)
        if r.status_code != 200:
            if raise_on_failure:
                raise AccountError(f"Apple didn't open the account session (HTTP {r.status_code}).")
            return False
        self._save()
        return True

    # ------------------------------------------------------------ data

    def private_emails(self) -> dict[str, Any] | None:
        """The site's Hide My Email data, or None when the saved session has expired."""
        r = self._keep(self.http.get(f"{API}/account/manage/email/private", headers=self._api_headers()))
        log.info("apple account email/private: HTTP %s", r.status_code)
        if r.status_code in (401, 403):
            return None
        if r.status_code != 200:
            raise AccountError(f"Apple didn't return the list (HTTP {r.status_code}).")
        self._save()
        data = r.json()
        log.info("apple account list layout: %s", describe(data))
        return data


def describe(data: Any, depth: int = 0) -> Any:
    """Field names and types only (never values), for diagnosing a changed response."""
    if isinstance(data, dict):
        return {k: describe(v, depth + 1) if depth < 2 else type(v).__name__ for k, v in data.items()}
    if isinstance(data, list):
        return [f"{len(data)} items", describe(data[0], depth + 1)] if data else "0 items"
    return type(data).__name__


def to_millis(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) if value > 1e11 else float(value) * 1000
    if isinstance(value, str) and value:
        if value.isdigit():
            return to_millis(int(value))
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            return None
    return None


def normalise(data: dict[str, Any], exclude: set[str]) -> list[dict[str, Any]]:
    """Turn the site's lists into the app's alias format, keeping only addresses that
    aren't already in iCloud's list (in practice: those made with Sign in with Apple)."""
    forward = data.get("forwardToEmailAddress") or ""
    out = []
    for key, active in (("privateEmailList", True), ("inactivePrivateEmailList", False)):
        for item in data.get(key) or []:
            if not isinstance(item, dict):
                continue
            address = item.get("emailAddress") or item.get("address") or ""
            if not address or address.lower() in exclude:
                continue
            name = item.get("displayName") or item.get("label") or ""
            out.append({
                "hme": address,
                "label": name,
                "note": item.get("note") or "",
                "isActive": active,
                "createTimestamp": to_millis(item.get("createdDate")),
                "anonymousId": f"siwa:{item.get('id') or address}",
                "forwardToEmail": forward,
                "origin": "SIGN_IN_WITH_APPLE",
                "domain": "",
                "siwa": True,
                "type": item.get("type") or "",
            })
    return out
