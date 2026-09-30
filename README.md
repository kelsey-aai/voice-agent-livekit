# Build a voice agent with LiveKit and AssemblyAI's Voice Agent API

Build a multi-user, browser-ready voice agent in Python. **LiveKit** handles the WebRTC transport — rooms, participants, recording, geographic distribution, mobile and browser SDKs. The **AssemblyAI Voice Agent API** handles the entire AI pipeline — speech-to-text, LLM, and text-to-speech — over a single WebSocket. Your worker is just a bridge between them.

This is **not** the LiveKit Agents framework. We're not plugging in separate STT, LLM, and TTS providers. The Voice Agent API replaces all of that with one connection. We use LiveKit purely for transport.

## Why combine LiveKit and the Voice Agent API?

WebRTC and AI are different problems with different best-in-class solutions:

- **LiveKit** is the easiest way to ship production-grade real-time audio. SDKs for Web, iOS, Android, React Native, Flutter, and Unity. Built-in recording, simulcast, adaptive bitrate, and end-to-end encryption. A managed cloud and a self-hostable open-source server.
- **AssemblyAI's Voice Agent API** is the easiest way to ship a voice agent. One WebSocket gives you Universal-3.6 Pro Realtime for speech-to-text, an LLM that decides what to say, a TTS engine with 30+ voices, plus neural turn detection, barge-in, and tool calling — all server-side, all in one connection.

Use them together and you get multi-user voice rooms with a real AI agent inside, without writing a STT/LLM/TTS orchestration layer or building your own WebRTC stack.

## How this differs from the LiveKit Agents framework

| | LiveKit Agents framework | This tutorial (Voice Agent API + LiveKit transport) |
|---|---|---|
| Where the AI lives | You configure STT, LLM, and TTS plugins separately | One AssemblyAI WebSocket — STT, LLM, and TTS all server-side |
| Services to wire up | 3+ (one per plugin) | 1 |
| API keys to manage | 3+ | 2 (AssemblyAI + LiveKit) |
| Speech-to-text | Plugin of your choice (Universal-3.6 Pro Realtime available) | Universal-3.6 Pro Realtime, built in |
| Turn detection | Plugin-dependent; configure VAD + endpointing | Built into the Voice Agent API |
| Barge-in | Framework handles it across plugins | Built in; one event (`reply.done` with `status: "interrupted"`) |
| Tool calling | LLM-plugin-specific | Built in; one event flow (`tool.call` → `tool.result`) |
| What LiveKit does | Transport + agent runtime | Transport only |

