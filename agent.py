import os

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    RunContext,
    TurnHandlingOptions,
    cli,
    function_tool,
)
from livekit.plugins import assemblyai, openai, silero

load_dotenv()

# Silero (used for barge-in) should agree with AssemblyAI's own VAD on what
# counts as speech, so both use the STT's default threshold.
VAD_THRESHOLD = 0.2

# Stand-in for your order system
ORDERS = {
    "1472": {"status": "out for delivery", "eta": "today before 6 pm"},
    "2210": {"status": "delivered", "eta": "delivered yesterday"},
}


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are a customer support agent for Acme. Speak in one or two "
                "short sentences, with no lists, markdown, or emojis. Confirm the "
                "caller's question before answering. Use lookup_order whenever "
                "the caller asks about an order."
            ),
        )

    async def on_enter(self) -> None:
        # A fixed greeting is spoken right away, without waiting on the LLM
        self.session.say("Hi, this is Acme support. What's going on?")

    @function_tool
    async def lookup_order(self, context: RunContext, order_number: str) -> dict:
        """Look up a customer's order by its order number.

        Args:
            order_number: The order number, digits only.
        """
        return ORDERS.get(order_number, {"status": "not found"})


server = AgentServer()


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load(activation_threshold=VAD_THRESHOLD)


server.setup_fnc = prewarm


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    tts = assemblyai.TTS(
        model="assemblyai-tts-1",
        voice="49c443a4-8a1d-5f7e-b70b-98bdbd9823c3",  # Audrey
    )
    # Open the TTS session now, so the first reply doesn't wait on the handshake
    tts.prewarm()

    session = AgentSession(
        stt=assemblyai.STT(
            model="universal-3-6-pro",
            mode="balanced",
            vad_threshold=VAD_THRESHOLD,
            keyterms_prompt=["Acme", "LiveKit", "AssemblyAI"],
        ),
        # The LLM Gateway, through its OpenAI-compatible API
        llm=openai.LLM(
            model="gemma-4-31b",
            base_url="https://llm-gateway.assemblyai.com/v1",
            api_key=os.environ["ASSEMBLYAI_API_KEY"],
        ),
        tts=tts,
        vad=ctx.proc.userdata["vad"],
        turn_handling=TurnHandlingOptions(
            # AssemblyAI decides when the user has finished speaking...
            turn_detection="stt",
            # ...so LiveKit shouldn't add its own delay on top
            endpointing={"min_delay": 0},
        ),
        tts_text_transforms=["filter_emoji", "filter_markdown"],
        # When the user interrupts, keep only the words they heard in the chat history
        use_tts_aligned_transcript=True,
    )

    await session.start(agent=Assistant(), room=ctx.room)


if __name__ == "__main__":
    cli.run_app(server)
