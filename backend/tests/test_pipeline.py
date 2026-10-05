"""Exercise replacement providers through pipeline, snapshots and gateway in temporary storage."""
import atexit
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Import the service against a private fixture, never the user's live data/config/token.
BOOTSTRAP = tempfile.TemporaryDirectory()
atexit.register(BOOTSTRAP.cleanup)
ROOT = Path(BOOTSTRAP.name)
(ROOT / "state").mkdir()
BASE_CONFIG = {
    "bind": "127.0.0.1", "base_url": "http://127.0.0.1:8765",
    "asr_fingerprint": "a" * 64, "output_fingerprint": "b" * 64,
    "model": {"name": "fixture", "path": "models/fixture.bin", "source": "fixture", "sha256": "c" * 64},
    "versions": {}, "providers": {"source": {"name": "fixture-source"}, "asr": {"name": "fixture-asr"}},
}
(ROOT / "state/config.json").write_text(json.dumps(BASE_CONFIG))
(ROOT / "state/api-token").write_text("test-token")
with patch.dict(os.environ, {"DOUYIN2TEXT_DATA_DIR": str(ROOT), "DOUYIN2TEXT_CONFIG": str(ROOT / "state/config.json")}):
    from douyin2text import pipeline, artifacts, markdown, api

from fastapi.testclient import TestClient
from douyin2text.providers.contracts import SourceMedia, Transcript
from douyin2text.providers.registry import SOURCE_FACTORIES, ASR_FACTORIES, effective_config


class AlternativeSource:
    name = "alternative-source"

    def headers(self):
        return {}

    async def resolve_id(self, url):
        return "1234567890123"

    async def describe(self, media_id, max_short_edge=1080):
        return SourceMedia(media_id, "测试标题", "描述", "作者", "2026-10-05T00:00:00Z", 1,
            f"https://www.douyin.com/video/{media_id}", ("video",), ("cover",), {}, 8, {"fixture": True})


class AlternativeASR:
    name = "alternative-asr"
    parameters = {"fixture": True}
    calls = 0

    async def ready(self):
        return True

    async def transcribe(self, audio):
        type(self).calls += 1
        if not audio.is_file():
            raise AssertionError("Audio working copy was not generated")
        return Transcript("测试文字", "zh", ({"start": 0, "end": 1, "text": "测试文字"},))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        for name in ("state", "models", "video", "audio", "metadata", "transcripts", "tmp"):
            (self.data / name).mkdir()
        (self.data / "models/fixture.bin").write_bytes(b"model")
        self.config = effective_config(BASE_CONFIG)
        for module in (pipeline, artifacts, markdown, api):
            self.start_patch(patch.object(module, "DATA", self.data))
        for module in (pipeline, api):
            self.start_patch(patch.object(module, "CONFIG", self.config))
        self.start_patch(patch.object(api, "DB", self.data / "state/jobs.sqlite3"))
        self.start_patch(patch.object(api, "WORKER_LOCK", self.data / "state/worker.lock"))
        self.start_patch(patch.dict(SOURCE_FACTORIES, {"fixture-source": lambda options: AlternativeSource()}))
        self.start_patch(patch.dict(ASR_FACTORIES, {"fixture-asr": lambda options: AlternativeASR()}))
        AlternativeASR.calls = 0

        async def download(urls, path, headers):
            path.write_bytes(b"fixture-media")

        def probe(path):
            return {"format": {"duration": "1"}, "streams": [
                {"codec_type": "video", "width": 720, "height": 720, "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac"}]}

        def media_command(args):
            if args[-1] != "-":
                Path(args[-1]).write_bytes(b"fixture-audio")

        self.start_patch(patch.object(pipeline, "fetch_candidates", download))
        self.start_patch(patch.object(pipeline, "probe", probe))
        self.start_patch(patch.object(pipeline, "run", media_command))

    def start_patch(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def process(self):
        return asyncio.run(pipeline.process({"url": "https://www.douyin.com/video/1234567890123", "id": "job"}, lambda *args, **kwargs: None))

    def test_alternative_providers_produce_and_reuse_valid_snapshot(self):
        manifest = self.process()
        self.assertEqual(manifest["source_provider"], "alternative-source")
        self.assertEqual(manifest["asr"]["engine"], "alternative-asr")
        self.assertTrue(artifacts.valid_files(manifest))
        media_id, snapshot = artifacts.snapshot(manifest)
        public = artifacts.public_manifest(snapshot, "job")
        self.assertEqual(public["transcription"]["engine"], "alternative-asr")
        self.assertIn(media_id, public["media"]["video"]["path"])
        text = artifacts.safe_path(snapshot["files"]["markdown"]["path"]).read_text()
        self.assertIn("alternative-asr", text)
        self.assertNotIn("Apple Metal", text)
        self.process()
        self.assertEqual(AlternativeASR.calls, 1)

    def test_changed_provider_config_does_not_reuse_old_asr(self):
        first = self.process()
        changed = {**BASE_CONFIG, "providers": {**BASE_CONFIG["providers"], "asr": {"name": "fixture-asr", "options": {"revision": 2}}}}
        self.config.update(effective_config(changed))
        second = self.process()
        self.assertNotEqual(first["asr"]["fingerprint"], second["asr"]["fingerprint"])
        self.assertEqual(AlternativeASR.calls, 2)

    def test_gateway_exposes_replacement_engine_and_authentication(self):
        manifest = self.process()
        async def cached(job, stage):
            return manifest
        with patch.object(api, "process", cached), TestClient(api.app) as client:
            self.assertEqual(client.get("/health").status_code, 401)
            headers = {"Authorization": "Bearer test-token", "Idempotency-Key": "fixture"}
            self.assertEqual(client.get("/health", headers=headers).status_code, 200)
            body = {"url": "https://www.douyin.com/video/1234567890123"}
            job = client.post("/v1/jobs", json=body, headers=headers).json()["job_id"]
            import time
            for _ in range(100):
                if client.get(f"/v1/jobs/{job}", headers=headers).json()["status"] == "succeeded":
                    break
                time.sleep(.01)
            result = client.get(f"/v1/jobs/{job}/manifest", headers=headers)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["transcription"]["engine"], "alternative-asr")
            self.assertNotIn(str(self.data), result.text)
            self.assertEqual(client.post("/v1/jobs", json=body, headers=headers).json()["job_id"], job)


if __name__ == "__main__":
    unittest.main()
