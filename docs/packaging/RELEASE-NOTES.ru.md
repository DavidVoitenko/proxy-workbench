# Release notes: desktop-поставка

**Версия:** 2.3.0
**Дата:** 26 сентября 2026
**Статус:** macOS-артефакт собран и проверен живым запуском на этой машине;
Windows-артефакт не собирался (машина — macOS), CI-задание на реальном
Windows-runner добавлено, но ещё не выполнялось

Этот файл — единственное место, где перечислено, что в поставке **есть**, а
что **только подготовлено**. Он написан по результатам выполненных здесь
проверок, а не по плану. Всё, что здесь не перечислено как проверенное, не
проверено.

## Что было сломано и что исправлено

| Дефект | Как выглядел | Что сделано | Чем проверено |
| --- | --- | --- | --- |
| **В `.app` не было меню-бара** | `desktop.py` собирает нативный Swift-helper, но ни spec, ни `build_macos.py` не клали его в bundle. Приложение запускалось и честно писало «в сборке нет helper меню-бара» | `packaging/tray_helper.py` компилирует helper через `swiftc` (тот самый `desktop.ensure_tray_helper`, без второй копии логики) и кладёт его в bundle как данные; spec без него прерывает сборку | Живой запуск собранного `.app`: helper в `Contents/Frameworks/tray/`, в его логе `ready {"status_item":true,"visible":true,"frame":[1186,1084,50,33],"menu_items":9}` |
| **Обычный запуск не доходил до фонового слоя** | `python -m proxy_workbench` и консольный скрипт `proxy-workbench` уходили в `gui.main`; меню-бар, один экземпляр, автозапуск и sleep/wake не работали при обычном запуске | `proxy_workbench/__main__.py` маршрутизирует: без аргументов и флаги хоста → `desktop.main`; `gui` и `--no-desktop` → только интерфейс; команда (`scan`, `collect`, …) → CLI. Frozen-сборки используют тот же маршрут | `proxy-workbench` без аргументов поднимает desktop host; повторный запуск отвечает «The application is already running: …», процессов 1 → 1 |
| **Замороженная сборка не могла выполнить проверку** | Worker запускается как `[sys.executable, 'scan', …]`; launcher звал `desktop.main()`, который отдавал `scan` в argparse интерфейса: `error: unrecognized arguments: scan` | Frozen launcher (`packaging/desktop_launcher.py`) маршрутизирует командные слова в CLI тем же правилом | `packaging/smoke.py` против собранного `.app`: полный цикл collect → scan → API → gateway, `smoke test passed` |
| **Windows-сборщик был нерабочим** | `pyinstaller` искался в `PATH` (сборка в venv без неё падала с `No such file or directory`), оба бинарника делили один workpath, `portable-README.txt` для установщика никогда не писался в `dist`, `[Files]` ссылался на относительные пути | `python -m PyInstaller` через тот же интерпретатор, свой workpath на бинарник, README пишется в `dist`, установщику передаётся `SourceDir`, добавлен поиск `iscc` в стандартных папках | Разбор всех spec, статический запуск spec с подменённым API PyInstaller, чтение PE-subsystem из синтетического PE. **Артефакт на Windows не собирался** |
| **`console=False` ничем не подтверждалось** | Единственная проверка — grep по тексту spec | `build_windows.py` читает PE-subsystem из самого `.exe` и отказывается публиковать пару, где GUI-бинарник не windowed или CLI не console | Функция проверена на синтетических PE32/PE32+; проверка на настоящем `.exe` выполнится в Windows CI |
| **Версия расходилась** | `branding.PRODUCT_VERSION = 2.3.0`, `pyproject.toml = 2.2.1`: bundle объявлял 2.3.0, wheel назывался 2.2.1 | `pyproject.toml` приведён к 2.3.0 | `pip show` / `python -m proxy_workbench --version` / `Info.plist` дают одно число |

## Что добавлено

