#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Поиск по локальной базе знаний инцидентов.

Ищет либо по свободному тексту, либо по сигнатурам из разбора логов
(`parse_logs.py --format json`). Совпадение сигнатуры весит больше всего.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kb_common import (  # noqa: E402
    kb_dir, load_incidents, signature_similarity, signatures_from_parsed,
    load_parsed, tokenize,
)

FIELD_WEIGHTS = [
    ('title', 6.0),
    ('tags', 5.0),
    ('services', 3.0),
    ('stands', 1.5),
]
SECTION_WEIGHTS = [
    ('Симптомы', 4.0),
    ('Причина', 2.5),
    ('Решение', 2.0),
    ('Диагностика', 1.2),
    ('Проверка', 0.8),
    ('Заметки', 0.5),
]
SIGNATURE_WEIGHT = 14.0


def _field_text(meta, key):
    val = meta.get(key)
    if isinstance(val, list):
        return ' '.join(str(v) for v in val)
    return str(val or '')


def score_incident(inc, query_tokens, signatures, filters):
    meta = inc['meta']
    reasons = []
    score = 0.0

    # фильтры — жёсткие
    for key, values in (('stands', filters.get('stand')),
                        ('services', filters.get('service')),
                        ('tags', filters.get('tag'))):
        if not values:
            continue
        have = {str(v).lower() for v in (meta.get(key) or [])}
        if not have & {v.lower() for v in values}:
            return None

    if signatures:
        known = [str(s) for s in (meta.get('signatures') or [])]
        best_pairs = []
        for sig in signatures:
            best = 0.0
            best_known = None
            for cand in known:
                sim = signature_similarity(sig, cand)
                if sim > best:
                    best, best_known = sim, cand
            if best >= 0.5:
                best_pairs.append((best, sig, best_known))
        for sim, sig, cand in sorted(best_pairs, reverse=True)[:4]:
            score += SIGNATURE_WEIGHT * sim
            reasons.append('сигнатура %.0f%%: %s' % (sim * 100, cand[:70]))

    if query_tokens:
        qset = set(query_tokens)
        for key, weight in FIELD_WEIGHTS:
            tokens = set(tokenize(_field_text(meta, key)))
            hit = qset & tokens
            if hit:
                score += weight * len(hit) / float(len(qset)) * 2
                reasons.append('%s: %s' % (key, ', '.join(sorted(hit)[:4])))
        for section, weight in SECTION_WEIGHTS:
            tokens = set(tokenize(inc['sections'].get(section, '')))
            hit = qset & tokens
            if hit:
                score += weight * len(hit) / float(len(qset))
                if weight >= 2.0:
                    reasons.append('%s: %s' % (section.lower(), ', '.join(sorted(hit)[:4])))

    if score <= 0:
        return None

    status = str(meta.get('status', '')).lower()
    if status == 'resolved':
        score *= 1.15
    elif status == 'wontfix':
        score *= 0.8
    return {'score': round(score, 2), 'reasons': reasons, 'inc': inc}


def render_md(hits, out, query_desc, total):
    if not hits:
        out.write('# База знаний: совпадений нет\n\n'
                  'Запрос: %s\nВсего записей в базе: %d\n\n'
                  'Инцидент, похоже, новый — после разбора имеет смысл его записать '
                  '(`kb_add.py`).\n' % (query_desc, total))
        return
    out.write('# База знаний: найдено %d из %d\n\nЗапрос: %s\n\n' % (len(hits), total, query_desc))
    for hit in hits:
        meta = hit['inc']['meta']
        out.write('## %s — %s\n' % (meta.get('id'), meta.get('title')))
        bits = []
        for key, label in (('date', ''), ('status', 'status'), ('severity', 'severity')):
            if meta.get(key):
                bits.append('%s%s' % (label + ' ' if label else '', meta[key]))
        for key in ('stands', 'services', 'tags'):
            if meta.get(key):
                bits.append('%s: %s' % (key, ', '.join(str(v) for v in meta[key])))
        out.write('- %s\n' % ' · '.join(bits))
        out.write('- релевантность: %.1f — %s\n' % (hit['score'], '; '.join(hit['reasons'][:4])))
        for section in ('Симптомы', 'Причина', 'Решение'):
            text = hit['inc']['sections'].get(section)
            if text:
                snippet = ' '.join(text.split())[:280]
                out.write('- **%s:** %s%s\n' % (section, snippet,
                                                '…' if len(text) > 280 else ''))
        if meta.get('files'):
            out.write('- код: %s\n' % ', '.join(str(f) for f in meta['files'][:5]))
        out.write('- файл: `%s`\n\n' % hit['inc']['path'])
    out.write('Совпадение сигнатуры не доказывает ту же причину — сверь стенд, '
              'сервис и условия перед выводом.\n')


