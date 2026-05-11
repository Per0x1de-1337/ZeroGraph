"""
Pipeline: orchestrates the full STT → LLM → TTS loop for one WebSocket session.

Flow per turn:
  client vad_start  → state LISTENING
  client vad_end    → semantic check → fire immediately or arm 200ms grace timer
  Deepgram final    → if timer running, cancel it and fire now (preemptive)
  Deepgram UTT_END  → fallback if VAD path didn't fire

Interruption flow:
  vad_start while SPEAKING → start 300ms timer
  timer fires → cancel LLM+TTS tasks → send INTERRUPT to browser → LISTENING
  vad_end within 300ms → cancel timer (backchannel, ignore)

LiveKit-inspired additions:
  - Backchannel filter: "yeah/ok/uh-huh" never reaches the LLM
  - Semantic endpointing: fires immediately on complete sentences, waits on hedges
  - Preemptive generation: Deepgram final cancels the grace timer → pipeline starts sooner
"""

import asyncio
import json
import logging
import time
from fastapi import WebSocket
from state_machine import StateMachine, AgentState
from stt import DeepgramSTT
from llm import LLM
from tts import CartesiaTTS

logger = logging.getLogger(__name__)

# Speech must persist for at least this long during SPEAKING to count as an interruption
INTERRUPT_CONFIRM_MS = 300

# ── Backchannel detection ──────────────────────────────────────────────────────
# Short filler responses that acknowledge but don't need an LLM reply.
_BACKCHANNELS = frozenset({
    "yeah", "yes", "yep", "yup", "ok", "okay", "sure", "right", "alright",
    "uh-huh", "mhm", "mm-hmm", "hmm", "mm", "hm", "nope", "no",
    "cool", "nice", "great", "thanks", "thank you", "good", "fine",
    "gotcha", "got it", "absolutely", "of course", "exactly", "correct",
    "please", "go ahead", "go on", "continue",
})

# Words that signal the user is mid-thought and hasn't finished
_HEDGE_ENDINGS = frozenset({
    "and", "but", "or", "um", "uh", "like", "so", "because",
    "although", "however", "though", "while", "since", "if", "then",
    "the", "a", "an",
})


def _is_backchannel(transcript: str) -> bool:
    """True for short filler responses that don't need an LLM reply."""
    clean = transcript.lower().strip().rstrip(".,!?").strip()
    words = clean.split()
    if not words:
        return True
    if len(words) <= 2:
        return all(w.rstrip(",") in _BACKCHANNELS for w in words)
    return False


def _is_semantically_complete(transcript: str) -> bool:
    """Heuristic: does the transcript look like a complete thought?

    Returns True  → fire immediately (no grace wait needed)
    Returns False → arm the 200ms grace timer instead
    """
    t = transcript.strip()
    if not t:
        return False
    # Deepgram adds punctuation — sentence-final punctuation is the strongest signal
    if t[-1] in ".?!":
        return True
    last_word = t.lower().split()[-1].rstrip(".,")
    # User is clearly mid-sentence
    if last_word in _HEDGE_ENDINGS:
        return False
    # Long enough without hedge endings → treat as complete
    return len(t.split()) >= 7


