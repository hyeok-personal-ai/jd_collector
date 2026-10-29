"""Register public company pages observed in cached development postings, without network."""
import gzip
import json
from pathlib import Path
from sqlalchemy import select
from app.database import SessionLocal,RawHtml,JobClassification
from app.aggregate_store import AggregateStore
from app.crawlers.zighang_public import public_rows,company_urls,COMPANY_SOURCE


def main():
    # Public company URLs verified on the site/search, placed first for the initial scan.
    urls=['https://www.zighang.com/company/dee2ca41-91e4-4624-b755-bfa2db406f51',
          'https://www.zighang.com/company/f4385efa-acaa-4289-93e1-c074adf0f6f6',
          'https://www.zighang.com/company/68de18dd-9b07-4efb-9a71-d1d6a4ff07a1',
          'https://www.zighang.com/company/752e5016-02f3-4b90-9775-d7ee56f3f558']
    domestic=[];other=[];inspected=0
    with SessionLocal() as db:
        query=select(RawHtml).join(JobClassification,(JobClassification.source_id==RawHtml.source_id)&
            (JobClassification.external_id==RawHtml.external_id)).where(RawHtml.source_id=='zighang',JobClassification.it_match=='yes')
        for raw in db.scalars(query):
            rows=public_rows(gzip.decompress(raw.html_gzip).decode('utf-8'));inspected+=1
            main=rows.get(raw.external_id,{})
            target=other if '해외' in (main.get('regions') or []) else domestic
            target.extend(company_urls(rows))
    urls=list(dict.fromkeys(urls+domestic+other))
    store=AggregateStore();store.pages(COMPANY_SOURCE,urls)
    report={'network_requests':0,'cached_development_pages':inspected,'registered_company_pages':len(urls),
            'scope':'Public SSR first company listing; not all companies or pagination',
            'pages':store.discovery_summary(COMPANY_SOURCE)}
    Path('reports/v2.4').mkdir(parents=True,exist_ok=True)
    Path('reports/v2.4/company-discovery-seed.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
