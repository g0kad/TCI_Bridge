"""
Minimal TCI (Transceiver Control Interface) server.

TCI is a WebSocket protocol. Two kinds of frames:

  * TEXT frames carry commands, format: "name:param1,param2,...;"
    e.g.  "vfo:0,0,14074000;"   "modulation:0,USB;"   "trx:0,true;"

  * BINARY frames carry audio (and, for real SDRs, IQ — we don't send
    IQ, since a CAT rig doesn't give us wideband spectrum data).

This implements just enough of the spec for a single-receiver,
non-IQ, "legacy CAT rig" style bridge — the bit relevant to your
use case, not the full ExpertSDR3 feature set. In particular:

  IMPLEMENTED
    - init handshake (protocol/device/trx_count/... then start;/ready;)
    - vfo:          (get + client-set frequency)     <-> rigctld set_freq
    - modulation:   (get + client-set mode)          <-> rigctld set_mode
    - trx:          (PTT)                            <-> rigctld set_ptt
    - audio_start: / audio_stop:                     -> streams RX audio
    - RX audio streamed as float32 stereo @ 48kHz binary frames

  NOT IMPLEMENTED (left as TODOs — check the spec doc if you need them)
    - iq_start: / iq_stop:           (no IQ source on a CAT rig anyway)
    - cw_message: / cw_macros:
    - multi-receiver (trx_count is hardcoded to 1)
    - authentication / TCI Remote Compactor framing

Cross-check exact command names/params against
github.com/maksimus1210/TCI/tree/master/doc before relying on this
against a specific downstream client — TCI has grown over several
protocol versions and not every client agrees on every field.
"""

from __future__ import annotations

import asyncio
import logging
import struct

import numpy as np
import websockets
from websockets.server import WebSocketServerProtocol

from rigctl_client import RigctldClient
from audio_bridge import AudioBridge, TCI_SAMPLE_RATE

log = logging.getLogger("tci")

# Binary audio frame header, per the TCI spec's stream framing:
# [ receiver: u32 ][ data_type: u32 ][ sample_rate placeholder ][ codec: u32 ] + payload
# frame_type = 1 is audio. Treat this struct as a starting point — confirm
# field widths/order against the spec/doc before depending on it.
_AUDIO_HEADER = struct.Struct("<IIII")
_AUDIO_FRAME_TYPE = 1


