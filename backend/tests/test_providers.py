import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from douyin2text.providers.contracts import Transcript, LoginRequired, ConfirmationRequired
from douyin2text.providers.f2 import F2Source
from douyin2text.providers.whisper_cpp import WhisperCpp
from douyin2text.providers.registry import create_provider, effective_config, SOURCE_FACTORIES


class ProviderTests(unittest.TestCase):
    def test_f2_normalization_and_resolution_selection(self):
        def candidate(edge, bitrate):
            return {"bit_rate": bitrate, "play_addr": {"width": edge, "height": edge,
                    "url_list": [f"https://www.douyinvod.com/{edge}-{bitrate}"], "data_size": 42}}
        raw = {"aweme_detail": {"author": {"nickname": "author"}, "desc": "title\nbody", "create_time": 0,
                "video": {"duration": 1000, "cover": {"url_list": ["https://www.douyinpic.com/cover"]},
                "bit_rate": [candidate(720, 100), candidate(1080, 200), candidate(1080, 300), candidate(2160, 999)]}}}
        info = F2Source.normalize("1234567890123", raw)
        self.assertEqual(info.video_urls, ("https://www.douyinvod.com/1080-300",))
        self.assertEqual((info.title, info.author, info.duration_seconds), ("title", "author", 1))
        self.assertEqual(info.raw_metadata, raw)
        with self.assertRaises(LoginRequired):
            F2Source.normalize("123", {})
        raw["aweme_detail"]["video"]["duration"] = 1800001
        with self.assertRaises(ConfirmationRequired):
            F2Source.normalize("123", raw)

    def test_whisper_http_contract_and_normalization(self):
        requests = []
        raw = {"text": "Hello", "language": "english", "segments": [{"start": 0, "end": 1, "text": "Hello"}]}
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json=raw if request.url.path == "/inference" else {})
        adapter = WhisperCpp({"transport": httpx.MockTransport(respond)})
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "audio.wav"
            audio.write_bytes(b"fixture")
            result = asyncio.run(adapter.transcribe(audio))
        self.assertEqual(result.language, "en")
        self.assertEqual(result.raw_response, raw)
        self.assertEqual([request.url.path for request in requests], ["/health", "/inference"])
        self.assertIn(b'verbose_json', requests[-1].content)

    def test_invalid_transcript_and_remote_whisper_rejected(self):
        with self.assertRaises(ValueError):
            Transcript("text", "zh", ({"start": 2, "end": 1, "text": "text"},))
        with self.assertRaises(ValueError):
            Transcript("text", "zh", ({"start": 0, "end": float("nan"), "text": "text"},))
        with self.assertRaises(ValueError):
            WhisperCpp({"base_url": "http://example.com"})
        unavailable = WhisperCpp({"transport": httpx.MockTransport(lambda request: httpx.Response(503))})
        self.assertFalse(asyncio.run(unavailable.ready()))

    def test_explicit_replacement_factory_without_f2_dependency(self):
        marker = object()
        self.assertIs(create_provider({"name": "replacement", "options": {}}, {"replacement": lambda options: marker}), marker)
        # Loading an adapter class does not import the optional F2 library.
        self.assertIsInstance(create_provider({"name": "local-asr", "factory": "douyin2text.providers.whisper_cpp:WhisperCpp"}, {}), WhisperCpp)
        with self.assertRaises(ValueError):
            create_provider({"name": "missing"}, SOURCE_FACTORIES)

    def test_provider_changes_invalidate_cache_and_preserve_legacy_keys(self):
        base = {"asr_fingerprint": "asr", "output_fingerprint": "output"}
        self.assertEqual(effective_config(base), base)
        original = {**base, "providers": {"asr": {"name": "whisper_cpp"}}}
        alternative = {**base, "providers": {"asr": {"name": "other"}}}
        for key in ("asr_fingerprint", "output_fingerprint"):
            self.assertNotEqual(effective_config(original)[key], effective_config(alternative)[key])
        self.assertEqual(effective_config(original), effective_config(original))


if __name__ == "__main__":
    unittest.main()
