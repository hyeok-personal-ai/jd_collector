"""Local, evidence-backed summaries; never invent candidate accomplishments."""
from .skill_matching import CATALOG, requirement_mentions


def summarize_jobs(jobs, selected, total):
    names=list(dict.fromkeys([*CATALOG,*selected]))
    rows={name:{'skill':name,'selected':name in selected,'required':0,'preferred':0,
                'mentioned':0,'posting_count':0,'examples':[]} for name in names}
    for job in jobs:
        result=requirement_mentions(job,names)
        for name,row in rows.items():
            kinds=[k for k in ('required','preferred','mentioned') if name in result[k]]
            if not kinds: continue
            row['posting_count']+=1
            for kind in kinds: row[kind]+=1
            if len(row['examples'])<3:
                evidence=next((e for e in result['evidence'] if e['skill']==name and e['category']==kinds[0]),None)
                if evidence:row['examples'].append({'title':job.title,'url':job.original_url,
                    'category':evidence['category'],'text':evidence['evidence']})
    ranked=sorted((r for r in rows.values() if r['posting_count']),
                  key=lambda r:(-r['required'],-r['preferred'],-r['posting_count'],r['skill']))
    focus=[r['skill'] for r in ranked if r['selected']][:5]
    investigate=[r['skill'] for r in ranked if not r['selected'] and r['required']][:5]
    return {'total':total,'analyzed':len(jobs),'sampled':total>len(jobs),'limit':300,
        'scope':'현재 검색 조건의 최신 등록일순 최대 300건. 출처 간 중복 공고가 포함될 수 있습니다.',
        'technologies':ranked,'resume_focus':focus,'investigate':investigate,
        'guidance':[
            {'title':'이력서: 기술 목록보다 실제 해결 경험',
             'body':'백엔드·서버 개발을 중심으로 소개하고, 프로젝트마다 문제 → 본인 역할 → 선택한 설계와 이유 → 검증 결과 순서로 정리하세요. 아래 요구 기술과 연결되는 실제 경험을 먼저 배치하세요. 수치가 없다면 만들지 말고 측정 방법과 확인한 결과를 적으세요.'},
            {'title':'포트폴리오: 운영까지 설명할 수 있는 서비스',
             'body':'대표 프로젝트에 API·데이터 모델, 요청 흐름과 배포 구조도, 실행 방법, 테스트를 포함하세요. 인증·권한, 트랜잭션, 장애 대응, CI/CD 중 직접 구현한 항목의 설계 선택과 한계를 설명하세요.'},
            {'title':'성능·안정성: 재현 가능한 근거',
             'body':'SQL 실행계획과 인덱스 비교, Redis 캐시의 만료·정합성, Kafka 재처리·중복 처리, Docker 배포·롤백 중 실제 사용한 기술에 대해 실험 조건과 전후 결과를 남기세요. 사용하지 않은 도구를 공고 빈도만 보고 억지로 추가할 필요는 없습니다.'},
            {'title':'지원 전: 개인 경험과 별도 대조',
             'body':'이 분석은 공고 요약이며 이력서 평가가 아닙니다. 선택하지 않은 기술도 미보유로 단정하지 않습니다. 경력 연수, 학력, 대안 기술, 필수·우대 원문을 함께 확인하세요.'}
        ]}
