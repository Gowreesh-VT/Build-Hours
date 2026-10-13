# Game-Dev-Event: head-gesture game controller

Teams build keyboard games (**Arrow keys + Z**). At demo time a webcam "station" reads the
player's head pose and mouth and presses those keys on the team's laptop remotely.

```
station.py (webcam, organizer)  --->  relay.py (Render)  <---  listener.py (team laptop)
```

Every component connects **out** to the relay, so it works on college Wi-Fi that blocks
device-to-device traffic. One relay handles 25 listeners and up to 5 stations.

| File | What it is |
|------|-----------|
| `relay.py` | WebSocket relay + `/status` and `/health` HTTP endpoints |
| `listener.py` | Runs on each team laptop and presses the keys (`--test` = print only) |
| `station.py` | Webcam app: head pose and mouth → keys, with overlay and hotkeys |
| `tools/fake_station.py` | Sends scripted keys to a team (no webcam needed) |
| `tools/key_viewer.html` | Browser page that shows which keys are held |
| `render.yaml`, `Dockerfile` | Deployment |
| `config.example.json` | Shared config (server, token, team, station thresholds) |

Guides: **[DEPLOY.md](DEPLOY.md)** · **[PARTICIPANTS.md](PARTICIPANTS.md)** · **[ORGANIZERS.md](ORGANIZERS.md)**

## Quick local test

```bash
pip install -r requirements-relay.txt -r requirements-listener.txt
python relay.py --port 8765 --token test
python listener.py --team 01 --server ws://localhost:8765 --token test --test
python tools/fake_station.py --server ws://localhost:8765 --token test --team 01
```

Run each command in its own terminal.
