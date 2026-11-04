#!/usr/bin/env python3
"""hme - manage Apple iCloud+ Hide My Email addresses from the Mac terminal.

Uses the (unofficial) iCloud web API through pyicloud. Your Apple ID password is
kept in the macOS Keychain; the iCloud session is kept in
~/Library/Application Support/hme so you only do 2FA occasionally.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudFailedLoginException
from pyicloud.utils import (
    delete_password_in_keyring,
    get_password_from_keyring,
    store_password_in_keyring,
)

APP_DIR = Path.home() / "Library" / "Application Support" / "hme"
SESSION_DIR = APP_DIR / "session"
CONFIG_FILE = APP_DIR / "config.json"


class HmeError(Exception):
    pass


# ---------------------------------------------------------------- config / auth


def load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(config: dict[str, Any]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


def complete_2fa(api: PyiCloudService) -> None:
    if api.requires_2fa:
        fido2 = list(getattr(api, "fido2_devices", []) or [])
        if fido2:
            print("Touch your security key to continue...")
            api.confirm_security_key(fido2[0])
        else:
            try:
                api.request_2fa_code()
            except Exception:
                # A code may already be showing on a trusted device.
                api.use_existing_trusted_device_code()
            print("A verification code was sent to your Apple devices.")
            for attempt in range(3):
                code = input("Enter 2FA code: ").strip()
                if api.validate_2fa_code(code):
                    break
                print("Invalid code.")
            else:
                raise HmeError("2FA failed.")
        if not api.is_trusted_session:
            api.trust_session()
    elif api.requires_2sa:
        devices = api.trusted_devices
        device = devices[0]
        if not api.send_verification_code(device):
            raise HmeError("Could not send verification code.")
        code = input("Enter verification code: ").strip()
        if not api.validate_verification_code(device, code):
            raise HmeError("Verification failed.")


def connect(apple_id: str | None = None, prompt_password: bool = False):
    config = load_config()
    apple_id = apple_id or config.get("apple_id")
    if not apple_id:
        raise HmeError("Not logged in. Run: hme login you@icloud.com")

    password = None if prompt_password else get_password_from_keyring(apple_id)
    if not password:
        password = getpass.getpass(f"Apple ID password for {apple_id}: ")

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    try:
        api = PyiCloudService(apple_id, password, cookie_directory=str(SESSION_DIR))
    except PyiCloudFailedLoginException as exc:
        raise HmeError(f"Login failed: {exc}") from exc
    complete_2fa(api)
    return api, apple_id, password


# ---------------------------------------------------------------- helpers


def copy_to_clipboard(text: str) -> bool:
    if shutil.which("pbcopy"):
        subprocess.run(["pbcopy"], input=text.encode(), check=False)
        return True
    return False


def fmt_date(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def fetch_all(api) -> list[dict[str, Any]]:
    return list(api.hidemyemail)


def find_alias(aliases: list[dict[str, Any]], query: str) -> dict[str, Any]:
    q = query.lower()
    exact = [
        a for a in aliases
        if a.get("hme", "").lower() == q or a.get("anonymousId") == query
    ]
    if exact:
        return exact[0]
    matches = [
        a for a in aliases
        if q in a.get("hme", "").lower() or q in (a.get("label") or "").lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise HmeError(f"No alias matches '{query}'.")
    print_table(matches)
    raise HmeError(f"'{query}' matches {len(matches)} aliases; be more specific.")


def print_table(aliases: list[dict[str, Any]]) -> None:
    rows = [
        (
            "●" if a.get("isActive", True) else "○",
            a.get("hme", ""),
            a.get("label") or "",
            fmt_date(a.get("createTimestamp")),
            (a.get("note") or "").replace("\n", " ")[:40],
        )
        for a in aliases
    ]
    headers = ("", "Address", "Label", "Created", "Note")
    widths = [max(len(str(r[i])) for r in rows + [headers]) for i in range(5)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)))


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def check_result(result: dict[str, Any], what: str) -> None:
    if not result:
        raise HmeError(f"{what} failed (iCloud returned no result).")


# ---------------------------------------------------------------- commands


def cmd_login(args) -> None:
    api, apple_id, password = connect(args.apple_id, prompt_password=True)
    store_password_in_keyring(apple_id, password)
    save_config({"apple_id": apple_id})
    count = len(fetch_all(api))
    print(f"Logged in as {apple_id}. You have {count} Hide My Email addresses.")


def cmd_logout(args) -> None:
    apple_id = load_config().get("apple_id")
    if apple_id:
        try:
            delete_password_in_keyring(apple_id)
        except Exception:
            pass
    shutil.rmtree(SESSION_DIR, ignore_errors=True)
    CONFIG_FILE.unlink(missing_ok=True)
    print("Logged out; Keychain password and session removed.")


def cmd_list(args) -> None:
    api, *_ = connect()
    aliases = fetch_all(api)
    if not args.all:
        want_active = not args.inactive
        aliases = [a for a in aliases if a.get("isActive", True) == want_active]
    if args.search:
        q = args.search.lower()
        aliases = [
            a for a in aliases
            if q in a.get("hme", "").lower()
            or q in (a.get("label") or "").lower()
            or q in (a.get("note") or "").lower()
        ]
    aliases.sort(key=lambda a: a.get("createTimestamp", 0), reverse=True)
    if args.json:
        print(json.dumps(aliases, indent=2))
        return
    if not aliases:
        print("No matching addresses.")
        return
    print_table(aliases)
    print(f"\n{len(aliases)} address(es)  ● active  ○ inactive")


def cmd_new(args) -> None:
    api, *_ = connect()
    email = api.hidemyemail.generate()
    if not email:
        raise HmeError("iCloud did not generate an address (limit reached?).")
    result = api.hidemyemail.reserve(email, args.label, args.note or "")
    check_result(result, "Reserving the address")
    copied = copy_to_clipboard(email)
    print(email + ("   (copied to clipboard)" if copied else ""))


def cmd_show(args) -> None:
    api, *_ = connect()
    alias = find_alias(fetch_all(api), args.query)
    details = api.hidemyemail[alias["anonymousId"]] or alias
    if args.json:
        print(json.dumps(details, indent=2))
        return
    fields = [
        ("Address", details.get("hme")),
        ("Label", details.get("label")),
        ("Note", details.get("note")),
        ("Active", "yes" if details.get("isActive", True) else "no"),
        ("Forwards to", details.get("forwardToEmail")),
        ("Created", fmt_date(details.get("createTimestamp"))),
        ("Origin", details.get("origin")),
        ("ID", details.get("anonymousId")),
    ]
    for name, value in fields:
        if value:
            print(f"{name:12} {value}")


def cmd_edit(args) -> None:
    api, *_ = connect()
    alias = find_alias(fetch_all(api), args.query)
    label = args.label if args.label is not None else alias.get("label") or ""
    note = args.note if args.note is not None else alias.get("note")
    result = api.hidemyemail.update_metadata(alias["anonymousId"], label, note)
    check_result(result, "Update")
    print(f"Updated {alias['hme']}.")


def cmd_copy(args) -> None:
    api, *_ = connect()
    alias = find_alias(fetch_all(api), args.query)
    copy_to_clipboard(alias["hme"])
    print(f"{alias['hme']}   (copied to clipboard)")


def cmd_deactivate(args) -> None:
    api, *_ = connect()
    alias = find_alias(fetch_all(api), args.query)
    if not alias.get("isActive", True):
        print(f"{alias['hme']} is already inactive.")
        return
    if not confirm(f"Stop forwarding mail from {alias['hme']}?", args.yes):
        return
    check_result(api.hidemyemail.deactivate(alias["anonymousId"]), "Deactivate")
    print(f"Deactivated {alias['hme']}.")


def cmd_reactivate(args) -> None:
    api, *_ = connect()
    alias = find_alias(fetch_all(api), args.query)
    if alias.get("isActive", True):
        print(f"{alias['hme']} is already active.")
        return
    check_result(api.hidemyemail.reactivate(alias["anonymousId"]), "Reactivate")
    print(f"Reactivated {alias['hme']}.")


def cmd_delete(args) -> None:
    api, *_ = connect()
    alias = find_alias(fetch_all(api), args.query)
    if not confirm(
        f"Permanently delete {alias['hme']} ({alias.get('label') or 'no label'})? "
        "This cannot be undone.",
        args.yes,
    ):
        return
    # iCloud only deletes addresses that are already deactivated.
    if alias.get("isActive", True):
        check_result(api.hidemyemail.deactivate(alias["anonymousId"]), "Deactivate")
    check_result(api.hidemyemail.delete(alias["anonymousId"]), "Delete")
    print(f"Deleted {alias['hme']}.")


def cmd_export(args) -> None:
    api, *_ = connect()
    aliases = fetch_all(api)
    out = Path(args.file).expanduser()
    with out.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["address", "label", "note", "active", "created", "forward_to", "id"])
        for a in aliases:
            writer.writerow([
                a.get("hme"),
                a.get("label"),
                a.get("note"),
                a.get("isActive", True),
                fmt_date(a.get("createTimestamp")),
                a.get("forwardToEmail"),
                a.get("anonymousId"),
            ])
    print(f"Exported {len(aliases)} addresses to {out}.")


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hme", description="Manage iCloud+ Hide My Email addresses."
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("login", help="log in and save your password in the Keychain")
    s.add_argument("apple_id", nargs="?", help="Apple ID email (remembered afterwards)")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("logout", help="forget password and session")
    s.set_defaults(func=cmd_logout)

    s = sub.add_parser("list", aliases=["ls"], help="list addresses")
    s.add_argument("search", nargs="?", help="filter on address, label or note")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--inactive", action="store_true", help="only deactivated ones")
    g.add_argument("--all", "-a", action="store_true", help="active and inactive")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("new", help="create an address and copy it to the clipboard")
    s.add_argument("label", help="what it's for, e.g. 'Netflix'")
    s.add_argument("--note", "-n")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("show", help="show details of one address")
    s.add_argument("query", help="address, label, or part of either")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("copy", help="copy an address to the clipboard")
    s.add_argument("query")
    s.set_defaults(func=cmd_copy)

    s = sub.add_parser("edit", help="change label and/or note")
    s.add_argument("query")
    s.add_argument("--label", "-l")
    s.add_argument("--note", "-n")
    s.set_defaults(func=cmd_edit)

    for name, func, help_ in (
        ("deactivate", cmd_deactivate, "stop forwarding mail (reversible)"),
        ("reactivate", cmd_reactivate, "resume forwarding mail"),
        ("delete", cmd_delete, "permanently delete an address"),
    ):
        s = sub.add_parser(name, help=help_)
        s.add_argument("query")
        if func is not cmd_reactivate:
            s.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
        s.set_defaults(func=func)

    s = sub.add_parser("export", help="export all addresses to CSV")
    s.add_argument("file", help="e.g. ~/Desktop/hide-my-email.csv")
    s.set_defaults(func=cmd_export)
    return p


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except HmeError as exc:
        sys.exit(f"hme: {exc}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