class Session:
    def __init__(
        self,
        ws: WebSocket,
        openai_key: str,
        deepgram_key: str,
        cartesia_key: str,
        voice_id: str,
    ):
        self.ws = ws
        self.sm = StateMachine()
        self.stt = DeepgramSTT(deepgram_key)
        self.llm = LLM(openai_key)
        self.tts = CartesiaTTS(cartesia_key, voice_id)

        self._pipeline_task: asyncio.Task | None = None
        self._interrupt_timer: asyncio.Task | None = None
        self._endpoint_timer: asyncio.Task | None = None

        # Blocks quick-endpoint from firing during echo tail after agent finishes
        self._echo_cooldown_until: float = 0.0

        # Transcript accumulated across multiple Deepgram final results per utterance
        self._transcript_buf: str = ""

        # Timing for metrics
        self._turn_start: float = 0.0
        self._metrics: dict = {}

        # Background drainer tasks
        self._drainer_tasks: list[asyncio.Task] = []

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def start(self):
        await self.stt.connect()

        self._drainer_tasks = [
            asyncio.create_task(self._drain_partials()),
            asyncio.create_task(self._drain_finals()),
            asyncio.create_task(self._drain_utterance_ends()),
        ]

        self.sm.force(AgentState.LISTENING)
        await self._send_state()
        logger.info("Session started")

    async def stop(self):
        self._cancel_interrupt_timer()
        await self._cancel_pipeline()
        for t in self._drainer_tasks:
            t.cancel()
        await asyncio.gather(*self._drainer_tasks, return_exceptions=True)
        await self.stt.disconnect()
        logger.info("Session stopped")

    # -------------------------------------------------------------------------
    # Incoming message handler (called by WebSocket handler in main.py)
    # -------------------------------------------------------------------------

    async def handle(self, message):
        if isinstance(message, bytes):
            # Always forward audio to Deepgram — client sends continuously.
            # Deepgram accumulates audio in all states so there's no transcript
            # burst when we transition back to LISTENING after SPEAKING.
            # The STT queue drainers ignore transcripts while not in LISTENING state.
            await self.stt.send(message)
        else:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                return

            t = data.get("type")
            if t == "vad_start":
                await self._on_vad_start()
            elif t == "vad_end":
                await self._on_vad_end()
            elif t == "ping":
                await self._send({"type": "pong", "t": data.get("t")})

    # -------------------------------------------------------------------------
    # VAD events
    # -------------------------------------------------------------------------

    async def _on_vad_start(self):
        # Cancel any pending quick-endpoint — user is speaking again
        self._cancel_endpoint_timer()

        if self.sm.state == AgentState.SPEAKING:
            self._cancel_interrupt_timer()
            self._interrupt_timer = asyncio.create_task(self._interruption_confirm())
        elif self.sm.state in (AgentState.LISTENING, AgentState.INTERRUPTED):
            self._turn_start = time.monotonic()

    async def _on_vad_end(self):
        # If user stopped during agent speech within confirmation window → backchannel
        self._cancel_interrupt_timer()

        transcript = self._transcript_buf.strip()
        in_cooldown = time.monotonic() < self._echo_cooldown_until

        if self.sm.state not in (AgentState.LISTENING, AgentState.INTERRUPTED):
            return
        if in_cooldown or len(transcript.split()) < 4:
            return

        self._cancel_endpoint_timer()

        if _is_semantically_complete(transcript):
            # Complete sentence detected — fire immediately, no grace period needed
            logger.info("Semantic endpoint (complete sentence)")
            await self._on_utterance_end()
        else:
            # Uncertain — wait 200ms for any in-flight Deepgram final
            self._endpoint_timer = asyncio.create_task(self._quick_endpoint())

    async def _quick_endpoint(self):
        """Grace period — lets any in-flight Deepgram final arrive before firing."""
        await asyncio.sleep(0.20)
        if self.sm.state in (AgentState.LISTENING, AgentState.INTERRUPTED):
            logger.info("Quick endpoint (grace period elapsed)")
            await self._on_utterance_end()

    async def _interruption_confirm(self):
        """Wait INTERRUPT_CONFIRM_MS. If still SPEAKING then fire the interruption."""
        await asyncio.sleep(INTERRUPT_CONFIRM_MS / 1000)
        if self.sm.state == AgentState.SPEAKING:
            logger.info("Interruption confirmed")
            await self._fire_interruption()

    async def _fire_interruption(self):
        await self._cancel_pipeline()
        self.sm.force(AgentState.INTERRUPTED)
        # Tell browser to flush its audio queue immediately
        await self._send({"type": "interrupt"})
        await self._send_state()
        # Discard any STT results queued while agent was speaking
        self.stt.flush_queues()
        self._transcript_buf = ""
        self._echo_cooldown_until = time.monotonic() + 0.25
        self.sm.force(AgentState.LISTENING)
        await self._send_state()

    # -------------------------------------------------------------------------
    # STT queue drainers (background tasks)
    # -------------------------------------------------------------------------

    async def _drain_partials(self):
        while True:
            text = await self.stt.partial_q.get()
            if self.sm.state in (AgentState.LISTENING, AgentState.INTERRUPTED):
                await self._send({"type": "transcript", "text": text, "final": False})

    async def _drain_finals(self):
        while True:
            text = await self.stt.final_q.get()
            if self.sm.state not in (AgentState.LISTENING, AgentState.INTERRUPTED):
                continue  # discard — agent is speaking or processing
            self._transcript_buf += (" " if self._transcript_buf else "") + text
            await self._send({
                "type": "transcript",
                "text": self._transcript_buf,
                "final": True,
            })

            # Preemptive generation: if the quick-endpoint grace timer is already
            # running, a Deepgram final just arrived — no need to keep waiting.
            # Cancel the timer and fire now to save up to 200ms per turn.
            if self._endpoint_timer and not self._endpoint_timer.done():
                logger.info("Preemptive endpoint (Deepgram final arrived during grace)")
                self._cancel_endpoint_timer()
                await self._on_utterance_end()

    async def _drain_utterance_ends(self):
        while True:
            await self.stt.utterance_end_q.get()
            await self._on_utterance_end()

    async def _on_utterance_end(self):
        transcript = self._transcript_buf.strip()
        if not transcript:
            return
        if self.sm.state not in (AgentState.LISTENING, AgentState.INTERRUPTED):
            return
        if time.monotonic() < self._echo_cooldown_until:
            logger.debug("Utterance end suppressed (echo cooldown)")
            self._transcript_buf = ""
            return

        # Backchannel filter: short affirmations don't need an LLM reply
        if _is_backchannel(transcript):
            logger.info(f"Backchannel suppressed: '{transcript}'")
            self._transcript_buf = ""
            return

        self._cancel_endpoint_timer()  # prevent double-fire if both quick + UTT_END arrive
        self._transcript_buf = ""
        self._metrics = {
            "ttfp": int((time.monotonic() - self._turn_start) * 1000)
        }
        logger.info(f"Utterance: '{transcript}'")

        self.sm.force(AgentState.PROCESSING)
        await self._send_state()

        self._pipeline_task = asyncio.create_task(
            self._run_pipeline(transcript)
        )

    # -------------------------------------------------------------------------
    # LLM + TTS pipeline
    # -------------------------------------------------------------------------

    async def _run_pipeline(self, transcript: str):
        """
        Produces LLM tokens → detects sentence boundaries → streams TTS audio.

        LLM and TTS run concurrently via a sentence queue:
          llm_producer fills queue with complete sentences
          tts_consumer drains queue, streams audio for each sentence

        This means TTS for sentence 1 plays while LLM is already generating
        sentence 2 - true pipeline parallelism.
        """
        sentence_q: asyncio.Queue[str | None] = asyncio.Queue()
        llm_start = time.monotonic()
        first_token_tracked = False

        async def llm_producer():
            nonlocal first_token_tracked
            buf = ""
            try:
                async for token in self.llm.stream(transcript):
                    if not first_token_tracked:
                        self._metrics["ttft"] = int(
                            (time.monotonic() - llm_start) * 1000
                        )
                        first_token_tracked = True

                    # Echo agent tokens to frontend for live display
                    await self._send({"type": "agent_token", "token": token})

                    buf += token
                    sentence, buf = _split_sentence(buf)
                    if sentence:
                        await sentence_q.put(sentence)

                if buf.strip():
                    await sentence_q.put(buf.strip())
            finally:
                await sentence_q.put(None)  # sentinel

        async def tts_consumer():
            first_audio = True
            tts_start = time.monotonic()
            while True:
                sentence = await sentence_q.get()
                if sentence is None:
                    break
                async for chunk in self.tts.stream(sentence):
                    if first_audio:
                        self._metrics["ttfa"] = int(
                            (time.monotonic() - tts_start) * 1000
                        )
                        first_audio = False
                        await self._send({"type": "metrics", **self._metrics})
                    await self.ws.send_bytes(chunk)

        self.sm.force(AgentState.SPEAKING)
        await self._send_state()

        try:
            await asyncio.gather(llm_producer(), tts_consumer())
        except asyncio.CancelledError:
            logger.info("Pipeline cancelled (interrupted)")
            raise
        except Exception as e:
            logger.error(f"Pipeline error: {e}")
        finally:
            if self.sm.state == AgentState.SPEAKING:
                # Flush STT results that accumulated while we were speaking
                self.stt.flush_queues()
                self._transcript_buf = ""
                # Cool-down: speaker echo can linger ~400ms after audio stops.
                # Block quick-endpoint from firing on the echo tail.
                self._echo_cooldown_until = time.monotonic() + 0.40
                self.sm.force(AgentState.LISTENING)
                await self._send_state()
                await self._send({"type": "agent_done"})

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    async def _cancel_pipeline(self):
        if self._pipeline_task and not self._pipeline_task.done():
            self._pipeline_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._pipeline_task), timeout=0.3
                )
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self._pipeline_task = None

    def _cancel_interrupt_timer(self):
        if self._interrupt_timer and not self._interrupt_timer.done():
            self._interrupt_timer.cancel()
        self._interrupt_timer = None

    def _cancel_endpoint_timer(self):
        if self._endpoint_timer and not self._endpoint_timer.done():
            self._endpoint_timer.cancel()
        self._endpoint_timer = None

    async def _send_state(self):
        await self._send({"type": "state", "state": self.sm.state.value})

    async def _send(self, data: dict):
        try:
            await self.ws.send_text(json.dumps(data))
        except Exception as e:
            logger.debug(f"Send failed (client likely disconnected): {e}")


# ---------------------------------------------------------------------------
# Sentence boundary splitter (module-level, pure function)
# ---------------------------------------------------------------------------

def _split_sentence(text: str) -> tuple[str | None, str]:
    """
    Extract the first complete sentence from `text`.
    Returns (sentence, remaining_text) or (None, text) if no boundary found.
    """
    for i, ch in enumerate(text):
        if ch in ".!?":
            after = i + 1
            # Sentence boundary = punctuation followed by space/end (not "3.14" or "Mr.")
            if after >= len(text) or text[after] in " \n\t":
                candidate = text[: after].strip()
                if len(candidate) > 4:  # skip tiny fragments like "Ok."
                    return candidate, text[after:].lstrip()
        # Long phrase comma split - avoids stalling TTS on run-on sentences
        if ch == "," and i >= 25:
            candidate = text[:i].strip()
            if candidate:
                return candidate, text[i + 1 :].lstrip()
    return None, text
