"""
Demo station: turns head gestures from a webcam into Arrow/Z/Space key presses
on a team's laptop, through the event relay.

Usage:
    python station.py --server wss://<app>.onrender.com --token XXX --name station1 --camera 0

Gestures (relative to a calibrated neutral pose):
    tilt head up / down     -> Up / Down
    turn head left / right  -> Left / Right   (diagonals allowed)
    open mouth              -> Z
    blink (eyes closed a bit longer than a natural blink) -> Space
Holding a gesture holds the key. No face = all keys released.

Hotkeys (in the video window):
    C calibrate   T select team   P pause   R release team
    + / - sensitivity   M mirror preview   Q quit
"""

import argparse
import asyncio
import json
import os
import platform
import ssl
import sys
import threading
import time
import urllib.request

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions, vision
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
             "face_landmarker/float16/1/face_landmarker.task")

# Defaults for the "station" section of config.json
DEFAULTS = {
    "name": "station1",
    "camera": 0,
    "yaw_enter": 12.0,      # head turn, in % of face width
    "yaw_exit": 7.0,
    "pitch_enter": 8.0,     # head tilt, in % of face width
    "pitch_exit": 5.0,
    "mouth_enter": 0.25,    # mouth opening above the calibrated closed value
    "mouth_exit": 0.15,
    "blink_enter": 0.45,    # eye closure: 0 = calibrated open eyes, 1 = fully shut
    "blink_exit": 0.30,
    "blink_hold_ms": 200,   # natural blinks are ~100-150 ms; ignore anything shorter
    "smoothing": 0.5,       # EMA weight of the newest frame (1.0 = no smoothing)
    "sensitivity": 1.0,     # thresholds are divided by this; +/- adjusts it
    "invert_yaw": False,
    "invert_pitch": False,
    "mirror": True,
    "demo_minutes": 10,
    "model_path": "face_landmarker.task",
}

KEY_ORDER = ("up", "down", "left", "right", "z", "space")

# MediaPipe face mesh landmark indices
NOSE_TIP = 1
FACE_LEFT, FACE_RIGHT = 234, 454        # face edges at cheek height
EYE_LEFT, EYE_RIGHT = 33, 263           # outer eye corners
LIP_TOP, LIP_BOTTOM = 13, 14            # inner lips
MOUTH_LEFT, MOUTH_RIGHT = 61, 291       # mouth corners
# Eye outline: corner, two upper-lid points, corner, two lower-lid points
LEFT_EYE = (33, 160, 158, 133, 153, 144)
RIGHT_EYE = (362, 385, 387, 263, 373, 380)

CALIBRATION_COUNTDOWN = 3.0
CALIBRATION_SAMPLE = 1.0


def ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def http_url(server, path):
    """wss://host -> https://host/path (relay serves HTTP on the same port)."""
    base = server.rstrip("/")
    if base.startswith("wss://"):
        base = "https://" + base[len("wss://"):]
    elif base.startswith("ws://"):
        base = "http://" + base[len("ws://"):]
    return base + path


# ============================================================ relay link ====

