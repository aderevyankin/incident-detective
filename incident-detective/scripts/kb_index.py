#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пересборка kb/index.json из markdown-записей.

Индекс — производная от markdown, его можно удалять и генерировать заново.
Нужен для быстрого обзора базы без чтения всех файлов.
"""

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_common import require_python; require_python()  # noqa: E402

from kb_common import dump_json, kb_dir, load_incidents, now, run_script  # noqa: E402


def rebuild(directory=None):
    directory = kb_dir(directory)
    incidents = load_incidents(directory)
    tags = Counter()
    stands = Counter()
    services = Counter()
    entries = []
    for inc in incidents:
        meta = inc['meta']
        for tag in meta.get('tags') or []:
            tags[str(tag)] += 1
        for stand in meta.get('stands') or []:
            stands[str(stand)] += 1
        for svc in meta.get('services') or []:
            services[str(svc)] += 1
        entries.append({
            # секции целиком: по ним считается релевантность, и поиск должен
            # давать тот же ответ по индексу, что и по markdown-файлам
            'sections': {k: v for k, v in inc['sections'].items() if v},
            'id': meta.get('id'),
            'title': meta.get('title'),
            'date': meta.get('date'),
            'stands': meta.get('stands') or [],
            'services': meta.get('services') or [],
            'tags': meta.get('tags') or [],
            'severity': meta.get('severity'),
            'status': meta.get('status'),
            # отсутствующий исход не подставляется здесь: индекс — зеркало
            # markdown, «не проверена» по умолчанию трактуется на чтении
            # (kb_common.outcome_of), а не при сборке
            'outcome': meta.get('outcome'),
            'outcome_date': meta.get('outcome_date'),
            'reuse_count': meta.get('reuse_count'),
            'reused_at': meta.get('reused_at'),
            'signatures': meta.get('signatures') or [],
            'files': meta.get('files') or [],
            'file': os.path.basename(inc['path']),
        })
    entries.sort(key=lambda e: str(e.get('date') or ''), reverse=True)
    # время правки самой свежей записи на момент сборки: по нему поиск понимает,
    # что индекс отстал. Сравнивать с временем сборки нельзя — оно округлено до
    # секунды, и свежий индекс выглядел бы устаревшим
    newest = 0.0
    for inc in incidents:
        try:
            newest = max(newest, os.path.getmtime(inc['path']))
        except OSError:
            pass
    index = {
        'generated': now().strftime('%Y-%m-%d %H:%M:%S'),
        'source_mtime': newest,
        'count': len(entries),
        'tags': dict(tags.most_common()),
        'stands': dict(stands.most_common()),
        'services': dict(services.most_common()),
        'incidents': entries,
    }
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, 'index.json')
    dump_json(index, path)
    return path, len(entries)


def main(argv=None):
    ap = argparse.ArgumentParser(description='Пересобрать индекс базы знаний.')
    ap.add_argument('--kb', help='директория базы знаний')
    args = ap.parse_args(argv)
    path, count = rebuild(args.kb)
    print('Индекс пересобран: %s (%d записей)' % (path, count))
    return 0


if __name__ == '__main__':
    sys.exit(run_script(main, __file__))
