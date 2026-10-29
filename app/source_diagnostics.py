"""Read-only source diagnostics: response metadata only, no bodies or credentials."""
import asyncio
import hashlib
import json
import os
import platform
import ssl
from urllib.parse import urlsplit

import httpx
from .config import USER_AGENT
from .fetcher import PoliteFetcher, browser_challenge

WP_LIST = 'https://inthiswork.com/wp-json/wp/v2/posts?categories=191700167,191700168&per_page=100&page=1&orderby=id&order=desc'


def response_evidence(response):
    return {'path':urlsplit(str(response.url)).path, 'status':response.status_code,
            'headers':{k:response.headers[k] for k in ('server','content-type','x-cache','x-cache-status','retry-after') if k in response.headers},
            'browser_challenge':browser_challenge(response),
            'body_bytes':len(response.content), 'body_sha256':hashlib.sha256(response.content).hexdigest()}


def failure_evidence(error):
    import re
    text=str(error).lower()
    code=re.search(r'(?:http |returned )(401|403|404|410|429|5\d\d)',text)
    return {'http_status':int(code[1]) if code else None,
            'stage':'robots' if 'robots.txt' in text else 'content',
            'kind':'browser_challenge' if 'browser security challenge' in text else (
                'robots_disallow' if 'disallow' in text else ('http_error' if code else 'source_error'))}


async def diagnose():
    result={'runtime':{'os':platform.system(),'python':platform.python_version(),
                      'httpx':httpx.__version__,'tls':ssl.OPENSSL_VERSION,'user_agent':USER_AGENT,
                      'proxy_env_present':any(os.getenv(k) for k in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'))},
            'responses':[]}
    async with PoliteFetcher(frozenset({'inthiswork.com'})) as fetcher:
        async def response_hook(response):
            await response.aread()
            result['responses'].append(response_evidence(response))
        fetcher.client.event_hooks['response']=[response_hook]
        try:
            response=await fetcher.get_response(WP_LIST)
            rows=json.loads(response.text)
            result.update(status='accessible',public_total=response.total,returned_posts=len(rows))
        except Exception as exc:
            result.update(status='failed',failure=failure_evidence(exc))
    return result


if __name__ == '__main__':
    print(json.dumps(asyncio.run(diagnose())))