class RelayClient:
    """Talks to the relay from a background thread. Thread-safe to call."""

    def __init__(self, server, token, name):
        self.server, self.token, self.name = server, token, name
        self.status = "connecting"
        self.connected = False
        self.team = None           # team the relay says we control
        self.wanted_team = None    # team to (re)claim after a reconnect
        self.team_online = False
        self.session = 0           # bumps on every confirmed claim
        self.notice = ""           # last error/info for the overlay
        self.notice_time = 0.0
        self.closing = False
        self.stopped = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.queue = None
        threading.Thread(target=self._thread, daemon=True).start()

    # ---- called from the UI thread ----
    def send(self, msg):
        """Queue a message; dropped while disconnected (stale keys are useless)."""
        q = self.queue
        if self.connected and q is not None:
            self.loop.call_soon_threadsafe(q.put_nowait, msg)

    def claim(self, team):
        self.wanted_team = team
        self.send({"type": "claim", "team": team})

    def release(self):
        self.wanted_team = None
        self.send({"type": "release"})

    def close(self):
        """Flush queued messages (key ups, release) then disconnect."""
        was_connected = self.connected
        self.send(None)                 # None tells the writer to close
        self.closing = True
        if was_connected:
            self.stopped.wait(2.0)

    def show(self, text):
        self.notice, self.notice_time = text, time.monotonic()

    # ---- background thread ----
    def _thread(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._run())
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())

    async def _run(self):
        delay = 0.5
        ctx = ssl_context() if self.server.startswith("wss://") else None
        while not self.closing:
            self.status = "connecting"
            try:
                async with connect(self.server, ssl=ctx, ping_interval=5,
                                   ping_timeout=10, open_timeout=15,
                                   close_timeout=2) as ws:
                    await ws.send(json.dumps({
                        "role": "station", "name": self.name,
                        "token": self.token, "team": self.wanted_team,
                    }))
                    self.queue = asyncio.Queue()
                    self.connected = True
                    self.status = "connected"
                    delay = 0.5
                    writer = asyncio.create_task(self._writer(ws))
                    beat = asyncio.create_task(self._heartbeat())
                    try:
                        await self._reader(ws)
                    finally:
                        writer.cancel()
                        beat.cancel()
            except ConnectionClosed as e:
                if e.rcvd and e.rcvd.code == 4001:
                    self.status = "WRONG TOKEN"
                    self.show("Relay rejected the token - restart with the right --token")
                    self.connected = False
                    break
            except (OSError, InvalidHandshake, asyncio.TimeoutError) as e:
                self.show(f"Cannot reach relay: {e or type(e).__name__}")
            self.connected = False
            self.queue = None
            if self.closing:
                break
            self.status = f"disconnected - retry in {delay:.0f}s"
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)
        if self.closing and self.status != "WRONG TOKEN":
            self.status = "closed"
        self.stopped.set()

    async def _reader(self, ws):
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            kind = msg.get("type")
            if kind == "claimed":
                self.team = self.wanted_team = msg["team"]
                self.team_online = msg.get("online", False)
                self.session += 1
                self.show(f"Controlling team {self.team}"
                          + ("" if self.team_online else " (listener OFFLINE)"))
            elif kind == "released":
                self.team = self.wanted_team = None
                self.show(f"Released team {msg.get('team')}")
            elif kind == "team_online":
                self.team_online = True
                self.show(f"Team {msg.get('team')} listener came online")
            elif kind == "team_offline":
                self.team_online = False
                self.show(f"Team {msg.get('team')} listener went OFFLINE")
            elif kind == "error":
                if msg.get("code") == "bad_token":
                    self.closing = True           # retrying won't help
                    self.status = "WRONG TOKEN"
                    await ws.close()
                    return
                if msg.get("code") == "team_taken":
                    if msg.get("team") == self.team:
                        self.team = None          # lost it while reconnecting
                    self.wanted_team = self.team  # keep what we still have
                self.show(msg.get("message", "error"))

    async def _writer(self, ws):
        try:
            while True:
                msg = await self.queue.get()
                if msg is None:                 # close() was called
                    await ws.close()
                    return
                await ws.send(json.dumps(msg))
        except ConnectionClosed:
            pass                                # the reader notices and reconnects

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(0.5)
            if self.team:
                self.queue.put_nowait({"type": "heartbeat"})


class StatusPoller:
    """Fetches GET /status in the background while the team picker is open."""

    def __init__(self, server):
        self.url = http_url(server, "/status")
        self.active = False
        self.teams = []
        self.error = ""
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        ctx = ssl_context() if self.url.startswith("https") else None
        while True:
            if self.active:
                try:
                    with urllib.request.urlopen(self.url, timeout=5, context=ctx) as r:
                        self.teams = json.load(r).get("teams", [])
                    self.error = ""
                except Exception as e:   # noqa: BLE001 - show any failure
                    self.error = f"status fetch failed: {e}"
            time.sleep(2.0)


# ========================================================= face tracking ====

def ensure_model(path):
    if os.path.exists(path):
        return path
    print(f"Downloading face model to {path} (~4 MB) ...", flush=True)
    with urllib.request.urlopen(MODEL_URL, context=ssl_context()) as r, \
            open(path, "wb") as f:
        f.write(r.read())
    return path


