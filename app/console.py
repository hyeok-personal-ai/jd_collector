"""JSON output for terminals and pipes with legacy Windows encodings."""
import json
import sys


def print_json_payload(payload: str) -> None:
    """Keep UTF-8 files untouched; escape JSON only for an incompatible stdout."""
    try:
        payload.encode(sys.stdout.encoding or 'utf-8')
    except UnicodeEncodeError:
        payload = json.dumps(json.loads(payload), ensure_ascii=True, indent=2)
    print(payload)


def configure_console():
    """Use Unicode for CLI pipes on Windows without changing imported library behaviour."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
