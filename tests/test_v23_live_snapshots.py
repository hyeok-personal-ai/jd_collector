"""Public ATS snapshots captured 2026-10-22 KST; tests themselves are offline."""
import json
from pathlib import Path
import pytest
from app.crawlers.public_feeds import parse_greenhouse, parse_lever

ROOT = Path(__file__).resolve().parents[1] / 'fixtures/real_public'
if not ROOT.exists():
    pytest.skip('Captured third-party postings are excluded from the public repository', allow_module_level=True)


@pytest.mark.parametrize('index,title,region', [
    (0, 'AI Agent Software Engineer, Intern', 'Seoul, South Korea'),
    (1, 'Forward Deployed Product Manager', 'San Mateo, California, United States'),
])
def test_greenhouse_real_snapshot(index, title, region):
    row = json.loads((ROOT/'greenhouse.json').read_text(encoding='utf-8'))[index]
    result = parse_greenhouse(row, 'get-delight')
    assert result.title == title
    assert result.region == region
    assert result.organization == 'delight.ai (Sendbird)'
    assert result.source_status == 'open'
    assert len(result.body_text) > 100
    assert result.original_url.startswith('https://job-boards.greenhouse.io/get-delight/jobs/')


@pytest.mark.parametrize('index,title,region', [
    (0, '【Open Position】チャネルトークの成長を牽引するビジネス職（Sales / CS / CX）', 'Tokyo, Hanzomon'),
    (1, 'Account Management Team Lead', 'Gangnam District, Seoul'),
])
def test_lever_real_snapshot(index, title, region):
    row = json.loads((ROOT/'lever.json').read_text(encoding='utf-8'))[index]
    result = parse_lever(row, 'zoyi')
    assert result.title == title
    assert result.region == region
    assert result.organization == 'Channel Corp. (채널코퍼레이션)'
    assert result.source_status == 'open'
    assert len(result.body_text) > 100
    assert result.apply_url.startswith('https://jobs.lever.co/zoyi/')