If you want the LiveKit Agents framework with AssemblyAI Universal-3.6 Pro Realtime as the STT plugin, see the full-deploy guide: [Build and deploy real-time AI voice agents using LiveKit and AssemblyAI](https://www.assemblyai.com/blog/build-and-deploy-real-time-ai-voice-agents-using-livekit-and-assemblyai). Universal-3.6 Pro Realtime requires `livekit-agents` 1.8.0+ (1.8.3+ via LiveKit Inference), and the plugin still defaults to `universal-3-5-pro`, so pass `model="universal-3-6-pro"` explicitly. If you want one WebSocket to do all of the AI, you're in the right place.

## Architecture

```
Browser / iOS / Android client (any LiveKit SDK)
        │
        │  WebRTC into a LiveKit room
        ▼
LiveKit Cloud (or self-hosted livekit-server)
        │
        │  remote audio track (24 kHz mono PCM16, after AudioStream resample)
        ▼
This Python worker — joins the room with livekit-rtc as a server-side participant
        │
        │  base64-encoded PCM16 chunks as { type: "input.audio", audio: "..." }
        ▼
┌────────────────────────────────────────────────────────────────┐
│  wss://agents.assemblyai.com/v1/ws                             │
│                                                                │
│  AssemblyAI Voice Agent API                                    │
│  ├── Universal-3.6 Pro Realtime  (speech → text)               │
│  ├── LLM                        (text → reply)                 │
│  └── TTS                        (reply → 24 kHz PCM16 audio)   │
│                                                                │
│  + neural turn detection                                       │
│  + barge-in                                                    │
│  + tool calling                                                │
└────────────────────────────────────────────────────────────────┘
        │
        │  base64-encoded PCM16 as { type: "reply.audio", data: "..." }
        ▼
This Python worker — capture_frame() into rtc.AudioSource
        │
        │  published as a local audio track on the worker's participant
        ▼
LiveKit Cloud
        │
        │  WebRTC out
        ▼
Browser / iOS / Android client(s) hear the agent
```

## Prerequisites

- **Python 3.10+**
- **An [AssemblyAI API key](https://www.assemblyai.com/dashboard/signup)** — free tier available, no credit card.
- **A LiveKit server** — either a free [LiveKit Cloud project](https://cloud.livekit.io) or a [self-hosted](https://docs.livekit.io/transport/self-hosting/local/) `livekit-server`.
- **A LiveKit client** to talk to the agent. The fastest path is the hosted [LiveKit Agents Playground](https://agents-playground.livekit.io).

You don't need a microphone or speakers on the worker machine — the worker is a server-side participant. All audio I/O happens in the browser/mobile client.

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/kelsey-aai/voice-agent-livekit
cd voice-agent-livekit

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in `.env`:

```ini
ASSEMBLYAI_API_KEY=           # https://www.assemblyai.com/dashboard/signup
LIVEKIT_URL=wss://<project>.livekit.cloud
LIVEKIT_API_KEY=              # LiveKit Cloud → Settings → Keys
LIVEKIT_API_SECRET=
ROOM_NAME=voice-agent-demo
```

For self-hosted LiveKit, run `livekit-server --dev` and use `LIVEKIT_URL=ws://localhost:7880`. The dev server prints an API key and secret on startup.

### 3. Run the worker

```bash
python worker.py
```

You should see the worker connect to LiveKit and wait for a participant:

```
Connecting to LiveKit room 'voice-agent-demo' at wss://...
Connected as worker. Publishing reply track …
Waiting for a participant to publish a microphone track …
```

### 4. Connect a client

The fastest way is the [LiveKit Agents Playground](https://agents-playground.livekit.io):

1. Open the playground.
2. Paste your `LIVEKIT_URL` and a token. Generate a token from the LiveKit Cloud dashboard (Settings → Keys → Create token), set the room to `voice-agent-demo` and the identity to anything other than `voice-agent`.
3. Click **Connect**, allow microphone access, and start talking. You'll hear the agent reply through your browser.

You can also connect from your own LiveKit Web/iOS/Android/Flutter app pointed at the same room.

## How it works

The worker is one file (`worker.py`) and roughly 250 lines. Five steps do the actual work.

### 1. Mint a LiveKit token and join the room

```python
from livekit import api, rtc

token = (
    api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    .with_identity("voice-agent")
    .with_grants(api.VideoGrants(
        room_join=True, room=ROOM_NAME,
        can_publish=True, can_subscribe=True,
    ))
    .to_jwt()
)

room = rtc.Room()
await room.connect(LIVEKIT_URL, token)
```

`AccessToken` from `livekit-api` builds a signed JWT with the grants this worker needs: subscribe to incoming audio, publish a reply track. `room.connect()` opens the WebRTC signaling and media path.

### 2. Publish a local audio track for the agent's voice

```python
audio_source = rtc.AudioSource(sample_rate=24_000, num_channels=1)
local_track = rtc.LocalAudioTrack.create_audio_track("agent-voice", audio_source)

await room.local_participant.publish_track(
    local_track,
    rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
)
```

`AudioSource` is LiveKit's pump for sending audio into a room. We configure it at 24 kHz mono — the Voice Agent API's default `audio/pcm` format — so we can hand reply audio straight to it without resampling.

### 3. Subscribe to the user's audio track

```python
@room.on("track_subscribed")
def on_track_subscribed(track, publication, participant):
    if track.kind == rtc.TrackKind.KIND_AUDIO:
        asyncio.create_task(bridge_to_voice_agent(track))
```

LiveKit emits `track_subscribed` when a remote participant publishes a track and it gets routed to us. We only care about audio.

### 4. Forward microphone audio to the Voice Agent API

```python
stream = rtc.AudioStream.from_track(
    track=mic_track,
    sample_rate=24_000,    # ask LiveKit to resample to 24 kHz for us
    num_channels=1,
)

async for event in stream:
    pcm16_bytes = bytes(event.frame.data)
    await ws.send(json.dumps({
        "type": "input.audio",
        "audio": base64.b64encode(pcm16_bytes).decode("ascii"),
    }))
```

`AudioStream` does the resampling for us. WebRTC carries audio at 48 kHz internally, but we ask for 24 kHz mono and the LiveKit FFI resampler handles the conversion. Each `AudioFrame` exposes `data` as a memoryview of int16 samples — converting to raw PCM16 bytes is just `bytes(event.frame.data)`. Base64-encode and ship as `input.audio`.

### 5. Play the agent's reply back into the room

```python
elif t == "reply.audio":
    pcm = base64.b64decode(event["data"])
    samples = len(pcm) // 2  # 2 bytes per int16, mono
    frame = rtc.AudioFrame(
        data=pcm,
        sample_rate=24_000,
        num_channels=1,
        samples_per_channel=samples,
    )
    await audio_source.capture_frame(frame)
```

The agent streams `reply.audio` events as soon as the LLM begins generating — you don't wait for the whole reply. Each chunk is wrapped in an `AudioFrame` and pushed into the `AudioSource`, which queues it up to 1 second deep and drains at 24 kHz on its own clock.

### 6. Handle barge-in

When the user speaks while the agent is talking, two things can happen — and both flush the playback queue:

```python
elif t == "input.speech.started":
    # User started talking; stop playback so they can be heard.
    audio_source.clear_queue()

elif t == "reply.done":
    if event.get("status") == "interrupted":
        audio_source.clear_queue()
```

`AudioSource.clear_queue()` immediately discards every queued frame so the user doesn't hear stale agent audio after they've spoken over it. Without this, the AudioSource would keep playing the buffered ~1 second of TTS even after the server has cut the agent off.

## Tuning the agent

### Pick a voice

```python
"output": {"voice": "james"}     # conversational US male
"output": {"voice": "sophie"}    # clear UK female
"output": {"voice": "diego"}     # Latin American Spanish
"output": {"voice": "arjun"}     # Hindi/Hinglish, code-switches with English
```

The full catalog is in the [voices reference](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/voices). Multilingual voices code-switch automatically. The Voice Agent API currently supports six languages end to end: English, Spanish, French, German, Italian, and Portuguese.

### Adjust the system prompt and greeting

These are the two highest-leverage settings for how the agent feels:

```python
"session": {
    "system_prompt": (
        "You are a customer support agent for Acme. Speak in 1–2 short "
        "sentences. Confirm the user's question before answering."
    ),
    "greeting": "Hi, this is Acme support — what's going on?",
}
```

You can re-send `session.update` mid-conversation to swap the prompt or voice. `greeting` is locked once spoken, but `system_prompt` and `voice` are not.

### Tune turn detection

Default values work for most apps. Override anything you want under `session.input.turn_detection`:

```python
"input": {
    "turn_detection": {
        "vad_threshold": 0.5,        # 0.0–1.0; higher = ignore more background noise
        "min_silence": 600,          # ms before confident end-of-turn
        "max_silence": 1500,         # ms hard ceiling
        "interrupt_response": True,  # set False to disable barge-in entirely
    }
}
```

For deliberate speech (eldercare, healthcare), raise `max_silence` to `2500`. For fast-paced conversation, drop `min_silence` to `300`.

### Boost domain-specific terms

If your conversation includes product names, medical terms, or rare proper nouns, add them to `session.input.keyterms`:

```python
"input": { "keyterms": ["Universal-3.6 Pro Realtime", "AssemblyAI", "LiveKit"] }
```

The recognizer biases toward those words, which dramatically improves accuracy on domain vocabulary.

### Multiple participants in one room

This worker bridges **one** remote audio track to the Voice Agent API. The agent only listens to the first participant who publishes audio — additional participants are ignored. There are two ways to scale this:

1. **One agent per room.** Spin up a separate worker process per room (LiveKit Cloud's worker dispatch makes this easy). Best for 1-on-1 use cases like phone-style support agents.
2. **Mix participants before sending.** If you genuinely want a meeting-style multi-talker agent, mix all remote audio with `rtc.AudioMixer` (bundled in `livekit-rtc`) and send the mix to one Voice Agent API session. The mixer handles sample-rate alignment and frame timing for you.

For most real-world apps, option 1 is the right call.

## Troubleshooting

**The worker connects but the client never hears the agent.**
The local track is published before the user joins — that's correct — but make sure your client subscribed to it. In the LiveKit Agents Playground, the agent's track shows up under the participant identity `voice-agent`. Confirm `can_subscribe=True` on the client's token.

**`UNAUTHORIZED` close on the AssemblyAI WebSocket.**
Your `ASSEMBLYAI_API_KEY` is missing, expired, or pasted with whitespace. Grab a fresh key from the [AssemblyAI dashboard](https://www.assemblyai.com/dashboard/signup) and confirm `.env` is loaded.

**LiveKit `ConnectError: invalid token`.**
The JWT signature didn't validate against the `LIVEKIT_API_SECRET`. Check that the URL, key, and secret all come from the same LiveKit project. For self-hosted, regenerate keys with `livekit-server generate-keys`.

**Audio is choppy or robotic.**
Almost always the audio buffer running dry. Confirm the worker is reaching the Voice Agent API with low jitter — run `python worker.py` close to your network egress. Inside `AudioSource(... queue_size_ms=1000)` you have one second of headroom; raise it to `2000` if you're seeing transient stalls.

**Audio sounds pitched up or down.**
Sample-rate mismatch. Both `AudioSource` and `AudioStream.from_track` must be configured at `sample_rate=24_000`, `num_channels=1`. The default for `AudioStream` is 48 kHz — make sure you override it.

**Agent keeps interrupting itself.**
Browser clients with `getUserMedia({ audio: { echoCancellation: true } })` (the default) handle this automatically. If you see it on a custom mobile client, make sure AEC is enabled on the capture side. The Voice Agent API also exposes `session.input.turn_detection.interrupt_response = false` to disable barge-in entirely while you debug.

The full Voice Agent API troubleshooting guide is in the [docs](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/troubleshooting).

## Related tutorials

- **Voice Agent API 5-minute quickstart** — minimal Python quickstart — the simplest version of this stack
- **Build a voice assistant app** — browser UI on top of the Voice Agent API with temporary-token auth
- **Voice agent that makes outbound calls** — Twilio outbound + Media Streams bridge with `audio/pcmu`
- **Twilio phone agent** — inbound phone calls — Twilio Media Streams + `audio/pcmu`
- **Agora voice agent** — Voice Agent API behind an Agora RTC channel
- **Daily.co voice agent** — Voice Agent API behind a Daily room using `daily-python`
- **Node.js voice agent** — JavaScript version of the 5-minute quickstart
- **Raw WebSocket voice agent** — every protocol event handled explicitly, with tool calling and session resume

## FAQ

### What is AssemblyAI's Voice Agent API?

AssemblyAI's Voice Agent API is a single WebSocket endpoint (`wss://agents.assemblyai.com/v1/ws`) that handles the full voice agent pipeline server-side: speech-to-text via Universal-3.6 Pro Realtime, an LLM that decides what to say, and a text-to-speech engine with 30+ voices. It includes neural turn detection, barge-in, and tool calling out of the box, so you can build a conversational agent without integrating separate STT, LLM, or TTS providers.

### Why use LiveKit with the Voice Agent API instead of going direct?

LiveKit handles real-time audio transport (WebRTC, mobile and browser SDKs, recording, scaling, and global edge distribution), which is a hard problem you don't want to solve yourself. The Voice Agent API handles the AI. Combining them gives you multi-user voice rooms, mobile clients, and recording — without building a WebRTC stack or wiring up STT, LLM, and TTS providers separately.

### Is this the LiveKit Agents framework?

No. The LiveKit Agents framework expects you to plug in separate STT, LLM, and TTS components. This tutorial uses the LiveKit `livekit-rtc` Python SDK directly to join a room as a server-side participant, then forwards audio to the AssemblyAI Voice Agent API, which replaces all three. If you want the framework approach with AssemblyAI as the STT, start with the [full LiveKit deploy guide](https://www.assemblyai.com/blog/build-and-deploy-real-time-ai-voice-agents-using-livekit-and-assemblyai).

### What audio format does the Voice Agent API expect?

By default the Voice Agent API uses `audio/pcm` — 16-bit signed little-endian PCM at 24,000 Hz, mono, base64-encoded. This worker configures both the LiveKit `AudioStream` (incoming) and `AudioSource` (outgoing) at 24 kHz mono so no manual resampling is needed; LiveKit's native FFI resampler handles the conversion to and from WebRTC's internal 48 kHz. For telephony you can switch to `audio/pcmu` (G.711 μ-law, 8 kHz) under `session.input.format` and `session.output.format`.

### Can the Voice Agent API call tools from inside a LiveKit room?

Yes. Register tool definitions in `session.tools` on `session.update`. When the agent decides to invoke one, the server emits a `tool.call` event with a `call_id`, function name, and arguments. Run the tool in your worker, then send back a `tool.result` event after you receive the next `reply.done` (not immediately on `tool.call`). Tool calls work the same whether the audio is coming from `sounddevice`, a LiveKit room, or a phone line — the transport is irrelevant to the AI layer.

### How do I scale to many concurrent rooms?

Run one worker per room. LiveKit Cloud's [agent dispatch](https://docs.livekit.io/agents/) (or your own room-watching service) can spin up a worker per active room, and each worker holds one Voice Agent API WebSocket. The Voice Agent API charges per session, LiveKit charges per minute of WebRTC, and both scale horizontally — there's no shared coordination point.

### How much does the Voice Agent API cost?

AssemblyAI offers a free tier so you can build and test without a credit card. For current pricing on the Voice Agent API and LiveKit Cloud, see the [AssemblyAI pricing page](https://www.assemblyai.com/pricing) and the [LiveKit Cloud pricing page](https://cloud.livekit.io).

## Resources

- [Voice Agent API overview](https://www.assemblyai.com/docs/voice-agents/voice-agent-api)
- [Session configuration](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/session-configuration)
- [Audio format](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/audio-format)
- [Voices catalog](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/voices)
- [Tool calling](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/tool-calling)
- [Browser integration](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/browser-integration)
- [Events reference](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/events-reference)
- [Troubleshooting](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/troubleshooting)
- [LiveKit overview](https://docs.livekit.io/intro/overview/)
- [LiveKit Cloud admin](https://docs.livekit.io/deploy/admin/)
- [Self-hosting LiveKit locally](https://docs.livekit.io/transport/self-hosting/local/)
- [Generating LiveKit tokens](https://docs.livekit.io/frontends/reference/tokens-grants/)
- [LiveKit Python SDKs on GitHub](https://github.com/livekit/python-sdks)

---

<div class="blog-cta_component">
  <div class="blog-cta_title">Build your first voice agent today</div>
  <div class="blog-cta_rt w-richtext">
    <p>Sign up for a free AssemblyAI account and ship a working voice agent with the Voice Agent API in minutes. No credit card required.</p>
  </div>
  <a href="https://www.assemblyai.com/dashboard/signup" class="button w-button">Start building</a>
</div>

<div class="blog-cta_component">
  <div class="blog-cta_title">Hear every Voice Agent API voice</div>
  <div class="blog-cta_rt w-richtext">
    <p>Compare 18 English voices and 16 multilingual voices side by side, then drop your pick straight into <code>session.output.voice</code>.</p>
  </div>
  <a href="https://www.assemblyai.com/docs/voice-agents/voice-agent-api/voices" class="button w-button">Browse voices</a>
</div>
