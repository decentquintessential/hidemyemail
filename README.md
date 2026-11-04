# hme — Hide My Email from the terminal

Manage your iCloud+ **Hide My Email** addresses on your Mac: list, search, create, relabel, deactivate, delete and export.

## Install

```bash
cd ~/Downloads/hidemyemail && ./install.sh
```

If it asks you to, add `~/.local/bin` to your PATH. Then:

```bash
hme login you@icloud.com
```

Enter your Apple ID password and the 2FA code that appears on your iPhone or Mac. The password is saved in the macOS Keychain (item `pyicloud://icloud-password`). The session is saved in `~/Library/Application Support/hme`. You only need to do 2FA again when Apple expires the session, usually every few weeks.

## Mac app

`install.sh` also builds **Hide My Email.app** into `~/Applications`. Open it from Spotlight or Launchpad. In the app you can:

- Sign in with your Apple ID and 2FA code. If you've already logged in with the `hme` command, the app signs in automatically.
- Search your addresses, switch between Active, Inactive and All, and sort by newest, oldest or name.
- Press ⌘N to create a new address. You can generate a different one before you confirm, and it's copied to the clipboard when created.
- Edit an address's label and note, copy it, deactivate or reactivate it, or delete it. The details also show where the address was created (Safari and the website, by hand, or an app), and search covers that too.
- ⌘-click or ⇧-click to select several addresses, then copy, deactivate, reactivate or delete them together. Right-click works on the selection too.
- Keyboard: ⌘C copies the selected addresses, Delete offers to deactivate or delete them, and the arrow keys move through the list.
- **Menu bar:** click the envelope icon in the menu bar, or press **⌃⌥⌘H** from any app, type what the address is for and press Return. The new address is created and copied to the clipboard, without opening the main window.
- Export everything to CSV, or sign out, from the ⋯ menu.

Note: iCloud doesn't allow editing the label or note of an inactive address. Reactivate it first.

The app is a SwiftUI front end. A background helper (`hme_helper.py`) does the iCloud work, using the same Python environment as the command-line tool. To rebuild only the app, run `app/build.sh`.

## Windows app

`windows/hide_my_email.py` is a Qt (PySide6) version of the app, with the same features. GitHub Actions builds it into a single `HideMyEmail.exe` on every push to `main`, using `.github/workflows/windows.yml`. To get the latest build:

1. Open the repository on GitHub and go to **Actions → Build Windows app**.
2. Open the latest run and download the **HideMyEmail-windows** artifact. It's a zip with two files:
   - `HideMyEmail-Setup.exe`, the installer (recommended). It installs for your user only, with no admin rights needed, adds a Start-menu shortcut, and offers a desktop shortcut and a "start when I sign in" option. You can uninstall it from Settings → Apps.
   - `HideMyEmail.exe`, the app on its own, which runs without installing.

Both files are unsigned, so Windows SmartScreen warns about them the first time. Click **More info → Run anyway**. Removing the warning would need a paid code-signing certificate.

The Windows app has the same features as the Mac app, including multi-select, Ctrl+C, Delete and a right-click menu. Instead of the menu bar it uses a **tray icon**: click it, or press **Ctrl+Alt+Shift+H** from any app, for the quick-create window. Closing the main window keeps the app running in the tray. To quit, right-click the tray icon.

On Windows, your password is stored in Credential Manager and the session in `%APPDATA%\HideMyEmail`.

Each build also runs `HideMyEmail.exe --selftest`, which checks that everything the app needs is packaged. It saves screenshots of the app rendered on Windows (main window, multi-select, quick-create) in the **selftest** artifact. The build also test-installs the installer.

## Command line usage

| Command | What it does |
|---|---|
| `hme list` | Active addresses, newest first |
| `hme list netflix` | Search address, label and note |
| `hme list --inactive` / `--all` | Deactivated ones / everything |
| `hme new "Bol.com" -n "webshop account"` | Create a new address and copy it to the clipboard |
| `hme copy bol` | Copy an existing address to the clipboard |
| `hme show bol` | Show all details (forward-to address, created date, ID) |
| `hme edit bol --label "Bol" --note "..."` | Change the label or note |
| `hme deactivate bol` | Stop forwarding (you can undo this) |
| `hme reactivate bol` | Start forwarding again |
| `hme delete bol` | Delete permanently (it deactivates first) |
| `hme export ~/Desktop/hme.csv` | Back up everything to a CSV file |
| `hme logout` | Remove the Keychain password and the session |

Wherever a command needs an address, you can give the full address, its label, or a unique part of either. If the text matches more than one address, hme lists the matches and asks you to be more specific.

## Caveats

- Apple has no official API for this. hme uses the same private iCloud web endpoints as icloud.com, through the [pyicloud](https://pypi.org/project/pyicloud/) library. Apple can change these endpoints at any time. If hme breaks, run `./install.sh` again to get the newest pyicloud.
- You need an iCloud+ subscription.
- Apple rate-limits address creation (roughly 5 per hour).