class FaceTracker:
    def __init__(self, model_path):
        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=model_path),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
        )
        self.landmarker = vision.FaceLandmarker.create_from_options(options)
        self.last_ts = 0

    def detect(self, frame_bgr):
        """Return the face landmark list, or None if no face."""
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        ts = max(self.last_ts + 1, int(time.monotonic() * 1000))
        self.last_ts = ts
        result = self.landmarker.detect_for_video(image, ts)
        return result.face_landmarks[0] if result.face_landmarks else None


def measure(landmarks, width, height):
    """Raw head/mouth numbers from landmarks (scale independent).

    yaw   : nose offset from face centre, % of face width (+ = image right)
    pitch : nose below the eye line, % of face width   (+ = lower)
    mouth : lip gap / mouth width
    eyes  : eye aspect ratio, averaged over both eyes (lid gap / eye width)
    """
    def pt(i):
        return np.array([landmarks[i].x * width, landmarks[i].y * height])

    nose = pt(NOSE_TIP)
    left, right = pt(FACE_LEFT), pt(FACE_RIGHT)
    face_w = max(np.linalg.norm(right - left), 1e-6)
    eye_y = (pt(EYE_LEFT)[1] + pt(EYE_RIGHT)[1]) / 2
    mouth_w = max(np.linalg.norm(pt(MOUTH_RIGHT) - pt(MOUTH_LEFT)), 1e-6)

    def eye_ratio(idx):
        p1, p2, p3, p4, p5, p6 = (pt(i) for i in idx)
        width = max(np.linalg.norm(p4 - p1), 1e-6)
        return (np.linalg.norm(p2 - p6) + np.linalg.norm(p3 - p5)) / (2 * width)

    return {
        "yaw": (nose[0] - (left[0] + right[0]) / 2) / face_w * 100,
        "pitch": (nose[1] - eye_y) / face_w * 100,
        "mouth": np.linalg.norm(pt(LIP_BOTTOM) - pt(LIP_TOP)) / mouth_w,
        "eyes": (eye_ratio(LEFT_EYE) + eye_ratio(RIGHT_EYE)) / 2,
        "nose": nose,
    }


