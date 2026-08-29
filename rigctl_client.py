"""
Minimal async client for Hamlib's rigctld.

rigctld speaks a simple line-based TCP protocol (this is the real,
documented protocol — not a guess): you write a short command and it
writes back one or more lines of response. We only need a handful of
commands for a CAT bridge:

    f            -> get_freq            reply: "<freq>\n"
    F <freq>     -> set_freq            reply: "RPRT 0\n"
    m            -> get_mode            reply: "<mode>\n<passband>\n"
    M <mode> <pb>-> set_mode            reply: "RPRT 0\n"
    t            -> get_ptt             reply: "<0|1>\n"
    T <0|1>      -> set_ptt             reply: "RPRT 0\n"

Run rigctld separately, e.g.:

    rigctld -m 3073 -r /dev/ttyUSB0 -s 38400 -T 127.0.0.1 -t 4532

(model number, serial port and baud rate depend on your rig --
`rigctl -l` lists model numbers.)
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("rigctl")


class RigctldError(RuntimeError):
    pass


class RigctldClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 4532):
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()  # rigctld handles one command at a time

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.port
        )
        log.info("connected to rigctld at %s:%s", self.host, self.port)

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

    async def _command(self, cmd: str, n_reply_lines: int = 1) -> list[str]:
        """Send one rigctld command, return its reply lines (RPRT stripped)."""
        if not self._writer or not self._reader:
            raise RigctldError("not connected")

        async with self._lock:
            self._writer.write((cmd + "\n").encode("ascii"))
            await self._writer.drain()

            lines: list[str] = []
            for _ in range(n_reply_lines):
                raw = await self._reader.readline()
                if not raw:
                    raise RigctldError("rigctld closed the connection")
                lines.append(raw.decode("ascii", "replace").strip())

            # set_* commands reply with a single "RPRT <code>" line
            if lines and lines[0].startswith("RPRT"):
                code = int(lines[0].split()[1])
                if code != 0:
                    raise RigctldError(f"{cmd!r} -> RPRT {code}")
            return lines

    # -- public API -------------------------------------------------

    async def get_freq(self) -> int:
        (line,) = await self._command("f")
        return int(float(line))

    async def set_freq(self, hz: int) -> None:
        await self._command(f"F {int(hz)}")

    async def get_mode(self) -> tuple[str, int]:
        mode, passband = await self._command("m", n_reply_lines=2)
        return mode, int(passband)

    async def set_mode(self, mode: str, passband: int = 0) -> None:
        await self._command(f"M {mode} {passband}")

    async def get_ptt(self) -> bool:
        (line,) = await self._command("t")
        return line.strip() == "1"

    async def set_ptt(self, on: bool) -> None:
        await self._command(f"T {1 if on else 0}")
