# HANDOFF: поставка (packaging) — F22, F23, дефект 26, R19, R20

**Ветка:** `integration/ultra-2026-09-25`
**Дата:** 26 сентября 2026
**Владею:** `packaging/`, `.github/workflows/`, `docs/packaging/*`, `README.md`,
`README.ru.md`, `CHANGELOG.md`, `pyproject.toml` (версия),
`proxy_workbench/__main__.py` (точка входа — без неё поставка не работает)
**Не владею:** остальным `proxy_workbench/`, `tests/`, `gui.py`, `db.py`,
`paths.py`

---

## 1. Коротко: что стало

Поставка доведена до состояния, в котором артефакт **реально содержит продукт
и реально запускается** на macOS. Windows-поставка приведена к состоянию, в
котором сборка обязана сработать на Windows, и проверена всем, что проверяется
без Windows; воспроизводимая проверка на настоящем Windows-runner добавлена в
CI, но **ещё не выполнялась**.

| Требование | Статус |
| --- | --- |
| F22 (доставка фонового слоя) | реализовано, проверено живым запуском macOS-артефакта |
| F23 (Windows/macOS/Linux) | macOS — собрано и проверено; Windows — код и конфигурация готовы, сборка не выполнялась; Linux — путь исходников/Docker/pipx не сломан |
| дефект 26 (реальная Mac-поставка, достоверные release notes) | закрыто для macOS; Windows честно помечен как непроверенный |
| R19 (desktop-цель не закрыта) | закрыто |
| R20 (документация отстаёт) | закрыто для своей области: CHANGELOG, README/README.ru, docs/packaging, матрица сборок |
| Сценарий 18 | macOS — проверен; Windows — job описан, но не выполнен |

---

## 2. Что было сломано и что исправлено

### 2.1 В `.app` не было меню-бара

`desktop.py` держит исходник нативного Swift-helper'а и компилирует его
`swiftc` один раз в файл с хешем. Но ни `build_macos.py`, ни spec не клали
helper в bundle: приложение запускалось, отдавало страницу и **не имело
меню-бара** — то есть половина обещанного F22 отсутствовала в артефакте.

Исправлено: `packaging/tray_helper.py` компилирует helper через
`desktop.ensure_tray_helper` (продуктовая функция, а не вторая копия логики),
кладёт его под простым именем в staging и отдаёт spec'у; spec прерывает
сборку, если helper не собрался, и кладёт его как данные в
`Contents/Frameworks/tray/`, то есть ровно туда, куда смотрит
`desktop.resource_path('tray', …)` в runtime (`sys._MEIPASS`).

### 2.2 Точка входа не доходила до фонового слоя

`python -m proxy_workbench` и консольный скрипт `proxy-workbench` уходили в
`gui.main`. Ровно так продукт запускает пользователь — и при этом не работали
меню-бар, один экземпляр, автозапуск и sleep/wake.

Исправлено в `proxy_workbench/__main__.py`:

| Вызов | Куда идёт |
| --- | --- |
| без аргументов | `desktop.main` — приложение |
| `gui …` или `--no-desktop` | `gui.main` — только интерфейс |
| `--status`, `--print-paths`, `--portable`, `--autostart`, `--autostart-status`, `--migrate-preview`, `--update-notice` | `desktop.main` — команды фонового слоя |
| команда (`run`, `scan`, `collect`, `bench`, `api-key`, …) | `proxytool.main` — CLI |

Решение принимается по **всей** командной строке: CLI допускает параметры до
команды (`proxy-workbench --workers 8 run`), и маршрутизация по первому токену
сломала бы такой вызов. Список команд читается из того же парсера, который их
определяет (`proxytool.parser()`), поэтому новый глагол не может случайно
уехать в интерфейс.

### 2.3 Frozen-сборка не могла выполнить проверку (найдено попутно)

Worker запускается как `[sys.executable, 'scan', …]`. Старый
`packaging/desktop_launcher.py` звал `desktop.main()` безусловно, и `scan`
уходил в argparse интерфейса. Воспроизведено на текущем коде до правки
launcher'а:

```
$ python -m proxy_workbench.desktop scan --data /tmp/pw-oldroute --no-sources
python -m proxy_workbench.desktop: error: unrecognized arguments: scan --no-sources
```