| Что | Состояние | Проверка |
| --- | --- | --- |
| Точка входа с тремя слоями: приложение / интерфейс / CLI, `--no-desktop` как выход | готово | живой запуск собранного `.app` и из исходников |
| `packaging/tray_helper.py`: сборка helper меню-бара для bundle | готово | `swiftc` отрабатывает за ~2 с, результат лежит в bundle и запускается |
| `packaging/verify_delivery.py`: per-user пути, portable mode, перенос старой папки — на настоящем артефакте | готово | 15 из 15 проверок на собранном `.app` |
| Валидация собранного macOS-артефакта расширена: helper в bundle, меню-бар по логу helper'а, второй запуск не плодит копию, нет orphan-процессов | готово | `packaging/build_macos.py` падает с кодом 2, если меню-бар не подтвердился |
| Валидация Windows-артефакта: PE-subsystem, запуск GUI `.exe` с временным `%LOCALAPPDATA%`, второй запуск, неизменность папки программы | готово, **не выполнено** | требует Windows |
| `.github/workflows/windows.yml` — сборка, запуск и проверка на настоящем Windows-runner | готово, **не выполнялось** | требует push/PR |
| Job `macos-artifact` в CI: сборка `.app`, smoke, сверка манифеста, выгрузка артефакта | готово, **не выполнялось** | требует push/PR |
| `release.yml`: Windows теперь отдаёт два бинарника + installer, добавлен macOS-job | готово, **не выполнялось** | требует тега |
| `proxy_workbench/desktop.py`: per-user пути, portable mode, миграция, worker command и cwd, подпись/нотаризация, обновление с backup и откатом | готово (предыдущая работа) | `tests/test_desktop_layout.py`, `test_desktop_migration.py`, `test_desktop_update.py`, `test_desktop_signing.py` |

## Матрица проверенных сборок

Собрано здесь, 26.09.2026, macOS 26.5.2, arm64, Python 3.14, PyInstaller 6.22.3.

| Сборка | Где проверена | Что именно проверено | Подпись |
| --- | --- | --- | --- |
| wheel + `pipx` | CI (ubuntu/macOS/Windows) | `packaging/smoke.py` | нет |
| `Start.bat` / `run.sh` | локально, `sh -n` + smoke | синтаксис и сквозной прогон | нет |
| Docker CLI | CI | `--version`, `--help`, `clear-data --yes` | нет |
| **macOS `Proxy Workbench.app` arm64** | **эта машина, живьём** | структура bundle; `ui/index.html` и `sources.json` внутри; helper меню-бара внутри и исполняем; запуск с временным `HOME`; страница с токеном на loopback-порту, выбранном ОС; меню-бар подтверждён логом helper'а (статус-элемент на экране, 9 пунктов, 5 действий); повторный запуск → «уже запущено», процессов 1 → 1; полный цикл collect → scan → API → gateway; **15/15 проверок поставки** (per-user, portable, миграция); bundle не изменён; после `SIGKILL` helper уходит сам | **ad-hoc, не подписан** |
| macOS `.zip` / `.dmg` | эта машина | сборка, SHA-256 в манифесте, `verify_release.py` | нет |
| Windows GUI `.exe` | **нигде** | только spec, статический разбор и чтение PE-заголовка | нет |
| Windows CLI `.exe` | **нигде** | то же | нет |
| Windows installer | **нигде** | только скрипт Inno Setup и его настройки | нет |
| Windows: запуск, второй запуск, sleep/wake, меню-бар | **нигде** | подготовлено, но не проверялось | нет |

## Чего нет и не обещается

- **Подписанных и нотарифицированных артефактов нет.** На машине сборки нет
  ни Developer ID Application в связке ключей, ни учётных данных нотаризации:
  `signing_status.available = false`, `notarization_status.available = false`.
  Собранный bundle получает **ad-hoc** подпись, `spctl` его отклоняет, и он
  честно записан в манифесте как `signed: false, kind: adhoc`. Этот проект не
  создаёт, не покупает и не хранит signing credentials; пайплайн только
  проверяет, есть ли уже имеющиеся.
- **Автоматической загрузки обновлений нет.** Есть проверка происхождения,
  backup, проверка схемы, откат и `--update-notice`. Публикующего сервера у
  проекта нет.
- **Windows-поставка не собрана и не запущена.** Машина сборки — macOS;
  PyInstaller собирает под ту машину, на которой работает. Всё, что можно было
  проверить без Windows, проверено (разбор и запуск spec, entry point,
  `console=False`/`console=True`, ресурсы, PE-subsystem, передача путей
  установщику, поведение при отсутствии `iscc`); всё, что нельзя, перечислено
  в разделе «Что должен сделать Windows-runner».
- **Intel и universal2 macOS не собираются** и не заявляются: для universal2
  нужны оба среза и их слияние, а `build_macos.py` на это не претендует.
- **Меню-бар есть только на macOS.** На Windows и Linux его нет, и в матрице
  выше он не заявлен.
