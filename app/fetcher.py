"""Domain-restricted, robots-aware, rate-limited HTTP adapter.

Validates same-origin redirects; never bypasses login, captcha, WAF, or blocks. Site terms
and API licensing are additional requirements beyond robots.txt.
"""
import asyncio
import socket
import time
from dataclasses import dataclass
from collections.abc import Callable
from urllib.parse import urlsplit, urljoin
from urllib.robotparser import RobotFileParser
import httpx
from .config import REQUEST_DELAY_SECONDS, USER_AGENT


class DNSResolutionError(ConnectionError):
    """The local resolver failed; this is not a page parser or robots denial."""


class BrowserChallengeError(PermissionError):
    """Origin protection needs operator permission, never challenge solving or spoofing."""


def browser_challenge(response):
    text=response.text.lower()
    return 'hashcash_config' in text and '/__challenge/' in text


def is_dns_error(exc: BaseException) -> bool:
    """Inspect wrapped httpx/httpcore causes without relying only on error text."""
    seen = set()
    node = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if isinstance(node, socket.gaierror):
            return True
        if 'name resolution' in str(node).casefold() or 'getaddrinfo failed' in str(node).casefold():
            return True
        node = node.__cause__ or node.__context__
    return False


@dataclass(frozen=True)
class FetchResult:
    status: int
    text: str | None
    etag: str | None = None
    last_modified: str | None = None
    total: int | None = None
    total_pages: int | None = None


