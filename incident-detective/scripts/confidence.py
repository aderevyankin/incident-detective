#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Оценка уверенности в выводе по инциденту.

Считает, на скольких независимых опорах стоит вывод: логи, база знаний, код,
сквозная цепочка запроса. Совпадение по одному контуру — гипотеза; сошлись
три — вывод. Число нужно не ради числа, а чтобы отчёт не выдавал догадку за
доказанное и прямо говорил, какой опоры не хватает.

  python3 parse_logs.py app.log --format json > /tmp/parsed.json
  python3 kb_search.py --from-parsed /tmp/parsed.json --format json > /tmp/kb.json
  python3 code_hints.py --from-parsed /tmp/parsed.json --repo . --format json > /tmp/code.json
  python3 confidence.py --parsed /tmp/parsed.json --kb /tmp/kb.json --code /tmp/code.json \
      --stand stage --service payment-api

Веса подобраны так, что ни один контур в одиночку не даёт «подтверждено»:
максимум одного контура — 0.30. Это осознанно: скилл диагностирует, а не
убеждает.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_common import require_python; require_python()  # noqa: E402

from kb_common import LEVEL_ORD, dump_json, run_script  # noqa: E402

WEIGHTS = [
    ('logs', 'Логи', 0.30),
    ('code', 'Код', 0.30),
    ('kb', 'База знаний', 0.25),
    ('trace', 'Цепочка запроса', 0.15),
]

CONFIRMED = 0.70
LIKELY = 0.40

# скор kb_search, при котором совпадение считается уверенным
KB_STRONG_SCORE = 20.0
KB_WEAK_SCORE = 3.0


def load_json(path):
    if not path:
        return None
    with open(path, 'r', encoding='utf-8') as fh:
        return json.load(fh)


def clamp(val):
    return max(0.0, min(1.0, val))


def plural(count, one, few, many):
    """'2 сервиса' вместо '2 сервиса(ов)' — вывод читают люди."""
    if count % 10 == 1 and count % 100 != 11:
        word = one
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        word = few
    else:
        word = many
    return '%d %s' % (count, word)


# --------------------------------------------------------------------------
# Контуры
# --------------------------------------------------------------------------


def score_logs(parsed):
    """Дают ли логи воспроизводимый отпечаток ошибки."""
    if parsed is None:
        return None
    parts, notes = 0.0, []

    sigs = [s for s in (parsed.get('signatures') or []) if s.get('value')]
    if sigs:
        parts += 0.4
        notes.append('сигнатур: %d (%s)' % (len(sigs), str(sigs[0].get('value'))[:60]))
    else:
        notes.append('сигнатур нет — ошибка не опознана как устойчивый шаблон')

    groups = parsed.get('groups') or []
    errs = [g for g in groups
            if LEVEL_ORD.get(g.get('level'), 2) >= LEVEL_ORD['ERROR']]
    top = max((g.get('count', 0) for g in errs), default=0)
    if top >= 3:
        parts += 0.3
        notes.append('повторяющаяся ошибка: %d× «%s»'
                     % (top, str(errs[0].get('template', ''))[:60]))
    elif errs:
        parts += 0.15
        notes.append('ошибка единичная (%d×) — совпадение не исключено' % top)
    else:
        notes.append('записей уровня ERROR не найдено')

    hist = parsed.get('histogram') or {}
    if len(hist) >= 3:
        vals = list(hist.values())
        peak, avg = max(vals), sum(vals) / float(len(vals))
        if peak >= max(5, avg * 3):
            parts += 0.3
            notes.append('всплеск: пик %d при фоне ~%.1f' % (peak, avg))
        else:
            parts += 0.1
            notes.append('ошибки размазаны по времени, выраженного всплеска нет')
    else:
        notes.append('гистограммы нет — окно инцидента по логам не очерчено')

    return {'value': clamp(parts), 'notes': notes}


