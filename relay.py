"""
Gesture relay: forwards key events from stations (webcam laptops) to listeners
(team laptops). Every client connects OUT to this server, so it works on
networks that block device-to-device traffic.

Run on Render:   python relay.py              (PORT and EVENT_TOKEN from env)
Run locally:     python relay.py --port 8765 --token XXX

Protocol (JSON text messages)
  First message from every client (hello):
      {"role": "listener", "team": "07", "token": "..."}
      {"role": "station",  "name": "station1", "token": "...", "team": "07"?}

  Station -> relay:
      {"type": "claim", "team": "07"}      take control of a team
      {"type": "release"}                  give the team back
      {"type": "key", "key": "up", "action": "down"}
      {"type": "heartbeat"}                every 0.5s while controlling a team

  Relay -> listener:
      {"type": "welcome", "team": "07", "controller": "station1" | null}
      {"type": "controller", "station": "station1" | null}
      {"type": "key", ...} / {"type": "release_all"} / {"type": "heartbeat"}

  Relay -> station:
      {"type": "welcome", "name": "station1"}
      {"type": "claimed", "team": "07"} / {"type": "released", "team": "07"}
      {"type": "team_online" | "team_offline", "team": "07"}
      {"type": "error", "code": "...", "message": "..."}

HTTP on the same port:  GET /health -> "ok",  GET /status -> JSON
"""

import argparse
import asyncio
import hmac
import http
import json
import logging
import os
import sys
import time

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

VALID_KEYS = {"up", "down", "left", "right", "z", "space"}
VALID_ACTIONS = {"down", "up"}
PING_INTERVAL = 5     # seconds between keepalive pings
PING_TIMEOUT = 10     # drop the client if no pong within this time
CLOSE_TIMEOUT = 2     # don't wait long for a dead client's close handshake
HELLO_TIMEOUT = 10    # client must say hello within this time

# Close codes clients understand
CLOSE_BAD_TOKEN = 4001
CLOSE_BAD_HELLO = 4002
CLOSE_REPLACED = 4003

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("relay")
logging.getLogger("websockets").setLevel(logging.WARNING)

EVENT_TOKEN = ""
STARTED = time.time()


class Station:
    def __init__(self, ws, name):
        self.ws = ws
        self.name = name
        self.team = None          # team ID this station controls, or None


listeners = {}    # team ID -> listener websocket
controllers = {}  # team ID -> Station currently controlling it
stations = set()  # all connected Station objects


def normalize_team(team):
    """'7' and '07' are the same team."""
    team = str(team or "").strip()
    return team.zfill(2) if team.isdigit() else team


async def send(ws, msg):
    """Send JSON; ignore clients that are already gone."""
    if ws is None:
        return False
    try:
        await ws.send(json.dumps(msg))
        return True
    except ConnectionClosed:
        return False


# ---------------------------------------------------------------- HTTP ----

def status_json():
    teams = sorted(set(listeners) | set(controllers))
    return {
        "teams": [
            {
                "team": t,
                "online": t in listeners,
                "controller": controllers[t].name if t in controllers else None,
            }
            for t in teams
        ],
        "stations": [{"name": s.name, "team": s.team} for s in stations],
        "uptime_s": int(time.time() - STARTED),
    }


def http_response(status, body, content_type):
    headers = Headers()
    headers["Content-Type"] = content_type
    headers["Access-Control-Allow-Origin"] = "*"
    headers["Cache-Control"] = "no-store"
    return Response(status.value, status.phrase, headers, body.encode())


def process_request(connection, request):
    """Serve plain HTTP routes; let WebSocket upgrades through."""
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None
    path = request.path.split("?")[0]
    if path == "/health":
        return http_response(http.HTTPStatus.OK, "ok", "text/plain")
    if path == "/status":
        body = json.dumps(status_json(), indent=2)
        return http_response(http.HTTPStatus.OK, body, "application/json")
    if path == "/":
        return http_response(
            http.HTTPStatus.OK,
            "Gesture relay is running. See /status and /health.\n",
            "text/plain",
        )
    return http_response(http.HTTPStatus.NOT_FOUND, "not found", "text/plain")


# ------------------------------------------------------------ listener ----

async def run_listener(ws, team):
    old = listeners.get(team)
    listeners[team] = ws
    if old is not None:
        log.info("listener %s: replacing previous connection", team)
        # Tell it why first (proxies like Render's can delay close frames),
        # then close in the background so a dead socket can't delay the new one.
        await send(old, {"type": "error", "code": "replaced",
                         "message": "Another laptop connected with this team ID"})
        asyncio.create_task(old.close(CLOSE_REPLACED, "replaced by a newer connection"))
    log.info("listener %s: connected from %s", team, ws.remote_address)

    station = controllers.get(team)
    await send(ws, {
        "type": "welcome",
        "team": team,
        "controller": station.name if station else None,
    })
    if station:
        await send(station.ws, {"type": "team_online", "team": team})

    try:
        # Listeners don't send anything meaningful; just wait for them to go.
        async for _ in ws:
            pass
    finally:
        if listeners.get(team) is ws:
            del listeners[team]
            log.info("listener %s: disconnected", team)
            station = controllers.get(team)
            if station:
                await send(station.ws, {"type": "team_offline", "team": team})


# ------------------------------------------------------------- station ----

