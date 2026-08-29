"""
Audio bridge between the rig's USB sound-card codec and asyncio queues.

sounddevice runs its callbacks on a separate PortAudio thread, not the
asyncio event loop, so we hand samples across via
loop.call_soon_threadsafe(). Keep the callbacks themselves trivial —
no blocking, no awaiting.

TCI (per the current TCI Remote spec) wants float32 audio, and RX
audio frames are stereo at 48 kHz. Most rig USB codecs run mono at
48 kHz (some at 44.1/16-bit), so we resample/upmix as needed rather
than assuming the codec matches TCI's wire format.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np
import sounddevice as sd

log = logging.getLogger("audio")

TCI_SAMPLE_RATE = 48_000  # what we advertise/send to TCI clients
BLOCK_SIZE = 960  # 20ms @ 48kHz — small enough for low latency


class AudioBridge:
    def __init__(
        self,
        device: str | int | None = None,
        rig_samplerate: int = 48_000,
        channels: int = 1,
    ):
        self.device = device
        self.rig_samplerate = rig_samplerate
        self.channels = channels

        self._loop = asyncio.get_event_loop()
        self.rx_queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=50)
        self._tx_queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=50)

        self._stream: sd.Stream | None = None

    # -- lifecycle ----------------------------------------------------

    def start(self) -> None:
        self._stream = sd.Stream(
            device=self.device,
            samplerate=self.rig_samplerate,
            channels=self.channels,
            dtype="float32",
            blocksize=BLOCK_SIZE,
            callback=self._callback,
        )
        self._stream.start()
        log.info(
            "audio stream open: device=%s rate=%d channels=%d",
            self.device,
            self.rig_samplerate,
            self.channels,
        )

    def stop(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()

    # -- PortAudio callback (runs on its own thread!) ------------------

    def _callback(self, indata, outdata, frames, time_info, status):
        if status:
            log.warning("audio status: %s", status)

        # RX: rig -> TCI clients
        rx_block = indata.copy()
        self._loop.call_soon_threadsafe(self._push_rx, rx_block)

        # TX: TCI client audio -> rig. Fill with silence if nothing queued
        # (e.g. no client is currently keyed up); never block the callback.
        try:
            tx_block = self._tx_queue.get_nowait()
            outdata[:] = tx_block[: len(outdata)]
        except asyncio.QueueEmpty:
            outdata.fill(0.0)

    def _push_rx(self, block: np.ndarray) -> None:
        try:
            self.rx_queue.put_nowait(block)
        except asyncio.QueueFull:
            # Drop audio rather than let latency creep up under load.
            log.debug("rx_queue full, dropping a block")

    # -- called from asyncio side --------------------------------------

    async def push_tx(self, block: np.ndarray) -> None:
        """Queue audio (from a TCI client) to be played out to the rig."""
        await self._tx_queue.put(block)

    @staticmethod
    def resample_to_tci(block: np.ndarray, in_rate: int) -> np.ndarray:
        """Cheap linear resample + mono->stereo upmix for the TCI wire format.

        Fine for voice-bandwidth ham audio; swap in a proper resampler
        (e.g. `resampy` or `scipy.signal.resample_poly`) if you care about
        artifacts at the top end of an SSB/data passband.
        """
        if in_rate != TCI_SAMPLE_RATE:
            n_out = int(len(block) * TCI_SAMPLE_RATE / in_rate)
            x_old = np.linspace(0, 1, len(block), endpoint=False)
            x_new = np.linspace(0, 1, n_out, endpoint=False)
            block = np.interp(x_new, x_old, block[:, 0]).astype("float32")
            block = block.reshape(-1, 1)

        if block.shape[1] == 1:
            block = np.repeat(block, 2, axis=1)
        return block
