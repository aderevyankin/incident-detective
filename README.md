# incident-triage — скилл разбора инцидентов для Qwen Code CLI

Скилл в формате [Agent Skills](https://qwenlm.github.io/qwen-code-docs/en/users/features/skills/):
папка со `SKILL.md`, YAML-фронтматтером и вспомогательными файлами. Формат общий для
Qwen Code и других агентов, так что скилл переносим.

Разбирает баги и инциденты на стендах по трём контурам:

| Контур | Вопрос | Скрипты |
|---|---|---|
| **База знаний** | Мы это уже видели? | `kb_search.py`, `kb_add.py`, `kb_index.py` |
| **Логи** | Что происходило на самом деле? | `parse_logs.py`, `timeline.py` |
| **Код** | Где ломается и из-за чего? | `code_hints.py` |

Связывает контуры **сигнатура** — устойчивый отпечаток ошибки (класс исключения,
HTTP-статус, шаблон сообщения). Логи её порождают, база знаний по ней ищет, код по ней
локализуется, новая запись базы её сохраняет. Так разбор второго такого же инцидента
начинается не с нуля, а с готового ответа.

## Установка

```bash
./install.sh              # личный скилл: ~/.qwen/skills/incident-triage
./install.sh --project    # проектный: ./.qwen/skills/incident-triage (едет в git)
```

Затем перезапусти Qwen Code — скиллы читаются при старте. Проверить: `/skills`.

Требуется `python3` (только stdlib, никаких зависимостей) и, для контура кода, `git`.

## Как вызывается

Скилл **самозапускающийся**: включается сам, когда в сообщении есть признак инцидента —
«упало на stage», «504 на проде», «почему падает», вставленный стектрейс, ссылка на
Kibana. Явная команда `/incident-triage` тоже работает, но не нужна.

Первым делом скилл заводит план через `todo_write` и дальше строго ему следует, чтобы
не утонуть в логах, забыв спросить базу знаний.

## Про логи

MCP-сервер скилл с собой не тащит. Он ищет уже подключённые в окружении источники
(Kibana, Loki, Grafana, Sentry, k8s — по ключевым словам в списке инструментов и
`settings.json`), а если ничего нет — честно просит у пользователя файл, архив или
ссылку. Никаких выводов «по догадке» без данных.

Парсинг полностью локальный и рассчитан на **неструктурированные и разнородные** логи:
формат определяется построчно (JSON, logfmt, syslog, nginx/apache, стектрейсы Java /
Python / Go / JS, произвольный текст), многострочные записи склеиваются, похожие
сообщения схлопываются в шаблоны с частотами. На выходе — сводка на страницу вместо
десятков тысяч строк.

## Структура

```
incident-triage/
├── SKILL.md                    точка входа: триггеры, план работы, шаги
├── references/
│   ├── log-sources.md          discovery MCP и запрос логов у пользователя
│   ├── parsing.md              форматы, эвристики, как читать сводку
│   ├── kb-format.md            формат записи базы знаний
│   └── code-analysis.md        от ошибки к месту в коде, связки «симптом → что смотреть»
├── scripts/
│   ├── parse_logs.py           парсер неструктурированных логов → сводка + сигнатуры
│   ├── timeline.py             единая хронология из нескольких источников + git
│   ├── code_hints.py           стектрейсы → файлы проекта, git blame, коммиты в окне
│   ├── kb_search.py            поиск по тексту и по сигнатурам
│   ├── kb_add.py               создание и дополнение записей
│   ├── kb_index.py             пересборка index.json
│   └── kb_common.py            общее: frontmatter, токенизация, сравнение сигнатур
├── templates/
│   ├── incident.md             шаблон записи базы знаний
│   └── postmortem.md           blameless-постмортем с «5 почему»
└── kb/                         база знаний (markdown + генерируемый index.json)
```

## Скрипты отдельно от агента

Всё работает и руками, без Qwen:

```bash
S=~/.qwen/skills/incident-triage/scripts

python3 $S/parse_logs.py app.log --level ERROR
python3 $S/parse_logs.py app.log --format json > /tmp/parsed.json
python3 $S/parse_logs.py app.log --trace 7f3a9c21      # цепочка одного запроса
python3 $S/parse_logs.py app.log --context 3           # сырые записи группы №3

python3 $S/kb_search.py "оплата висит 504"
python3 $S/kb_search.py --from-parsed /tmp/parsed.json --stand stage

python3 $S/code_hints.py --from-parsed /tmp/parsed.json --repo .
python3 $S/timeline.py --parsed api=/tmp/parsed.json --repo . \
        --event "2026-07-28 12:20|деплой 1.24"

python3 $S/kb_add.py --title "..." --stand stage --root-cause "..." \
        --from-parsed /tmp/parsed.json --file src/db/pool.py
```

## База знаний

По умолчанию лежит внутри скилла (`kb/`). Чтобы вести её командой в отдельном
репозитории:

```bash
export INCIDENT_KB_DIR=/path/to/team/incidents
```

Источник правды — markdown-файлы: читаются человеком, правятся руками, кладутся в git.
`index.json` производный, пересобирается через `kb_index.py`.

## Спецификация

Проект ведётся по [OpenSpec](https://github.com/Fission-AI/OpenSpec): спецификация —
источник истины, код должен ей соответствовать.

```bash
npx @fission-ai/openspec list --specs        # список capability
npx @fission-ai/openspec show log-parsing    # требования одной capability
npx @fission-ai/openspec validate --specs --strict
```

Восемь capability в `openspec/specs/`:

| Capability | О чём |
|---|---|
| `incident-detection` | самозапуск по признакам инцидента, план через `todo_write`, карточка |
| `log-ingestion` | поиск подключённых источников логов, запрос данных у пользователя |
| `log-parsing` | разбор неструктурированных логов, шаблонизация, сигнатуры |
| `incident-timeline` | сведение источников в одну хронологию |
| `knowledge-base` | формат записей, поиск по тексту и сигнатурам, пополнение |
| `code-correlation` | стектрейсы → код, git-история, правила предложений по правкам |
| `incident-reporting` | структура вывода, разделение факта и гипотезы, постмортем |
| `skill-packaging` | формат Agent Skills, установка, требования к окружению |

Новое изменение предлагается через `/opsx:propose` — команда установлена и для Qwen Code
(`.qwen/commands/`), и для Claude Code (`.claude/commands/`).

## Границы

Скилл только диагностирует: ничего не рестартует, не деплоит и не правит конфиги на
стендах. Правки в код предлагает, но не применяет молча. В базу знаний пишет шаблоны и
сигнатуры, а не сырые дампы; очевидные токены и пароли вычищаются при записи.
