# Organizers: running a demo station

Deploying the relay is covered in [DEPLOY.md](DEPLOY.md). This page covers the station laptops.

## Station setup (once per laptop)

```bash
python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements-station.txt
cp config.example.json config.json  # then edit server/token/station.name
```

Start it:

```bash
python station.py --server wss://<app>.onrender.com --token <TOKEN> --name station1 --camera 0
```

- The first run downloads MediaPipe's face model (`face_landmarker.task`, ~4 MB), so do it before the event.
- Wrong camera? Try `--camera 1`. macOS will ask for camera permission for your terminal app the first time.
- Give each station a different `--name` (`station1` ... `station5`). Teams see it.
- The window can be resized or maximised. Aim for 25+ fps (shown top right).
  If it's lower, close other apps and use better lighting.

## Hotkeys (click the video window first)

| Key | Action |
|-----|--------|
| **C** | Calibrate: 3-second countdown, then the player looks straight at the screen, eyes open, mouth closed |
| **T** | Pick a team: shows online teams, type the ID, **Enter** to confirm, **Esc** to cancel |
| **P** | Pause / resume (pausing releases all keys) |
| **R** | Release the current team so another station can take it |
| **+ / -** | Sensitivity up / down (lower thresholds = smaller head movements) |
| **M** | Mirror the preview (display only; controls don't change) |
| **Q** | Quit (releases all keys and the team first) |

## Gestures

| Gesture | Key |
|---------|-----|
| Tilt head up / down | Up / Down |
| Turn head left / right (the **player's** left/right) | Left / Right |
| Up/down and left/right together | Diagonals |
| Open mouth | Z |
| Blink: close both eyes a bit longer than a natural blink (0.2 s) | Space |

Holding a gesture holds the key. Returning to neutral releases it. If no face is visible,
everything is released.

The overlay shows the arrows, Z and SPACE lighting up, plus live **pitch / yaw / mouth / blink** bars.
Yellow ticks are the "press" thresholds and orange ticks are the "release" thresholds.
The gap between them stops keys flickering.

## Demo flow per team (10 minutes)

1. Team confirms their listener is running and shows **CONNECTED**. Their game window is **focused**.
2. Press **T** and check their team is listed as online, type the ID, press **Enter**.
   The top bar shows `Team 07` and the 10:00 timer starts.
   - "controlled by station3"? Another station has it. Pick the right team or ask them to press R.
   - "(offline)" next to the team? Their listener isn't connected. Ask them to check it.
3. Sit the player about an arm's length from the webcam, face lit from the front.
4. Press **C** and have them look straight at the screen, eyes open, mouth closed, for 3 s + 1 s.
5. Have them try each gesture while watching the arrows light up. Adjust with **+/-** if needed.
   Recalibrate (**C**) if they shift position.
6. Play. The timer turns red with **1 MINUTE LEFT** at 1:00 and shows **TIME UP** at 0.
7. Press **R** to release the team, or press **T** to go straight to the next team.
   Switching sends key-ups to the old team first.

Need a break mid-demo (game crashed, player swap)? Press **P**.

## Tuning (`config.json` → `"station"`)

| Setting | Meaning | Default |
|---------|---------|---------|
| `yaw_enter` / `yaw_exit` | head turn needed to press / to release (% of face width) | 12 / 7 |
| `pitch_enter` / `pitch_exit` | head tilt to press / release | 8 / 5 |
| `mouth_enter` / `mouth_exit` | mouth opening above calibrated closed mouth | 0.25 / 0.15 |
| `blink_enter` / `blink_exit` | eye closure (0 = calibrated open eyes, 1 = shut) | 0.45 / 0.30 |
| `blink_hold_ms` | how long eyes must stay closed before Space is pressed (filters natural blinks) | 200 |
| `smoothing` | weight of the newest frame (1.0 = no smoothing, lower = smoother but slower) | 0.5 |
| `sensitivity` | starting sensitivity multiplier (+/- changes it live) | 1.0 |
| `invert_yaw` / `invert_pitch` | flip a direction if it feels backwards | false |
| `demo_minutes` | timer length | 10 |

Keep each `exit` value below its `enter` value.

## Testing without a webcam

```bash
python tools/fake_station.py --server wss://<app>.onrender.com --token <TOKEN> --team 07
```

This sends a scripted sequence of arrows, diagonals, Z and Space. Custom script:
`--script "up 0.5, up+left 1, wait 1, z 0.3"`. Teams can watch the result in
`tools/key_viewer.html`. Use this at the registration desk alongside `listener.py --test`.

`https://<app>.onrender.com/status` shows every online team and which station controls it.

## Fallback procedure

If Render is unreachable (the top bar stays on "disconnected" and `/health` doesn't load):

1. Don't wait on Render. Set up a hotspot or router, then start the local relay on one
   organizer laptop: `python relay.py --port 8765 --token <TOKEN>` (see [DEPLOY.md](DEPLOY.md#4-local-fallback-render-down-or-venue-internet-down)).
2. Write `ws://<that laptop's IP>:8765` on the board.
3. Stations: quit (**Q**) and restart with `--server ws://<IP>:8765`.
4. Only the teams about to demo join the hotspot and restart their listener with the new `--server`.
5. When Render is back, switch everyone back the same way.

If a single station crashes, the relay releases its team and the listener lets go of every
key within 2 seconds. Restart the station and select the team again.