def score_kb(kb, stand, service):
    """Видели ли мы это раньше — и тот ли это случай."""
    if kb is None:
        return None
    # kb_search.py --format json всегда выдаёт список — раньше здесь
    # оставалась мёртвая ветка на случай словаря с ключом 'hits', которого
    # ни один вызывающий код никогда не производил
    hits = kb or []
    if not hits:
        return {'value': 0.0, 'notes': ['совпадений в базе нет — инцидент выглядит новым'],
                'warnings': []}

    best = hits[0]
    raw = float(best.get('score') or 0)
    span = KB_STRONG_SCORE - KB_WEAK_SCORE
    val = clamp((raw - KB_WEAK_SCORE) / span) if span > 0 else 0.0
    notes = ['%s «%s», релевантность %.1f'
             % (best.get('id'), str(best.get('title'))[:70], raw)]
    reasons = best.get('reasons') or []
    if reasons:
        notes.append('совпало по: %s' % '; '.join(str(r) for r in reasons[:3]))

    warnings = []
    meta = best.get('meta') or {}
    for key, expected, label in (('stands', stand, 'стенд'), ('services', service, 'сервис')):
        if not expected:
            continue
        have = {str(v).lower() for v in (meta.get(key) or [])}
        if have and expected.lower() not in have:
            val *= 0.6
            warnings.append('в записи %s другой %s (%s), совпадение может быть внешним'
                            % (best.get('id'), label, ', '.join(sorted(have))))
    return {'value': clamp(val), 'notes': notes, 'warnings': warnings}


def score_code(code):
    """Дотянулись ли до конкретного места в репозитории."""
    if code is None:
        return None
    parts, notes, warnings = 0.0, [], []

    frames = code.get('frames') or []
    resolved = [f for f in frames if f.get('matches')]
    if resolved:
        parts += 0.5
        notes.append('стектрейс лёг на файлы проекта: %s'
                     % ', '.join(str(f['matches'][0]) for f in resolved[:3]))
    elif frames:
        notes.append('фреймы есть, но ни один не нашёлся в репозитории — '
                     'ошибка, похоже, во внешней библиотеке')
    else:
        notes.append('стектрейсов в данных нет')

    grep = code.get('grep') or []
    if grep:
        parts += 0.2
        notes.append('текст ошибки найден в коде: %s'
                     % plural(len(grep), 'место', 'места', 'мест'))

    commits = code.get('commits') or []
    touched = set()
    for frame in resolved:
        blame = frame.get('blame') or {}
        if blame.get('hash'):
            touched.add(str(blame['hash'])[:7])
        for item in frame.get('history') or []:
            if item.get('hash'):
                touched.add(str(item['hash'])[:7])
    linked = [c for c in commits if str(c.get('hash', ''))[:7] in touched]
    if linked:
        parts += 0.3
        notes.append('коммит в окне трогает найденный файл: %s — %s'
                     % (linked[0].get('hash'), str(linked[0].get('subject'))[:60]))
    elif commits:
        parts += 0.05
        warnings.append('коммиты в окне есть (%d), но ни один не трогает найденные '
                        'файлы — совпадение по времени само по себе не доказательство'
                        % len(commits))
    elif code.get('git'):
        notes.append('коммитов в окне инцидента нет — версия кода не менялась')

    return {'value': clamp(parts), 'notes': notes, 'warnings': warnings}


def score_trace(trace):
    """Видно ли, на каком сервисе цепочка оборвалась."""
    if trace is None:
        return None
    parts, notes = 0.0, []
    hops = trace.get('hops') or []
    if len(hops) >= 2:
        parts += 0.5
        notes.append('цепочка собрана через %s: %s'
                     % (plural(len(hops), 'сервис', 'сервиса', 'сервисов'),
                        ' → '.join(str(h.get('service')) for h in hops)))
    elif hops:
        notes.append('цепочка внутри одного сервиса — межсервисной картины нет')
    else:
        notes.append('цепочка не собрана')

    first = trace.get('first_error')
    if first:
        parts += 0.5
        notes.append('первая ошибка: %s в %s' % (first.get('service'), first.get('ts')))
    elif hops:
        notes.append('ошибок в цепочке нет — возможно, взят не тот запрос')
    return {'value': clamp(parts), 'notes': notes}


MISSING_HINT = {
    'logs': 'разобрать логи: `parse_logs.py <лог> --format json > /tmp/parsed.json`',
    'kb': 'спросить базу: `kb_search.py --from-parsed /tmp/parsed.json --format json > /tmp/kb.json`',
    'code': 'связать с кодом: `code_hints.py --from-parsed /tmp/parsed.json --repo . --format json > /tmp/code.json`',
    'trace': 'собрать цепочку: `trace.py --log сервис=лог --id <correlation id> --format json > /tmp/trace.json`',
}

# контур проверен, но опоры дал мало — что именно докрутить
WEAK_HINT = {
    'logs': 'единичная ошибка без повторов и всплеска мало что доказывает — '
            'расширь окно (`--since/--until`) или добавь логи остальных реплик',
    'kb': 'похожего случая в базе нет — по итогам разбора запиши этот '
          '(`kb_add.py --from-parsed ...`), чтобы в следующий раз опора была',
    'code': 'до места в коде не дотянулись — нужен стектрейс целиком '
            '(`code_hints.py --text "..."`) и репозиторий той же версии, что на стенде',
    'trace': 'цепочка неполная — добавь логи соседних сервисов (`--log сервис=лог`) '
             'и проверь, что correlation id проходит через все из них',
}


