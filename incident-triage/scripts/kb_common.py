#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Общие утилиты базы знаний: чтение/запись markdown-записей, токенизация, скоринг."""

import json
import os
import re
import sys
from datetime import datetime

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_KB = os.path.join(SKILL_DIR, 'kb')

SECTIONS = ['Симптомы', 'Диагностика', 'Причина', 'Решение', 'Проверка', 'Заметки']

LIST_FIELDS = ('stands', 'services', 'tags', 'signatures', 'related', 'files', 'commits')


def kb_dir(explicit=None):
    return os.path.abspath(explicit or os.environ.get('INCIDENT_KB_DIR') or DEFAULT_KB)


# --------------------------------------------------------------------------
# Мини-парсер frontmatter (подмножество YAML, без внешних зависимостей)
# --------------------------------------------------------------------------

_KV_RE = re.compile(r'^([A-Za-z_][\w\-]*)\s*:\s*(.*)$')


def _unquote(val):
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in '"\'':
        return val[1:-1]
    return val


def _parse_inline_list(val):
    inner = val.strip()[1:-1].strip()
    if not inner:
        return []
    return [_unquote(p) for p in re.split(r',\s*', inner) if p.strip()]


def parse_frontmatter(text):
    """Возвращает (meta: dict, body: str)."""
    if not text.startswith('---'):
        return {}, text
    end = text.find('\n---', 3)
    if end == -1:
        return {}, text
    head = text[3:end].strip('\n')
    body = text[end + 4:].lstrip('\n')
    meta = {}
    key = None
    for line in head.split('\n'):
        if not line.strip() or line.strip().startswith('#'):
            continue
        if line.lstrip().startswith('- ') and key:
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(_unquote(line.lstrip()[2:]))
            continue
        m = _KV_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val.startswith('[') and val.endswith(']'):
            meta[key] = _parse_inline_list(val)
        elif val == '':
            meta[key] = []
        else:
            meta[key] = _unquote(val)
    for field in LIST_FIELDS:
        if field in meta and not isinstance(meta[field], list):
            raw = str(meta[field])
            meta[field] = [p.strip() for p in raw.split(',') if p.strip()]
    return meta, body


def dump_frontmatter(meta):
    lines = ['---']
    order = ['id', 'title', 'date', 'stands', 'services', 'tags', 'severity',
             'status', 'files', 'commits', 'related', 'signatures']
    keys = [k for k in order if k in meta] + [k for k in meta if k not in order]
    for key in keys:
        val = meta[key]
        if isinstance(val, list):
            if not val:
                continue
            if key == 'signatures' or any(len(str(v)) > 40 or ',' in str(v) for v in val):
                lines.append('%s:' % key)
                for item in val:
                    lines.append('  - "%s"' % str(item).replace('"', "'"))
            else:
                lines.append('%s: [%s]' % (key, ', '.join(str(v) for v in val)))
        else:
            text = str(val)
            if any(ch in text for ch in ':#[]') or text != text.strip():
                lines.append('%s: "%s"' % (key, text.replace('"', "'")))
            else:
                lines.append('%s: %s' % (key, text))
    lines.append('---')
    return '\n'.join(lines)


def split_sections(body):
    """Возвращает {заголовок: текст} по разделам '## '."""
    out = {}
    current = None
    buf = []
    for line in body.split('\n'):
        if line.startswith('## '):
            if current:
                out[current] = '\n'.join(buf).strip()
            current = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    if current:
        out[current] = '\n'.join(buf).strip()
    return out


# --------------------------------------------------------------------------
# Загрузка записей
# --------------------------------------------------------------------------


def load_incidents(directory=None):
    directory = kb_dir(directory)
    items = []
    if not os.path.isdir(directory):
        return items
    for name in sorted(os.listdir(directory)):
        if not name.endswith('.md') or name.upper().startswith('README'):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                text = fh.read()
        except OSError as exc:
            print('warning: %s: %s' % (path, exc), file=sys.stderr)
            continue
        meta, body = parse_frontmatter(text)
        if not meta.get('id'):
            meta['id'] = os.path.splitext(name)[0]
        if not meta.get('title'):
            head = re.search(r'^#\s+(.+)$', body, re.MULTILINE)
            meta['title'] = head.group(1).strip() if head else meta['id']
        items.append({'meta': meta, 'body': body, 'path': path,
                      'sections': split_sections(body)})
    return items


