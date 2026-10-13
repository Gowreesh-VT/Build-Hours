"""
Team listener: receives key events from the event relay and presses the keys
on this laptop, so a station's head gestures can play your game.

Usage:
    python listener.py --team 07 --server wss://<app>.onrender.com --token XXX
    python listener.py --test ...     (prints events, presses NO keys)

Values can also come from config.json ("team", "server", "token").
Only Arrow keys and Z are ever pressed.
"""

import argparse
import asyncio
import json
import os
import platform
import ssl
import sys
import time

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

VALID_KEYS = ("up", "down", "left", "right", "z")
SILENCE_TIMEOUT = 2.0    # release everything if nothing arrives for this long
MAX_BACKOFF = 5.0

CLOSE_BAD_TOKEN = 4001
CLOSE_REPLACED = 4003

SYSTEM = platform.system()   # "Windows", "Darwin" or "Linux"


class FatalError(Exception):
    """A problem that reconnecting won't fix (wrong token, duplicate team)."""


def log(text):
    print(time.strftime("[%H:%M:%S] ") + text, flush=True)


# --------------------------------------------------------- key injection ----

def make_injector():
    """Return (press, release) functions for this OS."""
    if SYSTEM == "Windows":
        # pydirectinput sends scan codes, which Pygame/Unity/DirectX games
        # read; plain virtual-key events are often ignored by them.
        import pydirectinput
        pydirectinput.PAUSE = 0          # no artificial delay between calls
        pydirectinput.FAILSAFE = False   # never abort because of mouse position
        return pydirectinput.keyDown, pydirectinput.keyUp

    from pynput.keyboard import Controller, Key
    keyboard = Controller()
    keymap = {"up": Key.up, "down": Key.down, "left": Key.left,
              "right": Key.right, "z": "z"}
    return (lambda k: keyboard.press(keymap[k]),
            lambda k: keyboard.release(keymap[k]))


class Keys:
    """Tracks which keys we are holding so nothing ever stays stuck."""

    def __init__(self, inject):
        self.inject = inject
        self.held = set()
        self.press_fn = self.release_fn = None
        if inject:
            self.press_fn, self.release_fn = make_injector()

    def _held_text(self):
        return ", ".join(k for k in VALID_KEYS if k in self.held) or "-"

    def press(self, key):
        if key in self.held:
            return                       # already down: never auto-repeat
        if self.inject:
            self.press_fn(key)
        self.held.add(key)
        log(f"KEY  {key:<5} DOWN   held: {self._held_text()}")

    def release(self, key):
        if self.inject:
            self.release_fn(key)         # always send "up", even if unsure
        self.held.discard(key)
        log(f"KEY  {key:<5} UP     held: {self._held_text()}")

    def release_all(self, reason):
        if not self.held:
            return
        for key in list(self.held):
            if self.inject:
                self.release_fn(key)
        self.held.clear()
        log(f"RELEASE ALL ({reason})")


# ------------------------------------------------------- macOS checks ----

def mac_accessibility_ok():
    """True if this terminal app may send key presses (macOS Accessibility)."""
    try:
        import ctypes
        lib = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except OSError:
        return True   # can't tell; assume fine


def print_mac_permission_help():
    print("""
!!! macOS has NOT given this terminal permission to press keys. !!!

  1. Open System Settings > Privacy & Security > Accessibility
  2. Click +, add the app you run this from (Terminal, iTerm, VS Code...)
     and switch it ON. If it is already listed, turn it off and on again.
  3. Quit that app completely (Cmd+Q), reopen it, and run this script again.

(Use --test to try the connection without pressing keys.)
""", flush=True)


# ----------------------------------------------------------- networking ----

def ssl_context_for(url):
    """Use certifi's CA list when available (fixes macOS python.org SSL errors)."""
    if not url.startswith("wss://"):
        return None
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


async def watchdog(keys, state):
    """Release everything if the relay goes quiet while keys are held."""
    while True:
        await asyncio.sleep(0.1)
        if keys.held and time.monotonic() - state["last_msg"] > SILENCE_TIMEOUT:
            keys.release_all(f"no message for {SILENCE_TIMEOUT:.0f}s")


