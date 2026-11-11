"""Portable reports from the same evidence returned by the application API."""
from datetime import datetime
from html import escape
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo
import os


def posting_report(analyses):
    blocks = [('title', '공고 분석 리포트'), ('p', '분석 시각: ' + datetime.now(ZoneInfo('Asia/Seoul')).isoformat(timespec='seconds')),
              ('p', '수집한 원문에서 추출한 분석입니다. 미기재는 조건 없음이 아니며 지원 자격·합격 가능성을 보장하지 않습니다.')]
    for item in analyses:
        blocks += [('h2', item['title']), ('p', item['organization'] or '회사 미확인'), ('link', item['source_url'])]
        c = item['conditions']
        blocks += [('p', '근무지: ' + (c['region'] or '미확인') + ' / 마감: ' + (c['deadline_on'] or c['deadline_text'] or '미확인'))]
        for warning in item.get('quality_warnings', []):
            blocks += [('h3','원문 확인 필요'), ('p',warning['message']), ('quote',warning['evidence'])]
        for label, key in [('담당 업무', 'duties'), ('필수·자격요건', 'required'), ('우대사항', 'preferred'), ('근무 조건·혜택', 'conditions')]:
            blocks.append(('h3', label))
            blocks += [('quote', row['text']) for row in item['sections'][key]] or [('p', '원문에서 구역을 확인하지 못했습니다.')]
        blocks.append(('h3', '기술 구분'))
        for key, label in [('required', '필수·자격요건'), ('preferred', '우대'), ('mentioned', '일반 언급')]:
            blocks.append(('p', label + ': ' + (' · '.join(item['technologies'][key]) or '미확인')))
        blocks += [('h3', '경력 근거'), *[('quote', x) for x in item['career']['evidence']]]
    return blocks


def market_report(data, filters):
    blocks = [('title', '채용 공고 기술 요구 리포트'), ('p', '분석 시각: ' + datetime.now(ZoneInfo('Asia/Seoul')).isoformat(timespec='seconds')),
              ('p', '검색 조건: ' + filters), ('p', f"검색 {data['total']}건 / 분석 {data['analyzed']}건"), ('p', data['scope'])]
    for row in data['technologies']:
        blocks += [('h2', row['skill']), ('p', f"필수 {row['required']} · 우대 {row['preferred']} · 일반 언급 {row['mentioned']} · 공고 수 {row['posting_count']}")]
        for example in row['examples']:
            blocks += [('quote', example['text']), ('p', example['title']), ('link', example['url'])]
    for guide in data['guidance']:
        blocks += [('h2', guide['title']), ('p', guide['body'])]
    return blocks


def experience_report(data):
    blocks = [('title', '내 경험과 공고 연결 리포트'), ('p', data['note']),
              ('p', '분석 시각: ' + datetime.now(ZoneInfo('Asia/Seoul')).isoformat(timespec='seconds')),
              ('p', '선택 문서: ' + ' · '.join(d['title'] for d in data.get('documents', []))),
              ('p', f"조건에 맞는 {data.get('eligible',0)}건 / 의미 비교 {data.get('compared',0)}건 중 상위 {len(data['items'])}건. 색인 대기 {data.get('unindexed',0)}건.")]
    for item in data['items']:
        blocks += [('h2', item['job']['title']), ('link', item['job']['original_url']), ('p', f"의미 유사도: {item['similarity']:.4f}")]
        for pair in item['evidence_pairs']:
            blocks += [('h3', '공고 요구와 내 경험'), ('quote', pair['posting_text']),
                       ('p', pair['document_title']), ('quote', pair['text']), ('p', f"문장 유사도: {pair['similarity']:.4f}")]
        for guide in item['guidance']:
            blocks += [('h3', '지원 문서 보완 확인'), ('quote', guide['requirement']), ('p', guide['action'])]
    return blocks


