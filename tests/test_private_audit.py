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



def test_valid_rate_limit_pause_is_not_hidden_failure():
    valid={'status':'cooldown','http_status':429,'resume_after':'2099-01-01T00:00:00+00:00'}
    assert audit.assessment(valid)['reason']=='rate_limit_cooldown'
    assert audit.assessment({**valid,'resume_after':'2020-01-01T00:00:00+00:00'})['status']=='invalid_cooldown'
    assert audit.assessment({**valid,'http_status':403})['status']=='invalid_cooldown'


from datetime import datetime,timedelta,timezone


def observation_run(day,event='schedule',healthy=True,cooldown=False):
    created=datetime(2026,11,day,0,23,tzinfo=timezone.utc)
    run={'id':day,'event':event,'created_at':created.isoformat()}
    report={'checked_at':(created+timedelta(minutes=3)).isoformat(),'healthy':healthy,
            'sources':{'inthiswork':{'status':'cooldown' if cooldown else 'completed'},
                       'zighang':{'status':'limited'}}}
    return run,report,created+timedelta(minutes=4)


def test_seven_scheduled_days_required_and_manual_runs_do_not_count():
    state=None
    for day in range(10,17):
        run,report,now=observation_run(day,event='workflow_dispatch')
        state=audit.daily_observation(state,report,run,'policy-a',now=now)
    assert state['healthy_schedule_streak']==0
    assert not state['seven_day_gate_passed']
    for day in range(10,17):
        run,report,now=observation_run(day)
        state=audit.daily_observation(state,report,run,'policy-a',now=now)
        assert state['seven_day_gate_passed'] is (day==16)
    assert state['healthy_schedule_streak']==7
    assert state['both_sources_checked_streak']==7


def test_missing_day_failed_run_and_stale_history_do_not_pass():
    state=None
    for day in [10,11,12,14,15,16,17]:
        run,report,now=observation_run(day)
        state=audit.daily_observation(state,report,run,'policy-a',now=now)
    assert state['healthy_schedule_streak']==4
    assert not state['seven_day_gate_passed']
    run,report,now=observation_run(18,healthy=False)
    state=audit.daily_observation(state,report,run,'policy-a',now=now)
    assert state['healthy_schedule_streak']==0
    run,report,now=observation_run(21,event='workflow_dispatch')
    state=audit.daily_observation(state,report,run,'policy-a',now=now)
    assert not state['latest_schedule_fresh']
    assert not state['seven_day_gate_passed']


def test_cooldown_is_operational_but_not_a_completed_source_check_day():
    state=None
    for day in range(10,17):
        run,report,now=observation_run(day,cooldown=day==15)
        state=audit.daily_observation(state,report,run,'policy-a',now=now)
    assert state['healthy_schedule_streak']==7
    assert state['both_sources_checked_streak']==1
    assert not state['seven_day_gate_passed']


def test_policy_changes_reset_history_and_older_runs_do_not_replace_latest():
    run,report,now=observation_run(10)
    state=audit.daily_observation(None,report,run,'policy-a',now=now)
    later=dict(run,id=110,created_at='2026-11-10T01:00:00+00:00')
    state=audit.daily_observation(state,report,later,'policy-a',now=now+timedelta(hours=1))
    state=audit.daily_observation(state,report,run,'policy-a',now=now+timedelta(hours=1))
    assert state['days']['2026-11-10']['run_id']=='110'
    run,report,now=observation_run(11,event='workflow_dispatch')
    state=audit.daily_observation(state,report,run,'policy-b',now=now)
    assert state['days']=={}


def test_korean_calendar_day_is_used():
    run,report,now=observation_run(10)
    run['created_at']='2026-11-09T23:55:00+00:00'
    state=audit.daily_observation(None,report,run,'policy-a',now=now)
    assert state['latest_scheduled_day']=='2026-11-10'
