"""
Deepgram STT wrapper — SDK v7 uses a synchronous WebSocket client.

We run it in a dedicated thread and communicate with the asyncio
pipeline via thread-safe asyncio.Queue calls.
"""
import asyncio
import queue
import threading
import time
import logging
from deepgram import DeepgramClient
from deepgram.core.events import EventType
from deepgram.listen.v1.types import ListenV1Results, ListenV1UtteranceEnd

logger = logging.getLogger(__name__)

# 20ms of silence at 16kHz 16-bit mono — streamed when no speech audio is queued.
# Without this, Deepgram never detects end-of-utterance because it needs silence
# frames to trigger UtteranceEnd, not just the absence of frames.
_SILENCE_FRAME = bytes(640)


class DeepgramSTT:
    def __init__(self, api_key: str):
        self._api_key = api_key
        self._loop: asyncio.AbstractEventLoop | None = None
        self._conn = None                    # V1SocketClient, set in thread
        self._audio_q: queue.Queue = queue.Queue()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

        # Queues drained by the pipeline in the asyncio loop
        self.partial_q: asyncio.Queue[str] = asyncio.Queue()
        self.final_q: asyncio.Queue[str] = asyncio.Queue()
        self.utterance_end_q: asyncio.Queue[bool] = asyncio.Queue()

    # ── Public API (called from asyncio) ─────────────────────────────────────

    async def connect(self):
        self._loop = asyncio.get_event_loop()
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()
        logger.info("Deepgram STT thread started")

    async def send(self, audio: bytes):
        """Queue raw PCM bytes to be forwarded to Deepgram."""
        self._audio_q.put_nowait(audio)

    def flush_queues(self):
        """Discard any buffered audio and pending transcript results (call on interruption)."""
        while not self._audio_q.empty():
            try: self._audio_q.get_nowait()
            except: break
        while not self.partial_q.empty():
            try: self.partial_q.get_nowait()
            except: break
        while not self.final_q.empty():
            try: self.final_q.get_nowait()
            except: break
        while not self.utterance_end_q.empty():
            try: self.utterance_end_q.get_nowait()
            except: break

    async def disconnect(self):
        self._stop_evt.set()
        # Unblock the audio-send loop with a sentinel
        self._audio_q.put_nowait(None)
        if self._conn:
            try:
                self._conn.send_close_stream()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)
        logger.info("Deepgram STT disconnected")

    # ── Thread body (runs outside asyncio event loop) ────────────────────────

    def _thread_main(self):
        try:
            client = DeepgramClient(api_key=self._api_key)
            with client.listen.v1.connect(
                model="nova-2",         # nova-2 has lower streaming partial latency than nova-3
                language="en",
                encoding="linear16",
                sample_rate=16000,
                interim_results=True,
                utterance_end_ms=1000,  # minimum valid value is 1000ms
                endpointing=300,        # Deepgram sends finals faster after 300ms silence
                vad_events=True,
                smart_format=True,
                punctuate=True,
            ) as conn:
                self._conn = conn
                conn.on(EventType.MESSAGE, self._on_message)

                # start_listening blocks until the connection closes
                listener = threading.Thread(
                    target=conn.start_listening, daemon=True
                )
                listener.start()

                # Stream audio (or silence) to Deepgram continuously.
                # Silence frames are required for UtteranceEnd detection —
                # Deepgram won't fire the event if frames simply stop arriving.
                while not self._stop_evt.is_set():
                    try:
                        chunk = self._audio_q.get_nowait()
                        if chunk is None:
                            break
                        conn.send_media(chunk)
                    except queue.Empty:
                        conn.send_media(_SILENCE_FRAME)
                        time.sleep(0.02)

                conn.send_close_stream()
                listener.join(timeout=2)

        except Exception as e:
            logger.error(f"Deepgram thread error: {e}", exc_info=True)

    def _on_message(self, msg):
        """Called from the Deepgram listener thread — must be thread-safe."""
        if not self._loop:
            return
        try:
            if isinstance(msg, ListenV1Results):
                alts = msg.channel.alternatives if msg.channel else []
                text = alts[0].transcript.strip() if alts else ""
                if not text:
                    return
                if msg.is_final:
                    self._loop.call_soon_threadsafe(self.final_q.put_nowait, text)
                else:
                    # Flush stale partials before pushing new one
                    while not self.partial_q.empty():
                        try:
                            self.partial_q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    self._loop.call_soon_threadsafe(
                        self.partial_q.put_nowait, text
                    )
            elif isinstance(msg, ListenV1UtteranceEnd):
                self._loop.call_soon_threadsafe(
                    self.utterance_end_q.put_nowait, True
                )
        except Exception as e:
            logger.error(f"STT message handler error: {e}")
