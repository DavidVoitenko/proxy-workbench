# Release notes: desktop-поставка

**Версия:** 2.2.1 (номер не менялся; изменения пока не выпущены)
**Дата:** 26 сентября 2026 (фоновый слой F22 добавлен и проверен)
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
- **Трей, автозапуск, sleep/wake и повторный запуск в существующий экземпляр
  (F22) реализованы и проверены живым запуском на macOS** — 26.09.2026, macOS
  26.5.2, arm64. Таблица проверок — ниже. **На Windows и Linux тот же слой
  подготовлен, но не проверен:** машина сборки — macOS, и «здесь работает» не
  является доказательством для другой ОС.
- **Замороженная сборка macOS пока остаётся без меню-бара.** Helper собирается
  из исходника, встроенного в `desktop.py`, через Xcode Command Line Tools; в
  bundle он не положен, потому что `packaging/build_macos.py` и spec — не мои
  файлы. Пока их не тронули, frozen-сборка честно пишет «меню-бар недоступен»
  и работает без него. Точная правка — в
  `docs/integration/HANDOFF/fix-desktop.md`.
- **Sleep/wake на Windows не определяется.** Там сон входит в монотонные часы,
  поэтому сравнение wall/monotonic его не видит, а нативного наблюдателя для
  Windows в этом слое нет. На macOS наблюдатель есть (NSWorkspace), на Linux
  работает сравнение часов. Это ограничение названо, а не закрыто заглушкой.
- **Собственного окна нет.** Интерфейс остаётся страницей в браузере; новый
  desktop-фреймворк не вводился, потому что F23 разрешает его только при
  проверенном ограничении, а такого ограничения здесь не обнаружено. Меню-бар
  — это NSStatusItem над той же страницей, а не второе приложение.

## Фоновая работа (F22): что добавлено и чем проверено

Слой живёт в `proxy_workbench/desktop.py` и не дублирует продукт: он читает то,
что интерфейс и так публикует, и просит интерфейс действовать. Меню-бар — это
маленький нативный helper на Swift, собираемый на машине из исходника,
встроенного в `desktop.py`; он не знает ничего о продукте и получает все
подписи и состояние по локальному сокету.

| Что | Состояние | Чем проверено живым запуском (macOS 26.5.2, arm64) |
| --- | --- | --- |
| Меню-бар со статусом, паузой, открытием интерфейса и выходом | работает | `ps` показывает helper; сам helper отчитывается в журнал: `{"status_item": true, "window": true, "visible": true, "frame": [976, 1084, 50, 33], "menu_items": 10}`; меню отрисовано с 10 пунктами и 5 действиями |
| Close ≠ quit | работает | страница получена по HTTP и отпущена — процесс жив и отвечает; `Выход` завершает приложение, порт закрыт, сокет удалён, запись экземпляра очищена, процессов не осталось |
| Повторный запуск обращается к первому | работает | второй запуск напечатал «The application is already running: http://127.0.0.1:…/», вышел с кодом 0, число процессов `1 → 1` |
| Второй экземпляр, запущенный старым способом (`gui.py`) | работает | хост видит занятый `gui-instance.lock`, открывает страницу и выходит: трей не мигает, rival не поднимается |
| Автозапуск только по действию пользователя | работает | plist отсутствует после установки и первого запуска; появляется после щелчка в меню и исчезает после второго щелчка; `RunAtLoad=true`, `KeepAlive=false` |
| Сон → пробуждение: `scheduler.mark_wake` | работает | в журнале `platform.wake → {"woke": true, "run_requests": 0}`; на реальном `Scheduler` тот же расписание, спавшее 6 часов, даёт **1 слитый запуск с `missed: 5` и `coalesced_after_sleep`**, а без отметки — **5 catch-up запусков** |
| Сон → пробуждение: `jobs.mark_awake` | работает | на настоящем `JobStore`: `mark_suspended` пишет checkpoint, `mark_awake` возвращает в бюджет 3600 с; повторный вызов — no-op |
| Смена сети отмечается, история не портится | работает | реальный `NWPathMonitor` в helper прислал `platform.network`; число строк в `results` до и после совпадает |
| Недоступное хранилище секретов не роняет фон | работает | `keyring` не установлен → `vault.state = "session"`, фон продолжает; при `SecretVaultLocked` в меню появляется строка с действием пользователя |
| Перенос старой папки при запуске, один раз | работает | при `PROXY_WORKBENCH_DATA` старый `data/` проекта перенесён (88 файлов), создан backup, напечатано сообщение, строка появилась в меню; повторный запуск не переносит заново |
| Выход сообщает о подключённых клиентах | работает | `Quit. Connected clients: 0. Stopping the listeners…` в stdout и в журнале |
| Нет orphan-процессов после `SIGKILL` | работает | helper умер сам, когда сокет закрылся; следующий запуск подхватил экземпляр и поднял новый helper |
| **Windows и Linux** | **не проверено** | машина macOS. Подготовлены: control-канал через loopback + токен, `HKCU\…\Run`, `$XDG_CONFIG_HOME/autostart`. Меню-бара там нет |

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

Живой запуск фонового слоя (F22) — 48 из 48 проверок, все через `ps`, сокет и
файлы, которые увидел бы пользователь; запускался настоящий продукт, а не
модуль в изоляции:

```sh
PROXY_WORKBENCH_DATA=/tmp/pwrun/data python -m proxy_workbench.desktop --no-browser --no-gateway --no-api
PROXY_WORKBENCH_DATA=/tmp/pwrun/data python -m proxy_workbench.desktop --status
```

Меню-бар при этом проверяется не чтением исходников, а двумя независимыми
фактами: процесс helper виден в `ps`, а сам helper пишет в
`<logs>/tray.log`, что создал `NSStatusItem` с окном на экране. Скриншотом
меню-бар не снимался: у процесса, который его запустил, нет прав Универсального
доступа, и `System Events` отвечает отказом — это ограничение окружения, а не
результат проверки.

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