То есть установленное приложение не могло проверить ни одного прокси.
Исправлено: frozen launcher (`desktop_launcher.py`) маршрутизирует тем же
правилом, что и консольный скрипт; `cli_launcher.py` — тоже. Подтверждено
сквозным прогоном: `packaging/smoke.py` против собранного `.app` проходит
полный цикл collect → scan → API → gateway, `smoke test passed`.

### 2.4 Windows: сборщик был нерабочим

| Что | Было | Стало |
| --- | --- | --- |
| Запуск PyInstaller | `shutil.which('pyinstaller') or 'pyinstaller'` — падало с `No such file or directory`, если venv/bin не в `PATH` | `[sys.executable, '-m', 'PyInstaller']`, тот же интерпретатор; `--pyinstaller PATH` оставлен |
| Workpath | один и тот же на оба бинарника | свой на каждый (`build/pyinstaller/<spec>`) |
| `portable-README.txt` для установщика | `[Files]` ссылался на `..\dist\portable-README.txt`, которого **никогда не было** — файл писался только в staging, который удалялся | README пишется в `dist` до вызова `iscc` |
| Пути установщика | относительные, резолвятся от рабочей папки компилятора; путь с пробелами ломал бы `OutputDir` | `OutputDir="{#OutDir}"`, `Source: "{#SourceDir}\…"`, `/DSourceDir` передаётся |
| `iscc` | только `PATH` | `PATH`, затем `C:\Program Files (x86)\Inno Setup 6`, затем `C:\Program Files\Inno Setup 6` |
| `console=False` | проверялось только чтением spec'а | `pe_subsystem()` читает PE-заголовок готового `.exe`; сборка падает, если GUI не windowed или CLI не console |
| Порядок | `require_windows()` добавлен: на не-Windows скрипт объясняет, почему нельзя, вместо того чтобы делать вид | |

### 2.5 Версия расходилась

`branding.PRODUCT_VERSION = 2.3.0`, `pyproject.toml version = 2.2.1`. Bundle
объявлял 2.3.0, wheel назывался 2.2.1, манифест писался по версии продукта.
`pyproject.toml` приведён к `2.3.0`.

---

## 3. Что проверено процессами и файлами

macOS 26.5.2, arm64, Python 3.14, PyInstaller 6.22.3, Xcode CLT (`swiftc`
в `/usr/bin/swiftc`). Публичные прокси и внешние endpoint-ы не использовались:
весь прогон идёт на loopback и локальных mock'ах.

### 3.1 Сборка

```sh
python packaging/build_macos.py --dist dist
# -> manifest: dist/proxy-workbench-2.3.0-macos-arm64.manifest.json
#    Proxy Workbench.app: signed=False (ad-hoc …), not notarized
#    menu bar: confirmed at launch
#    second launch: {"exit_code": 0, "instances_before": 1, "instances_after": 1, "one_instance": true}
```

Код выхода 0 означает, что прошли все проверки:

1. структура bundle и `Info.plist`;
2. `ui/index.html` и `sources.json` внутри bundle;
3. helper меню-бара внутри bundle **и исполняем** (иначе сборка падает — это
   был ровно тот случай, который молчал раньше);
4. собранное приложение стартует с временным `HOME`, отдаёт страницу с токеном
   на loopback-порту, выбранном ОС;
5. helper отчитался в `<logs>/tray.log`: `ready {"status_item": true, "button":
   true, "window": true, "visible": true, "frame": [1186, 1084, 50, 33],
   "menu_items": 9}` — и этот pid жив;
6. **второй запуск** артефакта напечатал `The application is already running:
   http://127.0.0.1:62033/`, вышел с кодом 0, число процессов `1 → 1`;
7. bundle не изменился ни одним байтом;
8. после остановки приложения процессов-сирот не осталось.

### 3.2 Меню-бар — двумя независимыми фактами

```
$ ps -A -o pid,ppid,command | grep -E "Proxy Workbench|proxy-workbench-tray"
29453 29449 …/Proxy Workbench.app/Contents/MacOS/Proxy Workbench --no-browser --port 0 …
29455 29453 …/Contents/Frameworks/tray/proxy-workbench-tray --socket …/desktop-control.sock --token …

$ tail -3 <logs>/tray.log
rendered {"enabled":4,"actions":["activate","pause","resume","toggleAutostart","quit"],"items":9,…}
ready {"status_item":true,"window":true,"frame":[1186,1084,50,33],"menu_items":9,"pid":29455,…}
exiting: the control socket was closed
```

