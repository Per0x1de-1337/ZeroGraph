import asyncio
import logging
import os
from typing import AsyncGenerator
from openai import AsyncOpenAI
import httpx

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a fast voice assistant in a real-time spoken conversation. "
    "ALWAYS reply in exactly 1 sentence — never more. "
    "No markdown, no bullet points, no preamble. "
    "Be direct: answer first, explain only if critical."
)

# LLM_PROVIDER: "openai" | "groq" | "local"
# Groq is OpenAI-compatible and delivers ~50ms TTFT even from India.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()
OLLAMA_MODEL  = os.getenv("OLLAMA_MODEL", "orca-mini")
OLLAMA_URL    = os.getenv("OLLAMA_URL", "http://localhost:11434")
GROQ_API_KEY  = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL    = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")


class LLM:
    def __init__(self, api_key: str):
        self._openai = AsyncOpenAI(api_key=api_key)
        # Groq uses the same OpenAI client — just different base_url + key
        self._groq = AsyncOpenAI(
            api_key=GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1",
        )
        self._history: list[dict] = []
        labels = {"openai": "OpenAI/gpt-4o-mini", "groq": f"Groq/{GROQ_MODEL}", "local": f"Ollama/{OLLAMA_MODEL}"}
        logger.info(f"LLM provider: {labels.get(LLM_PROVIDER, LLM_PROVIDER)}")

    def add_user(self, text: str):
        self._history.append({"role": "user", "content": text})

    def add_assistant(self, text: str):
        self._history.append({"role": "assistant", "content": text})

    def pop_last_user(self):
        if self._history and self._history[-1]["role"] == "user":
            self._history.pop()

    async def stream(self, transcript: str) -> AsyncGenerator[str, None]:
        self.add_user(transcript)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + self._history[-10:]

        if LLM_PROVIDER == "groq":
            async for token in self._stream_groq(messages):
                yield token
        elif LLM_PROVIDER == "local":
            async for token in self._stream_ollama(messages):
                yield token
        else:
            async for token in self._stream_openai(messages):
                yield token

    # ── Groq (OpenAI-compatible, ~50ms TTFT) ─────────────────────────────────

    async def _stream_groq(self, messages: list) -> AsyncGenerator[str, None]:
        full = ""
        try:
            stream = await self._groq.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                stream=True,
                max_tokens=120,
                temperature=0.7,
            )
            async for chunk in stream:
                token = chunk.choices[0].delta.content or ""
                if token:
                    full += token
                    yield token
            self.add_assistant(full)
        except asyncio.CancelledError:
            if full:
                self.add_assistant(full + "…")
            raise
        except Exception as e:
            logger.error(f"Groq error: {e}")
            self.pop_last_user()
            raise

    # ── OpenAI ───────────────────────────────────────────────────────────────

    async def _stream_openai(self, messages: list) -> AsyncGenerator[str, None]:
        full = ""
        try:
            stream = await self._openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                stream=True,
                max_tokens=300,
                temperature=0.7,
            )
            async for chunk in stream:
                token = chunk.choices[0].delta.content or ""
                if token:
                    full += token
                    yield token
            self.add_assistant(full)
        except asyncio.CancelledError:
            if full:
                self.add_assistant(full + "…")
            raise
        except Exception as e:
            logger.error(f"OpenAI error: {e}")
            self.pop_last_user()
            raise

    # ── Ollama (local) ───────────────────────────────────────────────────────

    async def _stream_ollama(self, messages: list) -> AsyncGenerator[str, None]:
        """Stream from local Ollama — zero network latency on LLM step."""
        full = ""
        url = f"{OLLAMA_URL}/api/chat"
        payload = {
            "model": OLLAMA_MODEL,
            "messages": messages,
            "stream": True,
            "options": {
                "num_predict": 200,
                "temperature": 0.7,
                "num_thread": 8,   # leave cores free for audio processing
            },
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                async with client.stream("POST", url, json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        import json
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        token = d.get("message", {}).get("content", "")
                        if token:
                            full += token
                            yield token
                        if d.get("done"):
                            break
            self.add_assistant(full)
        except asyncio.CancelledError:
            if full:
                self.add_assistant(full + "…")
            raise
        except Exception as e:
            logger.error(f"Ollama error: {e}")
            self.pop_last_user()
            raise