def variants_report(data):
    blocks = [('title', '문서 유형별 지원 우선순위 비교'), ('p', data['note']),
              ('p', '분석 시각: ' + datetime.now(ZoneInfo('Asia/Seoul')).isoformat(timespec='seconds')),
              ('p', f"동일 후보 {data['eligible']}건 / 비교 {data['compared']}건 / 미색인 또는 변경 중 {data['unindexed']}건")]
    facts = data.get('candidate_facts', {})
    blocks.append(('p', '사용자 확인 백엔드 실무 경력: ' + (f"{facts['professional_months']}개월" if facts.get('professional_months') is not None else '미확인') + ' (교육·사이드 프로젝트 제외)'))
    for variant in data['variants']:
        blocks.append(('p', variant['variant'] + ': ' + ' · '.join(f'{k} {v}건' for k, v in variant['counts'].items())))
    for doc in data['documents']:
        blocks.append(('p', f"입력 문서: {doc['variant']} / {doc['title']} / SHA-256 {doc.get('content_hash', '미확인')}"))
    for item in data['items']:
        job = item['job']
        blocks += [('h2', job['title']), ('p', job.get('organization') or '회사 미확인'),
                   ('link', job['original_url']),
                   ('p', f"{item['priority']} · {item.get('selection_label', item['best_variant'])}"),
                   ('p', item['best_variant_reason']),
                   ('p', '마감: ' + str(job.get('deadline_on') or job.get('deadline_text') or '미확인')),
                   ('p', '마지막 수집: ' + str(job.get('last_seen_at') or '미확인') + ' / 현재 모집 여부는 원문 확인 필요')]
        blocks.append(('p', '공고 SHA-256: ' + str(item['analysis']['provenance']['content_hash'])))
        for warning in item['analysis'].get('quality_warnings', []):
            blocks += [('h3','원문 확인 필요'), ('p',warning['message']), ('quote',warning['evidence'])]
        for variant in item['variants']:
            a = variant['assessment']
            counts = a['required_counts']
            blocks += [('h3', variant['variant']), ('p', f"{a['priority']} · 유형 내 {variant['rank']}위 · 유사도 {variant['similarity']:.4f}"),
                       ('p', f"필수 근거 {counts['supported']} · 불일치 {counts['mismatch']} · 미확인 {counts['unknown']} · 이력서 근거 {a['resume_supported']}"),
                       ('p', a['priority_reason'])]
            if variant.get('distinct_evidence'):
                blocks.append(('p','이 유형에서만 확인한 근거: '+' / '.join(variant['distinct_evidence'])))
            if variant.get('evidence_in_other_versions'):
                blocks.append(('p','다른 유형에는 근거가 있는 조건: '+' / '.join(variant['evidence_in_other_versions'])+'. 실제 경험과 버전 차이를 확인하세요.'))
        best = next(v['assessment'] for v in item['variants'] if v['variant'] == item['best_variant'])
        blocks.append(('h3', '추천 문서의 필수 조건 상세'))
        for row in best['requirements']:
            if row['category'] != 'required':
                continue
            blocks += [('quote', row['requirement']), ('p', row['label'] + ': ' + row['reason'])]
            for e in row['evidence']:
                label = '판정 근거: ' if row['state'] == 'supported' else '관련 문서 (충족 판정 아님): '
                blocks += [('p', label + e['document_title'] + (f" {e['page']}쪽" if e['page'] else '')), ('quote', e['text'])]
        blocks.append(('h3','추천 문서 보완 계획 - 필수 전체와 우대 3개'))
        guides = [g for g in best['guidance'] if g['category']=='required'] + [g for g in best['guidance'] if g['category']=='preferred'][:3]
        for g in guides:
            blocks += [('h3', '문서 보완: ' + g['requirement']),
                       ('p', '이력서: ' + g['resume_action']), ('p', '포트폴리오: ' + g['portfolio_action'])]
    return blocks