Helper — отдельный процесс с `PPID` приложения; он сам сообщает, что нарисовал
статус-элемент с окном на экране и девятью пунктами меню.

### 3.3 Нет orphan-процессов

Отдельная проверка с `SIGKILL` (сильнее, чем `terminate` при завершении
сборки): после `kill -9` приложения helper в течение нескольких секунд пишет
`exiting: the control socket was closed` и уходит; `ps` пуст.

### 3.4 Пути, portable mode, перенос старой папки — на артефакте

```sh
python packaging/verify_delivery.py "dist/Proxy Workbench.app"
```

15 из 15 проверок `ok`:

- **per-user**: адрес интерфейса, `data` в `~/Library/Application Support`,
  `cache` в `~/Library/Caches`, `logs` в `~/Library/Logs`, журнал фонового слоя
  рядом с данными, папка программы не изменена;
- **portable**: маркер `proxy-workbench-portable.json` включил режим, данные
  легла в `data/` **внутри копии** `.app`, и больше в копии не изменилось
  ничего;
- **миграция**: 9 файлов перенесено, база — через SQLite backup API, старая
  папка осталась на месте, `migration-receipt.json` называет рабочую копию и
  существующий backup, повторный запуск **не переносит заново** (одна запись
  `migration.applied` в журнале).

### 3.5 Сквозной прогон собранного артефакта

```sh
env HOME=/tmp/pw-smoke-home ALL_PROXY= HTTP_PROXY= HTTPS_PROXY= NO_PROXY=127.0.0.1,localhost \
  python packaging/smoke.py "dist/Proxy Workbench.app/Contents/MacOS/Proxy Workbench"
# -> {"exit_code": 0, "passed": 1}
# -> smoke test passed
```

Единственная проверка, которая проходит через worker, то есть через повторный
запуск того же исполняемого файла с командой `scan`.

### 3.6 Статические проверки Windows (без Windows)

- все spec'ы разобраны и **фактически выполнены** с подменённым API
  PyInstaller: записи в `datas` указывают на существующие файлы, entry point
  существует, `console`/`name`/`bundle_identifier`/`info_plist` именно такие, как
  задумано; macOS-spec теперь требует helper и он присутствует;
- `pe_subsystem()` проверен на синтетических PE32 и PE32+ (2 → `windows-gui`,
  3 → `windows-console`, 3 в PE32+ → `windows-console`, не-PE → `None`) и на
  отказ при переставленных бинарниках;
- `require_windows()` даёт внятный отказ на не-Windows;
- все четыре workflow парсятся (`ci.yml`, `release.yml`, `windows.yml`,
  `repo-settings.yml`);
- unit-тесты: `tests.test_packaging_release` — OK; `tests.test_desktop_layout` —
  OK. Полный `unittest discover -s tests` не запускался (по условию задачи его
  выполняет интегратор).

---

## 4. Что осталось непроверенным и почему

| Что | Почему |
| --- | --- |
| Windows `.exe` и installer | PyInstaller собирает под ту машину, на которой работает; машина — macOS |
| Запуск Windows GUI, второй запуск, per-user на Windows | требует Windows; job описан и добавлен |
| Windows-подпись (Authenticode) | нет сертификата в хранилище; `--sign` требует `PROXY_WORKBENCH_SIGN_THUMBPRINT` и завершается кодом 3 |
| macOS-подпись и нотаризация | нет Developer ID Application в связке ключей и нет `APPLE_ID`/`APPLE_PASSWORD`/`APPLE_TEAM_ID`; `signing_status()`/`notarization_status()` → `available: false`. Собранный bundle — **ad-hoc**, `spctl` его отклоняет, манифест пишет `signed: false, kind: adhoc` |
| Intel и universal2 | требуют сборки обоих срезов и их слияния; `build_macos.py` намеренно отказывается и не делает вид |
| Меню-бар, автозапуск, sleep/wake на Windows и Linux | слой на Windows не имеет наблюдателя сна, меню-бара там нет; ни то, ни другое не запускалось |
| Новые CI-job'ы | добавлены, но ещё ни разу не выполнялись: конфигурация CI не равна выполненному CI |
| Скриншот меню-бара | у процесса, который его запускает, нет прав Универсального доступа, `System Events` отвечает отказом — ограничение окружения |

