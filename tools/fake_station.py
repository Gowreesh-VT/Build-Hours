"""
Fake station: sends a scripted key sequence to one team, so you can test a
listener without a webcam.

    python tools/fake_station.py --server ws://localhost:8765 --token XXX --team 07

Script steps (comma separated, --script):
    up 0.5          press Up, hold 0.5s, release
    up+left 1       press Up and Left together, hold 1s, release both
    wait 1          do nothing for 1s
    hold z          press Z and leave it held (to test safety releases)

Other options:
    --stay 3        stay connected 3s after the script (heartbeats keep going)
    --no-heartbeat  never send heartbeats (listener should release after 2s)
"""

import argparse
import asyncio
import json
import ssl
import sys
import time

from websockets.asyncio.client import connect

DEFAULT_SCRIPT = ("up 0.5, down 0.5, left 0.5, right 0.5, z 0.3, "
                  "up+left 0.8, down+right 0.8, wait 0.5, right+z 0.6")


def log(name, text):
    print(time.strftime("[%H:%M:%S] ") + f"{name}: {text}", flush=True)


def parse_script(script):
    steps = []
    for part in script.split(","):
        words = part.split()
        if not words:
            continue
        if words[0] == "wait":
            steps.append(("wait", [], float(words[1])))
        elif words[0] == "hold":
            steps.append(("hold", words[1].split("+"), 0.0))
        else:
            hold = float(words[1]) if len(words) > 1 else 0.3
            steps.append(("tap", words[0].split("+"), hold))
    return steps


async def wait_for_claim(ws, name):
    """Return True when the relay confirms the claim, False if rejected."""
    while True:
        msg = json.loads(await ws.recv())
        log(name, f"relay says {msg}")
        if msg.get("type") == "claimed":
            return True
        if msg.get("type") == "error":
            return False


async def heartbeats(ws):
    while True:
        await ws.send(json.dumps({"type": "heartbeat"}))
        await asyncio.sleep(0.5)


async def print_replies(ws, name):
    async for raw in ws:
        log(name, f"relay says {json.loads(raw)}")


async def main():
    p = argparse.ArgumentParser(description="Send scripted keys to a team")
    p.add_argument("--server", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--team", required=True)
    p.add_argument("--name", default="fake-station")
    p.add_argument("--script", default=DEFAULT_SCRIPT)
    p.add_argument("--stay", type=float, default=0.0)
    p.add_argument("--no-heartbeat", action="store_true")
    args = p.parse_args()

    ctx = None
    if args.server.startswith("wss://"):
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ctx = ssl.create_default_context()

    async with connect(args.server, ssl=ctx) as ws:
        await ws.send(json.dumps({"role": "station", "name": args.name,
                                  "token": args.token}))
        log(args.name, f"relay says {json.loads(await ws.recv())}")   # welcome
        await ws.send(json.dumps({"type": "claim", "team": args.team}))
        if not await wait_for_claim(ws, args.name):
            log(args.name, f"could not take team {args.team}, exiting")
            sys.exit(2)

        tasks = [asyncio.create_task(print_replies(ws, args.name))]
        if not args.no_heartbeat:
            tasks.append(asyncio.create_task(heartbeats(ws)))

        async def key(k, action):
            log(args.name, f"send {k} {action}")
            await ws.send(json.dumps({"type": "key", "key": k, "action": action}))

        for kind, keys, seconds in parse_script(args.script):
            if kind == "wait":
                await asyncio.sleep(seconds)
                continue
            for k in keys:
                await key(k, "down")
            if kind == "tap":
                await asyncio.sleep(seconds)
                for k in keys:
                    await key(k, "up")

        if args.stay:
            log(args.name, f"script done, staying connected {args.stay}s")
            await asyncio.sleep(args.stay)
        for t in tasks:
            t.cancel()
        log(args.name, "disconnecting")


if __name__ == "__main__":
    asyncio.run(main())
