"""whisper.cpp HTTP adapter. Pipeline code consumes normalized transcripts."""
import asyncio
from urllib.parse import urlsplit

import httpx
import psutil

from .contracts import Transcript


class WhisperCpp:
    name = "whisper.cpp"
    parameters = {"language": "auto", "temperature": 0.0}

    def __init__(self, options=None):
        options = options or {}
        self.base_url = options.get("base_url", "http://127.0.0.1:8767").rstrip("/")
        url = urlsplit(self.base_url)
        if url.scheme not in ("http", "https") or url.hostname not in ("127.0.0.1", "localhost", "::1") or url.username or url.password or url.query or url.fragment or url.path:
            raise ValueError("whisper.cpp must use a loopback service URL")
        self.transport = options.get("transport")  # dependency injection for isolated tests

    def client(self, timeout):
        return httpx.AsyncClient(timeout=timeout, trust_env=False, transport=self.transport, follow_redirects=False)

    async def ready(self):
        try:
            async with self.client(2) as client:
                return (await client.get(self.base_url + "/health")).status_code == 200
        except httpx.HTTPError:
            return False

    async def transcribe(self, audio):
        for _ in range(300):
            if await self.ready():
                break
            await asyncio.sleep(2)
        else:
            raise RuntimeError("Local ASR not ready after 10 minutes")
        peak = 0
        stop = asyncio.Event()

        async def monitor():
            nonlocal peak
            while not stop.is_set():
                for process in psutil.process_iter(["name", "memory_info"]):
                    try:
                        if process.info["name"] == "whisper-server":
                            peak = max(peak, process.info["memory_info"].rss)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                try:
                    await asyncio.wait_for(stop.wait(), timeout=.5)
                except asyncio.TimeoutError:
                    pass

        watcher = asyncio.create_task(monitor())
        try:
            async with self.client(7200) as client:
                with audio.open("rb") as file:
                    response = await client.post(self.base_url + "/inference",
                        files={"file": (audio.name, file, "audio/wav")},
                        data={"response_format": "verbose_json", "language": "auto", "temperature": "0.0"})
                response.raise_for_status()
                return self.normalize(response.json(), peak)
        finally:
            stop.set()
            await watcher

    @staticmethod
    def normalize(raw, peak=0):
        language = {"english": "en", "chinese": "zh"}.get(raw.get("language"), raw.get("language"))
        if not isinstance(raw.get("text"), str) or not raw["text"].strip() or not isinstance(language, str) or not language:
            raise ValueError("ASR returned no transcription or language")
        segments = raw.get("segments")
        if not isinstance(segments, list) or not segments:
            raise ValueError("ASR returned no segments")
        return Transcript(raw["text"], language, tuple(segments), raw, peak)