def safe_url(value):
    from urllib.parse import urlsplit
    try:
        u = urlsplit(value)
        return value if u.scheme in ('https', 'http') and u.netloc and not u.username and not u.password else ''
    except ValueError:
        return ''


def markdown(blocks):
    lines = []
    for kind, value in blocks:
        # Treat source material as text, not authored HTML/Markdown instructions.
        text = escape(str(value)).replace('\\', '\\\\')
        for char in ('*', '_', '[', ']', '`', '#'):
            text = text.replace(char, '\\' + char)
        if kind == 'link':
            text = '<' + escape(safe_url(str(value))) + '>' if safe_url(str(value)) else text
        elif kind in ('title', 'h2', 'h3'):
            text = {'title': '# ', 'h2': '## ', 'h3': '### '}[kind] + text
        elif kind == 'quote':
            text = '\n'.join('> ' + line for line in text.splitlines())
        lines.append(text)
    return '\n\n'.join(lines) + '\n'


def html_report(blocks):
    parts = []
    for kind, value in blocks:
        text = escape(str(value))
        if kind == 'link' and safe_url(str(value)):
            parts.append(f'<p><a href="{escape(str(value),quote=True)}" rel="noopener noreferrer">공고 원문: {text}</a></p>')
        else:
            tag = {'title': 'h1', 'h2': 'h2', 'h3': 'h3', 'quote': 'blockquote'}.get(kind, 'p')
            parts.append(f'<{tag}>{text}</{tag}>')
    return '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Recruit Radar 리포트</title>
<style>body{font:16px/1.8 system-ui,sans-serif;color:#243238;background:#f4f6f7;margin:0}main{max-width:840px;margin:32px auto;background:white;padding:48px;border-radius:16px}h1{font-size:30px;line-height:1.4}h2{border-top:1px solid #dbe3e7;padding-top:28px;margin-top:40px;font-size:23px}h3{font-size:18px}p,blockquote{white-space:pre-wrap;overflow-wrap:anywhere}blockquote{margin:12px 0;padding:12px 20px;border-left:3px solid #20756a;background:#f3f8f7}a{color:#176b61} @media(max-width:600px){main{padding:22px;margin:0;border-radius:0}}@media print{body{background:white}main{margin:0;padding:0}h2,h3{break-after:avoid}a{color:inherit}}</style><main>''' + '\n'.join(parts) + '</main></html>'


def pdf_report(blocks):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    font = 'RadarKorean'
    if font not in pdfmetrics.getRegisteredFontNames():
        candidates = [os.getenv('RADAR_PDF_FONT', ''), 'C:/Windows/Fonts/malgun.ttf', '/usr/share/fonts/truetype/nanum/NanumGothic.ttf']
        path = next((p for p in candidates if p and Path(p).is_file()), None)
        if path:
            pdfmetrics.registerFont(TTFont(font, path))
        else:
            font = 'HYSMyeongJo-Medium'
            pdfmetrics.registerFont(UnicodeCIDFont(font))
    out = BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, rightMargin=44, leftMargin=44, topMargin=46, bottomMargin=46, title='Recruit Radar Report')
    story = []
    for kind, value in blocks:
        size = {'title': 22, 'h2': 16, 'h3': 12}.get(kind, 10)
        style = ParagraphStyle(kind, fontName=font, fontSize=size, leading=size*1.5, wordWrap='CJK',
            textColor=HexColor('#243238'), spaceBefore=12 if kind.startswith('h') else 0,
            spaceAfter=5, keepWithNext=kind in ('title', 'h2', 'h3') or str(value).startswith('이력서: '), splitLongWords=True,
            leftIndent=12 if kind == 'quote' else 0)
        text = escape(str(value)).replace('\n', '<br/>')
        story.append(Paragraph(text, style))
    def footer(canvas, document):
        canvas.setFont(font, 8)
        canvas.drawRightString(A4[0]-44, 24, str(document.page))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()
