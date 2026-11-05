import asyncio
import multiprocessing
import time
from types import SimpleNamespace

import httpx
import pytest

from app import dingtalk_rate, http_client
from app.config import Settings
from app.integrations import IntegrationError


def _process_requests(directory, output):
    settings=SimpleNamespace(dingtalk_rate_dir=directory,dingtalk_app_key='synthetic',dingtalk_qps=10)
    async def requests():
        for _ in range(3):
            await dingtalk_rate.acquire(settings)
            output.put(time.monotonic())
    asyncio.run(requests())


def test_distinct_processes_share_one_paced_sequence(tmp_path):
    context=multiprocessing.get_context('spawn');queue=context.Queue()
    processes=[context.Process(target=_process_requests,args=(str(tmp_path),queue)) for _ in range(2)]
    for process in processes:process.start()
    times=sorted(queue.get(timeout=15) for _ in range(6))
    for process in processes:process.join(10);assert process.exitcode==0
    assert all(b-a>=0.085 for a,b in zip(times,times[1:]))
    assert times[-1]-times[0]>=0.48


def test_cancelled_waiter_never_reserves_future_slots(tmp_path):
    settings=SimpleNamespace(dingtalk_rate_dir=str(tmp_path),dingtalk_app_key='synthetic',dingtalk_qps=10)
    async def exercise():
        await dingtalk_rate.acquire(settings)
        task=asyncio.create_task(dingtalk_rate.acquire(settings));await asyncio.sleep(0.01);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        await asyncio.sleep(0.11)
        start=time.monotonic();await dingtalk_rate.acquire(settings)
        assert time.monotonic()-start<0.08
    asyncio.run(exercise())


def test_unavailable_shared_directory_fails_before_network_without_exposing_path(tmp_path):
    path=tmp_path/'private-state';path.write_text('not a directory')
    settings=SimpleNamespace(dingtalk_rate_dir=str(path),dingtalk_app_key='synthetic',dingtalk_qps=5)
    with pytest.raises(IntegrationError) as caught:asyncio.run(dingtalk_rate.acquire(settings))
    assert caught.value.code=='dingtalk_rate_unavailable' and str(path) not in str(caught.value)


def test_http_hook_paces_exact_dingtalk_hosts_and_redirects_only(monkeypatch,tmp_path):
    paced=[];seen=[]
    async def pace(settings):paced.append(settings.dingtalk_qps)
    monkeypatch.setattr(dingtalk_rate,'acquire',pace)
    class Transport(httpx.MockTransport):
        def __init__(self,**kwargs):super().__init__(self.handle)
        def handle(self,request):
            seen.append(request.url.host)
            if request.url.path=='/redirect':return httpx.Response(302,headers={'Location':'https://api.dingtalk.com/final'})
            return httpx.Response(200)
    monkeypatch.setattr('httpx._client.AsyncHTTPTransport',Transport)
    s=Settings(_env_file=None,dingtalk_rate_dir=str(tmp_path))
    async def exercise():
        async with http_client.outbound_client(s,follow_redirects=True) as client:
            for url in ['https://api.dingtalk.com/start','https://oapi.dingtalk.com/start',
                        'https://api.dingtalk.com.evil.invalid/start','https://model.invalid/start',
                        'https://other.invalid/redirect']:
                await client.get(url)
    asyncio.run(exercise())
    assert paced==[5,5,5] and seen[-1]=='api.dingtalk.com'
