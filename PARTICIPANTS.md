# Participants: running the listener

At demo time an organizer's webcam station plays your game through head gestures.
`listener.py` runs on **your** laptop and presses **Arrow keys and Z** for it.
That's the only input it ever sends, so build your game around **Arrow keys + Z**.

You'll get from the organizers: your **team ID** (e.g. `07`), the **server URL**, and the **token**.

## Setup in 5 steps

**1. Install Python 3.10 or newer**
- Windows: <https://www.python.org/downloads/>. Tick **"Add python.exe to PATH"** during install.
- macOS: <https://www.python.org/downloads/> (or `brew install python`).
- Linux: `sudo apt install python3 python3-venv python3-pip` (or your distro's equivalent).

**2. Get the files.** Download `listener.py`, `requirements-listener.txt` and
`tools/key_viewer.html` from the event repo (or `git clone` it). Open a terminal in that folder.

**3. Install the dependencies**

Windows (PowerShell or cmd):
```bash
py -m pip install -r requirements-listener.txt
```
macOS / Linux (a virtual environment avoids "externally managed" errors):
```bash
python3 -m venv venv && source venv/bin/activate && pip install -r requirements-listener.txt
```

**4. Check it works**

First the connection test. It presses **no** keys. Run it at the registration desk:
```bash
python listener.py --team 07 --server wss://<app>.onrender.com --token <TOKEN> --test
```
You should see `CONNECTED (team 07)` and `Registered as team 07`.
(On Windows use `py` instead of `python` if `python` isn't found. On macOS/Linux use the venv from step 3.)

Then check that key presses arrive. Stop the test with Ctrl+C, run the same command **without** `--test`,
open `tools/key_viewer.html` in a browser, **click the page**, and ask an organizer to send test keys.
The arrows and Z should light up.

**5. Demo time.** Start the listener (without `--test`), start your game, and
**click your game window so it has keyboard focus**. Leave both running. Ctrl+C stops the listener.

Tip: to skip typing the options, copy `config.example.json` to `config.json` next to
`listener.py` and fill in `server`, `token` and `team`. Then just run `python listener.py`.

## What you'll see

```
[14:02:10] CONNECTED  (team 07)
[14:02:10] Registered as team 07. Controlled by: nobody yet
[14:05:31] >>> Now controlled by station: station2
[14:05:33] KEY  left  DOWN   held: left
[14:05:34] KEY  left  UP     held: -
[14:15:31] RELEASE ALL (station released the team)
```

Safety: if the connection drops, if the station lets go, or if nothing arrives for
2 seconds, the listener releases every key, so nothing stays stuck.
It reconnects on its own.

## Troubleshooting

**Keys arrive in the terminal but the game doesn't react**
- Click inside your game window. Keys go to whichever window has focus.
- Windows: if your game or engine runs **as Administrator**, run the terminal as Administrator too.
  Windows blocks key presses into elevated windows.
- Make sure the game reads Arrow keys and Z (not WASD/space).
- Check with `tools/key_viewer.html` first. If keys show there, the problem is in the game.

**macOS: "macOS has NOT given this terminal permission to press keys"**
1. System Settings → Privacy & Security → **Accessibility**.
2. Add the app you run the script from (Terminal, iTerm, VS Code...) and switch it **on**.
   If it's already there, switch it off and on again.
3. Quit that app completely (Cmd+Q), reopen it, run the listener again.

**Linux: keys don't arrive**
- `pynput` needs an **X11** session. On Wayland, log out and pick "Ubuntu on Xorg" (or similar)
  at the login screen.

**Antivirus warning or the script gets blocked**
- `listener.py` is a plain, readable Python file that simulates key presses, which some antivirus
  tools flag. Allow it, or add an exception for the folder. We deliberately ship no `.exe`.

**`CERTIFICATE_VERIFY_FAILED` / SSL errors (macOS)**
- Make sure `certifi` installed (step 3). With python.org Python you can also run
  *Applications → Python 3.x → Install Certificates.command*.

**"the relay rejected the token"**: check the token for typos.

**"another laptop connected as team 07"**: two laptops used the same team ID.
Only one listener per team. Check your team number.

**Stuck on "Reconnecting..."**: check your internet. The organizers may switch to a
local fallback server and give you a new `ws://...` address. Restart with that `--server`.
