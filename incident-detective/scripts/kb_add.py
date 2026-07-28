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
    DEFAULT_KB, ENV_KB, KB_DEFAULT, KB_PROJECT, SECTIONS, dump_frontmatter, kb_is_empty,
    load_incidents, load_parsed, next_id, norm_signature, now, project_kb_dir,
    resolve_kb, run_script, signatures_from_parsed, slugify,
)

# Код возврата «расположение базы не выбрано»: отличается и от ошибки аргументов
# (2), и от обычного отказа (1), чтобы агент опознал его без разбора текста.
RC_KB_NOT_CHOSEN = 3

SECRET_RE = re.compile(
    r'(?i)(password|passwd|secret|token|api[_-]?key|authorization|bearer|'
    r'private[_-]?key|access[_-]?key)\s*[=:]\s*\S+')


def scrub(text):
    """Вырезает очевидные секреты перед записью в базу."""
    if not text:
        return text
    return SECRET_RE.sub(lambda m: m.group(0).split('=')[0].split(':')[0] + '=<redacted>', text)


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
        text = sections.get(name, '').strip()
        parts.append('## %s\n\n%s\n' % (name, text if text else '_не заполнено_'))
    return '\n'.join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Создать или дополнить запись базы знаний по инциденту.')
    ap.add_argument('--title', help='заголовок: суть + причина')
    ap.add_argument('--stand', action='append', help='стенд (можно повторять или через запятую)')
    ap.add_argument('--service', action='append', help='сервис/компонент')
    ap.add_argument('--tags', action='append', help='теги через запятую')
    ap.add_argument('--symptoms', help='что видел пользователь, его словами')
    ap.add_argument('--diagnosis', help='как искали причину')
    ap.add_argument('--root-cause', help='причина')
    ap.add_argument('--fix', help='что сделали')
    ap.add_argument('--verify', help='как проверить, что починилось')
    ap.add_argument('--notes', help='ссылки, тикеты, грабли')
    ap.add_argument('--severity', choices=['low', 'medium', 'high', 'critical'])
    ap.add_argument('--status', choices=['resolved', 'workaround', 'open', 'wontfix'],
                    default='resolved')
    ap.add_argument('--signature', action='append', default=[], help='сигнатура вручную')
    ap.add_argument('--from-parsed', help='JSON parse_logs.py — подтянуть сигнатуры')
    ap.add_argument('--file', action='append', default=[], help='файл кода, связанный с багом')
    ap.add_argument('--commit', action='append', default=[], help='подозрительный коммит')
    ap.add_argument('--related', action='append', default=[], help='id связанного инцидента')
    ap.add_argument('--update', help='id существующей записи — дополнить её, а не создавать новую')
    ap.add_argument('--date', help='дата разбора YYYY-MM-DD (по умолчанию сегодня)')
    ap.add_argument('--kb', help='директория базы знаний')
    ap.add_argument('--dry-run', action='store_true', help='показать результат, но не писать')
    args = ap.parse_args(argv)

    directory, source = resolve_kb(args.kb)
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

    incidents = load_incidents(directory)

    signatures = list(args.signature)
    services = split_csv(args.service)
    if args.from_parsed:
        parsed = load_parsed(args.from_parsed)
        signatures.extend(signatures_from_parsed(parsed))
        # 'access' — служебный компонент nginx-парсера, не сервис
        services.extend([s for s in list(parsed.get('stats', {}).get('services', {}))[:3]
                         if s.lower() != 'access'])
    services = merge_list([], services)

    target = None
    if args.update:
        for inc in incidents:
            if str(inc['meta'].get('id', '')).lower() == args.update.lower():
                target = inc
                break
        if target is None:
            raise SystemExit('Запись %s не найдена в %s' % (args.update, directory))

    if target is None and not args.title:
        raise SystemExit('Для новой записи нужен --title')

    when = now()
    date = args.date or when.strftime('%Y-%m-%d')

    if target is None:
        meta = {
            'id': next_id(incidents, when),
            'title': args.title,
            'date': date,
            'stands': split_csv(args.stand),
            'services': services,
            'tags': split_csv(args.tags),
            'status': args.status,
        }
        if args.severity:
            meta['severity'] = args.severity
        sections = {}
        existing_body = ''
    else:
        meta = dict(target['meta'])
        sections = dict(target['sections'])
        existing_body = target['body']
        if args.title:
            meta['title'] = args.title
        meta['stands'] = merge_list(meta.get('stands'), split_csv(args.stand))
        meta['services'] = merge_list(meta.get('services'), services)
        meta['tags'] = merge_list(meta.get('tags'), split_csv(args.tags))
        if args.severity:
            meta['severity'] = args.severity
        if args.status:
            meta['status'] = args.status
        meta.setdefault('date', date)

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
        ('Заметки', args.notes),
    ]
    for name, value in updates:
        if not value:
            continue
        value = scrub(value.strip())
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

    if args.dry_run:
        sys.stdout.write(text)
        sys.stdout.write('\n---\n[dry-run] записалось бы в %s\n' % path)
        return 0

    os.makedirs(directory, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)

    action = 'дополнена' if target is not None else 'создана'
    print('Запись %s: %s' % (action, path))
    if source == KB_PROJECT:
        # Путь не задавали ни флагом, ни переменной — база нашлась в репозитории.
        # Директорию мог завести коллега, и запись в общий репозиторий не должна
        # пройти незамеченной.
        print('база знаний найдена в корне репозитория: %s' % directory)
    print('id: %s' % meta['id'])
    if meta.get('signatures'):
        print('сигнатур: %d' % len(meta['signatures']))

    try:
        import kb_index
        kb_index.rebuild(directory)
        print('index.json обновлён')
    except Exception as exc:                                # noqa: BLE001
        print('warning: индекс не обновлён: %s' % exc, file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(run_script(main, __file__))
