# Deploying the relay

The relay (`relay.py`) is the only server. Stations and listeners both connect **out** to it,
so it works on college Wi-Fi that blocks laptop-to-laptop traffic.

```
station.py (webcam)  --->  relay (Render, wss://)  <---  listener.py (team laptop)
```

## 1. Pick an event token

Everyone needs this shared secret. Make a short random one:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(8))"
```

Do **not** commit it to git. You will type it into Render and hand it to teams on the day.

## 2. Deploy on Render (from GitHub)

1. Push this repo to GitHub (public or private).
2. Sign in at <https://dashboard.render.com> with GitHub.
3. Click **New + → Blueprint** and select this repo. Render reads `render.yaml`:
   free web service, region **Singapore**, Python runtime,
   build `pip install -r requirements-relay.txt`, start `python relay.py`,
   health check `/health`.
4. Render asks for **EVENT_TOKEN** (it is marked `sync: false`). Paste your token. Click **Apply**.
5. Wait for the deploy to show **Live**. Your relay URL is
   `wss://<service-name>.onrender.com` (the dashboard shows `https://...`; swap in `wss://`).
6. Check it in a browser:
   - `https://<service-name>.onrender.com/health` → `ok`
   - `https://<service-name>.onrender.com/status` → `{"teams": [], ...}`

**Without Blueprint:** New + → Web Service → pick the repo, then set Runtime *Python 3*,
Region *Singapore*, Plan *Free*, Build `pip install -r requirements-relay.txt`,
Start `python relay.py`, and under *Advanced* set Health Check Path `/health`
and add env var `EVENT_TOKEN`.

**Changing the token later:** service → **Environment** → edit `EVENT_TOKEN` → Save.
Render restarts the relay; everyone must restart with the new token.

**Docker instead:** choose Runtime *Docker* (the `Dockerfile` is in the repo root) and set
`EVENT_TOKEN` the same way. Render injects `PORT`; the relay reads it.

## 3. Warm the service before the event

Free Render services **sleep after ~15 minutes without traffic**, and the first request
after that takes up to about a minute while it wakes.

- About **10 minutes before** the demos, open `https://<service-name>.onrender.com/status`
  and wait until it returns JSON.
- Once listeners are connected, their traffic keeps it awake. If there is a long break with
  nobody connected, open `/status` again before resuming.
- Render may restart the service occasionally. Listeners and stations reconnect on their
  own, and stations re-claim their team.

Watch logs live under the service's **Logs** tab: every connect, disconnect, claim,
rejection and routing error is logged there.

## 4. Local relay (event Wi-Fi or hotspot, preset `--2`)

This is the **primary setup for demos**: keys take a few milliseconds instead of ~0.3 s, and
it works without internet. You need a network where laptops **can** reach each other: event
Wi-Fi that allows it, a phone hotspot, or a Wi-Fi router with "client/AP isolation" off.
Test it on the real network before the event (Testing Guide, Test 7).

> Phone hotspots usually allow about 10 devices. That's fine, because only the stations and
> the teams demoing **right now** need to join. Teams can switch networks when it's their turn.

On the organizer laptop that will run the relay:

```bash
pip install -r requirements-relay.txt
python relay.py --port 8765 --token <EVENT_TOKEN>
```

Find that laptop's IP address on the hotspot:

- macOS: `ipconfig getifaddr en0`
- Windows: `ipconfig` (look for "IPv4 Address" under Wi-Fi)
- Linux: `hostname -I`

Allow incoming connections if the firewall asks (Windows: tick *Private networks*).
Check from another laptop: `http://<IP>:8765/health` should show `ok`.

Keep the relay laptop plugged in and awake (macOS: run `caffeinate -d` in a spare terminal;
Windows: set sleep to *Never*), and ask for its IP to be reserved so it doesn't change.

**Set preset 2 before check-in** so everyone can just type `--2`. At the top of `listener.py`:

```python
PRESETS = {
    "1": ("internet relay (Render)", "wss://gesture-relay-xbs9.onrender.com"),
    "2": ("event Wi-Fi relay", "ws://192.168.43.10:8765"),   # <- the relay laptop's IP
}
```

Commit and push it before teams download the listener. Then:

```bash
python listener.py --team 07 --token <EVENT_TOKEN> --2
python station.py  --token <EVENT_TOKEN> --name station1 --2
```

If the IP changes later, anyone can override it: `--server ws://<new IP>:8765`.
To switch to the internet relay, use `--1` instead of `--2`.

## Pre-event checklist

- [ ] `/health` returns `ok` on the Render URL
- [ ] `python tools/fake_station.py --token <T> --team 99 --1`
      reaches a `--test` listener for team 99
- [ ] Each station laptop has run `station.py` once (it downloads the face model, ~4 MB)
- [ ] A fallback laptop has `relay.py` + `websockets` installed and you know how to start it
- [ ] Token written on the whiteboard / registration sheet, not in the public repo
