import asyncio
import base64
import json
import logging
import httpx
from typing import AsyncGenerator

logger = logging.getLogger(__name__)

CARTESIA_SSE_URL = "https://api.cartesia.ai/tts/sse"
CARTESIA_VERSION = "2024-06-10"


class CartesiaTTS:
    """
    Streams audio from Cartesia Sonic-2 via Server-Sent Events.

    Uses httpx async streaming so each sentence starts generating
    immediately as LLM produces it - no waiting for the full response.
    """

    def __init__(self, api_key: str, voice_id: str):
        self._api_key = api_key
        self._voice_id = voice_id

    async def stream(self, text: str) -> AsyncGenerator[bytes, None]:
        if not text.strip():
            return

        headers = {
            "X-API-Key": self._api_key,
            "Cartesia-Version": CARTESIA_VERSION,
            "Content-Type": "application/json",
        }
        body = {
            "model_id": "sonic-2",
            "transcript": text,
            "voice": {"mode": "id", "id": self._voice_id},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": 16000,
            },
            "stream": True,
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0)
            ) as client:
                async with client.stream(
                    "POST", CARTESIA_SSE_URL, headers=headers, json=body
                ) as response:
                    if response.status_code != 200:
                        body_bytes = await response.aread()
                        raise RuntimeError(
                            f"Cartesia HTTP {response.status_code}: {body_bytes.decode()}"
                        )

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:].strip()
                        if not raw:
                            continue
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        # Terminal signal
                        if payload.get("done") or payload.get("type") == "done":
                            break

                        # Audio chunk - field is "data" containing base64 PCM
                        audio_b64 = payload.get("data")
                        if audio_b64:
                            yield base64.b64decode(audio_b64)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"TTS stream error: {e}")
            raise
