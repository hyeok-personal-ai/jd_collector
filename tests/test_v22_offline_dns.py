"""Only local mocks and fixtures; this suite does not contact public hosts."""
import asyncio
import httpx
import pytest

from app.fetcher import DNSResolutionError, PoliteFetcher
from app.network_diagnostics import diagnose
from app.offline_replay import FixtureServer, run_suite
from app.verification import error_category


def test_network_diagnostic_no_external_queries_without_live(monkeypatch):
    import app.network_diagnostics as nd
    hosts = []
    def resolve(host):
        hosts.append(host)
        assert host == 'localhost'
        return {'host': host, 'ok': True}
    monkeypatch.setattr(nd, 'resolve_host', resolve)
    report = diagnose(live=False)
    assert report['diagnosis'] == 'NO_EXTERNAL_LOOKUP_PERFORMED'
    assert hosts == ['localhost']


def test_dns_failure_diagnostics_separate_from_parser(monkeypatch):
    import app.network_diagnostics as nd
    def resolve(host):
        if host == 'localhost':
            return {'host': host, 'ok': True}
        return {'host': host, 'ok': False, 'error_type': 'gaierror'}
    monkeypatch.setattr(nd, 'resolve_host', resolve)
    report = nd.diagnose(live=True, hosts=('one.example','two.example'))
    assert report['diagnosis'] == 'DNS_UNAVAILABLE'
    assert report['loopback']['ok']
    assert error_category(['DNSResolutionError: failed DNS']) == 'dns_failure'


def test_fetcher_exposes_dns_error_instead_of_robots_denial():
    def fail(request):
        raise httpx.ConnectError('Temporary failure in name resolution', request=request)
    async def go():
        async with PoliteFetcher(frozenset({'example.com'}), delay=0,
                                 transport=httpx.MockTransport(fail)) as fetcher:
            with pytest.raises(DNSResolutionError):
                await fetcher.get_text('https://example.com/jobs')
    asyncio.run(go())


def test_robots_denied_does_not_attempt_job_path():
    touched=[]
    def serve(req):
        touched.append(req.url.path)
        return httpx.Response(200, text='User-agent: *\nDisallow: /\n')
    async def go():
        async with PoliteFetcher(frozenset({'example.com'}), delay=0,
                                 transport=httpx.MockTransport(serve)) as fetcher:
            with pytest.raises(PermissionError):
                await fetcher.get_text('https://example.com/jobs')
    asyncio.run(go())
    assert touched == ['/robots.txt']


def test_fixture_server_blocks_unknown_host():
    server=FixtureServer('greenhouse')
    req=httpx.Request('GET','https://not-listed.example.com/robots.txt')
    with pytest.raises(AssertionError):
        server.serve(req)


def test_full_public_replay_and_domestic_offline_parsers():
    result=run_suite()
    assert result['result'] == 'PASS'
    assert result['external_sites_verified'] == 0
    assert result['unique_jobs_in_temp_db'] == 4
    assert {x['provider'] for x in result['public_ats']} == {
        'greenhouse','lever','ashby','smartrecruiters'}
    assert len(result['domestic_html']) == 2
    gh = next(x for x in result['public_ats'] if x['provider']=='greenhouse')
    assert gh['changed_posting_updated'] == 1
    assert gh['preserved_on_failure'] is True
