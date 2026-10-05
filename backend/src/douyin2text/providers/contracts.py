from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol
import math


class LoginRequired(RuntimeError):
    pass


class ConfirmationRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceMedia:
    media_id: str
    title: str
    description: str
    author: str
    published_at: str
    duration_seconds: float
    canonical_url: str
    video_urls: tuple[str, ...]
    cover_urls: tuple[str, ...]
    selection: Mapping[str, Any]
    expected_bytes: int
    raw_metadata: Mapping[str, Any]


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str
    segments: tuple[Mapping[str, Any], ...]
    raw_response: Mapping[str, Any] = field(default_factory=dict)
    peak_rss_bytes: int = 0

    def __post_init__(self):
        if not isinstance(self.text, str) or not self.text.strip() or not isinstance(self.language, str) or not self.language or not self.segments:
            raise ValueError("Transcript requires text, language and segments")
        for segment in self.segments:
            start, end = segment.get("start"), segment.get("end")
            if (not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                    or not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start
                    or not isinstance(segment.get("text"), str)):
                raise ValueError("Transcript has an invalid segment")

    def as_dict(self):
        return {"text": self.text, "language": self.language,
                "segments": list(self.segments), "raw_response": dict(self.raw_response)}


class SourceProvider(Protocol):
    name: str

    def headers(self) -> Mapping[str, str]: ...
    async def resolve_id(self, url: str) -> str: ...
    async def describe(self, media_id: str, max_short_edge: int = 1080) -> SourceMedia: ...


class TranscriptionProvider(Protocol):
    name: str
    parameters: Mapping[str, Any]

    async def ready(self) -> bool: ...
    async def transcribe(self, audio: Path) -> Transcript: ...
