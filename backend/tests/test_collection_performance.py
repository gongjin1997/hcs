"""采集路径、并发边界和统计的回归测试；不访问真实 HCS。"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.collectors.base import RunContext
from app.collectors.dataarts import DataArtsRealtimeCollector
from app.hcs.auth import AkSkAuth
from app.hcs.client import HcsClient
from app.hcs.endpoints import EndpointResolver
from app.hcs.errors import HcsError
from app.services import settings_service


class JobClient:
    def __init__(self, names, delay=0.0):
        self.names, self.delay = names, delay
        self.paths = []
        self.active = self.peak = 0
        self.lock = threading.Lock()

    def get(self, service, path, **kwargs):
        if path.endswith('/jobs'):
            p = kwargs['params']
            jobs = [{'name': name} for name in self.names]
            start, size = p.get('offset', 0) * p.get('limit', 100), p.get('limit', 100)
            return {'jobs': jobs[start:start + size],
                    'total': len(jobs)}
        with self.lock:
            self.paths.append(path)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            if not path.endswith('/status'):
                return {'name': unquote(path.split('/jobs/', 1)[1]), 'processType': 'REAL_TIME', 'nodes': []}
            return {'status': 'NORMAL', 'nodes': []}
        finally:
            with self.lock:
                self.active -= 1


def test_job_names_are_encoded_as_one_path_segment():
    names = ['中文 空格#百分号%', 'folder/job?x=1']
    client = JobClient(names)
    ctx = RunContext(client=client, account='test', project_id='p')
    observations = DataArtsRealtimeCollector().collect(ctx)
    assert len(observations) == 2
    assert set(client.paths) == {f'/v1/{{project_id}}/jobs/{quote(n, safe="")}{suffix}'
                                 for n in names for suffix in ('', '/status')}


def test_three_hundred_jobs_use_bounded_parallel_requests():
    names = [f'job-{n}' for n in range(300)]
    client = JobClient(names, delay=0.002)
    ctx = RunContext(client=client, account='test', project_id='p', params={'hcs_concurrency': 4})
    observations = DataArtsRealtimeCollector().collect(ctx)
    assert [o.name for o in observations] == names
    assert 2 <= client.peak <= 4
    assert client.active == 0


def test_collection_settings_validate_and_roundtrip(mem_db):
    cfg, _ = settings_service.save(mem_db, 'collect', {'hcs_concurrency': 4, 'hcs_qps': {'dataarts': 2}}, 'test')
    assert cfg.hcs_concurrency == 4
    assert settings_service.get_collect(mem_db).hcs_qps['dataarts'] == 2
    for kwargs in [{'hcs_concurrency': 17}, {'hcs_qps': {'dataarts': 0}}, {'hcs_qps': {'unknown': 2}}]:
        with pytest.raises(ValidationError):
            settings_service.CollectionSettings(**kwargs)


def test_hcs_statistics_count_concurrent_requests_and_errors():
    def respond(request):
        return httpx.Response(503 if request.url.path == '/bad' else 200, json={'ok': True})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = HcsClient(http, AkSkAuth('test-ak', 'test-sk'),
                           EndpointResolver({'dataarts': 'mock'}, 'r', 'example.com', 'http'),
                           'p', max_retries=0, qps={'dataarts': 1000})
        client.get('dataarts', '/ok')
        with pytest.raises(HcsError):
            client.get('dataarts', '/bad')
        snapshot = client.metrics_snapshot()
        assert snapshot['calls'] == 2 and snapshot['errors'] == 1
        assert snapshot['services']['dataarts']['calls'] == 2
        assert snapshot['rate_limit_ms'] >= 0


def test_simultaneous_unauthorized_requests_refresh_only_once(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), 'hcs_service_concurrency', 4)
    gate = threading.Barrier(4)

    class Auth:
        kind = 'TEST'
        token = 'old'
        refreshes = 0

        def apply(self, request, service):
            request.headers['X-Auth-Token'] = self.token

        def refresh(self):
            self.refreshes += 1
            self.token = 'new'
            return True

    def respond(request):
        if request.headers['X-Auth-Token'] == 'old':
            gate.wait(timeout=5)
            return httpx.Response(401, json={})
        return httpx.Response(200, json={'ok': True})

    auth = Auth()
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = HcsClient(http, auth, EndpointResolver({'dataarts': 'mock'}, 'r', 'example.com', 'http'),
                           'p', qps={'dataarts': 1000}, max_concurrency=4)
        with ThreadPoolExecutor(max_workers=4) as pool:
            result = list(pool.map(lambda _: client.get('dataarts', '/test'), range(4)))
        assert all(row['ok'] for row in result)
        assert auth.refreshes == 1
        assert client.metrics_snapshot()['calls'] == 8 and client.metrics_snapshot()['errors'] == 4


def test_encoded_job_name_roundtrips_through_mock_hcs(monkeypatch):
    from app.hcs.factory import build_client
    from mock_hcs import inventory
    from mock_hcs.app import app

    from .test_pipeline import SETTINGS

    name = '中文 空格/作业#百分号%'
    monkeypatch.setattr(inventory, 'RT_JOBS', {name: 'NORMAL'})
    with TestClient(app, base_url='http://mock') as http:
        ctx = RunContext(client=build_client(SETTINGS, http=http), account='a', project_id='p')
        observations = DataArtsRealtimeCollector().collect(ctx)
    assert not ctx.errors
    assert any(o.name == name and o.status_raw == 'NORMAL' for o in observations)