def handle_message(msg, keys):
    kind = msg.get("type")
    if kind == "key":
        key, action = msg.get("key"), msg.get("action")
        if key not in VALID_KEYS:
            log(f"ignored unknown key {key!r}")
        elif action == "down":
            keys.press(key)
        elif action == "up":
            keys.release(key)
    elif kind == "release_all":
        if keys.held:
            keys.release_all("station released the team")
        else:
            log("RELEASE ALL received (nothing was held)")
    elif kind == "controller":
        station = msg.get("station")
        if station:
            log(f">>> Now controlled by station: {station}")
        else:
            log(">>> No station is controlling this team")
    elif kind == "welcome":
        station = msg.get("controller")
        log(f"Registered as team {msg.get('team')}. Controlled by: {station or 'nobody yet'}")
    elif kind == "error":
        log(f"Relay error: {msg.get('message')}")
        # Act on the message itself: some proxies (Render) delay the close frame.
        if msg.get("code") == "bad_token":
            raise FatalError("the relay rejected the token. Check --token.")
        if msg.get("code") == "replaced":
            raise FatalError("another laptop connected with this team ID. "
                             "Exiting so the two don't fight. Check your team ID.")
    # heartbeats only refresh last_msg


async def run(args, keys):
    state = {"last_msg": time.monotonic()}
    dog = asyncio.create_task(watchdog(keys, state))
    hello = json.dumps({"role": "listener", "team": args.team, "token": args.token})
    delay = 0.5
    try:
        while True:
            log(f"Connecting to {args.server} as team {args.team} ...")
            try:
                async with connect(args.server, ssl=ssl_context_for(args.server),
                                   ping_interval=5, ping_timeout=10,
                                   open_timeout=15, close_timeout=2) as ws:
                    await ws.send(hello)
                    log(f"CONNECTED  (team {args.team})")
                    delay = 0.5
                    async for raw in ws:
                        state["last_msg"] = time.monotonic()
                        try:
                            handle_message(json.loads(raw), keys)
                        except ValueError:
                            log("ignored a non-JSON message")
                    log(f"DISCONNECTED (relay closed the connection, code {ws.close_code})")
            except FatalError as e:
                keys.release_all("disconnected")
                sys.exit(f"ERROR: {e}")
            except ConnectionClosed as e:
                code = e.rcvd.code if e.rcvd else None
                if code == CLOSE_BAD_TOKEN:
                    keys.release_all("disconnected")
                    sys.exit("ERROR: the relay rejected the token. Check --token.")
                if code == CLOSE_REPLACED:
                    keys.release_all("disconnected")
                    sys.exit(f"ERROR: another laptop connected as team {args.team}. "
                             "Exiting so the two don't fight. Check your team ID.")
                log(f"DISCONNECTED ({e})")
            except (OSError, InvalidHandshake, asyncio.TimeoutError) as e:
                log(f"DISCONNECTED (cannot reach relay: {e or type(e).__name__})")
            except InvalidURI:
                sys.exit(f"ERROR: bad server URL {args.server!r} (use wss://... or ws://...)")
            keys.release_all("disconnected")
            log(f"Reconnecting in {delay:.1f}s ...")
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_BACKOFF)
    finally:
        dog.cancel()


def load_args():
    parser = argparse.ArgumentParser(description="Game jam key listener")
    parser.add_argument("--team", help="your team ID, e.g. 07")
    parser.add_argument("--server", help="relay URL, e.g. wss://xxx.onrender.com")
    parser.add_argument("--token", help="event token from the organizers")
    parser.add_argument("--config", default="config.json",
                        help="optional JSON file with team/server/token")
    parser.add_argument("--test", action="store_true",
                        help="print events but do NOT press any keys")
    args = parser.parse_args()

    if os.path.exists(args.config):
        with open(args.config) as f:
            cfg = json.load(f)
        for name in ("team", "server", "token"):
            if getattr(args, name) is None and cfg.get(name) is not None:
                setattr(args, name, str(cfg[name]))

    missing = [n for n in ("team", "server", "token") if not getattr(args, n)]
    if missing:
        parser.error("missing " + ", ".join("--" + n for n in missing))
    args.team = args.team.strip().zfill(2) if args.team.strip().isdigit() else args.team.strip()
    return args


def main():
    args = load_args()
    print("=" * 56)
    print(f" Game jam listener   team {args.team}   ({SYSTEM})")
    print(" MODE: TEST - no keys will be pressed" if args.test
          else " MODE: LIVE - keys WILL be pressed. Click your game window!")
    print(" Press Ctrl+C to stop.")
    print("=" * 56, flush=True)

    if not args.test and SYSTEM == "Darwin" and not mac_accessibility_ok():
        print_mac_permission_help()

    keys = Keys(inject=not args.test)
    try:
        asyncio.run(run(args, keys))
    except KeyboardInterrupt:
        pass
    finally:
        keys.release_all("exiting")
        log("Stopped.")


if __name__ == "__main__":
    main()
