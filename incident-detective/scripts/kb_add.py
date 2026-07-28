#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Добавление или дополнение записи в локальной базе знаний инцидентов."""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_common import require_python; require_python()  # noqa: E402

from kb_common import (  # noqa: E402
    DEFAULT_KB, DISTINGUISHERS_SECTION, ENV_KB, KB_DEFAULT, KB_ENV, KB_FLAG, KB_PROJECT,
    KIND_INCIDENT, KIND_SOURCE, KINDS, LEVELS, OUTCOME_UNVERIFIED, OUTCOMES, SECTIONS,
    VERDICTS, dump_frontmatter, is_auto, kb_is_empty, kind_of, load_incidents, load_parsed,
    merge_scrub_counts, next_id, norm_signature, now, project_kb_dir, read_report,
    render_scrub_summary, resolve_kb, run_script, scrub_text, signatures_from_parsed,
    slugify, source_entry_id, write_report,
)

# Код возврата «расположение базы не выбрано»: отличается и от ошибки аргументов
# (2), и от обычного отказа (1), чтобы агент опознал его без разбора текста.
RC_KB_NOT_CHOSEN = 3


def scrub(text, counts=None):
    """Вырезает секреты и персональные данные перед записью в базу.

    Правила и сама функция очистки живут в `kb_common.scrub_text` — общие для
    всех путей, которыми текст разбора покидает разбор. `counts` — словарь,
    куда суммируются срабатывания по видам за весь запуск, если передан.
    """
    if not text:
        return text
    clean, found = scrub_text(text)
    if counts is not None:
        merge_scrub_counts(counts, found)
    return clean


def find_by_signature(incidents, signatures):
    """Запись базы с той же сигнатурой или None.

    Флапающий алерт за ночь порождает десятки одинаковых разборов: без этого
    поиска каждый из них завёл бы свою запись, и база превратилась бы в ленту
    повторов вместо знания.
    """
    wanted = {norm_signature(s) for s in signatures if norm_signature(s)}
    if not wanted:
        return None
    for inc in incidents:
        known = {norm_signature(s) for s in inc['meta'].get('signatures') or []}
        if wanted & known:
            return inc
    return None


def split_csv(values):
    out = []
    for value in values or []:
        out.extend([p.strip() for p in str(value).split(',') if p.strip()])
    return out


def merge_list(existing, new):
    seen = {str(v).lower(): v for v in (existing or [])}
    for item in new:
        if str(item).lower() not in seen:
            seen[str(item).lower()] = item
    return list(seen.values())


def merge_signatures(existing, new):
    known = {norm_signature(s): s for s in (existing or [])}
    for sig in new:
        key = norm_signature(sig)
        if key and key not in known:
            known[key] = sig
    return list(known.values())


def build_body(sections):
    parts = []
    for name in SECTIONS:
        # раздел с отличительными признаками — единственный необязательный:
        # плейсхолдером не заполняется, отсутствие признаков не создаёт
        # пустого раздела
        if name == 'Заметки':
            diff = sections.get(DISTINGUISHERS_SECTION, '').strip()
            if diff:
                parts.append('## %s\n\n%s\n' % (DISTINGUISHERS_SECTION, diff))
        text = sections.get(name, '').strip()
        parts.append('## %s\n\n%s\n' % (name, text if text else '_не заполнено_'))
    return '\n'.join(parts)


def note_in_report(report, entry):
    """Проставляет в отчёте разбора, что стало с записью базы знаний.

    Отчёт читает обвязка, а не человек в чате: «запись не сделана и почему» там
    должно быть значением поля, а не отсутствием строки.
    """
    if not report:
        return
    payload = read_report(report)
    if payload is None:
        sys.stderr.write('warning: отчёт %s не прочитан — отметку о записи базы '
                         'знаний проставить некуда\n' % report)
        return
    payload['kb_entry'] = entry
    if not write_report(payload, report):
        sys.stderr.write('warning: отчёт %s не сохранён\n' % report)


