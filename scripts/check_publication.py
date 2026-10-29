"""Check tracked public files without printing matched secret values."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {'.gitignore', '.env.example', 'README.md', 'PUBLICATION.md',
              'CHANGELOG.md', 'pyproject.toml', 'start.ps1'}
FOLDERS = {'app': {'.py', '.json', '.html'}, 'tests': {'.py'},
           'fixtures': {'.json', '.jsonl', '.html'}, 'sources': {'.json', '.txt'}}
SCRIPTS = {'scripts/check_publication.py', 'scripts/seed_zighang_company_discovery.py'}
PATTERNS = {
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'GitHub credential': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    'AWS credential': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'Slack credential': re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{20,}\b'),
    'OpenAI credential': re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b'),
    'personal Windows path': re.compile(r'[A-Za-z]:[\\/]+Users[\\/]+(?!Public\b)[^\s"\'<>]+', re.I),
    'personal Unix path': re.compile(r'/(?:Users|home)/[A-Za-z0-9_.-]+/'),
    'email address': re.compile(r'\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b'),
    'credential in URL': re.compile(r'https?://[A-Za-z0-9._~%+-]+:[^\s/@"\'<>]+@'),
}
SYNTHETIC_CASES = {
    'tests/test_v17_catalog_scaling.py': 'https://alice' + ':pass' + '@jobs.lever.co/palantir',
}


def allowed_path(path):
    p = Path(path)
    if p.is_absolute() or '..' in p.parts or 'real_public' in p.parts:
        return False
    if path in ROOT_FILES or path in SCRIPTS or path == '.github/workflows/ci.yml':
        return True
    if any(part.startswith('.') or part == '__pycache__' for part in p.parts):
        return False
    return bool(p.parts and p.parts[0] in FOLDERS and p.suffix in FOLDERS[p.parts[0]])


def findings(path, text):
    issues = []
    for label, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            example = SYNTHETIC_CASES.get(path)
            if example and label in {'email address', 'credential in URL'}:
                start = text.find(example)
                if start >= 0 and start <= match.start() < start + len(example):
                    continue
            if label == 'email address':
                domain = match.group(1).lower()
                if domain.endswith(('.example', '.test', '.invalid', '.example.org',
                                    '.example.com', '.users.noreply.github.com')) or domain in {
                                        'example.org', 'example.com', 'example.net', 'yourdomain.example',
                                        'users.noreply.github.com'}:
                    continue
            # No actual values are emitted, even when a check fails in public CI.
            issues.append(f'{path}:{text.count(chr(10), 0, match.start()) + 1}: {label}')
    return issues


def main():
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode('utf-8').split('\0')
    paths = [p for p in tracked if p]
    if not paths:
        print('No tracked files; stage the reviewed allowlisted files first.', file=sys.stderr)
        return 1
    errors = []
    for path in paths:
        if not allowed_path(path):
            errors.append(f'{path}: file outside public allowlist')
            continue
        try:
            entry = subprocess.check_output(['git', 'ls-files', '--stage', '--', path], cwd=ROOT)
            raw = subprocess.check_output(['git', 'show', ':' + path], cwd=ROOT)
            if not entry.startswith(b'100644 ') or len(raw) > 1_000_000:
                errors.append(f'{path}: non-regular or oversized staged file')
                continue
            text = raw.decode('utf-8')
        except (UnicodeError, OSError, subprocess.CalledProcessError):
            errors.append(f'{path}: file is not readable UTF-8 text')
            continue
        errors.extend(findings(path, text))
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    print(f'Public file check passed: {len(paths)} tracked UTF-8 files; no pattern findings.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