def next_id(incidents, when=None):
    when = when or datetime.now()
    prefix = 'INC-%04d-%02d' % (when.year, when.month)
    used = []
    for inc in incidents:
        m = re.match(r'INC-(\d{4})-(\d{2})-(\d+)', str(inc['meta'].get('id', '')))
        if m and '%s-%s' % (m.group(1), m.group(2)) == '%04d-%02d' % (when.year, when.month):
            used.append(int(m.group(3)))
    return '%s-%03d' % (prefix, (max(used) + 1) if used else 1)


def slugify(text, limit=40):
    translit = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '',
        'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
    }
    out = []
    for ch in text.lower():
        if ch in translit:
            out.append(translit[ch])
        elif ch.isalnum() and ord(ch) < 128:
            out.append(ch)
        else:
            out.append('-')
    slug = re.sub(r'-{2,}', '-', ''.join(out)).strip('-')
    return slug[:limit].strip('-') or 'incident'


# --------------------------------------------------------------------------
# Токенизация и скоринг
# --------------------------------------------------------------------------

TOKEN_RE = re.compile(r'[0-9a-zA-Zа-яёА-ЯЁ_]{2,}', re.UNICODE)

STOP = {
    'the', 'and', 'for', 'not', 'was', 'are', 'this', 'that', 'with', 'from',
    'при', 'для', 'что', 'как', 'это', 'все', 'или', 'его', 'она', 'они',
    'был', 'была', 'было', 'быть', 'есть', 'нет', 'там', 'тут', 'наш', 'мы',
    'на', 'по', 'из', 'до', 'за', 'об', 'же', 'ли', 'но', 'то', 'бы',
}

# окончания, которые режем для грубой нормализации русских словоформ
RU_SUFFIXES = ('ами', 'ями', 'ого', 'ему', 'ому', 'ыми', 'ими', 'ах', 'ях',
               'ов', 'ев', 'ый', 'ий', 'ая', 'яя', 'ое', 'ее', 'ые', 'ие',
               'ам', 'ям', 'ом', 'ем', 'ой', 'ей', 'ую', 'юю', 'ть', 'ла',
               'ли', 'ло', 'на', 'а', 'я', 'ы', 'и', 'у', 'ю', 'е', 'о', 'ь')


def stem(word):
    word = word.lower()
    if len(word) <= 4:
        return word
    if re.search(r'[а-яё]', word):
        for suf in RU_SUFFIXES:
            if word.endswith(suf) and len(word) - len(suf) >= 4:
                return word[:-len(suf)]
        return word
    if len(word) > 6:
        for suf in ('ing', 'tion', 'ed', 'es', 's'):
            if word.endswith(suf) and len(word) - len(suf) >= 4:
                return word[:-len(suf)]
    return word


def tokenize(text, keep_stop=False):
    out = []
    for word in TOKEN_RE.findall(str(text or '')):
        low = word.lower()
        if not keep_stop and (low in STOP or len(low) < 3):
            continue
        out.append(stem(low))
    return out


def norm_signature(sig):
    """Приводит сигнатуру к сравнимому виду."""
    text = str(sig or '').strip().lower()
    if text.startswith('tmpl:'):
        text = text[5:]
    text = re.sub(r'<\w+>', '<v>', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip(' .,:;|')


def signature_similarity(a, b):
    """0..1 — насколько похожи две сигнатуры."""
    na, nb = norm_signature(a), norm_signature(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.85
    ta, tb = set(tokenize(na, keep_stop=True)), set(tokenize(nb, keep_stop=True))
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if not inter:
        return 0.0
    return inter / float(len(ta | tb))


def load_parsed(path):
    """Читает JSON-вывод parse_logs.py."""
    with open(path, 'r', encoding='utf-8') as fh:
        return json.load(fh)


def signatures_from_parsed(parsed):
    return [s.get('value') for s in parsed.get('signatures', []) if s.get('value')]
