"""Operator-controlled source access gates.

Technical reachability and robots.txt are not permission to reuse a site's data.
Public-site crawling is disabled until the operator has reviewed the current
terms and obtained any necessary authorization. Flags are attestations, NOT
permission grants. Official APIs have independent credential checks.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AccessRule:
    site_id: str
    mode: str
    reason: str
    apply_url: str | None = None


ACCESS_RULES: dict[str, AccessRule] = {
    "company_public": AccessRule("company_public", "public_career_read", "User-requested public career pages; bounded same-origin GET with robots checks"),
    "public_greenhouse": AccessRule("public_greenhouse", "documented_public_get", "Public GET, no key; review reuse terms before redistributing"),
    "public_lever": AccessRule("public_lever", "documented_public_get", "Public GET, no key; review reuse terms before redistributing"),
    "public_ashby": AccessRule("public_ashby", "documented_public_get", "Public GET, no key; review reuse terms before redistributing"),
    "public_smartrecruiters": AccessRule("public_smartrecruiters", "documented_public_get", "Public GET, no key; review reuse terms before redistributing"),
    "domestic_ats": AccessRule("domestic_ats", "permission_required_html", "Greeting/Ninehire public HTML adapter. Opt in only after policy review; no private endpoints"),
    "approved_company_listings": AccessRule("approved_company_listings", "permission_required_html", "Explicitly approved same-origin company listing adapter; HTML scraping disabled by default"),
    "company_jsonld": AccessRule("company_jsonld", "permission_required_html", "Only operator-approved, explicitly listed company pages; set ALLOW_HTML_COMPANY_JSONLD=1"),
    "demo": AccessRule("demo", "offline", "Bundled synthetic examples; no site access"),
    "saramin": AccessRule("saramin", "licensed_api", "Use the approved API; website terms restrict automated collection", "https://oapi.saramin.co.kr/join"),
    "wanted": AccessRule("wanted", "licensed_api", "Use approved Jobs API; website terms prohibit crawling", "https://openapi.wanted.jobs/apply/"),
    "jobkorea": AccessRule("jobkorea", "permission_required_html", "Official API preferred; HTML collection requires explicit permission", "https://www.jobkorea.co.kr/service/api"),
    "incruit": AccessRule("incruit", "permission_required_html", "Terms for Incruit services restrict bulk automated data collection"),
    "inthiswork": AccessRule("inthiswork", "public_career_read", "User-requested public WordPress GET, public recruitment categories, robots checked"),
    "zighang": AccessRule("zighang", "public_career_read", "User-requested robots-advertised sitemap and public SSR JobPosting only; blocked API never used"),
}


def access_status(site_id: str, environ: dict[str, str] | None = None) -> tuple[bool, str]:
    env = os.environ if environ is None else environ
    rule = ACCESS_RULES.get(site_id)
    if rule is None:
        return False, f"Unknown data source: {site_id}"
    if rule.mode in {"documented_public_get", "public_career_read"}:
        return True, "No API key required; respect provider terms, robots.txt and fair-use limits"
    if rule.mode == "offline":
        return True, "offline fixture"
    if rule.mode == "licensed_api":
        if site_id == "saramin":
            okay = bool(env.get("SARAMIN_ACCESS_KEY", "").strip())
            return okay, "approved API key configured" if okay else "SARAMIN_ACCESS_KEY required (approval first)"
        okay = bool(env.get("WANTED_AUTH_VALUE", "").strip() and env.get("WANTED_AUTH_HEADER", "").strip() and env.get("WANTED_APPROVED_LIST_URL", "").strip())
        return okay, "licensed endpoint and key configured" if okay else "WANTED_APPROVED_LIST_URL, WANTED_AUTH_HEADER, WANTED_AUTH_VALUE required after approval"
    flag = f"ALLOW_HTML_{site_id.upper()}"
    if env.get(flag) == "1":
        return True, f"operator attests authorization: {flag}=1 (also check robots.txt)"
    return False, f"HTML disabled by default; if authorized after terms review set {flag}=1"


def require_access(site_id: str) -> None:
    allowed, explanation = access_status(site_id)
    if not allowed:
        raise PermissionError(f"{site_id}: {explanation}")
