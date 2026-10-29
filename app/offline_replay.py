"""End-to-end, strictly offline replay of actual public ATS crawler code.

Synthetic fixtures simulate HTTP over httpx.MockTransport; no sockets, DNS or
external API keys. The normal crawler/robots/parser/cache/runner/DB path runs.
NEVER label a synthetic replay as proof that a real site works or permits reuse.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session, sessionmaker

from .crawlers.public_feeds import (GreenhousePublicCrawler, LeverPublicCrawler,
                                    AshbyPublicCrawler, SmartRecruitersPublicCrawler)
from .database import Base, Job, CrawlRun
from .fetcher import PoliteFetcher
from .runner import run_crawler
from .verification_cli import offline_snapshots

FIXTURES = Path(__file__).resolve().parent.parent / 'fixtures'
PROVIDERS = {
    'greenhouse': (GreenhousePublicCrawler, 'boards-api.greenhouse.io', 'greenhouse.json'),
    'lever': (LeverPublicCrawler, 'api.lever.co', 'lever.json'),
    'ashby': (AshbyPublicCrawler, 'api.ashbyhq.com', 'ashby.json'),
    'smartrecruiters': (SmartRecruitersPublicCrawler, 'api.smartrecruiters.com', 'smartrecruiters_list.json'),
}


@contextmanager
def llm_off():
    """Ensure replay never triggers Ollama even if user exported its model env."""
    key = 'NORMALIZATION_LLM_MODEL'
    saved = os.environ.get(key)
    os.environ[key] = ''
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


class FixtureServer:
    def __init__(self, provider: str):
        self.provider = provider
        self.hostname = PROVIDERS[provider][1]
        self.data = json.loads((FIXTURES / 'offline_public' / PROVIDERS[provider][2]).read_text())
        self.detail = json.loads((FIXTURES / 'offline_public' / 'smartrecruiters_detail.json').read_text())
        self.revision = 1
        self.bad_feed = False
        self.count = {'robots': 0, 'list_200': 0, 'list_304': 0, 'detail': 0}
        self.requests: list[dict] = []

    def serve(self, request: httpx.Request) -> httpx.Response:
        # Fail fast on any unexpected access. No fallback to real network.
        if request.method != 'GET' or request.url.host != self.hostname:
            raise AssertionError('Replay tried an unregistered host or method')
        path = request.url.path
        self.requests.append({'path': path, 'conditional': bool(request.headers.get('if-none-match'))})
        if path == '/robots.txt':
            self.count['robots'] += 1
            return httpx.Response(200, text='User-agent: *\nAllow: /\n')
        if self.provider == 'greenhouse':
            is_listing = path == '/v1/boards/fixtureco/jobs'
        elif self.provider == 'lever':
            is_listing = path == '/v0/postings/fixtureco'
        elif self.provider == 'ashby':
            is_listing = path == '/posting-api/job-board/fixtureco'
        else:
            is_listing = path == '/v1/companies/fixtureco/postings'
            if path == '/v1/companies/fixtureco/postings/S001':
                self.count['detail'] += 1
                return httpx.Response(200, json=self.detail)
        if not is_listing:
            raise AssertionError(f'Unexpected replay URL: {path}')
        if self.bad_feed:
            self.count['list_200'] += 1
            return httpx.Response(200, json={'unexpected': True}, headers={'ETag': '"bad"'})
        tag = f'"fixture-{self.revision}"'
        if request.headers.get('if-none-match') == tag:
            self.count['list_304'] += 1
            return httpx.Response(304, headers={'ETag': tag})
        self.count['list_200'] += 1
        data = json.loads(json.dumps(self.data))
        if self.provider == 'greenhouse' and self.revision == 2:
            data['jobs'][0]['title'] = 'Software Engineer Intern — Updated'
            data['jobs'][0]['updated_at'] = '2026-10-13T03:00:00Z'
        return httpx.Response(200, json=data, headers={'ETag': tag})

    def fetcher_factory(self, allowed_hosts: frozenset[str]) -> PoliteFetcher:
        assert self.hostname in allowed_hosts
        return PoliteFetcher(allowed_hosts, transport=httpx.MockTransport(self.serve), delay=0)


def _crawler(provider: str):
    clazz = PROVIDERS[provider][0]
    crawler = clazz(max_pages=1)
    crawler.boards = ['fixtureco']
    return crawler


async def public_replay(factory) -> list[dict]:
    reports = []
    for provider in PROVIDERS:
        server = FixtureServer(provider)
        options = {'session_factory': factory, 'fetcher_factory': server.fetcher_factory,
                   'init_database': False, 'recheck_hours': 24}
        first = await run_crawler(_crawler(provider), **options)
        second = await run_crawler(_crawler(provider), **options)
        with factory() as session:
            jobs = session.scalars(select(Job).where(Job.source_id == f'public_{provider}')).all()
        assert first.status == 'completed' and first.created_count == 1, (provider,first)
        assert len(jobs) == 1 and jobs[0].original_url.startswith('https://'), provider
        assert second.status == 'completed' and second.created_count == 0, (provider,second)
        assert server.count['list_304'] == 1, (provider,server.count)
        assert all(s in server.count for s in ('robots','list_200','list_304')), provider
        result = {'provider': provider, 'created': first.created_count,
                  'second_run_created': second.created_count,
                  'http_304_count': server.count['list_304'],
                  'saved_jobs': len(jobs)}
        if provider == 'greenhouse':
            server.revision = 2
            third = await run_crawler(_crawler(provider), **options)
            assert third.updated_count == 1 and third.status == 'completed', third
            with factory() as session:
                same = session.scalars(select(Job).where(Job.source_id == 'public_greenhouse')).one()
                assert same.title.endswith('Updated')
            # Malformed list fails only the source, but must NOT mark the prior job closed.
            server.bad_feed = True
            fourth = await run_crawler(_crawler(provider), **options)
            assert fourth.status == 'partial' and fourth.failed_count >= 1, fourth
            with factory() as session:
                saved = session.scalars(select(Job).where(Job.source_id == 'public_greenhouse')).one()
                assert saved.source_status == 'open' and saved.title.endswith('Updated')
            result['changed_posting_updated'] = third.updated_count
            result['malformed_response_status'] = fourth.status
            result['preserved_on_failure'] = True
        if provider == 'smartrecruiters':
            assert server.count['detail'] == 1, server.count
            result['detail_requests'] = server.count['detail']
        reports.append(result)
    return reports


def run_suite() -> dict:
    with TemporaryDirectory(prefix='recruit-radar-offline-') as tmp:
        engine = create_engine('sqlite:///' + str(Path(tmp) / 'replay.db'))
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            with llm_off():
                public = asyncio.run(public_replay(sessions))
            domestic = offline_snapshots(FIXTURES / 'v21_snapshots.json', sample_limit=5)
            assert len(domestic) == 2
            assert all(r['status'] == 'sample_ok' for r in domestic)
            with sessions() as session:
                total = session.scalar(select(func.count()).select_from(Job))
                assert total == 4
                runs = session.scalar(select(func.count()).select_from(CrawlRun))
            return {'mode': 'offline_synthetic_replay', 'network_access': 'none_mock_transport',
                    'external_sites_verified': 0, 'public_ats': public,
                    'domestic_html': [{'family': r['family'], 'status': r['status'],
                                       'sample_parsed': r['sample_parsed'],
                                       'reference_exact_match_rate': r['reference_exact_match_rate']}
                                      for r in domestic],
                    'unique_jobs_in_temp_db': total, 'crawl_runs': runs, 'result': 'PASS'}
        finally:
            engine.dispose()


def main():
    from .console import configure_console
    configure_console()
    parser = argparse.ArgumentParser(description='Offline synthetic replay, never opens sockets')
    parser.add_argument('--output', help='Optional path for JSON report')
    args = parser.parse_args()
    result = run_suite()
    payload = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(payload + '\n', encoding='utf-8')
    print(payload)


if __name__ == '__main__':
    main()
