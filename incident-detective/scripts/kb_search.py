#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Поиск по локальной базе знаний инцидентов.

Ищет либо по свободному тексту, либо по сигнатурам из разбора логов
(`parse_logs.py --format json`). Совпадение сигнатуры весит больше всего.
"""

import argparse
import io
import json
import os
import subprocess
import sys
from datetime import datetime

# Без f-строк намеренно: на старом интерпретаторе должно печататься сообщение, а не SyntaxError.
if sys.version_info < (3, 8):
    sys.stderr.write('incident-detective: нужен Python 3.8 или новее, запущен %s (%s)\n'
                     % (sys.version.split()[0], sys.executable))
    sys.exit(2)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kb_common import (  # noqa: E402
    DISTINGUISHERS_SECTION, MAX_SUMMARY_CHARS, OUTCOME_CONFIRMED, OUTCOME_REFUTED,
    OUTCOME_UNVERIFIED, kb_dir, load_incidents_fast, load_parsed, now, outcome_of, run_script,
    signature_similarity, signatures_from_parsed, tokenize,
)

OUTCOME_LABELS = {
    OUTCOME_CONFIRMED: 'подтверждена',
    OUTCOME_REFUTED: 'ОПРОВЕРГНУТА',
    OUTCOME_UNVERIFIED: 'не проверена',
}

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


def record_age_days(meta):
    """Возраст записи в днях от `date` до «сейчас» — None, если дата не разобралась."""
    raw = str(meta.get('date') or '').strip()
    if not raw:
        return None
    try:
        recorded = datetime.strptime(raw, '%Y-%m-%d')
    except ValueError:
        return None
    return max(0, (now() - recorded).days)


def files_changed_since(repo, files, date):
    """Менялся ли хоть один из `files` в `repo` после `date`.

    Возвращает True/False, либо None, если проверить нечем (нет репозитория, нет
    даты, нет файлов, git недоступен). Устаревание не вычисляется — только
    сообщается факт, решение остаётся за человеком.
    """
    if not repo or not files or not date:
        return None
    try:
        datetime.strptime(str(date), '%Y-%m-%d')
    except ValueError:
        return None
    for rel in files:
        try:
            proc = subprocess.run(
                ['git', '-C', repo, 'log', '-1', '--format=%cd', '--date=short',
                 '--since=%s 23:59:59' % date, '--', str(rel)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        if proc.stdout.decode('utf-8', 'replace').strip():
            return True
    return False


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


def render_md(hits, out, query_desc, total, budget=MAX_SUMMARY_CHARS, repo=None):
    if not hits:
        out.write('# База знаний: совпадений нет\n\n'
                  'Запрос: %s\nВсего записей в базе: %d\n\n'
                  'Инцидент, похоже, новый — после разбора имеет смысл его записать '
                  '(`kb_add.py`).\n' % (query_desc, total))
        return
    # выдача едет в контекст агента: показываем сколько влезает, а не сколько нашлось
    shown = hits
    if budget > 0:
        shown = []
        used = 0
        for hit in hits:
            buf = io.StringIO()
            render_hit(hit, buf, repo=repo)
            used += len(buf.getvalue())
            if used > budget and shown:
                break
            shown.append(hit)
    out.write('# База знаний: найдено %d из %d\n\nЗапрос: %s\n\n'
              % (len(hits), total, query_desc))
    for hit in shown:
        render_hit(hit, out, repo=repo)
    if len(shown) < len(hits):
        out.write('_Показано %d записей из %d — выдача ограничена по объёму. '
                  'Полный список: `--format json`._\n\n' % (len(shown), len(hits)))
    out.write('Совпадение сигнатуры не доказывает ту же причину — сверь стенд, '
              'сервис и условия перед выводом.\n')


def render_hit(hit, out, repo=None):
    meta = hit['inc']['meta']
    outcome = outcome_of(meta)
    out.write('## %s — %s\n' % (meta.get('id'), meta.get('title')))
    bits = []
    for key, label in (('date', ''), ('status', 'status'), ('severity', 'severity')):
        if meta.get(key):
            bits.append('%s%s' % (label + ' ' if label else '', meta[key]))
    for key in ('stands', 'services', 'tags'):
        if meta.get(key):
            bits.append('%s: %s' % (key, ', '.join(str(v) for v in meta[key])))
    out.write('- %s\n' % ' · '.join(bits))
    out.write('- исход: %s\n' % OUTCOME_LABELS[outcome])
    if outcome == OUTCOME_REFUTED:
        out.write('- **эта версия причины уже проверялась и не подтвердилась** — '
                  'не принимай её как готовый ответ\n')
    age = record_age_days(meta)
    extra_bits = []
    if age is not None:
        extra_bits.append('возраст: %d дн.' % age)
    reuse_count = meta.get('reuse_count')
    try:
        reuse_count = int(reuse_count) if reuse_count not in (None, '') else 0
    except (TypeError, ValueError):
        reuse_count = 0
    if reuse_count:
        extra_bits.append('переиспользована: %d раз, последний раз %s'
                          % (reuse_count, meta.get('reused_at') or '?'))
    if extra_bits:
        out.write('- %s\n' % ' · '.join(extra_bits))
    out.write('- релевантность: %.1f — %s\n' % (hit['score'], '; '.join(hit['reasons'][:4])))
    for section in ('Симптомы', 'Причина', 'Решение'):
        text = hit['inc']['sections'].get(section)
        if text:
            snippet = ' '.join(text.split())[:280]
            out.write('- **%s:** %s%s\n' % (section, snippet,
                                            '…' if len(text) > 280 else ''))
    distinguishers = hit['inc']['sections'].get(DISTINGUISHERS_SECTION)
    if distinguishers:
        out.write('- **отличительные признаки:** %s\n'
                  % ' '.join(distinguishers.split())[:280])
    if meta.get('files'):
        out.write('- код: %s\n' % ', '.join(str(f) for f in meta['files'][:5]))
        changed = files_changed_since(repo, meta.get('files'), meta.get('date'))
        if changed is not None:
            out.write('- код менялся после записи: %s — сведение к проверке, '
                      'не вывод об устаревании\n' % ('да' if changed else 'нет'))
    out.write('- файл: `%s`\n\n' % hit['inc']['path'])


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
    ap.add_argument('--repo', help='репозиторий — сверить, менялись ли файлы записи после её даты')
    ap.add_argument('--list', action='store_true', help='показать все записи')
    ap.add_argument('--format', choices=['md', 'json'], default='md')
    args = ap.parse_args(argv)

    directory = kb_dir(args.kb)
    # поиск вызывается на каждом разборе, а база растёт: читаем индекс, а не
    # тысячу markdown-файлов подряд
    incidents, warning = load_incidents_fast(directory)
    out = sys.stdout
    if warning:
        sys.stderr.write('warning: %s\n' % warning)

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
    # id вторым ключом: при равных баллах порядок не должен зависеть от того,
    # прочитаны записи из индекса или из markdown — иначе один и тот же запрос
    # даёт разные ответы. Опровергнутые записи — отдельной группой ниже
    # подтверждённых и непроверенных: релевантность (score) при этом не меняется,
    # переупорядочивается только положение в выдаче.
    hits.sort(key=lambda h: (outcome_of(h['inc']['meta']) == OUTCOME_REFUTED,
                             -h['score'], str(h['inc']['meta'].get('id') or '')))
    hits = hits[:args.top]

    desc = ' '.join(query_parts[:8]) or '(по сигнатурам)'
    if signatures:
        desc += ' + %d сигнатур' % len(signatures)

    if args.format == 'json':
        render_json(hits, out)
    else:
        render_md(hits, out, desc, len(incidents), repo=args.repo)
    return 0


if __name__ == '__main__':
    sys.exit(run_script(main, __file__))
