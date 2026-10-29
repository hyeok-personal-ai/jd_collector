"""Offline gold-set evaluation; optional real local Ollama calls, no site requests."""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .database import Base
from .job_labels import label_job
from .normalization import normalize_draft
from .schema import JobDraft

DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'evaluation.jsonl'

async def evaluate(path: str | Path = DEFAULT_FIXTURE, *, model: str | None = None,
                   session_factory=None, extractor=None) -> dict:
    """Each JSONL row contains input JobDraft fields, expected labels and/or extracted fields.

    `model=None` measures only rule labels/cleaning. A model evaluates the local
    Ollama path. Model failures fall back to rules and score as missing values.
    """
    path = Path(path)
    if session_factory is None:
        engine = create_engine('sqlite://')
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
    metrics = {'cases':0,'fields_checked':0,'fields_correct':0,'field_accuracy':None,
               'by_field':{},'failures':[],'mode':'ollama' if model else 'rules'}
    for index, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        raw = JobDraft.model_validate(row['input'])
        kwargs = {'llm_model':model or ''}
        if extractor is not None:
            kwargs['extractor'] = extractor
        draft = await normalize_draft(raw, session_factory, **kwargs)
        actual = {**draft.model_dump(mode='json'), **label_job(draft).as_dict()}
        metrics['cases'] += 1
        for field, expected in row['expected'].items():
            measured = actual.get(field)
            hit = measured == expected
            score = metrics['by_field'].setdefault(field, {'correct':0,'total':0})
            score['total'] += 1
            score['correct'] += int(hit)
            metrics['fields_checked'] += 1
            metrics['fields_correct'] += int(hit)
            if not hit:
                metrics['failures'].append({'case':index,'field':field,'expected':expected,'actual':measured})
    if metrics['fields_checked']:
        metrics['field_accuracy'] = round(metrics['fields_correct']/metrics['fields_checked'],4)
    return metrics


def main():
    from .console import configure_console
    configure_console()
    p=argparse.ArgumentParser(description='Evaluate extraction and entry/IT classification from local JSONL')
    p.add_argument('--fixtures',default=str(DEFAULT_FIXTURE))
    p.add_argument('--model',default='',help='Optional Ollama model name, e.g. qwen3:4b')
    p.add_argument('--output',default='',help='Optional JSON report file')
    args=p.parse_args()
    result=asyncio.run(evaluate(args.fixtures,model=args.model or None))
    payload=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:
        Path(args.output).write_text(payload+'\n',encoding='utf-8')
    print(payload)

if __name__=='__main__':
    main()
