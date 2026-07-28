#!/usr/bin/env bash
# Установка скилла incident-triage для Qwen Code CLI.
#
#   ./install.sh              — личный скилл (~/.qwen/skills/)
#   ./install.sh --project    — проектный скилл (./.qwen/skills/), едет в git с командой
#
# Копирует, а не симлинкует: Qwen читает файлы при старте, симлинк на внешний
# каталог может не подхватиться.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/incident-triage"
NAME="incident-triage"

if [[ "${1:-}" == "--project" ]]; then
    DEST_ROOT="$(pwd)/.qwen/skills"
    SCOPE="проектный (.qwen/skills)"
else
    DEST_ROOT="$HOME/.qwen/skills"
    SCOPE="личный (~/.qwen/skills)"
fi

DEST="$DEST_ROOT/$NAME"

if [[ ! -f "$SRC/SKILL.md" ]]; then
    echo "Не нашёл $SRC/SKILL.md — запускай скрипт из корня репозитория." >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "Нужен python3 — скрипты скилла написаны на нём (только stdlib)." >&2
    exit 1
fi

# Проверка здесь — удобство, а не гарантия: скилл часто разворачивают копированием
# директории, без запуска установщика. Пригодность окружения скилл проверяет сам
# перед разбором (шаг 1 SKILL.md).
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    echo "Нужен python3 версии 3.8 или новее, найден $(python3 -V 2>&1)." >&2
    exit 1
fi

mkdir -p "$DEST_ROOT"

if [[ -d "$DEST" ]]; then
    echo "Скилл уже установлен: $DEST"
    read -r -p "Перезаписать? Записи базы знаний в kb/ будут сохранены [y/N] " answer
    [[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Отменено."; exit 0; }
    BACKUP="$(mktemp -d)"
    if compgen -G "$DEST/kb/INC-*.md" >/dev/null; then
        cp "$DEST"/kb/INC-*.md "$BACKUP/" 2>/dev/null || true
        echo "Записи базы сохранены во временную папку."
    fi
    rm -rf "$DEST"
fi

cp -R "$SRC" "$DEST"
chmod +x "$DEST"/scripts/*.py

if [[ -n "${BACKUP:-}" ]] && compgen -G "$BACKUP/INC-*.md" >/dev/null; then
    cp "$BACKUP"/INC-*.md "$DEST/kb/"
    rm -rf "$BACKUP"
    python3 "$DEST/scripts/kb_index.py" --kb "$DEST/kb" >/dev/null
    echo "Записи базы знаний возвращены на место."
fi

python3 "$DEST/scripts/kb_index.py" --kb "$DEST/kb" >/dev/null

echo
echo "Установлено: $DEST"
echo "Область: $SCOPE"
echo
echo "Дальше:"
echo "  1. Перезапусти Qwen Code — скиллы читаются при старте."
echo "  2. Проверь: /skills — в списке должен быть incident-triage."
echo "  3. Скилл включается сам, когда речь заходит о баге или инциденте."
echo "     Принудительно: /incident-triage"
echo
echo "База знаний: $DEST/kb"
echo "Держать её отдельно (например, в репозитории команды):"
echo "  export INCIDENT_KB_DIR=/path/to/team/incidents"
