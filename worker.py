"""
LiveKit room participant + AssemblyAI Voice Agent API bridge.

LiveKit handles the WebRTC transport (multi-user rooms, browser/mobile clients,
recording, scaling, geographic distribution). The AssemblyAI Voice Agent API
handles the entire AI pipeline (speech-to-text, LLM, text-to-speech) over a
single WebSocket — no separate STT, LLM, or TTS services to wire up.

Pipeline:

    Browser/mobile client (any LiveKit SDK)
            │ WebRTC into a LiveKit room
            ▼
    This worker — joins the room as a server-side participant
            │ subscribes to remote audio (24 kHz mono PCM16)
            ▼
    wss://agents.assemblyai.com/v1/ws  (Voice Agent API)
            │ replies with 24 kHz mono PCM16
            ▼
    This worker publishes a local audio track back into the room

Verified against livekit==1.1.6 and livekit-api==1.1.0. The livekit `AudioStream`
and `AudioSource` natively resample to/from any sample rate, so we ask both for
24 kHz mono — the same format the Voice Agent API uses by default.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import signal
from typing import Optional

import websockets
from dotenv import load_dotenv
from livekit import api, rtc

# ── Configuration ──────────────────────────────────────────────────────────

load_dotenv()

ASSEMBLYAI_API_KEY = os.environ["ASSEMBLYAI_API_KEY"]
LIVEKIT_URL = os.environ["LIVEKIT_URL"]
LIVEKIT_API_KEY = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ["LIVEKIT_API_SECRET"]
ROOM_NAME = os.environ.get("ROOM_NAME", "voice-agent-demo")

VOICE_AGENT_URL = "wss://agents.assemblyai.com/v1/ws"

# 24 kHz mono PCM16 is the Voice Agent API default. LiveKit can deliver any
# sample rate, so we just ask for 24 kHz directly on both sides.
SAMPLE_RATE = 24_000
NUM_CHANNELS = 1

# 20 ms of audio per outbound capture frame. The Voice Agent API recommends
# ~50 ms chunks, but smaller chunks reduce barge-in latency without hurting
# the STT side.
SAMPLES_PER_FRAME = SAMPLE_RATE // 50  # 20 ms = 480 samples at 24 kHz

SYSTEM_PROMPT = (
    "You are a friendly voice assistant joining a LiveKit call. "
    "Keep replies to one or two short sentences. Speak in natural prose — "
    "no bullet points, no markdown, no lists."
)
GREETING = "Hi there — I'm joining your LiveKit room. What can I help with?"
VOICE = "ivy"  # see https://www.assemblyai.com/docs/voice-agents/voice-agent-api/voices

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
log = logging.getLogger("voice-agent-livekit")


# ── Token generation (so this worker can join the LiveKit room) ────────────


def make_access_token(identity: str = "voice-agent") -> str:
    """Generate a server-side LiveKit JWT for this worker.

    For browser clients you would generate tokens from a separate HTTP endpoint;
    here we just mint one directly so the worker can connect.
    """
    return (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name("AssemblyAI Voice Agent")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=ROOM_NAME,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .to_jwt()
    )


# ── The bridge ─────────────────────────────────────────────────────────────


class VoiceAgentBridge:
    """Bridges one LiveKit remote audio track to the AssemblyAI Voice Agent API."""

    def __init__(self, room: rtc.Room, audio_source: rtc.AudioSource) -> None:
        self.room = room
        self.audio_source = audio_source
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.session_ready = asyncio.Event()
        self.session_id: Optional[str] = None
        self._mic_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None

    async def run(self, mic_track: rtc.RemoteAudioTrack) -> None:
        headers = {"Authorization": f"Bearer {ASSEMBLYAI_API_KEY}"}
        log.info("Connecting to Voice Agent API …")

        async with websockets.connect(VOICE_AGENT_URL, additional_headers=headers) as ws:
            self.ws = ws

            await ws.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": {
                            "system_prompt": SYSTEM_PROMPT,
                            "greeting": GREETING,
                            "input": {"format": {"encoding": "audio/pcm"}},
                            "output": {
                                "voice": VOICE,
                                "format": {"encoding": "audio/pcm"},
                            },
                        },
                    }
                )
            )

            self._mic_task = asyncio.create_task(self._forward_mic(mic_track))
            self._recv_task = asyncio.create_task(self._receive_loop())

            try:
                await asyncio.gather(self._mic_task, self._recv_task)
            except asyncio.CancelledError:
                pass

    async def _forward_mic(self, mic_track: rtc.RemoteAudioTrack) -> None:
        """Read remote audio frames from LiveKit and ship them to the agent."""
        # AudioStream resamples to whatever sample_rate we ask for.
        stream = rtc.AudioStream.from_track(
            track=mic_track,
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
        )
        await self.session_ready.wait()
        log.info("Forwarding mic audio to Voice Agent API …")

        async for event in stream:
            if self.ws is None or self.ws.closed:
                break
            # AudioFrame.data is a memoryview of int16 samples; raw PCM16 bytes
            # are the underlying buffer.
            pcm16_bytes = bytes(event.frame.data)
            await self.ws.send(
                json.dumps(
                    {
                        "type": "input.audio",
                        "audio": base64.b64encode(pcm16_bytes).decode("ascii"),
                    }
                )
            )

    async def _receive_loop(self) -> None:
        """Decode events from the agent and pump reply audio back into the room."""
        assert self.ws is not None
        async for raw in self.ws:
            event = json.loads(raw)
            t = event.get("type")

            if t == "session.ready":
                self.session_id = event.get("session_id")
                log.info("Session ready (%s).", self.session_id)
                self.session_ready.set()

            elif t == "input.speech.started":
                # Barge-in: user is talking again. Stop playback immediately so the
                # agent can hear them and so we don't keep speaking over them.
                self.audio_source.clear_queue()

            elif t == "transcript.user":
                log.info("User: %s", event.get("text", ""))

            elif t == "transcript.agent":
                log.info("Agent: %s", event.get("text", ""))

            elif t == "reply.audio":
                pcm = base64.b64decode(event["data"])
                # base64 decoded → raw PCM16 → wrap in AudioFrame and capture.
                samples = len(pcm) // 2  # 2 bytes per int16 sample, mono
                frame = rtc.AudioFrame(
                    data=pcm,
                    sample_rate=SAMPLE_RATE,
                    num_channels=NUM_CHANNELS,
                    samples_per_channel=samples,
                )
                await self.audio_source.capture_frame(frame)

            elif t == "reply.done":
                if event.get("status") == "interrupted":
                    self.audio_source.clear_queue()

            elif t == "session.error":
                log.error("Session error: %s", event)
                return


# ── Main entrypoint ────────────────────────────────────────────────────────


async def main() -> None:
    room = rtc.Room()
    audio_source = rtc.AudioSource(sample_rate=SAMPLE_RATE, num_channels=NUM_CHANNELS)
    local_track = rtc.LocalAudioTrack.create_audio_track("agent-voice", audio_source)

    bridge_started = asyncio.Event()
    active_bridge: dict[str, asyncio.Task] = {}

    @room.on("participant_connected")
    def _on_participant_connected(p: rtc.RemoteParticipant) -> None:
        log.info("Participant joined: %s", p.identity)

    @room.on("track_subscribed")
    def _on_track_subscribed(
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        if "task" in active_bridge:
            log.info(
                "Already bridging audio for another participant; ignoring %s.",
                participant.identity,
            )
            return
        log.info("Subscribed to audio track from %s.", participant.identity)
        bridge = VoiceAgentBridge(room, audio_source)
        active_bridge["task"] = asyncio.create_task(bridge.run(track))  # type: ignore[arg-type]
        bridge_started.set()

    @room.on("disconnected")
    def _on_disconnected(*_: object) -> None:
        log.info("Disconnected from LiveKit room.")
        for task in active_bridge.values():
            task.cancel()

    token = make_access_token()
    log.info("Connecting to LiveKit room %r at %s …", ROOM_NAME, LIVEKIT_URL)
    await room.connect(LIVEKIT_URL, token)
    log.info("Connected as worker. Publishing reply track …")

    # Publish first so the room can route audio back to clients the moment the
    # agent starts speaking.
    await room.local_participant.publish_track(
        local_track,
        rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
    )

    log.info("Waiting for a participant to publish a microphone track …")

    # Wait until someone joins and publishes audio, then keep running until
    # the bridge task ends or we get a SIGINT.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows

    try:
        await stop.wait()
    finally:
        log.info("Shutting down.")
        for task in active_bridge.values():
            task.cancel()
        await room.disconnect()
        await audio_source.aclose()


if __name__ == "__main__":
    asyncio.run(main())
