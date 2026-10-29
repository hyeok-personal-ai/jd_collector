"""The six target sources; statuses are implementation confidence, NOT policy approval."""
from .real_sites import SITE_LISTS

SOURCES = [
    {"id":"domestic_ats", "url":"https://www.greetinghr.com/", "adapter":"reviewed_public_html", "note":"Greeting/Ninehire pattern parser, explicit operator authorization required"},
    {"id":"approved_company_listings", "url":"https://schema.org/JobPosting", "adapter":"operator_authorized_html_listing", "note":"Explicit first-party listing configuration + permission; link-only default"},
    {"id":"company_jsonld", "url":"https://schema.org/JobPosting", "adapter":"operator_authorized_jsonld", "note":"Explicit company job URLs only; no general-domain crawling"},
    {"id": "public_greenhouse", "url": "https://docs.greenhouse.io/job-board.html", "adapter": "documented_public_get", "note": "No key; default boards: get-delight, seoulrobotics, marqvision, moloco"},
    {"id": "public_lever", "url": "https://github.com/lever/postings-api", "adapter": "documented_public_get", "note": "No key; default: zoyi, palantir, clovirtualfashion, matchgroup"},
    {"id": "public_ashby", "url": "https://developers.ashbyhq.com/docs/public-job-posting-api", "adapter": "documented_public_get", "note": "No key; default: furiosa-ai; configurable PUBLIC_ASHBY_BOARDS"},
    {"id": "public_smartrecruiters", "url": "https://developers.smartrecruiters.com/docs/customer-overview", "adapter": "documented_public_get", "note": "No key; configure PUBLIC_SMARTRECRUITERS_COMPANIES before running"},
    {"id": "jobkorea", "url": SITE_LISTS["jobkorea"], "adapter": "html_experimental", "note": "Actual HTML may change; approved API preferred"},
    {"id": "saramin", "url": SITE_LISTS["saramin"], "adapter": "official_api_needs_key", "note": "SARAMIN_ACCESS_KEY; official API summary fields, IT/debut filter approximate"},
    {"id": "incruit", "url": "https://lab.incruit.com/navi/jobs?sort=deadline&occu=IT", "adapter": "html_fail_closed", "note": "Dynamic React page; stable detail links not live-verified"},
    {"id": "inthiswork", "url": SITE_LISTS["inthiswork"], "adapter": "html_experimental", "note": "WordPress archive IDs; validate with live HTML"},
    {"id": "zighang", "url": "https://zighang.com/recruitment", "adapter": "html_fail_closed", "note": "Dynamic page; stable detail links not live-verified"},
    {"id": "wanted", "url": "https://www.wanted.co.kr/wdlist/518?country=kr&job_sort=job.latest_order&years=0&years=3&locations=all&selected=660&selected=872&selected=873&selected=895", "adapter": "official_api_needs_key", "note": "Requires approved OpenAPI URL, credential and schema mapping verification"},
]
