"""
CAT/Audio -> TCI bridge, entry point.

Prereqs:
    1. rigctld already running against your rig, e.g.:
       rigctld -m 3073 -r /dev/ttyUSB0 -s 38400 -T 127.0.0.1 -t 4532
    2. Your rig's USB audio codec visible to the Pi
       (`python -m sounddevice` lists device names/indices).

Usage:
    python main.py \
        --rigctld-host 127.0.0.1 --rigctld-port 4532 \
        --audio-device "USB Audio CODEC" \
        --tci-port 40001
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from rigctl_client import RigctldClient
from audio_bridge import AudioBridge
from tci_server import TCIServer


async def run(args: argparse.Namespace) -> None:
    rig = RigctldClient(host=args.rigctld_host, port=args.rigctld_port)
    await rig.connect()

    audio = AudioBridge(
        device=args.audio_device,
        rig_samplerate=args.audio_rate,
        channels=1,
    )
    audio.start()

    server = TCIServer(
        rig=rig,
        audio=audio,
        host=args.tci_host,
        port=args.tci_port,
        device_name=args.device_name,
    )

    try:
        await server.serve_forever()
    finally:
        audio.stop()
        await rig.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="CAT/Audio -> TCI bridge")
    parser.add_argument("--rigctld-host", default="127.0.0.1")
    parser.add_argument("--rigctld-port", type=int, default=4532)
    parser.add_argument(
        "--audio-device",
        default=None,
        help="sounddevice name or index for the rig's USB audio codec "
        "(default: system default input/output)",
    )
    parser.add_argument("--audio-rate", type=int, default=48000)
    parser.add_argument("--tci-host", default="0.0.0.0")
    parser.add_argument("--tci-port", type=int, default=40001)
    parser.add_argument("--device-name", default="CAT-TCI-Bridge")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
