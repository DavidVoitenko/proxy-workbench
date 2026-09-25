# Release notes: desktop-поставка

**Версия:** 2.2.1 (номер не менялся; изменения пока не выпущены)
**Дата:** 25 сентября 2026
**Статус:** модуль и сборщики готовы и проверены частично; релиз не выпускался

Этот файл — единственное место, где перечислено, что в поставке **есть**, а
что **только подготовлено**. Он написан по результатам локальных проверок, а
не по плану. Всё, что здесь не перечислено как проверенное, не проверено.

## Что добавлено

| Что | Состояние | Проверка |
| --- | --- | --- |
| `proxy_workbench/desktop.py`: per-user `data`/`cache`/`logs`, явный portable mode, миграция старой папки, worker command и cwd, чтение ресурсов из bundle, проверка обновления с backup и откатом, проверка подписи и нотаризации | готово | `tests/test_desktop_layout.py`, `tests/test_desktop_migration.py`, `tests/test_desktop_update.py`, `tests/test_desktop_signing.py` |
| macOS `.app` (arm64, onedir) + `.zip` + `.dmg` | собрано на этой машине | `packaging/build_macos.py`, включая запуск собранного приложения |
| Windows GUI `.exe` (`console=False`) и отдельный CLI `.exe` (`console=True`) | сборщик и spec готовы; на Windows не собирались | `tests/test_packaging_release.py` (дескрипторы), `build_windows.py` не запускался |
| Per-user installer (Inno Setup, `PrivilegesRequired=lowest`) | скрипт готов; `iscc` на этой машине нет | `tests/test_packaging_release.py` |
| Portable-вариант Windows (zip с обоими бинарниками) | готово | `packaging/build_windows.py` |
| Манифест артефактов и проверка релиза (`release_manifest.py`, `verify_release.py`) | готово | `tests/test_packaging_release.py` |
| Тонкий desktop host поверх существующего интерфейса (`desktop.main`) | готово | `tests/test_desktop_update.py` (host-команды), запуск собранного `.app` |

## Что изменилось в поведении

- **Замороженная сборка больше не пишет `data/` рядом с исполняемым файлом.**
  Раньше `paths.default_data()` для frozen возвращал
  `Path(sys.executable).parent / 'data'`, что невозможно в `Program Files` и
  внутри `.app`. Теперь установленная сборка использует per-user-папки, а
  portable mode включается только явно. Исходники, `pipx` и Docker сохраняют
  прежний `data/` рядом с проектом.
- Появились отдельные `cache` и `logs`, которых раньше не было.
- `macos_bundle()` определяет корень `.app` на два уровня выше каталога
  исполняемого файла (раньше проверка искала `.app` на один уровень выше и
  никогда не срабатывала).

## Чего нет и не обещается

- **Подписанных и нотарифицированных артефактов нет.** На машине сборки нет
  ни Developer ID Application в связке ключей, ни учётных данных нотаризации:
  `signing_status.available = false`, `notarization_status.available = false`.
  Собранный bundle получает **ad-hoc** подпись, `spctl` его отклоняет, и он
  честно записан в манифесте как `signed: false, kind: adhoc`.
- **Автоматической загрузки обновлений нет.** Есть проверка, backup, проверка
  схемы и откат плюс `--update-notice`. Публикующего сервера у проекта нет.
- **Windows-сборки в этой сессии не собирались** — машина сборки macOS.
  Installer тоже не собран: `iscc` не установлен.
- **Intel и universal2 macOS не собираются** и не заявляются.
- **Трей, автозапуск, sleep/wake, повторный запуск в существующий экземпляр
  (F22) не реализованы** — это отдельная задача, не входившая в эту работу.
- **Собственного окна нет.** Интерфейс остаётся страницей в браузере; новый
  desktop-фреймворк не вводился, потому что F23 разрешает его только при
  проверенном ограничении, а такого ограничения здесь не обнаружено.

## Матрица проверенных сборок

| Сборка | Где проверялась | Что проверено | Подпись |
| --- | --- | --- | --- |
| wheel | CI, Ubuntu/macOS/Windows | `packaging/smoke.py` | нет |
| `Start.bat` / `run.sh` | локально | `sh -n`, smoke | нет |
| Docker CLI | CI | `--version`, `--help`, `clear-data --yes` | нет |
| Windows `.exe` (старый spec) | CI | `packaging/smoke.py` | нет |
| **macOS `.app` arm64** | **эта машина, 25.09.2026** | структура bundle, наличие `ui/index.html` и `sources.json` внутри, запуск с временным `HOME`, отдача страницы с токеном, запись только в per-user-папку, неизменность bundle | **ad-hoc, не подписан** |
| macOS `.dmg` / `.zip` | эта машина | сборка, SHA-256 в манифесте, `verify_release.py` | нет |
| Windows GUI/CLI `.exe` | нигде | только дескрипторы и тесты | нет |
| Windows installer | нигде | только скрипт Inno Setup и тест его настроек | нет |

## Проверки, выполненные в этой работе

Команды запускались из корня репозитория; полный `unittest discover -s tests`
не запускался (по условию задачи его выполняет интегратор после сборки всех
модулей).

```sh
.venv/bin/python -m unittest tests.test_desktop_layout      # 24 теста, OK
.venv/bin/python -m unittest tests.test_desktop_migration   # 14 тестов, OK
.venv/bin/python -m unittest tests.test_desktop_update      # 34 теста, OK
.venv/bin/python -m unittest tests.test_desktop_signing     # 27 тестов, OK
.venv/bin/python -m unittest tests.test_packaging_release   # 25 тестов, OK
.venv/bin/python packaging/build_macos.py --dist /tmp/pw-dist --pyinstaller <venv>/bin/pyinstaller
.venv/bin/python packaging/verify_release.py /tmp/pw-dist/proxy-workbench-2.2.1-macos-arm64.manifest.json
```

## Что должно произойти на интеграции

1. Владелец `paths.py` переключает `default_data()` на `desktop.resolve_layout()`
   для frozen-сборок (см. `docs/integration/HANDOFF/desktop.md`).
2. Владелец `gui.py` начинает воркер с `desktop.child_cwd()` и
   `desktop.worker_command()` вместо `cwd=ROOT.parent`.
3. Владелец workflow добавляет job на macOS в `release.yml` и заменяет
   Windows job на два бинарника.
4. `CHANGELOG.md` получает запись в `## [Unreleased]`: macOS-поставка,
   per-user-пути, portable mode, раздельные GUI/CLI на Windows.

До пунктов 1–4 документация README/CHANGELOG описывает старую поставку, и
это расхождение нужно закрыть до публикации релиза.