### 4.1 Что должен сделать Windows-runner

Job `.github/workflows/windows.yml` (runner `windows-2022`) делает это уже
сейчас и падает, если не получилось:

1. `python packaging/build_windows.py --dist dist --installer`:
   - собрать `proxy-workbench-gui.exe` (`console=False`) и
     `proxy-workbench-cli.exe` (`console=True`), каждому свой workpath;
   - прочитать PE-subsystem обоих и отказаться, если GUI не windowed или CLI не
     console;
   - запустить GUI `.exe` с пустым `%LOCALAPPDATA%`/`%APPDATA%`, дождаться
     `gui-address.json`, забрать страницу по loopback, убедиться, что папка
     программы не изменилась и что второй запуск дошёл до первого
     (`instances_before == instances_after`);
   - записать `portable-README.txt` в `dist`, собрать portable-zip и, если
     `iscc` есть на runner'е, per-user installer;
   - записать манифест с `subsystems`, `verified` и `installer.built`;
2. `python packaging/smoke.py dist/proxy-workbench-cli.exe`;
3. `python packaging/verify_release.py <manifest>`;
4. выгрузка артефактов.

Job **не** подписывает и не будет: подпись требует сертификата в хранилище
и `PROXY_WORKBENCH_SIGN_THUMBPRINT`. Он также не проверяет меню-бар и
sleep/wake — их на Windows нет.

---

## 5. Что нужно от других владельцев

### 5.1 Срочно: `tests/test_paths.py::test_entry_point_dispatch`

Тест утверждает старое поведение и **упал** после изменения точки входа:

```
AssertionError: Lists differ: [(['--no-browser'],)] != [([],), (['--no-browser'],)]
```

Он не только устарел: он вызывает `entry.main([])` без мока `desktop.main`,
то есть на машине, где никто не держит `data/gui-instance.lock`, тест
**запустит приложение целиком** и не вернётся. Новая проверка должна мокать
`proxy_workbench.desktop.main` и `proxy_workbench.gui.main` одновременно и
утверждать, что `[]` и `--no-desktop` идут в host, `gui` — в интерфейс, а
команда — в CLI. Пока тест не обновлён, полный прогон suite на машине без
занятого lock'а зависнет.

Рекомендуемый набор проверок маршрутизации (все — чистые, без сети и без
запуска приложения):

| Вызов | Ожидание |
| --- | --- |
| `[]` | `desktop.main([])` |
| `['gui', '--no-browser']` | `gui.main(['--no-browser'])` |
| `['--no-desktop']` | `gui.main([])` |
| `['--status']`, `['--print-paths']`, `['--portable']`, `['--autostart', 'on']` | `desktop.main(<как есть>)` |
| `['scan', '--data', 'x']` | `proxytool.main(...)` |
| `['--workers', '8', 'run']` | `proxytool.main(...)` — параметры до команды |
| `['--version']`, `['--help']` | `proxytool.main(...)` |

### 5.2 `paths.py` (владелец — не я)

- `paths.default_data()` для frozen по-прежнему возвращает `data/` рядом с
  исполняемым файлом. Frozen-сборки это обходят в своих launcher'ах
  (`packaging/desktop_launcher.py`, `packaging/cli_launcher.py`), но любой
  другой frozen-вход, который дойдёт до `default_data()` без `--data`, получит
  read-only-путь. Правильнее переключить `default_data()` на
  `desktop.resolve_layout()`.
- Ограничение, о котором стоит помнить: control-сокет лежит внутри папки
  данных, а путь unix-сокета ограничен 104 байтами. macOS-`TMPDIR` уже около
  60 символов, поэтому проверки запуска используют `/tmp`
  (`build_macos._short_home`). Сборку/проверку с длинным `HOME` видно сразу:
  сокет не bind'ится, и меню-бар не появляется.

### 5.3 `gui.py` (владелец — не я)

`gui.py` запускает worker с `cwd=ROOT.parent`. Внутри bundle это
`sys._MEIPASS.parent` — временная папка лаунчера, а не пользовательская.
`desktop.child_cwd(layout)` возвращает правильный ответ, и его стоит
подставить. На собранном `.app` это не проявилось (smoke прошёл), но это
зависит от того, что `ROOT.parent` существует и доступен для записи, а не от
контракта.