def store(text, path, directory, kb_source, lines, action, scrub_counts, dry_run,
          report=None, entry_id=None):
    """Общий хвост записи: предпросмотр либо файл, отчёт и пересборка индекса."""
    if dry_run:
        # Текст здесь уже очищен — ровно то, что записалось бы. Отдельного
        # прохода очистки для предпросмотра нет: dry-run не должен показывать
        # то, чего запись на самом деле не сохранит.
        sys.stdout.write(text)
        sys.stdout.write('\n---\n[dry-run] записалось бы в %s\n' % path)
        summary = render_scrub_summary(scrub_counts)
        if summary:
            sys.stdout.write('%s\n' % summary)
        return 0

    os.makedirs(directory, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)

    print('Запись %s: %s' % (action, path))
    if kb_source == KB_PROJECT:
        # Путь не задавали ни флагом, ни переменной — база нашлась в репозитории.
        # Директорию мог завести коллега, и запись в общий репозиторий не должна
        # пройти незамеченной.
        print('база знаний найдена в корне репозитория: %s' % directory)
    for line in lines:
        print(line)
    summary = render_scrub_summary(scrub_counts)
    if summary:
        # Молчаливая очистка плоха с двух сторон: не заметишь ни лишней
        # маскировки нужного идентификатора, ни того, что персональные данные
        # вообще были в тексте.
        print(summary)

    note_in_report(report, {'written': True, 'id': entry_id, 'path': path,
                            'action': action, 'reason': None})

    try:
        import kb_index
        kb_index.rebuild(directory)
        print('index.json обновлён')
    except Exception as exc:                                # noqa: BLE001
        print('warning: индекс не обновлён: %s' % exc, file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# Карта источников
# --------------------------------------------------------------------------

# Строка похожа на выгруженный лог, а не на адресацию: время с уровнем рядом
# либо несколько строк подряд. Карта хранит, куда идти за логами, а не сами
# логи — фрагмент выгрузки в ней и бесполезен, и опасен (в него утекают данные).
_LOG_TIME_RE = re.compile(r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}')
_LOG_LEVEL_RE = re.compile(r'\b(%s)\b' % '|'.join(LEVELS))


def reject_log_fragment(value, where):
    """Прерывает запись, если в поле карты приехал кусок выгруженных логов."""
    text = str(value or '')
    if not text:
        return
    if len([ln for ln in text.split('\n') if ln.strip()]) > 1:
        raise SystemExit(
            'В поле %s несколько строк — похоже на выдержку из логов. Карта хранит '
            'адресацию, а не данные: оставь способ обращения, адрес и запрос.' % where)
    if _LOG_TIME_RE.search(text) and _LOG_LEVEL_RE.search(text):
        raise SystemExit(
            'В поле %s строка лога (время и уровень) — в карту она не пишется. '
            'Карта хранит адресацию, а не данные.' % where)


def parse_pairs(values, flag):
    """`--flag ключ=значение` (можно повторять и перечислять через запятую)."""
    out = {}
    for item in values or []:
        for part in str(item).split(','):
            part = part.strip()
            if not part:
                continue
            key, sep, val = part.partition('=')
            if not sep or not key.strip():
                raise SystemExit('%s задаётся как "ключ=значение": %r' % (flag, part))
            out[key.strip()] = val.strip()
    return out


def source_body(stand, services):
    return ('# Карта источников: %s / %s\n\n'
            'Откуда берутся логи для этой пары стенда и сервиса. Запись — адресация,\n'
            'а не кэш: самих логов здесь нет, за ними надо сходить в источник.\n'
            % (stand, ', '.join(services) or '—'))


def add_source(args, directory, entries, kb_source):
    """Создание, обновление и пометка записи карты источников."""
    for name, value in (('--symptoms', args.symptoms), ('--diagnosis', args.diagnosis),
                        ('--root-cause', args.root_cause), ('--fix', args.fix),
                        ('--verify', args.verify), ('--notes', args.notes),
                        ('--distinguishing', args.distinguishing)):
        if value:
            raise SystemExit('%s — поле разбора инцидента, в запись карты оно не пишется. '
                             'У карты фиксированный набор полей: стенд, сервис, источник, '
                             'адрес, запрос, соответствие полей.' % name)

    stands = split_csv(args.stand)
    services = split_csv(args.service)
    scrub_counts = {}

    target = None
    if args.update:
        for entry in entries:
            if str(entry['meta'].get('id', '')).lower() == args.update.lower():
                target = entry
                break
        if target is None:
            raise SystemExit('Запись %s не найдена в %s' % (args.update, directory))
        if kind_of(target['meta']) != KIND_SOURCE:
            raise SystemExit('%s — разбор инцидента, а не запись карты: '
                             'обнови её без --kind source' % target['meta'].get('id'))
        stand = stands[0] if stands else str(target['meta'].get('stand') or '')
    else:
        if len(stands) > 1:
            raise SystemExit('Запись карты описывает один стенд — источники стендов '
                             'записываются отдельно')
        if not stands or not services:
            raise SystemExit('Для записи карты нужны --stand и --service: карта '
                             'адресует пару «стенд + сервис»')
        stand = stands[0]
        entry_id = source_entry_id(stand, services[0])
        for entry in entries:
            if str(entry['meta'].get('id', '')).lower() == entry_id.lower():
                # та же пара уже описана — обновляем её, дубля не заводим
                target = entry
                break

    source = parse_pairs(args.source, '--source')
    address = parse_pairs(args.address, '--address')
    fields = parse_pairs(args.fields, '--fields')
    if target is None and not source and not args.mark_checked:
        raise SystemExit('Для новой записи карты нужен --source: чем и как брались логи '
                         '(например --source kind=mcp --source server=kibana-mcp '
                         '--source tool=search_logs)')

    for key, value in list(source.items()) + list(address.items()) + list(fields.items()):
        reject_log_fragment(value, '--source/--address/--fields (%s)' % key)
    reject_log_fragment(args.query, '--query')
    reject_log_fragment(args.note, '--note')

    source = {k: scrub(v, scrub_counts) for k, v in source.items()}
    address = {k: scrub(v, scrub_counts) for k, v in address.items()}
    fields = {k: scrub(v, scrub_counts) for k, v in fields.items()}
    query = scrub(args.query, scrub_counts) if args.query else None

    when = now()
    date = args.date or when.strftime('%Y-%m-%d')

    if target is None:
        meta = {'id': source_entry_id(stand, services[0]), 'kind': KIND_SOURCE,
                'stand': stand, 'services': services}
    else:
        meta = dict(target['meta'])
        meta['kind'] = KIND_SOURCE
        # заголовок у записи карты не хранится: он выводится из пары стенда и
        # сервиса и заново пишется в тело. `load_incidents` подставляет его при
        # чтении — во фронтматтер он попадать не должен
        meta.pop('title', None)
        if stand:
            meta['stand'] = stand
        meta['services'] = merge_list(meta.get('services'), services)

    for key, value in (('source', source), ('address', address), ('fields', fields)):
        if value:
            merged = dict(meta.get(key) or {}) if isinstance(meta.get(key), dict) else {}
            merged.update(value)
            meta[key] = merged
    if query:
        meta['query'] = query

    touched = bool(source or address or fields or query)
    if args.mark_checked:
        mark = {'source': args.mark_checked, 'verdict': args.verdict, 'date': date}
        note = scrub(args.note, scrub_counts) if args.note else None
        if note:
            mark['note'] = note
        marks = [m for m in (meta.get('checked') or []) if isinstance(m, dict)]
        # повторная проверка того же инструмента заменяет прежнюю пометку, а не
        # ложится рядом: иначе по перечню не понять, что верно сейчас
        marks = [m for m in marks
                 if str(m.get('source') or '').lower() != str(args.mark_checked).lower()]
        marks.append(mark)
        meta['checked'] = marks
    if touched or not args.mark_checked:
        # подтверждение — это состоявшаяся выгрузка; пометка бесполезного
        # источника ничего не подтверждает и дату подтверждения не двигает
        meta['confirmed'] = date

    text = dump_frontmatter(meta) + '\n\n' + source_body(meta.get('stand'),
                                                         meta.get('services') or [])
    path = target['path'] if target is not None else os.path.join(directory,
                                                                  '%s.md' % meta['id'])
    lines = ['id: %s' % meta['id']]
    if args.mark_checked:
        lines.append('источник %s помечен как %s' % (args.mark_checked, args.verdict))
    action = 'обновлена' if target is not None else 'создана'
    return store(text, path, directory, kb_source, lines, action, scrub_counts,
                 args.dry_run, report=args.report, entry_id=meta['id'])


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Создать или дополнить запись базы знаний по инциденту.')
    ap.add_argument('--kind', choices=KINDS, default=KIND_INCIDENT,
                    help='вид записи: разбор инцидента (по умолчанию) или карта источников')
    ap.add_argument('--title', help='заголовок: суть + причина')
    ap.add_argument('--stand', action='append', help='стенд (можно повторять или через запятую)')
    ap.add_argument('--service', action='append', help='сервис/компонент')
    ap.add_argument('--tags', action='append', help='теги через запятую')
    ap.add_argument('--symptoms', help='что видел пользователь, его словами')
    ap.add_argument('--diagnosis', help='как искали причину')
    ap.add_argument('--root-cause', help='причина')
    ap.add_argument('--fix', help='что сделали')
    ap.add_argument('--verify', help='как проверить, что починилось')
    ap.add_argument('--distinguishing',
                    help='отличительный признак: проверяемое наблюдение, по которому '
                         'видно, что текущий случай не этот, несмотря на совпадение сигнатуры')
    ap.add_argument('--notes', help='ссылки, тикеты, грабли')
    ap.add_argument('--severity', choices=['low', 'medium', 'high', 'critical'])
    ap.add_argument('--status', choices=['resolved', 'workaround', 'open', 'wontfix'],
                    default='resolved')
    ap.add_argument('--outcome', choices=OUTCOMES,
                    help='исход разбора: confirmed/refuted/unverified '
                         '(по умолчанию для новой записи — unverified)')
    ap.add_argument('--signature', action='append', default=[], help='сигнатура вручную')
    ap.add_argument('--from-parsed', help='JSON parse_logs.py — подтянуть сигнатуры')
    ap.add_argument('--file', action='append', default=[], help='файл кода, связанный с багом')
    ap.add_argument('--commit', action='append', default=[], help='подозрительный коммит')
    ap.add_argument('--related', action='append', default=[], help='id связанного инцидента')
    ap.add_argument('--update', help='id существующей записи — дополнить её, а не создавать новую')
    # Поля записи карты источников (--kind source)
    ap.add_argument('--source', action='append',
                    help='карта: способ обращения, «ключ=значение» (kind=mcp, '
                         'server=kibana-mcp, tool=search_logs; kind=file, path=...)')
    ap.add_argument('--address', action='append',
                    help='карта: адрес внутри источника, «ключ=значение» '
                         '(index=logs-stage-*, namespace=payment)')
    ap.add_argument('--query', help='карта: сработавший запрос')
    ap.add_argument('--fields', action='append',
                    help='карта: соответствие полей канонической схеме, «ключ=значение» '
                         '(time=@timestamp, level=log.level)')
    ap.add_argument('--mark-checked',
                    help='карта: пометить названный инструмент как бесполезный для этой '
                         'пары стенда и сервиса')
    ap.add_argument('--verdict', choices=VERDICTS,
                    help='карта: чем именно бесполезен — empty (пуст) или unavailable '
                         '(недоступен)')
    ap.add_argument('--note', help='карта: причина пометки, коротко')
    ap.add_argument('--date', help='дата разбора YYYY-MM-DD (по умолчанию сегодня)')
    ap.add_argument('--kb', help='директория базы знаний')
    ap.add_argument('--report',
                    help='отчёт разбора (report.json) — проставить в нём, что стало '
                         'с записью базы знаний')
    ap.add_argument('--dry-run', action='store_true', help='показать результат, но не писать')
    args = ap.parse_args(argv)

    directory, source = resolve_kb(args.kb)

    if is_auto() and source not in (KB_FLAG, KB_ENV):
        # Спросить некого, а выбрать за пользователя, куда положить запись,
        # нельзя: база проекта и база внутри скилла — разные решения, и в
        # автономном прогоне их принимает обвязка, задав путь явно. Разбор при
        # этом состоялся, поэтому это не ошибка, а отметка в отчёте.
        reason = ('расположение базы знаний не задано (ни --kb, ни %s) — '
                  'в автономном режиме запись не создаётся' % ENV_KB)
        print('Запись в базу знаний не сделана: %s' % reason)
        note_in_report(args.report, {'written': False, 'id': None, 'path': None,
                                     'action': None, 'reason': reason})
        return 0

    if source == KB_DEFAULT and kb_is_empty(directory):
        # Расположение никто не выбирал: ни флага, ни переменной, ни базы в
        # корне репозитория, ни записей внутри скилла. Спросить скрипт не может
        # — вопрос задаёт агент, — поэтому он сообщает об этом и ничего не
        # создаёт: директория базы не должна появляться молча.
        sys.stderr.write(
            'Расположение базы знаний не выбрано — запись не сделана.\n'
            'Спросите пользователя, где держать базу, и повторите запуск с --kb:\n'
            '  в корне проекта: %s\n'
            '  в директории скилла: %s\n'
            'Выбор закрепляется строкой export %s=<путь> в профиле оболочки.\n'
            % (project_kb_dir(), DEFAULT_KB, ENV_KB))
        return RC_KB_NOT_CHOSEN

    entries = load_incidents(directory)

    if args.kind == KIND_SOURCE or args.mark_checked:
        # пометка бесполезного источника — операция над картой, и требовать к ней
        # ещё и --kind source значит заставлять писать очевидное
        if args.mark_checked and not args.verdict:
            ap.error('--mark-checked требует --verdict empty|unavailable: '
                     '«пусто» и «недоступен» — разные факты')
        if args.verdict and not args.mark_checked:
            ap.error('--verdict без --mark-checked: непонятно, какой источник помечать')
        return add_source(args, directory, entries, source)

    incidents = [e for e in entries if kind_of(e['meta']) == KIND_INCIDENT]

    # Сводка срабатываний очистки за весь запуск — по всем полям записи, а не
    # только по текстам секций: заголовок, сигнатуры и значения из
    # --from-parsed идут в базу тем же путём и чистятся так же.
    scrub_counts = {}

    signatures = [scrub(s, scrub_counts) for s in args.signature]
    services = split_csv(args.service)
    if args.from_parsed:
        parsed = load_parsed(args.from_parsed)
        signatures.extend(scrub(s, scrub_counts) for s in signatures_from_parsed(parsed))
        # 'access' — служебный компонент nginx-парсера, не сервис
        services.extend([s for s in list(parsed.get('stats', {}).get('services', {}))[:3]
                         if s.lower() != 'access'])
    services = merge_list([], services)

    title = scrub(args.title, scrub_counts) if args.title else args.title

    target = None
    if args.update:
        for inc in incidents:
            if str(inc['meta'].get('id', '')).lower() == args.update.lower():
                target = inc
                break
        if target is None:
            raise SystemExit('Запись %s не найдена в %s' % (args.update, directory))
        if kind_of(target['meta']) == KIND_SOURCE:
            raise SystemExit('%s — запись карты источников, а не разбор: '
                             'обновляй её с --kind source' % target['meta'].get('id'))

    if target is None and is_auto():
        # повтор известной сигнатуры обновляет запись, а не плодит новую:
        # спросить «это тот же случай?» в автономном прогоне некого
        target = find_by_signature(incidents, signatures)
        if target is not None:
            print('Сигнатура уже описана записью %s — обновляю её, дубля не завожу'
                  % target['meta'].get('id'))

    if target is None and not title:
        raise SystemExit('Для новой записи нужен --title')

    when = now()
    date = args.date or when.strftime('%Y-%m-%d')

    if target is None:
        meta = {
            'id': next_id(incidents, when),
            'kind': KIND_INCIDENT,
            'title': title,
            'date': date,
            'stands': split_csv(args.stand),
            'services': services,
            'tags': split_csv(args.tags),
            'status': args.status,
            # фикс ещё не подтверждён практикой — по умолчанию исход «не проверена»,
            # даже если запись создаётся сразу после разбора
            'outcome': args.outcome or OUTCOME_UNVERIFIED,
        }
        if args.severity:
            meta['severity'] = args.severity
        if args.outcome:
            meta['outcome_date'] = date
        sections = {}
        existing_body = ''
    else:
        meta = dict(target['meta'])
        sections = dict(target['sections'])
        existing_body = target['body']
        if title:
            meta['title'] = title
        meta['stands'] = merge_list(meta.get('stands'), split_csv(args.stand))
        meta['services'] = merge_list(meta.get('services'), services)
        meta['tags'] = merge_list(meta.get('tags'), split_csv(args.tags))
        if args.severity:
            meta['severity'] = args.severity
        if args.status:
            meta['status'] = args.status
        if args.outcome:
            meta['outcome'] = args.outcome
            meta['outcome_date'] = date
        meta.setdefault('date', date)
        # `--update` фиксирует зафиксированный повтор инцидента — это наблюдаемое
        # событие, а не самооценка полезности записи
        meta['reuse_count'] = int(meta.get('reuse_count') or 0) + 1
        meta['reused_at'] = date

    meta['signatures'] = merge_signatures(meta.get('signatures'), signatures)
    if args.file:
        meta['files'] = merge_list(meta.get('files'), split_csv(args.file))
    if args.commit:
        meta['commits'] = merge_list(meta.get('commits'), split_csv(args.commit))
    if args.related:
        meta['related'] = merge_list(meta.get('related'), split_csv(args.related))

    updates = [
        ('Симптомы', args.symptoms),
        ('Диагностика', args.diagnosis),
        ('Причина', args.root_cause),
        ('Решение', args.fix),
        ('Проверка', args.verify),
        (DISTINGUISHERS_SECTION, args.distinguishing),
        ('Заметки', args.notes),
    ]
    for name, value in updates:
        if not value:
            continue
        value = scrub(value.strip(), scrub_counts)
        prev = sections.get(name, '').strip()
        if prev and prev != '_не заполнено_' and value not in prev:
            sections[name] = '%s\n\n_%s:_ %s' % (prev, date, value)
        else:
            sections[name] = value

    if target is not None and not any(v for _n, v in updates):
        # ничего не дополняли текстом — фиксируем повтор
        note = 'Повторилось %s%s.' % (
            date, ' на стенде ' + ', '.join(split_csv(args.stand)) if args.stand else '')
        sections['Заметки'] = (sections.get('Заметки', '').strip() + '\n\n' + note).strip()

    body = build_body(sections)
    text = dump_frontmatter(meta) + '\n\n' + body

    if target is not None:
        path = target['path']
    else:
        fname = '%s-%s.md' % (meta['id'], slugify(meta['title']))
        path = os.path.join(directory, fname)

    lines = ['id: %s' % meta['id']]
    if meta.get('signatures'):
        lines.append('сигнатур: %d' % len(meta['signatures']))
    action = 'дополнена' if target is not None else 'создана'
    return store(text, path, directory, source, lines, action, scrub_counts, args.dry_run,
                 report=args.report, entry_id=meta['id'])


if __name__ == '__main__':
    sys.exit(run_script(main, __file__))
