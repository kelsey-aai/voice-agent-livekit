# Build a production voice agent with LiveKit and AssemblyAI

Companion code for the AssemblyAI blog post **[Build a production voice agent with LiveKit and AssemblyAI](https://www.assemblyai.com/blog/build-production-voice-agent-livekit-assemblyai)**.

A multi-user, browser-ready voice agent in about 100 lines of Python. **LiveKit** handles transport and orchestration through the LiveKit Agents framework. **AssemblyAI** runs all three models behind it:

- **Speech-to-text:** [Universal-3.6 Pro Realtime](https://www.assemblyai.com/blog/universal-3-6-pro-realtime) (`universal-3-6-pro`), with end-of-turn detection built in
- **LLM:** the [LLM Gateway](https://www.assemblyai.com/products/llm-gateway), through LiveKit's OpenAI plugin (it speaks the OpenAI Chat Completions API)
- **Text-to-speech:** the [AssemblyAI Text-to-Speech API](https://www.assemblyai.com/products/text-to-speech) (`assemblyai-tts-1`)

One AssemblyAI API key covers all three. Barge-in, tool calling, and turn detection work out of the box.

## Who does what

| Job | Handled by | Where you set it |
|---|---|---|
| Rooms, WebRTC, clients | LiveKit Cloud or self-hosted `livekit-server` | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` |
| Speech-to-text | Universal-3.6 Pro Realtime | `assemblyai.STT(model="universal-3-6-pro")` |
| Turn detection | Universal-3.6 Pro Realtime (punctuation-based end of turn) | `TurnHandlingOptions(turn_detection="stt")` |
| Language model | LLM Gateway (OpenAI-compatible) | `openai.LLM(base_url="https://llm-gateway.assemblyai.com/v1")` |
| Text-to-speech | `assemblyai-tts-1` | `assemblyai.TTS(voice=...)` |
| Barge-in | LiveKit stops playback; the TTS plugin cancels the request in flight | On by default; Silero VAD in `AgentSession` |
| Tool calling | The LLM request (OpenAI-style tools) | `@function_tool` methods on your `Agent` |
| API keys to manage | 2 | AssemblyAI + LiveKit |

## Architecture

```
Browser / iOS / Android client (any LiveKit SDK)
        │
        │  WebRTC into a LiveKit room
        ▼
LiveKit Cloud (or self-hosted livekit-server)
        │
        │  dispatches a job to your agent worker for each new room
        ▼
agent.py — LiveKit Agents AgentSession, joined to the room
        │
        ├── assemblyai.STT  → wss://streaming.assemblyai.com
        │                     Universal-3.6 Pro Realtime: transcripts + end of turn
        │
        ├── openai.LLM      → https://llm-gateway.assemblyai.com/v1
        │                     LLM Gateway: the reply, plus tool calls
        │
        └── assemblyai.TTS  → wss://tts.us.assemblyai.com
                              assemblyai-tts-1: one session, one request per sentence
        │
        │  agent audio published as a track in the same room
        ▼
LiveKit Cloud → WebRTC out → the client hears the agent
```

Your code never touches WebRTC or audio frames. LiveKit moves the media and runs the turn-taking loop; the three plugins turn audio into text, text into a reply, and the reply back into audio.

## Prerequisites

- **Python 3.10+** (the LiveKit Agents framework requires it)
- **An [AssemblyAI API key](https://www.assemblyai.com/dashboard/signup).** The same key works for speech-to-text, text-to-speech, and the LLM Gateway. LLM Gateway access may require a paid account; if yours doesn't have it yet, use your own OpenAI key for the LLM (see [Swapping a piece out](#swapping-a-piece-out)).
- **A LiveKit server**, either a free [LiveKit Cloud project](https://cloud.livekit.io) or a [self-hosted](https://docs.livekit.io/transport/self-hosting/local/) `livekit-server`. Console mode doesn't need one.
- **A LiveKit client** to talk to the agent. The fastest path is the hosted [LiveKit Agents Playground](https://agents-playground.livekit.io).

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/kelsey-aai/voice-agent-livekit
cd voice-agent-livekit

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

The `openai` extra installs LiveKit's OpenAI plugin, which is how the agent talks to the LLM Gateway. `silero` is the voice activity detector LiveKit uses for barge-in.

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in `.env`:

```ini
ASSEMBLYAI_API_KEY=           # https://www.assemblyai.com/dashboard/signup

# Not needed for console mode, only to connect to LiveKit
LIVEKIT_URL=wss://<project>.livekit.cloud
LIVEKIT_API_KEY=              # LiveKit Cloud → Settings → Keys
LIVEKIT_API_SECRET=
```

For self-hosted LiveKit, run `livekit-server --dev` and use `LIVEKIT_URL=ws://localhost:7880`. The dev server prints an API key and secret on startup.

### 3. Talk to it in your terminal

```bash
# One-time: download the Silero VAD model files
python agent.py download-files

# Talk to the agent with your own microphone and speakers
python agent.py console
```

Console mode uses your computer's microphone and speakers and needs only your AssemblyAI API key. Use headphones so the agent doesn't hear its own voice. Recent `livekit-agents` releases print a deprecation notice for `console` and point at `lk agent console`, which runs the same mode through the LiveKit CLI. On a machine without a microphone, `python agent.py console --text` takes typed turns.

### 4. Connect it to a LiveKit room

With your LiveKit credentials in `.env`, run the worker in development mode:

```bash
python agent.py dev
```

The worker registers with your LiveKit project and waits. Because `@server.rtc_session()` sets no `agent_name`, LiveKit dispatches the agent into every new room automatically. Then connect a client:

1. Open the [LiveKit Agents Playground](https://agents-playground.livekit.io) and connect it to your LiveKit Cloud project, or paste your `LIVEKIT_URL` and a token generated in the LiveKit Cloud dashboard (Settings → Keys → Create token).
2. Click **Connect**, allow microphone access, and start talking. The agent joins the room, greets you, and answers through your browser.

You can also connect from your own LiveKit Web, iOS, Android, or Flutter app pointed at the same project.

## How it works

Everything is in [`agent.py`](agent.py). Five pieces do the real work.

### 1. Speech-to-text and end of turn

```python
stt=assemblyai.STT(
    model="universal-3-6-pro",
    mode="balanced",
    vad_threshold=VAD_THRESHOLD,
    keyterms_prompt=["Acme", "LiveKit", "AssemblyAI"],
),
```

`assemblyai.STT` streams the room's audio to Universal-3.6 Pro Realtime and gets back transcripts plus an end-of-turn decision. `mode="balanced"` applies the turn-silence settings that match streaming directly to the API. `vad_threshold` keeps AssemblyAI's voice activity detection in step with Silero's. `keyterms_prompt` boosts the names your agent has to hear right.

### 2. The LLM, through the LLM Gateway

```python
llm=openai.LLM(
    model="gemma-4-31b",
    base_url="https://llm-gateway.assemblyai.com/v1",
    api_key=os.environ["ASSEMBLYAI_API_KEY"],
),
```

LiveKit's stock `openai.LLM` works once you point `base_url` at the Gateway and pass your AssemblyAI key. `model` takes any model on the [available models list](https://www.assemblyai.com/docs/llm-gateway/available-models) that supports tools and streaming.

### 3. The voice

```python
tts = assemblyai.TTS(
    model="assemblyai-tts-1",
    voice="49c443a4-8a1d-5f7e-b70b-98bdbd9823c3",  # Audrey
)
tts.prewarm()
```

`assemblyai.TTS` keeps one WebSocket session open for the whole call and sends each sentence of the LLM's reply as its own request, so the agent starts speaking as soon as the first sentence is ready. Set `voice` explicitly: the plugin's default voice differs from the API's own default. `tts.prewarm()` opens the session while the agent is still joining the room.

### 4. Turn handling and barge-in

```python
turn_handling=TurnHandlingOptions(
    turn_detection="stt",
    endpointing={"min_delay": 0},
),
tts_text_transforms=["filter_emoji", "filter_markdown"],
use_tts_aligned_transcript=True,
```

`turn_detection="stt"` hands the end-of-turn decision to Universal-3.6 Pro Realtime, and `min_delay: 0` stops LiveKit from adding its own wait on top. When the user talks over the agent, LiveKit stops playback and the TTS plugin cancels the request in flight while keeping the session for the next reply. `use_tts_aligned_transcript=True` uses per-word timings from the TTS so the chat history keeps an interrupted reply only up to the word the user heard.

### 5. Tool calling

```python
@function_tool
async def lookup_order(self, context: RunContext, order_number: str) -> dict:
    """Look up a customer's order by its order number.

    Args:
        order_number: The order number, digits only.
    """
    return ORDERS.get(order_number, {"status": "not found"})
```

Tools live in the LLM request. Any `@function_tool` method on your `Agent` is sent to the LLM as an OpenAI-style tool, with its schema built from the type hints and its description from the docstring. Swap the `ORDERS` dictionary for a call to your real backend.

## Tuning the agent

### Pick a voice and a language

```python
# Any voice ID from the dashboard voice library, or a voice you cloned
tts = assemblyai.TTS(
    voice="49c443a4-8a1d-5f7e-b70b-98bdbd9823c3",  # Audrey
    language="es",  # a code or an English name such as "spanish"
)

# Later in the call: the next reply opens a new session with these options
tts.update_options(language="french")
```

Copy a voice ID from the [voice library](https://www.assemblyai.com/dashboard/text-to-speech/voices) in the dashboard, or [clone one](https://www.assemblyai.com/docs/tts/cloned-voices) from a short recording of a speaker who consents to it. `assemblyai-tts-1` speaks every voice in all six of its languages: English, Spanish, French, German, Italian, and Portuguese. The API doesn't detect language, so set `language` whenever the LLM writes in a language other than the voice's own. See [Voices](https://www.assemblyai.com/docs/tts/voices).

### Adjust the instructions and greeting

Both live in the `Assistant` class: `instructions` is the system prompt, and the line passed to `self.session.say()` in `on_enter` is the greeting. Keep the instructions asking for short, plain sentences; a short first sentence gets audio out sooner.

### Tune turn detection

```python
stt=assemblyai.STT(
    model="universal-3-6-pro",
    mode="max_accuracy",  # "min_latency" · "balanced" · "max_accuracy"
    vad_threshold=VAD_THRESHOLD,
),
```

`min_latency` hands turns off fastest, `balanced` is the right default for most conversations, and `max_accuracy` favors quality when callers read out long entities such as account numbers or addresses. The [Universal-3.6 Pro on LiveKit](https://www.assemblyai.com/docs/voice-agents/livekit-universal-3-6-pro) guide covers the individual turn-silence settings.

### Boost domain terms and tune for the room

```python
stt=assemblyai.STT(
    model="universal-3-6-pro",
    mode="balanced",
    vad_threshold=VAD_THRESHOLD,
    prompt="Customer support call about a delayed delivery.",
    keyterms_prompt=["Acme", "Express Saver", "LiveKit"],
    voice_focus="far-field",
),
```

- `prompt` describes what the conversation is about. Start with none, and add context only if domain vocabulary is being misrecognized.
- `keyterms_prompt` boosts exact strings: product names, brand names, rare proper nouns.
- `voice_focus` isolates the speaker's voice: `"far-field"` for laptop mics and open offices, `"near-field"` for headsets and handsets.

On `livekit-agents` 1.6.6 and later, `AgentSession` forwards each agent reply into the STT's `agent_context` for you.

### Swapping a piece out

Every model is its own plugin. Already pay for OpenAI directly? Drop the `base_url` and `api_key` arguments from `openai.LLM` and set `OPENAI_API_KEY`. Want a different model on the Gateway? Change the `model` string. The rest of the agent stays the same.

### Multiple participants in one room

An `AgentSession` listens to one participant: the first one to join the room, unless you name another with `room_options=room_io.RoomOptions(participant_identity=...)` in `session.start()`. To scale, run one agent per room (LiveKit dispatches a job for every new room), or switch the linked participant as the conversation moves; see LiveKit's [Agents documentation](https://docs.livekit.io/agents/).

## Deploy it

```bash
python agent.py start
```

`start` runs the worker in production mode and gives active calls time to finish before shutdown. Each worker registers with LiveKit and takes jobs as rooms are created, so you scale out by running more of them. See LiveKit's [deployment guide](https://docs.livekit.io/agents/ops/deployment/).

### Keep data in the EU

```python
assemblyai.STT(..., base_url="wss://streaming.eu.assemblyai.com")
assemblyai.TTS(..., base_url="wss://tts.eu.assemblyai.com")
openai.LLM(..., base_url="https://llm-gateway.eu.assemblyai.com/v1")
```

The EU LLM Gateway serves a different set of models, so check the [available models](https://www.assemblyai.com/docs/llm-gateway/available-models) before you pick one.

## Troubleshooting

**The agent never joins the room.** Check that the worker printed its registration with your LiveKit project and that `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET` all come from the same project. If you set an `agent_name` on `@server.rtc_session()`, automatic dispatch is off.

**Authentication errors from AssemblyAI.** Your `ASSEMBLYAI_API_KEY` is missing, expired, or pasted with whitespace. If only the LLM fails, your account may not have LLM Gateway access yet; switch `openai.LLM` to your own OpenAI key.

**LiveKit `ConnectError: invalid token`.** The URL, key, and secret must all come from the same LiveKit project. For self-hosted, regenerate keys with `livekit-server generate-keys`.

**Silero model files are missing.** Run `python agent.py download-files` once on every machine or container image that runs the worker.

**The first reply is slow after a long silence.** A TTS session closes after two minutes idle, and the next reply opens a new one. That's expected in calls with long pauses; see [Limits](https://www.assemblyai.com/docs/tts/limits).

**Agent keeps interrupting itself.** Browser clients with echo cancellation (the `getUserMedia` default) handle this. On a custom mobile client, enable acoustic echo cancellation on the capture side. In console mode, use headphones.

## Resources

- Blog post: [Build a production voice agent with LiveKit and AssemblyAI](https://www.assemblyai.com/blog/build-production-voice-agent-livekit-assemblyai)
- [TTS on LiveKit (docs)](https://www.assemblyai.com/docs/tts/livekit)
- [Universal-3.6 Pro on LiveKit (docs)](https://www.assemblyai.com/docs/voice-agents/livekit-universal-3-6-pro)
- [LLM Gateway available models](https://www.assemblyai.com/docs/llm-gateway/available-models)
- [AssemblyAI pricing](https://www.assemblyai.com/pricing)
- [AssemblyAI reference voice agent for LiveKit](https://github.com/AssemblyAI-Solutions/assemblyai-livekit-reference-voice-agent)
- [LiveKit Agents documentation](https://docs.livekit.io/agents/)