class PoliteFetcher:
    def __init__(self, allowed_hosts: frozenset[str], *, delay: float = REQUEST_DELAY_SECONDS,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not allowed_hosts:
            raise ValueError('Site crawler must declare allowed_hosts')
        self.allowed_hosts = allowed_hosts
        self.delay = max(0.0, delay)
        self.client = httpx.AsyncClient(
            headers={'User-Agent': USER_AGENT}, timeout=httpx.Timeout(15.0),
            follow_redirects=False, limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
            transport=transport,
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_request: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._captured_html: dict[str, str] = {}
        self.metrics = []
        self.approve_redirect = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()

    def _check_host(self, url: str, hosts: frozenset[str] | None = None) -> tuple[str, str]:
        u = urlsplit(url)
        if u.scheme != 'https' and u.hostname not in {'example.org'}:
            raise ValueError('Non-HTTPS live request prohibited')
        if not u.hostname or u.hostname not in (hosts or self.allowed_hosts) or u.username or u.password or u.port not in (None, 443):
            raise ValueError('Disallowed hostname, port or credentials in URL')
        return f'{u.scheme}://{u.netloc}', u.hostname

    async def _get_robots(self, origin: str) -> RobotFileParser | None:
        if origin in self._robots:
            return self._robots[origin]
        url = origin + '/robots.txt'
        try:
            response = await self.client.get(url)
            for _ in range(4):
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                destination = urljoin(str(response.url), response.headers.get('location', ''))
                approved=bool(self.approve_redirect and self.approve_redirect(destination))
                if approved:self.allowed_hosts=frozenset({*self.allowed_hosts,urlsplit(destination).hostname})
                next_origin, _ = self._check_host(destination, self.allowed_hosts if approved else frozenset({urlsplit(origin).hostname}))
                if next_origin != origin and not approved:
                    raise PermissionError('Cross-origin robots redirect requires review')
                response = await self.client.get(destination)
        except httpx.RequestError as exc:
            if is_dns_error(exc):
                raise DNSResolutionError(f'DNS lookup failed while checking robots.txt for {origin}') from None
            raise ConnectionError(f'Cannot reach robots.txt for {origin}: {type(exc).__name__}') from None
        if browser_challenge(response):
            raise BrowserChallengeError(f'robots.txt HTTP {response.status_code}: browser security challenge for {origin}; site operator bot allowance required')
        if response.status_code in (404, 410):
            self._robots[origin] = None
            return None
        if response.status_code != 200:
            raise PermissionError(f'robots.txt check returned {response.status_code} for {origin}')
        rp = RobotFileParser()
        rp.set_url(url)
        rp.parse(response.text.splitlines())
        self._robots[origin] = rp
        return rp

    def capture_rendered_html(self, url: str, html: str) -> None:
        self._check_host(url)
        self._captured_html[url] = html

    def pop_captured_html(self, url: str) -> str | None:
        return self._captured_html.pop(url, None)

    async def get_response(self, url: str, *, headers: dict[str, str] | None = None,
                           params: dict | None = None, capture: bool = False,
                           accept: str | None = None,
                           before_request: Callable[[], None] | None = None) -> FetchResult:
        origin, hostname = self._check_host(url)
        lock = self._locks.setdefault(hostname, asyncio.Lock())
        async with lock:
            rp = await self._get_robots(origin)
            if rp is not None and not rp.can_fetch(USER_AGENT, url):
                raise PermissionError(f'robots.txt disallows {origin}{urlsplit(url).path}')
            delay = max(self.delay, float(rp.crawl_delay(USER_AGENT) or 0) if rp else 0)
            request_headers = dict(headers or {})
            if accept:
                request_headers['Accept'] = accept
            for attempt in range(3):
                elapsed = time.monotonic() - self._last_request.get(hostname, -1e10)
                if elapsed < delay:
                    await asyncio.sleep(delay-elapsed)
                self._last_request[hostname] = time.monotonic()
                try:
                    target = url
                    for hop in range(5):
                        if before_request is not None:
                            before_request()
                        started = time.monotonic()
                        code = 0
                        try:
                            response = await self.client.get(target, headers=request_headers, params=params if hop == 0 else None)
                            code = response.status_code
                        finally:
                            self.metrics.append({'host': hostname, 'status': code,
                                                 'elapsed_ms': (time.monotonic()-started)*1000})
                        if code not in (301, 302, 303, 307, 308):
                            break
                        destination = urljoin(str(response.url), response.headers.get('location', ''))
                        approved=bool(self.approve_redirect and self.approve_redirect(destination))
                        if approved:self.allowed_hosts=frozenset({*self.allowed_hosts,urlsplit(destination).hostname})
                        next_origin, _ = self._check_host(destination)
                        if (next_origin != origin and not approved) or destination == target or hop == 4:
                            raise PermissionError('Redirect outside the allowed origin or redirect loop')
                        if next_origin != origin:
                            rp=await self._get_robots(next_origin)
                            origin=next_origin
                            hostname=urlsplit(destination).hostname
                            delay=max(delay,float(rp.crawl_delay(USER_AGENT) or 0) if rp else 0)
                        if rp is not None and not rp.can_fetch(USER_AGENT, destination):
                            raise PermissionError('robots.txt disallows redirected destination')
                        await asyncio.sleep(delay)
                        target = destination
                    if browser_challenge(response):
                        raise BrowserChallengeError(f'HTTP {response.status_code}: browser security challenge for {origin}; site operator bot allowance required')
                    if response.status_code in (429, 500, 502, 503, 504):
                        if attempt < 2:
                            retry = response.headers.get('Retry-After','')
                            seconds = min(60.0, float(retry)) if retry.isdecimal() else 2.0**attempt
                            await asyncio.sleep(max(delay, seconds))
                            continue
                        raise RuntimeError(f'HTTP {response.status_code} from {hostname}')
                    if response.status_code == 304:
                        return FetchResult(304, None, response.headers.get('ETag'), response.headers.get('Last-Modified'))
                    if not 200 <= response.status_code < 300:
                        # Never leak access-key query parameter through httpx status errors.
                        raise RuntimeError(f'HTTP {response.status_code} from {hostname}')
                    result = FetchResult(response.status_code, response.text,
                                         response.headers.get('ETag'), response.headers.get('Last-Modified'),
                                         int(response.headers['X-WP-Total']) if 'X-WP-Total' in response.headers else None,
                                         int(response.headers['X-WP-TotalPages']) if 'X-WP-TotalPages' in response.headers else None)
                    if capture:
                        self._captured_html[url] = response.text
                    return result
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if is_dns_error(exc):
                        raise DNSResolutionError(f'DNS lookup failed for {hostname}') from None
                    if attempt == 2:
                        raise ConnectionError(f'Network error contacting {hostname}') from None
                    await asyncio.sleep(max(delay, 2.0**attempt))
        raise RuntimeError('Unexpected fetch retry exhaustion')

    async def render_html(self, url: str) -> str:
        """Optional normal headless browser rendering, never a blocker bypass.

        The caller MUST first complete a successful, robots-permitted GET.
        Only used for explicitly approved client-rendered pages.
        """
        origin, _ = self._check_host(url)
        rp = await self._get_robots(origin)
        if rp is not None and not rp.can_fetch(USER_AGENT, url):
            raise PermissionError('robots.txt disallows browser rendering')
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError('Install pip install -e ".[playwright]" and playwright install chromium') from None
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(user_agent=USER_AGENT, service_workers='block')
                self.render_denials = []
                async def guard(route):
                    request = route.request
                    try:
                        target_host = urlsplit(request.url).hostname or ''
                        allowed = set(self.allowed_hosts)
                        # Known ATS applications load their own public assets/data on sibling hosts.
                        # Navigation stays on the selected employer origin; every resource still
                        # passes HTTPS, read-only and per-origin robots checks.
                        if not request.is_navigation_request():
                            for suffix in ('.roundhr.com', '.greetinghr.com', '.ninehire.site'):
                                if urlsplit(url).hostname.endswith(suffix) and target_host.endswith(suffix):
                                    allowed.add(target_host)
                        target_origin, _ = self._check_host(request.url, frozenset(allowed))
                        if request.method not in ('GET', 'HEAD'):
                            raise ValueError('Only read requests allowed')
                        policy = await self._get_robots(target_origin)
                        if policy is not None and not policy.can_fetch(USER_AGENT, request.url):
                            raise ValueError('robots denied')
                    except (ValueError, PermissionError, ConnectionError) as exc:
                        self.render_denials.append({'host': urlsplit(request.url).hostname,
                                                   'reason': str(exc)[:160]})
                        await route.abort()
                    else:
                        await route.continue_()
                await context.route('**/*', guard)
                page = await context.new_page()
                response = await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                if response is None or not (200 <= response.status < 300):
                    raise RuntimeError('Browser page could not load successfully')
                # Reject navigation away from permitted origin (e.g. login redirects).
                self._check_host(page.url)
                try:
                    await page.wait_for_load_state('networkidle', timeout=8000)
                except Exception:
                    pass  # Long-polling pages still have a bounded rendering window.
                await page.wait_for_timeout(1200)  # bounded wait; no scrolling/captcha interaction
                return await page.content()
            finally:
                await browser.close()

    async def get_text(self, url: str, *, capture: bool = False) -> str:
        result = await self.get_response(url, capture=capture)
        if result.text is None:
            raise ValueError('Unexpected HTTP 304')
        return result.text

    async def get_json(self, url: str, *, params: dict | None = None,
                       headers: dict[str, str] | None = None,
                       before_request: Callable[[], None] | None = None):
        import json
        result = await self.get_response(url, params=params, headers=headers, accept='application/json', before_request=before_request)
        if result.text is None:
            raise ValueError('Unexpected HTTP 304 on API request')
        try:
            return json.loads(result.text)
        except ValueError:
            raise ValueError(f'Invalid JSON response from {urlsplit(url).hostname}') from None