def render_json(hits, out):
    payload = [{
        'id': h['inc']['meta'].get('id'),
        'title': h['inc']['meta'].get('title'),
        'score': h['score'],
        'reasons': h['reasons'],
        'path': h['inc']['path'],
        'meta': h['inc']['meta'],
        'sections': h['inc']['sections'],
    } for h in hits]
    json.dump(payload, out, ensure_ascii=False, indent=2)
    out.write('\n')


def render_list(incidents, out):
    out.write('# База знаний: %d записей\n\n' % len(incidents))
    for inc in sorted(incidents, key=lambda i: str(i['meta'].get('date', '')), reverse=True):
        meta = inc['meta']
        out.write('- **%s** %s — %s [%s]\n' % (
            meta.get('id'), meta.get('date', ''), meta.get('title'),
            ', '.join(str(t) for t in (meta.get('tags') or []))))


def main(argv=None):
    ap = argparse.ArgumentParser(description='Поиск похожих инцидентов в локальной базе знаний.')
    ap.add_argument('query', nargs='*', help='слова симптома')
    ap.add_argument('--from-parsed', help='JSON-вывод parse_logs.py — искать по сигнатурам')
    ap.add_argument('--signature', action='append', default=[],
                    help='явная сигнатура (можно повторять)')
    ap.add_argument('--stand', action='append', help='фильтр по стенду')
    ap.add_argument('--service', action='append', help='фильтр по сервису')
    ap.add_argument('--tag', action='append', help='фильтр по тегу')
    ap.add_argument('--top', type=int, default=5)
    ap.add_argument('--min-score', type=float, default=1.0)
    ap.add_argument('--kb', help='директория базы знаний')
    ap.add_argument('--list', action='store_true', help='показать все записи')
    ap.add_argument('--format', choices=['md', 'json'], default='md')
    args = ap.parse_args(argv)

    directory = kb_dir(args.kb)
    incidents = load_incidents(directory)
    out = sys.stdout

    if not incidents:
        out.write('База знаний пуста или не найдена: %s\n'
                  'Первую запись можно создать через kb_add.py\n' % directory)
        return 0

    if args.list:
        render_list(incidents, out)
        return 0

    signatures = list(args.signature)
    query_parts = list(args.query)
    if args.from_parsed:
        parsed = load_parsed(args.from_parsed)
        signatures.extend(signatures_from_parsed(parsed))
        for exc in list(parsed.get('stats', {}).get('exceptions', {}))[:5]:
            query_parts.append(exc)
        for svc in list(parsed.get('stats', {}).get('services', {}))[:3]:
            query_parts.append(svc)

    if not signatures and not query_parts:
        ap.error('нужен текст запроса, --signature или --from-parsed')

    query_tokens = tokenize(' '.join(query_parts))
    filters = {'stand': args.stand, 'service': args.service, 'tag': args.tag}

    hits = []
    for inc in incidents:
        result = score_incident(inc, query_tokens, signatures, filters)
        if result and result['score'] >= args.min_score:
            hits.append(result)
    hits.sort(key=lambda h: -h['score'])
    hits = hits[:args.top]

    desc = ' '.join(query_parts[:8]) or '(по сигнатурам)'
    if signatures:
        desc += ' + %d сигнатур' % len(signatures)

    if args.format == 'json':
        render_json(hits, out)
    else:
        render_md(hits, out, desc, len(incidents))
    return 0


if __name__ == '__main__':
    sys.exit(main())