class TCIServer:
    def __init__(
        self,
        rig: RigctldClient,
        audio: AudioBridge,
        host: str = "0.0.0.0",
        port: int = 40001,
        device_name: str = "CAT-TCI-Bridge",
    ):
        self.rig = rig
        self.audio = audio
        self.host = host
        self.port = port
        self.device_name = device_name

        self._clients: set[WebSocketServerProtocol] = set()
        self._audio_task: asyncio.Task | None = None

    async def serve_forever(self) -> None:
        async with websockets.serve(self._handle_client, self.host, self.port):
            log.info("TCI server listening on ws://%s:%d", self.host, self.port)
            await asyncio.Future()  # run forever

    # -- per-client lifecycle -------------------------------------------

    async def _handle_client(self, ws: WebSocketServerProtocol) -> None:
        log.info("client connected: %s", ws.remote_address)
        self._clients.add(ws)
        try:
            await self._send_init(ws)
            self._ensure_audio_pump()
            async for message in ws:
                if isinstance(message, bytes):
                    await self._handle_binary(ws, message)
                else:
                    await self._handle_text(ws, message)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)
            log.info("client disconnected: %s", ws.remote_address)

    async def _send_init(self, ws: WebSocketServerProtocol) -> None:
        """The init block a TCI client expects before 'ready;'."""
        freq = await self.rig.get_freq()
        mode, _ = await self.rig.get_mode()

        init_lines = [
            "protocol:TCI,1.0;",
            f"device:{self.device_name};",
            "receive_only:false;",
            "trx_count:1;",
            "channels_count:1;",
            f"modulations_list:{','.join(_TCI_MODES)};",
            f"vfo_limits:0,30000000;",  # adjust to your rig's real range
            f"audio_samplerate:{TCI_SAMPLE_RATE};",
            "iq_samplerate:0;",  # no IQ source on a plain CAT rig
            f"vfo:0,0,{freq};",
            f"modulation:0,{_to_tci_mode(mode)};",
            "trx:0,false;",
            "start;",
            "ready;",
        ]
        for line in init_lines:
            await ws.send(line)

    # -- inbound commands -------------------------------------------------

    async def _handle_text(self, ws: WebSocketServerProtocol, message: str) -> None:
        for command in filter(None, message.split(";")):
            await self._dispatch(ws, command.strip())

    async def _dispatch(self, ws: WebSocketServerProtocol, command: str) -> None:
        if ":" not in command:
            return
        name, _, rest = command.partition(":")
        params = rest.split(",")

        try:
            if name == "vfo" and len(params) >= 3:
                # vfo:receiver,vfo_index,frequency
                freq = int(params[2])
                await self.rig.set_freq(freq)
                await self._broadcast(f"vfo:{params[0]},{params[1]},{freq};")

            elif name == "modulation" and len(params) >= 2:
                mode = _from_tci_mode(params[1])
                await self.rig.set_mode(mode)
                await self._broadcast(f"modulation:{params[0]},{params[1]};")

            elif name == "trx" and len(params) >= 2:
                on = params[1].strip().lower() == "true"
                await self.rig.set_ptt(on)
                await self._broadcast(f"trx:{params[0]},{params[1]};")

            elif name == "audio_start":
                # Handled implicitly: we pump RX audio to every connected
                # client once _ensure_audio_pump() is running. A per-client
                # opt-in/out flag is a TODO if you need to support clients
                # that don't want audio (e.g. a logger that's control-only).
                pass

            elif name == "audio_stop":
                pass

            else:
                log.debug("unhandled command: %s", command)

        except Exception:
            log.exception("error handling command: %s", command)

    async def _handle_binary(self, ws: WebSocketServerProtocol, data: bytes) -> None:
        """TX audio from a client (e.g. WSJT-X sending FT8 audio to key up)."""
        if len(data) <= _AUDIO_HEADER.size:
            return
        _receiver, frame_type, _rate, _codec = _AUDIO_HEADER.unpack_from(data)
        if frame_type != _AUDIO_FRAME_TYPE:
            return
        payload = data[_AUDIO_HEADER.size :]
        samples = np.frombuffer(payload, dtype="float32").reshape(-1, 2)
        mono = samples.mean(axis=1, keepdims=True)  # rig audio in is mono
        await self.audio.push_tx(mono)

    # -- outbound: RX audio pump ------------------------------------------

    def _ensure_audio_pump(self) -> None:
        if self._audio_task is None or self._audio_task.done():
            self._audio_task = asyncio.create_task(self._audio_pump())

    async def _audio_pump(self) -> None:
        while self._clients:
            block = await self.audio.rx_queue.get()
            block = self.audio.resample_to_tci(block, self.audio.rig_samplerate)
            header = _AUDIO_HEADER.pack(0, _AUDIO_FRAME_TYPE, TCI_SAMPLE_RATE, 0)
            frame = header + block.astype("float32").tobytes()
            await self._broadcast_binary(frame)

    async def _broadcast(self, text: str) -> None:
        if self._clients:
            await asyncio.gather(
                *(ws.send(text) for ws in list(self._clients)),
                return_exceptions=True,
            )

    async def _broadcast_binary(self, data: bytes) -> None:
        if self._clients:
            await asyncio.gather(
                *(ws.send(data) for ws in list(self._clients)),
                return_exceptions=True,
            )


# Hamlib mode names <-> TCI mode names rarely match exactly.
# This is a starting map for the common ones — extend as needed.
_TCI_MODES = ["USB", "LSB", "CW", "AM", "FM", "DIGL", "DIGU"]
_HAMLIB_TO_TCI = {
    "USB": "USB",
    "LSB": "LSB",
    "CW": "CW",
    "AM": "AM",
    "FM": "FM",
    "PKTUSB": "DIGU",
    "PKTLSB": "DIGL",
}
_TCI_TO_HAMLIB = {v: k for k, v in _HAMLIB_TO_TCI.items()}


def _to_tci_mode(hamlib_mode: str) -> str:
    return _HAMLIB_TO_TCI.get(hamlib_mode, hamlib_mode)


def _from_tci_mode(tci_mode: str) -> str:
    return _TCI_TO_HAMLIB.get(tci_mode, tci_mode)
