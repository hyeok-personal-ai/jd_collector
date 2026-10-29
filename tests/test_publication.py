"""Protect the boundary between public code and local collection data."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('publication_check', Path(__file__).parents[1] / 'scripts/check_publication.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def test_blocks_operational_files_even_if_force_added():
    for path in ['data/inventory.sqlite3', 'recruit.db-wal', 'reports/status.json',
                 '.env', 'fixtures/real_public/lever.json', 'backup.zip', 'app/__pycache__/x.py']:
        assert not check.allowed_path(path)
    assert check.allowed_path('app/aggregate_store.py')
    assert check.allowed_path('fixtures/offline_public/lever.json')


def test_secrets_and_personal_paths_are_redacted_in_findings():
    value = 'gh' + 'p_' + 'z' * 30
    result = check.findings('test.txt', value)
    assert result and value not in str(result)
    assert check.findings('test.txt', 'C:' + '\\Users\\private-person\\file.txt')
    assert check.findings('test.txt', 'private-person' + '@mail.example.biz')
    assert not check.findings('test.txt', 'test@example.org')
