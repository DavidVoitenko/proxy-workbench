# Contributing / Участие в проекте

Спасибо за интерес к Proxy Workbench. Contributions принимаются в виде bug reports, documentation improvements, focused code changes и tests. Не требуется отдельное CLA; contributions публикуются на условиях MIT License.

## Principles / Принципы

- Сначала опишите проблему и ожидаемый результат.
- Делайте небольшие, reviewable changes с узкой целью.
- Не добавляйте telemetry, account requirements или скрытую network activity.
- Не включайте credentials, tokens, private endpoints, proxy credentials, personal data, local paths или hostnames.
- Не добавляйте live scans и network tests в CI.
- Не изменяйте generated results, databases или другие файлы в `data/`.

## Local setup / Локальная настройка

Требуется Python 3.11 или новее.

```sh
python -m venv .venv
python -m pip install --requirement requirements.txt
```

Dependency должна оставаться pinned. При изменении `httpx[socks]` обновите согласованную запись в `requirements.txt` и `pyproject.toml`, затем объясните совместимость и security impact в pull request.

## Tests / Проверки

Запускайте только локальные mock-based unit tests:

```sh
python -m unittest discover -s tests -v
```

Tests используют временные данные и локальные HTTP/DNS mocks. Не запускайте полный proxy scan, публичные источники, DNSBL или произвольные внешние endpoints в рамках разработки или CI. Новые tests также должны быть детерминированными, изолированными и не требовать accounts или secrets.

## Change workflow / Рабочий процесс

1. Создайте branch от `main`; не добавляйте изменения непосредственно в main branch.
2. Следуйте существующему стилю Python и небольшим focused commits.
3. Добавьте regression test для bug fix или test coverage для нового behavior.
4. Обновите public documentation и `CHANGELOG.md`, если изменилось пользовательское поведение.
5. Убедитесь, что workflow выполняет unittest only и не делает proxy/network checks.
6. Откройте pull request с neutral title, кратким контекстом и результатами локальных test runs.

## Data and artifacts / Данные и артефакты

Храните private configuration, databases, exports и generated reports только в игнорируемой папке `data/`. Не добавляйте их в patch. Перед публикацией проверьте diff на наличие secrets, account configuration, live endpoint responses, hostname, personal names, email addresses и absolute home paths.

## Security reports

Не используйте public issue для раскрытия ещё не исправленной уязвимости. Следуйте `SECURITY.md` и предоставьте минимальный reproduction на local mocks.

## Code of conduct

Участники обязуются соблюдать `CODE_OF_CONDUCT.md`. Maintainers могут запросить изменение или отклонить contribution, который нарушает policy, безопасность или scope проекта.
