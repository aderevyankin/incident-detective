#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пересборка kb/index.json из markdown-записей.

Индекс — производная от markdown, его можно удалять и генерировать заново.
Нужен для быстрого обзора базы без чтения всех файлов.
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime

# Без f-строк намеренно: на старом интерпретаторе должно печататься сообщение, а не SyntaxError.
if sys.version_info < (3, 8):
    sys.stderr.write('incident-triage: нужен Python 3.8 или новее, запущен %s (%s)\n'
                     % (sys.version.split()[0], sys.executable))
    sys.exit(2)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kb_common import kb_dir, load_incidents  # noqa: E402


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
        symptoms = ' '.join(inc['sections'].get('Симптомы', '').split())
        entries.append({
            'id': meta.get('id'),
            'title': meta.get('title'),
            'date': meta.get('date'),
            'stands': meta.get('stands') or [],
            'services': meta.get('services') or [],
            'tags': meta.get('tags') or [],
            'severity': meta.get('severity'),
            'status': meta.get('status'),
            'signatures': meta.get('signatures') or [],
            'files': meta.get('files') or [],
            'symptoms': symptoms[:300],
            'file': os.path.basename(inc['path']),
        })
    entries.sort(key=lambda e: str(e.get('date') or ''), reverse=True)
    index = {
        'generated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'count': len(entries),
        'tags': dict(tags.most_common()),
        'stands': dict(stands.most_common()),
        'services': dict(services.most_common()),
        'incidents': entries,
    }
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, 'index.json')
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2)
    return path, len(entries)


def main(argv=None):
    ap = argparse.ArgumentParser(description='Пересобрать индекс базы знаний.')
    ap.add_argument('--kb', help='директория базы знаний')
    args = ap.parse_args(argv)
    path, count = rebuild(args.kb)
    print('Индекс пересобран: %s (%d записей)' % (path, count))
    return 0


if __name__ == '__main__':
    sys.exit(main())
