"""Persisted source-specific cooldowns for private scheduled collection."""
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

KEY='rate_limit_cooldown'

def record_cooldown(store, source, retry_after='', *, now=None):
    now=time.time() if now is None else now
    old=store.meta(source, KEY) or {}
    strikes=min(4, int(old.get('strikes',0))+1)
    delay=6*3600*2**(strikes-1)
    try:
        requested=float(retry_after) if str(retry_after).strip().isdigit() else parsedate_to_datetime(retry_after).timestamp()-now
        delay=max(delay,requested)
    except (ValueError, TypeError, OverflowError):pass
    value={'until':now+delay,'strikes':strikes,'http_status':429}
    store.meta(source,KEY,value)
    return value

def waiting_status(store, source, *, now=None):
    now=time.time() if now is None else now
    value=store.meta(source,KEY) or {}
    if value.get('until',0)<=now:return None
    return {'status':'cooldown','reason':'rate_limit_cooldown','http_status':429,
            'resume_after':datetime.fromtimestamp(value['until'],timezone.utc).isoformat(),
            'strikes':value['strikes'],'checked_at':datetime.fromtimestamp(now,timezone.utc).isoformat()}

async def stop_on_rate_limit(response, store, source):
    if response.status_code==429:
        record_cooldown(store,source,response.headers.get('Retry-After',''))
        raise RuntimeError('HTTP 429: source cooldown saved; resume in a later scheduled run')
