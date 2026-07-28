#!/usr/bin/env bash
# Установка скилла incident-detective для Qwen Code CLI.
#
#   ./install.sh              — личный скилл (~/.qwen/skills/)
#   ./install.sh --project    — проектный скилл (./.qwen/skills/), едет в git с командой
#   ./install.sh --yes        — не спрашивать подтверждения на перезапись
#   ./install.sh --help       — справка
#
# Копирует, а не симлинкует: Qwen читает файлы при старте, симлинк на внешний
# каталог может не подхватиться.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/incident-detective"
NAME="incident-detective"

usage() {
    cat <<'EOF'
Установка скилла incident-detective.

    ./install.sh              личный скилл (~/.qwen/skills/)
    ./install.sh --project    проектный скилл (./.qwen/skills/)
    ./install.sh --yes        не спрашивать подтверждения на перезапись
    ./install.sh --help       эта справка

Переустановка поверх существующей установки сохраняет записи базы знаний
(kb/INC-*.md): они копируются в резервную папку, её путь печатается, и после
успешного возврата записей папка убирается. База, вынесенная наружу через
INCIDENT_KB_DIR, переустановкой не затрагивается.
EOF
}

PROJECT=0
ASSUME_YES=0

# Разбор циклом, а не по "$1": иначе `--yes --project` молча ставит личный скилл,
# а опечатка в имени флага выглядит как установка по умолчанию.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --project) PROJECT=1 ;;
        --yes|-y)  ASSUME_YES=1 ;;
        --help|-h) usage; exit 0 ;;
        *)
            echo "Неизвестный аргумент: $1" >&2
            echo >&2
            usage >&2
            exit 1
            ;;
    esac
    shift
done

if [[ "$PROJECT" -eq 1 ]]; then
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

STAGE=""
BACKUP=""

# Недостроенная копия — мусор рядом с целью, убирается всегда. Резервная папка с
# записями базы — наоборот: если возврат не дошёл до конца, она остаётся, и её
# путь уже напечатан.
cleanup() {
    [[ -n "$STAGE" && -d "$STAGE" ]] && rm -rf "$STAGE"
    return 0
}
trap cleanup EXIT

if [[ -d "$DEST" ]]; then
    echo "Скилл уже установлен: $DEST"
    if [[ "$ASSUME_YES" -ne 1 ]]; then
        if [[ ! -t 0 ]]; then
            echo "Нет терминала для подтверждения перезаписи." >&2
            echo "Запусти с --yes, если переустановка поверх $DEST — то, что нужно." >&2
            exit 1
        fi
        read -r -p "Перезаписать? Записи базы знаний в kb/ будут сохранены [y/N] " answer
        [[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Отменено."; exit 0; }
    fi
    if compgen -G "$DEST/kb/INC-*.md" >/dev/null; then
        BACKUP="$(mktemp -d)"
        cp "$DEST"/kb/INC-*.md "$BACKUP/"
        echo "Записи базы сохранены: $BACKUP"
    fi
fi

# Копия собирается рядом с местом назначения — на том же файловом томе, иначе mv
# перестанет быть переименованием и снова станет копированием, которое может
# сорваться на полпути. Старая установка до этого момента не тронута.
STAGE="$(mktemp -d "$DEST_ROOT/.$NAME.XXXXXX")"
chmod 755 "$STAGE"

# tar вместо cp -R: фильтрует артефакты разработки штатно и одинаково ведёт себя
# на macOS и Linux. Байткод, скомпилированный чужой версией Python, в поставке
# не нужен.
tar -C "$SRC" --exclude='__pycache__' --exclude='*.pyc' -cf - . | tar -C "$STAGE" -xf -

chmod +x "$STAGE"/scripts/*.py

if [[ -n "$BACKUP" ]]; then
    cp "$BACKUP"/INC-*.md "$STAGE/kb/"
    rm -rf "$BACKUP"
    BACKUP=""
    echo "Записи базы знаний возвращены на место."
fi

# Окно, в котором цели нет на месте, сжато до пары операций над готовой копией.
rm -rf "$DEST"
mv "$STAGE" "$DEST"
STAGE=""

# -B: иначе первый же запуск скрипта из установленной директории положит туда
# scripts/__pycache__ — то самое, что из поставки только что исключили.
python3 -B "$DEST/scripts/kb_index.py" --kb "$DEST/kb" >/dev/null

echo
echo "Установлено: $DEST"
echo "Область: $SCOPE"
echo
echo "Дальше:"
echo "  1. Перезапусти Qwen Code — скиллы читаются при старте."
echo "  2. Проверь: /skills — в списке должен быть incident-detective."
echo "  3. Скилл включается сам, когда речь заходит о баге или инциденте."
echo "     Принудительно: /incident-detective"
echo
echo "База знаний: $DEST/kb — пока расположение не выбрано."
echo "При первой записи скилл спросит, где её держать: в корне проекта,"
echo "внутри скилла или по своему пути. Вынесенную наружу базу переустановка"
echo "не затрагивает. Закрепить выбор:"
echo "  export INCIDENT_KB_DIR=/path/to/team/incidents"