async def release_team(station, notify_station=True):
    """Give up control of the station's team and release all its keys."""
    team = station.team
    if team is None:
        return
    station.team = None
    if controllers.get(team) is station:
        del controllers[team]
    listener = listeners.get(team)
    await send(listener, {"type": "release_all"})
    await send(listener, {"type": "controller", "station": None})
    log.info("station %s: released team %s", station.name, team)
    if notify_station:
        await send(station.ws, {"type": "released", "team": team})


async def claim_team(station, team):
    team = normalize_team(team)
    if not team:
        await send(station.ws, {"type": "error", "code": "bad_team",
                                "message": "No team ID given"})
        return
    owner = controllers.get(team)
    if owner is station:
        await send(station.ws, {"type": "claimed", "team": team,
                                "online": team in listeners})
        return
    if owner is not None:
        log.info("station %s: claim of team %s rejected (held by %s)",
                 station.name, team, owner.name)
        await send(station.ws, {
            "type": "error", "code": "team_taken", "team": team,
            "message": f"Team {team} is controlled by {owner.name}",
        })
        return

    await release_team(station)   # switching teams: free the old one first
    station.team = team
    controllers[team] = station
    listener = listeners.get(team)
    await send(listener, {"type": "controller", "station": station.name})
    log.info("station %s: claimed team %s (listener %s)", station.name, team,
             "online" if listener else "OFFLINE")
    await send(station.ws, {"type": "claimed", "team": team,
                            "online": listener is not None})


async def forward_key(station, msg):
    key, action = msg.get("key"), msg.get("action")
    if key not in VALID_KEYS or action not in VALID_ACTIONS:
        log.info("station %s: invalid key message %s", station.name, msg)
        await send(station.ws, {"type": "error", "code": "bad_key",
                                "message": f"Invalid key message: {msg}"})
        return
    if station.team is None:
        log.info("station %s: key %s %s dropped, no team claimed",
                 station.name, key, action)
        await send(station.ws, {"type": "error", "code": "no_team",
                                "message": "Select a team first"})
        return
    listener = listeners.get(station.team)
    if listener is None:
        log.info("station %s: key %s %s dropped, team %s offline",
                 station.name, key, action, station.team)
        return
    await send(listener, {"type": "key", "key": key, "action": action})


async def run_station(ws, name, first_team):
    station = Station(ws, name)
    stations.add(station)
    log.info("station %s: connected from %s", name, ws.remote_address)
    await send(ws, {"type": "welcome", "name": name})
    if first_team:
        await claim_team(station, first_team)

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                log.info("station %s: ignoring non-JSON message", name)
                continue
            kind = msg.get("type")
            if kind == "key":
                await forward_key(station, msg)
            elif kind == "heartbeat":
                if station.team:
                    await send(listeners.get(station.team), {"type": "heartbeat"})
            elif kind == "claim":
                await claim_team(station, msg.get("team"))
            elif kind == "release":
                await release_team(station)
            else:
                log.info("station %s: unknown message type %r", name, kind)
    finally:
        stations.discard(station)
        await release_team(station, notify_station=False)
        log.info("station %s: disconnected", name)


# ------------------------------------------------------------ handshake ----

async def handler(ws):
    try:
        hello = json.loads(await asyncio.wait_for(ws.recv(), HELLO_TIMEOUT))
        if not isinstance(hello, dict):
            raise ValueError("hello must be a JSON object")
    except (asyncio.TimeoutError, ValueError, ConnectionClosed) as e:
        log.info("rejected %s: bad or missing hello (%s)", ws.remote_address, e)
        await ws.close(CLOSE_BAD_HELLO, "bad hello")
        return

    token = str(hello.get("token", ""))
    if not hmac.compare_digest(token.encode(), EVENT_TOKEN.encode()):
        log.info("rejected %s: wrong token (role=%s team=%s)",
                 ws.remote_address, hello.get("role"), hello.get("team"))
        await send(ws, {"type": "error", "code": "bad_token",
                        "message": "Wrong event token"})
        await ws.close(CLOSE_BAD_TOKEN, "wrong token")
        return

    role = hello.get("role")
    team = normalize_team(hello.get("team"))
    try:
        if role == "listener" and team:
            await run_listener(ws, team)
        elif role == "station":
            name = str(hello.get("name") or "station")[:32]
            await run_station(ws, name, team)
        else:
            log.info("rejected %s: bad role/team in hello %s",
                     ws.remote_address, hello)
            await send(ws, {"type": "error", "code": "bad_hello",
                            "message": "hello needs role and team"})
            await ws.close(CLOSE_BAD_HELLO, "bad hello")
    except ConnectionClosed:
        pass   # normal disconnect; cleanup ran in the finally blocks


async def main():
    global EVENT_TOKEN
    parser = argparse.ArgumentParser(description="Gesture game relay server")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", "8765")))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--token", default=os.environ.get("EVENT_TOKEN", ""),
                        help="event token (default: EVENT_TOKEN env var)")
    args = parser.parse_args()

    EVENT_TOKEN = args.token
    if not EVENT_TOKEN:
        sys.exit("ERROR: set the EVENT_TOKEN env var or pass --token")

    async with serve(
        handler,
        args.host,
        args.port,
        process_request=process_request,
        ping_interval=PING_INTERVAL,
        ping_timeout=PING_TIMEOUT,
        close_timeout=CLOSE_TIMEOUT,
        max_size=4096,
    ) as server:
        log.info("relay listening on %s:%d", args.host, args.port)
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