def combine(scores):
    """Взвешенная сумма по доступным контурам.

    Отсутствующий контур не перераспределяет свой вес на остальные: вывод,
    сделанный по одним логам, и не должен дотягивать до «подтверждено».
    """
    total = 0.0
    rows = []
    for key, label, weight in WEIGHTS:
        item = scores.get(key)
        value = item['value'] if item else None
        contrib = (value or 0.0) * weight
        total += contrib
        rows.append({'key': key, 'label': label, 'weight': weight,
                     'value': value, 'contribution': round(contrib, 3),
                     'notes': (item or {}).get('notes', []),
                     'warnings': (item or {}).get('warnings', [])})
    return clamp(total), rows


def verdict(total):
    if total >= CONFIRMED:
        return 'подтверждено данными'
    if total >= LIKELY:
        return 'вероятная причина'
    return 'гипотеза'


def render_md(total, rows, claim, out):
    w = out.write
    w('# Уверенность в выводе: %.2f — %s\n\n' % (total, verdict(total)))
    if claim:
        w('> %s\n\n' % claim)

    w('| Контур | Вес | Оценка | Вклад |\n|---|---|---|---|\n')
    for row in rows:
        val = '—' if row['value'] is None else '%.2f' % row['value']
        w('| %s | %.2f | %s | %.2f |\n' % (row['label'], row['weight'], val,
                                           row['contribution']))
    w('\n')

    for row in rows:
        if row['value'] is None:
            continue
        w('**%s — %.2f**\n' % (row['label'], row['value']))
        for note in row['notes']:
            w('- %s\n' % note)
        w('\n')

    warnings = [(r['label'], t) for r in rows for t in r['warnings']]
    if warnings:
        w('## Что настораживает\n\n')
        for label, text in warnings:
            w('- **%s:** %s\n' % (label, text))
        w('\n')

    missing = [r for r in rows if r['value'] is None]
    weak = [r for r in rows if r['value'] is not None and r['value'] < 0.5]
    if missing or weak:
        w('## Что поднимет уверенность\n\n')
        for row in missing:
            w('- **%s** — контур не проверялся: %s\n' % (row['label'], MISSING_HINT[row['key']]))
        for row in weak:
            w('- **%s** (%.2f) — %s\n'
              % (row['label'], row['value'], WEAK_HINT[row['key']]))
        w('\n')

    if total >= CONFIRMED:
        w('Опор достаточно: причину можно излагать как установленную, '
          'приложив к ней перечисленные доказательства.\n')
    elif total >= LIKELY:
        w('Формулируй как вероятную причину и назови, чего не хватает для '
          'подтверждения. Не выдавай за факт.\n')
    else:
        w('Это гипотеза. В отчёте так и пиши: «предполагаю», а следом — что нужно '
          'проверить, чтобы подтвердить или отбросить.\n')


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Оценка уверенности в выводе по числу сошедшихся контуров.')
    ap.add_argument('--parsed', help='JSON parse_logs.py')
    ap.add_argument('--kb', help='JSON kb_search.py --format json')
    ap.add_argument('--code', help='JSON code_hints.py --format json')
    ap.add_argument('--trace', help='JSON trace.py --id ... --format json')
    ap.add_argument('--stand', help='стенд текущего инцидента — сверить с записью базы')
    ap.add_argument('--service', help='сервис текущего инцидента — сверить с записью базы')
    ap.add_argument('--claim', help='формулировка вывода, которую оцениваем')
    ap.add_argument('--format', choices=['md', 'json'], default='md')
    args = ap.parse_args(argv)

    if not any((args.parsed, args.kb, args.code, args.trace)):
        ap.error('нужен хотя бы один артефакт: --parsed, --kb, --code или --trace')

    scores = {
        'logs': score_logs(load_json(args.parsed)),
        'kb': score_kb(load_json(args.kb), args.stand, args.service),
        'code': score_code(load_json(args.code)),
        'trace': score_trace(load_json(args.trace)),
    }
    total, rows = combine(scores)

    if args.format == 'json':
        dump_json({'confidence': round(total, 3), 'verdict': verdict(total),
                  'claim': args.claim, 'contours': rows}, sys.stdout)
    else:
        render_md(total, rows, args.claim, sys.stdout)
    return 0


if __name__ == '__main__':
    sys.exit(run_script(main, __file__))
