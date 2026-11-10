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
              ('p', '선택 문서: ' + ' · '.join(d['title'] for d in data.get('documents', [])))]
    for item in data['items']:
        blocks += [('h2', item['job']['title']), ('link', item['job']['original_url']), ('p', f"의미 유사도: {item['similarity']:.4f}")]
        for pair in item['evidence_pairs']:
            blocks += [('h3', '공고 요구와 내 경험'), ('quote', pair['posting_text']),
                       ('p', pair['document_title']), ('quote', pair['text']), ('p', f"문장 유사도: {pair['similarity']:.4f}")]
        for guide in item['guidance']:
            blocks += [('h3', '지원 문서 보완 확인'), ('quote', guide['requirement']), ('p', guide['action'])]
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
            keepWithNext=kind in ('title', 'h2', 'h3'), splitLongWords=True,
            leftIndent=12 if kind == 'quote' else 0)
        text = escape(str(value)).replace('\n', '<br/>')
        story += [Paragraph(text, style), Spacer(1, 5)]
    def footer(canvas, document):
        canvas.setFont(font, 8)
        canvas.drawRightString(A4[0]-44, 24, str(document.page))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()
