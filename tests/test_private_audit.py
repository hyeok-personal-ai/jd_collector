import importlib.util
from pathlib import Path

spec=importlib.util.spec_from_file_location('audit',Path(__file__).parents[1]/'scripts/audit_private_run.py')
audit=importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_stale_success_cannot_hide_workflow_failure():
    run={'id':1,'created_at':'2026-10-30T00:00:00Z'}
    doc={'generated_at':'2026-10-29T00:00:00Z','source_status':{'inthiswork':{'status':'completed'}}}
    report=audit.report_for(run,doc,'failure')
    assert not report['healthy'] and not report['snapshot_fresh']
    assert report['sources']=={}


def test_challenge_classified_without_claiming_an_ip_rule():
    result=audit.assessment({'status':'blocked','failure':{'http_status':403},
        'responses':[{'path':'/robots.txt','status':403,'browser_challenge':True,'body':'private data'}]})
    assert result['reason']=='site_browser_challenge'
    assert 'private data' not in str(result)
    assert audit.assessment({'status':'blocked','failure':{'http_status':403}})['reason']=='access_denied_rule_unknown'


def test_budget_continuation_is_healthy_but_missing_source_is_not():
    run={'id':1,'created_at':'2026-10-30T00:00:00Z'}
    doc={'generated_at':'2026-10-30T00:01:00Z','source_status':{
        'inthiswork':{'status':'completed'},'zighang':{'status':'limited'}}}
    assert audit.report_for(run,doc,'success')['healthy']
    del doc['source_status']['zighang']
    assert not audit.report_for(run,doc,'success')['healthy']
