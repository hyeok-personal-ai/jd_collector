import asyncio
import json
import time
import pytest
from app.aggregate_store import AggregateStore
from app.crawlers.zighang_recent import discover_recent, pending_recent, RECENT_SOURCE, INDEX
from app.deadline_policy import today_kst


def test_recent_discovery_resumes_filters_and_persists_without_nondev_bodies(tmp_path):
    store=AggregateStore(tmp_path/'inventory.sqlite')
    ids=['00000000-0000-0000-0000-00000000000'+str(i) for i in range(1,4)]
    urls=['https://zighang.com/recruitment/'+i for i in ids]
    sitemap='https://zighang.com/seo/sitemap/sitemap-recruitment-1.xml'
    def xml(items):
        return '<urlset>'+''.join('<url><loc>'+u+'</loc><lastmod>'+today_kst().isoformat()+'</lastmod></url>' for u in items)+'</urlset>'
    class Fetcher:
        calls=[]
        async def get_text(self,url):
            self.calls.append(url)
            if url==INDEX:return xml([sitemap,'https://evil.example/sitemap-recruitment-2.xml'])
            if url==sitemap:return xml(urls)
            row={'id':url.rsplit('/',1)[1],'title':'영업 담당자' if url==urls[0] else '백엔드 개발자',
                 'status':'EXPIRED' if url==urls[2] else 'ACTIVE','deadlineType':'상시',
                 'content':{'type':'doc','content':[{'type':'text','text':'fixture body'}]}}
            return '<script>self.__next_f.push('+json.dumps([1,'1:'+json.dumps(row)+'\n'])+')</script>'
    f=Fetcher()
    asyncio.run(discover_recent(f,store,limit=1))
    assert len(pending_recent(store))==2
    assert store.focus_summary('zighang')=={}
    asyncio.run(discover_recent(f,store,limit=10))
    assert f.calls==[INDEX,sitemap,*urls]
    assert store.focus_summary('zighang')=={'pending':1}
    with store.db() as db:
        assert all(r[0] is None for r in db.execute('SELECT payload FROM aggregate_refs WHERE source=?',(RECENT_SOURCE,)))
    assert pending_recent(store)==[]


def test_access_denial_stops_batch_and_cools_down(tmp_path):
    store=AggregateStore(tmp_path/'inventory.sqlite')
    store.meta(RECENT_SOURCE,'last_scan',time.time())
    store.save_page(RECENT_SOURCE,'x',[{'id':str(i),'url':'https://zighang.com/recruitment/'+str(i),'revision':today_kst().isoformat()} for i in range(2)])
    class Fetcher:
        calls=0
        async def get_text(self,url):
            self.calls+=1
            raise PermissionError('robots challenge')
    f=Fetcher()
    with pytest.raises(PermissionError):asyncio.run(discover_recent(f,store))
    assert f.calls==1
    assert len(pending_recent(store))==1


def test_private_snapshot_preserves_recent_scan_cursor(tmp_path,monkeypatch):
    from app import cloud
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    store=AggregateStore(tmp_path/'first.sqlite')
    store.meta(RECENT_SOURCE,'last_scan',123)
    store.pages(RECENT_SOURCE,['https://zighang.com/seo/sitemap/sitemap-recruitment-1.xml'])
    store.save_page(RECENT_SOURCE,'x',[{'id':'candidate','url':'https://zighang.com/recruitment/candidate','revision':today_kst().isoformat()}])
    snapshot=cloud.export_snapshot(sessionmaker(bind=engine),store)
    restored=AggregateStore(tmp_path/'second.sqlite')
    cloud.restore_inventory(snapshot,restored)
    assert restored.meta(RECENT_SOURCE,'last_scan')==123
    assert pending_recent(restored)[0]['id']=='candidate'
    engine.dispose()


def test_current_wordpress_coverage_does_not_count_historical_failure(tmp_path):
    store=AggregateStore(tmp_path/'inventory.sqlite')
    store.pages('inthiswork',['page'])
    store.save_page('inthiswork','page',[{'id':'old','url':'old'}])
    store.mark('inthiswork','old','failed','private historical post')
    store.meta('inthiswork','scan_started',time.time())
    store.save_page('inthiswork','page',[{'id':'new','url':'new'}])
    store.mark('inthiswork','new','done')
    store.meta('inthiswork','expected',1)
    store.meta('inthiswork','scanned_unique_count',1)
    result=store.summary('inthiswork')
    assert result['current_list_complete']
    assert result['current_inventory']=={'done':1}
    assert not result['complete']  # Historical error remains visible, not deleted.
    store.mark('inthiswork','new','failed')
    assert not store.summary('inthiswork')['current_list_complete']


def test_backlog_does_not_expire_and_newest_and_oldest_both_progress(tmp_path):
    store=AggregateStore(tmp_path/'inventory.sqlite')
    rows=[{'id':str(i),'url':'https://example.org/'+str(i),'revision':'2020-01-01' if i==0 else today_kst().isoformat()} for i in range(20)]
    store.save_page(RECENT_SOURCE,'x',rows)
    batch=pending_recent(store,10)
    assert len(batch)==10 and len({r['id'] for r in batch})==10
    assert '0' in {r['id'] for r in batch}
    store.mark(RECENT_SOURCE,'1','done')
    store.save_page(RECENT_SOURCE,'x',[{**rows[1],'revision':today_kst().isoformat()+'T18:00:00Z'}])
    assert '1' in {r['id'] for r in pending_recent(store,10)}


def test_sitemap_rotation_and_full_timestamp_revision(tmp_path):
    store=AggregateStore(tmp_path/'inventory.sqlite')
    pages=['https://zighang.com/seo/sitemap/sitemap-recruitment-'+str(i)+'.xml' for i in range(1,6)]
    job='https://zighang.com/recruitment/00000000-0000-0000-0000-000000000001'
    stamp=today_kst().isoformat()+'T01:02:03Z'
    class Fetcher:
        calls=[]
        async def get_text(self,url):
            self.calls.append(url)
            targets=pages if url==INDEX else [job]
            return '<urlset>'+''.join('<url><loc>'+u+'</loc><lastmod>'+stamp+'</lastmod></url>' for u in targets)+'</urlset>'
    f=Fetcher()
    asyncio.run(discover_recent(f,store,limit=0))
    assert f.calls==[INDEX,*pages[:3]]
    assert pending_recent(store,1)[0]['revision']==stamp
    store.meta(RECENT_SOURCE,'last_scan',0)
    f.calls=[]
    asyncio.run(discover_recent(f,store,limit=0))
    assert f.calls==[INDEX,pages[0],pages[1],pages[3]]
