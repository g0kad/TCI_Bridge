# cat-tci-bridge (skeleton)

A slim CAT/Audio → TCI bridge, meant to run headless on a Raspberry Pi.
This is a **starting skeleton**, not a finished, spec-verified
implementation — see "What's solid vs. what to verify" below.

## Architecture

```
   rig (serial CAT + USB audio codec)
        │                    │
        │ CAT                │ audio
        ▼                    ▼
   rigctld            AudioBridge (sounddevice)
   (Hamlib)                  │
        │                    │
        └─────────┬──────────┘
                   ▼
             TCIServer (websockets)
                   │
                   ▼
        any TCI client (WSJT-X, JTDX, MSHV, ...)
```

- `rigctl_client.py` — talks to Hamlib's `rigctld` over its plain
  TCP line protocol. This part is accurate and standard; rigctld's
  protocol hasn't changed in years.
- `audio_bridge.py` — grabs the rig's USB audio codec via
  `sounddevice`/PortAudio, hands blocks across to asyncio via queues.
- `tci_server.py` — the actual protocol bridge: a `websockets` server
  speaking TCI's text-command / binary-audio-frame protocol.
- `main.py` — wires the three together, CLI args for host/port/device.

## What's solid vs. what to verify

**Solid:**
- rigctld client (`f`/`F`/`m`/`M`/`t`/`T` are the real, documented commands)
- Overall architecture and asyncio wiring
- TCI's broad shape: WebSocket server, text commands as
  `name:params;`, binary frames for audio, an init handshake ending in
  `start;`/`ready;`

**Needs checking against the spec before you trust it on real gear:**
- The exact **binary audio frame header** (`_AUDIO_HEADER` in
  `tci_server.py`) — field order/widths are a plausible placeholder,
  not verified byte-for-byte against the spec.
- The exact **init block field names/order** TCI clients expect.
- Whether your target client (WSJT-X, JTDX, etc.) needs `iq_start:`
  even when it's told `iq_samplerate:0;` — some clients assume IQ
  is always available and may need special-casing.

Before relying on this against a specific client, pull the spec doc
from `github.com/maksimus1210/TCI/tree/master/doc` and diff it
against `tci_server.py`'s `_send_init` and `_dispatch` — that's a
30-minute check, not a rewrite.

## Running

```bash
pip install -r requirements.txt

# 1. Start rigctld against your actual rig
rigctld -m 3073 -r /dev/ttyUSB0 -s 38400 -T 127.0.0.1 -t 4532 &

# 2. Find your rig's USB audio device name
python -m sounddevice

# 3. Start the bridge
python main.py --audio-device "USB Audio CODEC" --tci-port 40001
```

Then point a TCI client at `ws://<pi-ip>:40001`.

## Known gaps / TODOs

- No authentication (matches TCI's own norm of "trust the LAN")
- Single receiver only (`trx_count` hardcoded to 1)
- No IQ streaming (a CAT rig doesn't have wideband spectrum data to offer)
- CW macros / `cw_message:` not handled
- Audio resampling is a naive linear interpolation — fine for voice
  bandwidth, worth swapping for something better if artifacts show up
  in a digital-mode passband
- No reconnect/retry logic if rigctld or the audio device disappears
  mid-session — worth adding for an unattended Pi deployment