### 5.4 Предсуществующий падающий тест (не связан с поставкой)

`tests.test_desktop_signing.SchemaMirrorTests.test_the_fallback_schema_version_matches_the_storage_layer`:
`desktop.FALLBACK_SCHEMA_VERSION = 15`, `db.SCHEMA_VERSION = 18`. Это зеркало
константы схемы, к поставке отношения не имеет; `desktop.py` я не правил.

### 5.5 Литтер в корне репозитория от переноса (`.gitignore`)

Перенос старой папки кладёт её копию **рядом со старой папкой** с именем
`data-pre-update-<метка>`. Запуск desktop host из исходников с
`PROXY_WORKBENCH_DATA` в другую папку (в том числе любой запуск
`tests.test_desktop_layout` / `test_desktop_migration`) поэтому оставляет в
корне репозитория `data-pre-update-…` — копию пользовательского `data/`.
`.gitignore` содержит `data/`, но не `data-pre-*/`, поэтому эта папка видна как
неотслеживаемый мусор (в моих прогонах она появилась дважды; обе удалены,
`data/` не пострадал). Владельцу `.gitignore` стоит добавить `data-pre-*/` и
`data-*-backup-*/`.

---

## 6. Изменённые и добавленные файлы

| Файл | Что |
| --- | --- |
| `proxy_workbench/__main__.py` | маршрутизация трёх слоёв; чтение команд CLI из его же парсера |
| `packaging/tray_helper.py` | **новый**: компиляция и staging helper'а меню-бара |
| `packaging/verify_delivery.py` | **новый**: per-user / portable / миграция на собранном артефакте |
| `packaging/desktop_launcher.py` | маршрутизация в frozen-сборке + per-user-пути по умолчанию |
| `packaging/launcher.py` | делегирует `desktop_launcher` |
| `packaging/cli_launcher.py` | тот же per-user-default для отдельного CLI-бинарника |
| `packaging/proxy-workbench-macos.spec` | helper меню-бара в bundle, `proxy_workbench.__main__` в hiddenimports |
| `packaging/proxy-workbench-windows-gui.spec` | `__main__` в hiddenimports, пояснение про worker |
| `packaging/proxy-workbench-windows-cli.spec` | `__main__` в hiddenimports |
| `packaging/build_macos.py` | свой интерпретатор для PyInstaller, проверки helper'а/меню-бара/второго запуска/сирот, `menu_bar` в манифесте, `--no-tray` |
| `packaging/build_windows.py` | свой интерпретатор, workpath на бинарник, `pe_subsystem`, проверка запуска, `SourceDir`/`iscc`-поиск, README в `dist`, отказ на не-Windows |
| `packaging/windows-installer.iss` | абсолютные пути, `SourceDir`, `OutputDir` в кавычках |
| `.github/workflows/windows.yml` | **новый**: сборка и проверка Windows-поставки на runner'е |
| `.github/workflows/ci.yml` | job `macos-artifact` |
| `.github/workflows/release.yml` | `windows-desktop` (два бинарника + installer) и `macos-desktop` |
| `docs/packaging/README.md` | точка входа, состав macOS-сборки, честная Windows-часть, CI-таблица |
| `docs/packaging/RELEASE-NOTES.ru.md` | переписан: что сломано, что исправлено, матрица проверенных сборок, что должен сделать Windows-runner |
| `CHANGELOG.md` | записи о поставке; «Known gaps» приведены к факту (F22 больше не в gaps) |
| `README.md`, `README.ru.md` | мак-приложение и Windows-установщик, `--no-desktop`, per-user пути, «не подписано», `--workers` как потолок, `--max-requests`, `--run-max-bytes`, `--count-what`, `bench` |
| `pyproject.toml` | версия `2.2.1` → `2.3.0` |

## 7. Ограничения подписи

Ни один артефакт не подписан и не занотифицирован, потому что ключей нет.
Ничего не публиковалось, ни один credential не создавался, не покупался и не
хранился. Ни один артефакт не описан как подписанный: манифест пишет
`signed: false` с причиной, `verify_release.py` падает, если манифест
заявляет подпись, которую локальные инструменты не подтверждают, а
`--sign` без реальной идентичности завершается кодом 3, ничего не собрав.
`release.yml` импортирует сертификат только если он уже передан в secrets, и
иначе собирает без подписи.
