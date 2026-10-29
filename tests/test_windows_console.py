import io
import json

from app.console import print_json_payload


def test_json_output_survives_cp949_pipe(monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding='cp949')
    monkeypatch.setattr('sys.stdout', stream)
    expected = {'title': '엔지니어 ー 🚀'}
    print_json_payload(json.dumps(expected, ensure_ascii=False))
    stream.flush()
    assert json.loads(raw.getvalue().decode('cp949')) == expected


def test_json_output_preserves_unicode_in_utf8(monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding='utf-8')
    monkeypatch.setattr('sys.stdout', stream)
    print_json_payload('{"title": "엔지니어 🚀"}')
    stream.flush()
    assert '🚀' in raw.getvalue().decode('utf-8')