- **Sleep/wake на Windows не определяется.** Там сон входит в монотонные часы,
  поэтому сравнение wall/monotonic его не видит, а нативного наблюдателя для
  Windows в этом слое нет. На macOS наблюдатель есть (NSWorkspace), на Linux
  работает сравнение часов. Это ограничение названо, а не закрыто заглушкой.
- **Собственного окна нет.** Интерфейс остаётся страницей в браузере; новый
  desktop-фреймворк не вводился, потому что F23 разрешает его только при
  проверенном ограничении, а такого ограничения здесь не обнаружено. Меню-бар
  — это `NSStatusItem` над той же страницей, а не второе приложение.

## Проверки, выполненные в этой работе

Живой запуск собранного macOS-артефакта — всё через процессы, сокеты и файлы,
которые увидел бы пользователь:

```sh
python packaging/build_macos.py --dist dist --no-dmg
python packaging/verify_delivery.py "dist/Proxy Workbench.app"
python packaging/verify_release.py dist/proxy-workbench-2.3.0-macos-arm64.manifest.json
python packaging/smoke.py "dist/Proxy Workbench.app/Contents/MacOS/Proxy Workbench"
```

`build_macos.py` завершился кодом 0 и записал в манифест:

```json
"menu_bar": {"confirmed": true, "checked": true,
             "helper": "…/Proxy Workbench.app/Contents/Frameworks/tray/proxy-workbench-tray",
             "report": {"status_item": true, "visible": true, "frame": [1186, 1084, 50, 33], "menu_items": 9}},
"verified": {"launched": true, "detail": {"bundle_unchanged": true,
             "second_launch": {"exit_code": 0, "instances_before": 1, "instances_after": 1,
                               "said": ["The application is already running: http://127.0.0.1:62033/"]}}}
```

`verify_delivery.py` — 15 из 15 проверок `ok`: per-user адрес/data/cache/logs и
неизменность папки программы; portable mode по маркеру (данные внутри копии
`.app`, всё остальное в копии не изменено); перенос старой папки (9 файлов,
база через SQLite backup, старая папка на месте, receipt называет рабочую
копию, названный им backup существует, второй запуск не переносит заново — одна
запись `migration.applied` в журнале).

Меню-бар проверяется не чтением исходников и не наличием файла, а двумя
независимыми фактами: helper виден в `ps` отдельным процессом, и сам helper
пишет в `<logs>/tray.log`, что нарисовал статус-элемент. Отдельно проверено,
что после `kill -9` приложения helper выходит сам («exiting: the control socket
was closed») и процессов не остаётся. Скриншотом меню-бар не снимался: у
процесса, который его запустил, нет прав Универсального доступа, и
`System Events` отвечает отказом — это ограничение окружения, а не результат
проверки.

## Что должен сделать Windows-runner

Ниже — не пожелание, а то, что job в `.github/workflows/windows.yml` делает
сейчас и что он падает, если не получилось:

1. `python packaging/build_windows.py --dist dist --installer` на
   `windows-2022`:
   - собрать `proxy-workbench-gui.exe` (`console=False`) и
     `proxy-workbench-cli.exe` (`console=True`), каждый со своим workpath;
   - прочитать PE-subsystem у обоих и отказаться, если GUI не windowed или CLI
     не console;
   - запустить GUI `.exe` с пустым `%LOCALAPPDATA%`/`%APPDATA%`, дождаться
     `gui-address.json`, забрать страницу по loopback, убедиться, что папка
     программы не изменилась, и что второй запуск дошёл до первого
     (`instances_before == instances_after`);
   - записать `portable-README.txt` в `dist`, собрать portable-zip и, если
     `iscc` есть на runner'е, per-user installer;
   - записать манифест с `subsystems`, `verified` и `installer.built`.
2. `python packaging/smoke.py dist/proxy-workbench-cli.exe` — сквозной цикл
   CLI-бинарника.
3. `python packaging/verify_release.py <manifest>` — пересчёт SHA-256 и проверка
   подписи для каждого артефакта.
4. Выгрузка артефактов job'ом.

Чего job **не** делает и не будет: не подписывает (нужен сертификат в хранилище
и `PROXY_WORKBENCH_SIGN_THUMBPRINT`), не проверяет меню-бар (его на Windows
нет), не проверяет sleep/wake (наблюдателя для Windows в слое нет). Это
ограничения продукта, а не недоработка job'а.
