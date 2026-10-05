import concurrent.futures
import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import os
if os.environ.get("DOUYIN2TEXT_RUN_DEPLOYMENT_TESTS") != "1":
    raise unittest.SkipTest("Requires a prepared deployment; set DOUYIN2TEXT_RUN_DEPLOYMENT_TESTS=1")

from fastapi.testclient import TestClient
from douyin2text import api
from douyin2text.artifacts import DATA, safe_path
from douyin2text.safe_network import validate_url, PublicBackend

class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = patch.object(api, 'DB', Path(cls.tmp.name) / 'jobs.sqlite3')
        cls.db.start()
        cls.lock = patch.object(api, 'WORKER_LOCK', Path(cls.tmp.name) / 'worker.lock')
        cls.lock.start()
        cls.migration = patch.object(api, 'import_legacy_jobs', lambda: None)
        cls.migration.start()
        cls.config = patch.dict(api.CONFIG, {'allow_source_language': True})
        cls.config.start()
        m = json.loads((DATA / 'metadata/7691977131957472558.manifest.json').read_text())
        async def cached_process(job, stage):
            stage('downloading')
            return m
        cls.pipeline = patch.object(api, 'process', cached_process)
        cls.pipeline.start()
        cls.client = TestClient(api.app)
        cls.client.__enter__()
        cls.headers = {'Authorization': 'Bearer ' + api.TOKEN, 'Idempotency-Key': 'test-fixture'}
        cls.body = {'url': 'https://www.douyin.com/video/7691977131957472558', 'options': {'language': 'zh', 'max_height': 1080}}
        cls.jid = cls.client.post('/v1/jobs', json=cls.body, headers=cls.headers).json()['job_id']
        for _ in range(100):
            if cls.client.get('/v1/jobs/' + cls.jid, headers=cls.headers).json()['status'] == 'succeeded':
                break
            time.sleep(.1)
        cls.manifest = cls.client.get('/v1/jobs/' + cls.jid + '/manifest', headers=cls.headers).json()
        cls.mid = cls.manifest['media']['video']['path'].split('/')[3]

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.pipeline.stop()
        cls.config.stop()
        cls.db.stop()
        cls.lock.stop()
        cls.migration.stop()
        cls.tmp.cleanup()

    def test_authentication_and_versioned_errors(self):
        for path in ('/health', '/v1/jobs/' + self.jid, self.manifest['media']['video']['path']):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.json()['api_version'], '1')
            self.assertNotIn(api.TOKEN, r.text)

    def test_health_reports_readiness_and_503(self):
        self.assertEqual(self.client.get('/health', headers=self.headers).json()['status'], 'ready')
        async def unavailable():
            return False
        with patch.object(api, 'asr_ready', unavailable):
            response = self.client.get('/health', headers=self.headers)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()['api_version'], '1')

    def test_private_dns_destinations_rejected(self):
        async def run():
            for ip in ('127.0.0.1', '192.168.31.78', '10.0.0.1', '169.254.169.254'):
                loop = asyncio.get_running_loop()
                async def resolve(*args, **kwargs):
                    return [(2, 1, 6, '', (ip, 443))]
                with patch.object(loop, 'getaddrinfo', resolve):
                    with self.assertRaisesRegex(ValueError, 'Non-public'):
                        await PublicBackend().connect_tcp('www.douyin.com', 443)
        asyncio.run(run())

    def test_atomic_concurrent_idempotency_and_conflict(self):
        h = {**self.headers, 'Idempotency-Key': 'parallel-key'}
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(lambda _: self.client.post('/v1/jobs', json=self.body, headers=h), range(20)))
        self.assertEqual({r.json()['job_id'] for r in results}, {self.jid})
        other = {**self.body, 'url': 'https://www.douyin.com/video/7691977131957472559'}
        self.assertEqual(self.client.post('/v1/jobs', json=other, headers=h).status_code, 409)
        self.assertEqual(self.client.post('/v1/jobs', json=self.body, headers={'Authorization': self.headers['Authorization']}).status_code, 400)

    def test_manifest_and_markdown_integrity(self):
        r = self.client.get('/v1/jobs/' + self.jid + '/markdown', headers=self.headers)
        self.assertEqual(hashlib.sha256(r.content).hexdigest(), self.manifest['markdown']['sha256'])
        self.assertEqual(self.manifest['transcription']['language'], 'en')
        self.assertNotIn('/Users/', json.dumps(self.manifest))
        self.assertTrue(r.headers['content-type'].startswith('text/markdown'))

    def test_real_media_range_boundaries_suffix_and_head(self):
        for kind in ('video', 'audio', 'cover'):
            record = self.manifest['media'][kind]
            path = record['path']
            for interval in ('0-15', '100-115', '-16', f'{record["bytes"] - 16}-'):
                r = self.client.get(path, headers={**self.headers, 'Range': 'bytes=' + interval})
                self.assertEqual(r.status_code, 206)
                self.assertEqual(len(r.content), 16)
                self.assertEqual(r.headers['etag'], '"' + record['sha256'] + '"')
            self.assertEqual(self.client.get(path, headers={**self.headers, 'Range': 'bytes=999999999999-'}).status_code, 416)
            self.assertEqual(self.client.head(path, headers=self.headers).headers['content-length'], str(record['bytes']))

    def test_opaque_playback_and_expiry(self):
        r = self.client.post('/v1/media/' + self.mid + '/playback', json={'kind': 'video'}, headers=self.headers)
        self.assertEqual(r.status_code, 200)
        playback = r.json()
        self.assertNotIn(api.TOKEN, playback['url'])
        path = '/play/' + playback['url'].rsplit('/', 1)[1]
        self.assertEqual(self.client.get(path, headers={'Range': 'bytes=0-15'}).status_code, 206)
        with api.connect() as c:
            c.execute('UPDATE playback SET expires=0')
        self.assertEqual(self.client.get(path).status_code, 401)

    def test_stopped_results_not_published(self):
        api.update(self.jid, 'waiting_confirmation')
        self.assertEqual(self.client.get('/v1/jobs/' + self.jid + '/manifest', headers=self.headers).status_code, 409)
        api.update(self.jid, 'succeeded')

    def test_validation_limits_and_no_redirects(self):
        for url in ('https://127.0.0.1/', 'http://www.douyin.com/video/123', 'https://www.douyin.com.evil.test/', 'https://u:p@www.douyin.com/', 'https://www.douyin.com:8765/'):
            self.assertEqual(self.client.post('/v1/jobs', json={**self.body, 'url': url}, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post('/v1/jobs', content=b'x'*16385, headers=self.headers).status_code, 413)
        self.assertEqual(self.client.get('/health/', headers=self.headers, follow_redirects=False).status_code, 404)
        self.assertEqual(self.client.post('/v1/jobs', json={'url': self.body['url'], 'options': {'language': 'en'}}, headers=self.headers).status_code, 400)

    def test_symlink_escape_rejected(self):
        target = DATA / 'tmp' / 'v1-test-escape'
        target.symlink_to('/etc/hosts')
        try:
            with self.assertRaises(ValueError):
                safe_path('tmp/v1-test-escape')
        finally:
            target.unlink()

    def test_restart_preserves_success_and_recovers_interrupted_queue(self):
        saved = api.get(self.jid)
        api.update(self.jid, 'transcribing')
        api.initialize()
        self.assertIn(api.get(self.jid)['status'], ('queued', 'downloading', 'succeeded'))
        # Existing immutable artifact mapping and original creation time survive recovery.
        self.assertEqual(api.get(self.jid)['artifact_id'], saved['artifact_id'])
        self.assertEqual(api.get(self.jid)['created'], saved['created'])
        api.update(self.jid, 'succeeded')

if __name__ == '__main__':
    unittest.main()