class GestureMapper:
    """Turns raw measurements into a set of held keys, with EMA + hysteresis."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.neutral = None        # {"yaw", "pitch", "mouth", "eyes"} from calibration
        self.smooth = None
        self.active = set()
        self.closed_since = None   # when the eyes last closed past blink_enter
        self.values = {"yaw": 0.0, "pitch": 0.0, "mouth": 0.0, "blink": 0.0}

    def calibrate(self, samples):
        self.neutral = {k: float(np.mean([s[k] for s in samples]))
                        for k in ("yaw", "pitch", "mouth", "eyes")}
        self.reset()

    def reset(self):
        self.smooth = None
        self.active = set()
        self.closed_since = None

    def threshold(self, name):
        """(enter, exit) for 'yaw' / 'pitch' / 'mouth' / 'blink' at current sensitivity."""
        s = self.cfg["sensitivity"]
        return self.cfg[name + "_enter"] / s, self.cfg[name + "_exit"] / s

    def update(self, raw):
        if raw is None or self.neutral is None:
            self.reset()
            return set()

        a = self.cfg["smoothing"]
        if self.smooth is None:
            self.smooth = {k: raw[k] for k in ("yaw", "pitch", "mouth")}
        else:
            for k in self.smooth:
                self.smooth[k] = a * raw[k] + (1 - a) * self.smooth[k]

        # Signed so that + means the PLAYER's right / up (camera is not mirrored:
        # when you turn right, your nose moves to the image's left).
        yaw = -(self.smooth["yaw"] - self.neutral["yaw"])
        pitch = -(self.smooth["pitch"] - self.neutral["pitch"])
        mouth = self.smooth["mouth"] - self.neutral["mouth"]
        # Blinks are short, so use the raw (unsmoothed) eye value.
        blink = 1 - raw["eyes"] / max(self.neutral["eyes"], 1e-6)
        if self.cfg["invert_yaw"]:
            yaw = -yaw
        if self.cfg["invert_pitch"]:
            pitch = -pitch
        self.values = {"yaw": yaw, "pitch": pitch, "mouth": mouth, "blink": blink}

        inputs = {
            "up": (pitch, "pitch"), "down": (-pitch, "pitch"),
            "right": (yaw, "yaw"), "left": (-yaw, "yaw"),
            "z": (mouth, "mouth"),
        }
        for key, (value, name) in inputs.items():
            enter, exit_ = self.threshold(name)
            limit = exit_ if key in self.active else enter
            if value > limit:
                self.active.add(key)
            else:
                self.active.discard(key)

        # Space: eyes must stay closed longer than a natural blink.
        enter, exit_ = self.threshold("blink")
        eyes_closed = blink > (exit_ if self.closed_since else enter)
        if not eyes_closed:
            self.closed_since = None
            self.active.discard("space")
        else:
            if self.closed_since is None:
                self.closed_since = time.monotonic()
            if time.monotonic() - self.closed_since >= self.cfg["blink_hold_ms"] / 1000:
                self.active.add("space")
        return set(self.active)


class KeySender:
    """Sends only changes: 'down' when a gesture starts, 'up' when it ends."""

    def __init__(self, client):
        self.client = client
        self.held = set()

    def apply(self, wanted):
        for key in sorted(self.held - wanted):
            self.client.send({"type": "key", "key": key, "action": "up"})
        for key in sorted(wanted - self.held):
            self.client.send({"type": "key", "key": key, "action": "down"})
        self.held = set(wanted)

    def release_all(self):
        self.apply(set())


# =============================================================== overlay ====

FONT = cv2.FONT_HERSHEY_SIMPLEX
WHITE, DARK = (255, 255, 255), (25, 25, 25)
GREEN, RED, YELLOW = (60, 200, 60), (50, 50, 230), (0, 220, 255)
GREY, ORANGE = (90, 90, 90), (0, 150, 255)


def text(img, s, org, scale=0.5, color=WHITE, thick=1):
    """Text on a dark box so it stays readable over any video."""
    (tw, th), base = cv2.getTextSize(s, FONT, scale, thick)
    x, y = org
    cv2.rectangle(img, (x - 3, y - th - 3), (x + tw + 3, y + base + 1), DARK, -1)
    cv2.putText(img, s, org, FONT, scale, color, thick, cv2.LINE_AA)


def center_text(img, s, y, scale=1.0, color=WHITE, thick=2):
    (w, _), _ = cv2.getTextSize(s, FONT, scale, thick)
    text(img, s, ((img.shape[1] - w) // 2, y), scale, color, thick)


def draw_keys(img, held):
    h = img.shape[0]
    size, gap = 38, 4
    x0, y0 = 14, h - 2 * size - gap - 30
    boxes = {
        "up": (x0 + size + gap, y0),
        "left": (x0, y0 + size + gap),
        "down": (x0 + size + gap, y0 + size + gap),
        "right": (x0 + 2 * (size + gap), y0 + size + gap),
        "z": (x0 + 3 * (size + gap) + 10, y0 + size + gap),
        "space": (x0 + 4 * (size + gap) + 10, y0 + size + gap),
    }
    for key, (x, y) in boxes.items():
        on = key in held
        w = 2 * size if key == "space" else size
        cv2.rectangle(img, (x, y), (x + w, y + size), GREEN if on else GREY, -1)
        cv2.rectangle(img, (x, y), (x + w, y + size), WHITE, 1)
        cx, cy, r = x + size // 2, y + size // 2, size // 3
        tips = {"up": ((cx, cy + r), (cx, cy - r)), "down": ((cx, cy - r), (cx, cy + r)),
                "left": ((cx + r, cy), (cx - r, cy)), "right": ((cx - r, cy), (cx + r, cy))}
        if key == "z":
            cv2.putText(img, "Z", (cx - 8, cy + 8), FONT, 0.7, WHITE, 2, cv2.LINE_AA)
        elif key == "space":
            cv2.putText(img, "SPACE", (x + 7, cy + 6), FONT, 0.5, WHITE, 1, cv2.LINE_AA)
        else:
            cv2.arrowedLine(img, *tips[key], WHITE, 2, tipLength=0.45)


def draw_bar(img, label, value, enter, exit_, x, y, signed):
    """Horizontal bar; ticks at the enter (bright) and exit (dim) thresholds."""
    w, h = 150, 12
    span = enter * 2 if enter > 0 else 1
    lo = -span if signed else 0
    def px(v):
        v = min(max(v, lo), span)
        return int(x + (v - lo) / (span - lo) * w)
    cv2.rectangle(img, (x, y), (x + w, y + h), (40, 40, 40), -1)
    active = abs(value) > enter if signed else value > enter
    zero = px(0)
    cv2.rectangle(img, (min(zero, px(value)), y), (max(zero, px(value)), y + h),
                  GREEN if active else (200, 200, 200), -1)
    for t in ([enter, -enter] if signed else [enter]):
        cv2.line(img, (px(t), y - 3), (px(t), y + h + 3), YELLOW, 2)
    for t in ([exit_, -exit_] if signed else [exit_]):
        cv2.line(img, (px(t), y), (px(t), y + h), ORANGE, 1)
    text(img, f"{label} {value:+6.2f}" if not signed else f"{label} {value:+6.1f}",
         (x - 112, y + 11), 0.45)


def draw_overlay(img, st):
    h, w = img.shape[:2]
    client, mapper = st["client"], st["mapper"]
    now = time.monotonic()

    # Top bar: station, team, connection, timer
    cv2.rectangle(img, (0, 0), (w, 34), DARK, -1)
    team = client.team or "--"
    color = GREEN if client.connected else (RED if "TOKEN" in client.status else ORANGE)
    text(img, f"{client.name}", (8, 23), 0.6, WHITE, 2)
    text(img, f"Team {team}", (130, 23), 0.6,
         YELLOW if client.team else GREY, 2)
    if client.team and not client.team_online:
        text(img, "(offline)", (225, 23), 0.45, RED)
    text(img, client.status, (300, 23), 0.45, color)

    if st["demo_start"] is not None:
        left = st["demo_seconds"] - (now - st["demo_start"])
        if left > 0:
            warn = left <= 60
            mm, ss = divmod(int(left), 60)
            blink = warn and int(now * 2) % 2 == 0
            text(img, f"{mm:02d}:{ss:02d}", (w - 95, 26), 0.8,
                 RED if warn else WHITE, 2 if not blink else 3)
            if warn:
                center_text(img, "1 MINUTE LEFT", 80, 0.8, RED, 2)
        elif int(now * 2) % 2 == 0:
            text(img, "TIME UP", (w - 120, 26), 0.8, RED, 2)
            center_text(img, "TIME UP", 80, 1.2, RED, 3)
    text(img, f"{st['fps']:.0f} fps  sens {mapper.cfg['sensitivity']:.1f}x",
         (w - 150, 50), 0.4, (200, 200, 200))

    # Notice line (errors, claim results)
    if client.notice and now - client.notice_time < 5:
        text(img, client.notice, (8, 54), 0.5, YELLOW)

    # Centre messages
    if st["calib_start"] is not None:
        t = now - st["calib_start"]
        if t < CALIBRATION_COUNTDOWN:
            center_text(img, "Look straight, eyes open, mouth closed", h // 2 - 40, 0.8, YELLOW)
            center_text(img, str(int(CALIBRATION_COUNTDOWN - t) + 1), h // 2 + 30, 2.5, YELLOW, 4)
        else:
            center_text(img, "Hold still...", h // 2, 1.0, YELLOW)
    elif mapper.neutral is None:
        center_text(img, "Press C to calibrate", h // 2, 1.0, YELLOW)
    elif st["paused"]:
        center_text(img, "PAUSED (P to resume)", h // 2, 1.0, ORANGE)
    elif not st["face"]:
        center_text(img, "NO FACE - keys released", h // 2, 0.9, RED)
    elif not client.team:
        center_text(img, "Press T to pick a team", h // 2, 0.9, YELLOW)

    # Keys and gauges
    draw_keys(img, st["keys"].held)
    bx, by = w - 165, h - 110
    v = mapper.values
    draw_bar(img, "pitch", v["pitch"], *mapper.threshold("pitch"), bx, by, True)
    draw_bar(img, "yaw", v["yaw"], *mapper.threshold("yaw"), bx, by + 22, True)
    draw_bar(img, "mouth", v["mouth"], *mapper.threshold("mouth"), bx, by + 44, False)
    draw_bar(img, "blink", v["blink"], *mapper.threshold("blink"), bx, by + 66, False)

    text(img, "C calib  T team  P pause  R release  +/- sens  M mirror  Q quit",
         (8, h - 8), 0.42, (220, 220, 220))

    if st["picking"] is not None:
        draw_team_picker(img, st)


def draw_team_picker(img, st):
    h, w = img.shape[:2]
    x0, y0, x1, y1 = 40, 44, w - 40, h - 122
    img[y0:y1, x0:x1] = DARK
    cv2.rectangle(img, (x0, y0), (x1, y1), WHITE, 1)
    text(img, "Select team: type ID, Enter = OK, Esc = cancel", (x0 + 12, y0 + 24), 0.5)
    text(img, f"Team: {st['picking']}_", (x0 + 12, y0 + 58), 0.9, YELLOW, 2)
    poller = st["poller"]
    if poller.error:
        text(img, poller.error[:70], (x0 + 12, y0 + 84), 0.4, RED)
    teams = [t for t in poller.teams if t["online"]]
    text(img, f"Online teams ({len(teams)}):", (x0 + 12, y0 + 106), 0.5)
    rows, col_w = 9, (x1 - x0 - 12) // 4
    for i, t in enumerate(teams[:rows * 4]):
        col, row = divmod(i, rows)
        owner = t["controller"]
        if owner == st["client"].name:
            label, color = f"{t['team']} you", YELLOW
        elif owner:
            label, color = f"{t['team']} {owner[:9]}", GREY
        else:
            label, color = f"{t['team']} free", GREEN
        text(img, label, (x0 + 12 + col * col_w, y0 + 130 + row * 20), 0.45, color)


# ================================================================== main ====

def load_config(path):
    cfg = dict(DEFAULTS)
    top = {}
    if os.path.exists(path):
        with open(path) as f:
            top = json.load(f)
        cfg.update(top.get("station", {}))
    return top, cfg


def parse_args():
    p = argparse.ArgumentParser(description="Head-gesture demo station")
    p.add_argument("--server", help="relay URL, e.g. wss://xxx.onrender.com")
    p.add_argument("--token", help="event token")
    p.add_argument("--name", help="station name shown to everyone")
    p.add_argument("--camera", type=int, help="webcam index (default 0)")
    p.add_argument("--config", default="config.json")
    return p.parse_args()


def open_camera(index):
    backend = cv2.CAP_DSHOW if platform.system() == "Windows" else cv2.CAP_ANY
    cap = cv2.VideoCapture(index, backend)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_FPS, 30)
    if not cap.isOpened():
        sys.exit(f"ERROR: cannot open camera {index}. Try --camera 1. "
                 "On macOS allow camera access for your terminal app.")
    return cap


def main():
    args = parse_args()
    top, cfg = load_config(args.config)
    server = args.server or top.get("server")
    token = args.token or top.get("token")
    if args.name:
        cfg["name"] = args.name
    if args.camera is not None:
        cfg["camera"] = args.camera
    if not server or not token:
        sys.exit("ERROR: give --server and --token (or put them in config.json)")

    model = cfg["model_path"]
    if not os.path.isabs(model):
        model = os.path.join(HERE, model)
    tracker = FaceTracker(ensure_model(model))
    cap = open_camera(cfg["camera"])

    client = RelayClient(server, token, cfg["name"])
    keys = KeySender(client)
    mapper = GestureMapper(cfg)
    st = {
        "client": client, "mapper": mapper, "keys": keys,
        "poller": StatusPoller(server),
        "paused": False, "face": False, "fps": 0.0,
        "calib_start": None, "calib_samples": [],
        "picking": None,                    # typed team ID while picker is open
        "demo_start": None, "demo_seconds": cfg["demo_minutes"] * 60,
    }
    seen_session, seen_team = 0, None
    window = f"Gesture station - {cfg['name']}"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, 960, 720)
    last_frame = time.monotonic()

    while True:
        ok, frame = cap.read()
        if not ok:
            client.show("Camera read failed")
            time.sleep(0.05)
            continue
        now = time.monotonic()
        st["fps"] = 0.9 * st["fps"] + 0.1 / max(now - last_frame, 1e-3)
        last_frame = now
        h, w = frame.shape[:2]

        # New claim confirmed (team switch or re-claim after reconnect):
        # the listener has nothing held, so start from scratch.
        if client.session != seen_session:
            seen_session = client.session
            keys.held.clear()
            mapper.reset()
        if client.team != seen_team:
            seen_team = client.team
            st["demo_start"] = now if client.team else None

        landmarks = tracker.detect(frame)
        raw = measure(landmarks, w, h) if landmarks else None
        st["face"] = raw is not None

        # Calibration: countdown, then average a second of samples.
        if st["calib_start"] is not None:
            t = now - st["calib_start"]
            if t >= CALIBRATION_COUNTDOWN and raw is not None:
                st["calib_samples"].append(raw)
            if t >= CALIBRATION_COUNTDOWN + CALIBRATION_SAMPLE:
                if len(st["calib_samples"]) >= 5:
                    mapper.calibrate(st["calib_samples"])
                    client.show("Calibrated")
                else:
                    client.show("Calibration failed: no face seen")
                st["calib_start"] = None

        if st["calib_start"] is not None or st["paused"]:
            mapper.reset()
            wanted = set()
        else:
            wanted = mapper.update(raw)
        keys.apply(wanted if client.team else set())

        # Draw
        view = cv2.flip(frame, 1) if cfg["mirror"] else frame
        if raw is not None:
            nx, ny = raw["nose"]
            if cfg["mirror"]:
                nx = w - nx
            cv2.circle(view, (int(nx), int(ny)), 4, YELLOW, -1)
        draw_overlay(view, st)
        cv2.imshow(window, view)

        # Hotkeys
        k = cv2.waitKey(1) & 0xFF
        if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
            k = ord("q")
        if k == 255:
            continue
        if st["picking"] is not None:
            if k == 27:                                   # Esc
                st["picking"] = None
                st["poller"].active = False
            elif k in (13, 10):                           # Enter
                team = st["picking"].strip()
                st["picking"] = None
                st["poller"].active = False
                if team:
                    keys.release_all()                    # ups go to the OLD team
                    client.claim(team.zfill(2) if team.isdigit() else team)
            elif k in (8, 127):                           # Backspace
                st["picking"] = st["picking"][:-1]
            elif chr(k).isascii() and chr(k).isalnum() and len(st["picking"]) < 8:
                st["picking"] += chr(k)
            continue

        key = chr(k).lower()
        if key == "q":
            break
        elif key == "c":
            keys.release_all()
            st["calib_start"], st["calib_samples"] = now, []
        elif key == "t":
            st["picking"] = ""
            st["poller"].active = True
        elif key == "p":
            st["paused"] = not st["paused"]
            keys.release_all()
            client.show("Paused" if st["paused"] else "Resumed")
        elif key == "r":
            keys.release_all()
            client.release()
        elif key in "+=":
            cfg["sensitivity"] = round(min(cfg["sensitivity"] + 0.1, 3.0), 1)
            client.show(f"Sensitivity {cfg['sensitivity']:.1f}x")
        elif key in "-_":
            cfg["sensitivity"] = round(max(cfg["sensitivity"] - 0.1, 0.3), 1)
            client.show(f"Sensitivity {cfg['sensitivity']:.1f}x")
        elif key == "m":
            cfg["mirror"] = not cfg["mirror"]

    # Quit: release every held key, give the team back, then disconnect.
    keys.release_all()
    client.release()
    client.close()
    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
