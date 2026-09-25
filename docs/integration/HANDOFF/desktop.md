# Handoff: desktop

**Требования:** F23 (поставка Windows/macOS/Linux), дефект 26 (нет macOS-поставки, release notes пусты), R19 (desktop-цель не закрыта).
**Контракт:** `CONTRACTS.ru.md` §3.4 (перенос data path), §3.2 (backup до миграции), §5.4 (`gui.CHILD_ENV` — часть контракта), §7.1 строки F22/F23, §7.2 дефект 26, §7.3 R19/R20. Версия 1.
**База:** ветка `integration/ultra-2026-09-25`, состояние рабочего дерева на момент написания.

**Владение:** `proxy_workbench/desktop.py`, `packaging/*` (кроме `packaging/proxy-workbench.spec`), `docs/packaging/*`, `tests/test_desktop_*.py`, `tests/test_packaging_release.py`. Файлы других исполнителей не редактировались; `proxy-workbench-sources` не читалась и не трогалась.

---

## 1. Прошу внести в чужие файлы

### 1.1 `proxy_workbench/paths.py` — frozen больше не пишет рядом с исполняемым файлом

| Место | Что внести | Почему |
| --- | --- | --- |
| `default_data()` (`paths.py:12-28`), ветка `if FROZEN` | Удалить `return Path(sys.executable).resolve().parent / 'data'`. Вместо этого делегировать: `from .desktop import resolve_layout; return resolve_layout().data` | R19: «frozen пишет рядом с executable». В `Program Files` и внутри `.app` писать нельзя, и замороженная сборка там просто не запускается |

Тонкость, которую надо сохранить при делегировании: `resolve_layout()` имеет
**шесть** правил, и правило 5 (исходники) специально воспроизводит текущее
поведение — `data/` рядом с проектом, когда рядом `pyproject.toml` и
`Start.bat`. Если `default_data()` начнёт возвращать per-user-путь и в
checkout, у пользователей `Start.bat`, `run.sh` и pipx пропадёт привычная
папка, а вместе с ней — все настройки и база.

Функцию `default_data()` оставить с той же сигнатурой: её вызывают
`gui.py:1081` и `proxytool.py:1986`.

`worker_command()` (`paths.py:31-35`) менять не нужно — он уже делает то, что
нужно. `desktop.worker_command()` дублирует его с тем же поведением; при
желании `paths.worker_command` может начать делегировать в
`desktop.worker_command`, но это не обязательно: обе функции совпадают, и
контракт на них не меняется.

### 1.2 `proxy_workbench/gui.py` — cwd и worker command воркера

| Место | Что внести | Почему |
| --- | --- | --- |
| `App.start`, `subprocess.Popen(..., cwd=ROOT.parent, env=CHILD_ENV)` (`gui.py:501`) | `cwd=desktop.child_cwd(desktop.resolve_layout().data)`, либо (лучше) один раз при инициализации `App` сохранить `self.cwd = desktop.child_cwd(...)` | F23: «корректные ресурсы, cwd и worker command». `ROOT.parent` — это `paths.PACKAGE.parent`, а внутри bundle это распакованный read-only каталог `sys._MEIPASS`. Воркер, которому нужен рабочий каталог, получает отказ |
| `command = paths.worker_command(...)` (`gui.py:452`) | Оставить `paths.worker_command` — он уже даёт верную команду для frozen и для checkout | Менять нечего; это зафиксировано тестом |
| `main()`, `parser.add_argument('--data', default=paths.default_data())` (`gui.py:1081`) | Значение по умолчанию останется верным после §1.1 | — |

**Чего делать не надо:** `desktop.child_environment()` намеренно **не**
устанавливает `PROXY_WORKBENCH_LANG`. `CHILD_ENV` (`gui.py:76`) — часть
контракта по CONTRACTS §5.4, и тест `tests/test_i18n.py:27` фиксирует его
состав. Второй писатель этой переменной ломал бы перевод интерфейса.

### 1.3 `.github/workflows/release.yml` — macOS job и два Windows-бинарника

Файл не мой (по условию текущей задачи), поэтому прошу владельца workflow.

| Место | Что внести | Почему |
| --- | --- | --- |
| Добавить job `macos-app` на `macos-14` (arm64) | `python packaging/build_macos.py --dist dist --pyinstaller "$(which pyinstaller)"`; приложить `*.zip`, `*.dmg` и `*.manifest.json` | Дефект 26: «Нет macOS-поставки». Release-job для Mac отсутствует |
| То же в job с `--sign --notarize` | Добавить отдельный job или condition, который ставит флаги только при наличии `APPLE_ID`/`APPLE_PASSWORD`/`APPLE_TEAM_ID`; иначе `build_macos.py` вернёт код 3 и job упадёт | Подписывать без ключей нельзя; падать надо громко, а не собирать ad-hoc и назвать подписанным |
| Заменить шаг `Build the executable` в `windows-executable` | `python packaging/build_windows.py --dist dist --installer`, приложить `proxy-workbench-gui.exe`, `proxy-workbench-cli.exe`, `*-portable.zip`, `*-setup.exe` | F23: «GUI без лишней консоли и отдельный CLI». Старый spec даёт один `console=True` бинарник |
| Заменить шаг `Smoke test the executable` | `python packaging/smoke.py dist/proxy-workbench-cli.exe` | `smoke.py` читает вывод `--version` из stdout. У windowed-бинарника stdout нет, поэтому smoke проверяет CLI-бинарник |
| Перед публикацией | `python packaging/verify_release.py dist/*.manifest.json` | Непубликуемый артефакт, не совпадающий с манифестом, должен останавливать release |

### 1.4 `.github/workflows/ci.yml` — macOS собирает `.app`

Добавить в существующий job `package` шаг для `runner.os == 'macOS'`:
`python packaging/build_macos.py --dist dist --no-verify --no-dmg`. Комментарий
в этом job («macOS is intentionally covered as an installed wheel/source-package
path; this job does not claim to produce or validate a signed .app/.dmg») после
этого шага станет неточным и его нужно поправить.

### 1.5 `CHANGELOG.md` — запись в `## [Unreleased]`

`CHANGELOG.md` не входит в мою зону. Прошу добавить в `## [Unreleased]`
(раздел сейчас пуст — это часть R20) запись о:

- macOS-поставке: самодостаточный `.app` arm64, `.zip` и `.dmg`;
- per-user `data`/`cache`/`logs` для установленной сборки и явном portable mode;
- переносе старой папки с preview, backup и сохранением оригинала;
- раздельных Windows GUI/CLI бинарниках и per-user установщике;
- проверке подписи: ad-hoc-сборка честно называется неподписанной.

Текст для переиспользования — `docs/packaging/RELEASE-NOTES.ru.md`.

### 1.6 `packaging/proxy-workbench.spec` — старый spec

Файл исключён из моей зоны условием задачи, поэтому я его не трогал. Он
продолжает собирать один `console=True` бинарник и используется в
`release.yml`/`ci.yml` для Windows. Как только §1.3 будет применён, spec
станет неиспользуемым; удалять его — решение владельца.

### 1.7 `proxy_workbench/maintenance.py` (F24) — правка не требуется

`RUNTIME_DIRS = ("exports",)` (`maintenance.py:32`), поэтому `clear_runtime`
не трогает ни `cache/`, ни `logs/`, ни новые `cache-*`/`logs-*` папки.
Отдельно добавлять их в `RUNTIME_FILES` **не нужно и не следует**: это
пересоздаваемые каталоги, а не runtime-состояние проверки.

---

## 2. Что уже сделано у меня

Публичный API `proxy_workbench/desktop.py` (всё в `__all__`):

**Пути и portable mode**
- `resolve_layout(environ=None, *, frozen_=None, executable=None, platform=None, home=None, package=None) -> Layout`.
  Шесть правил в фиксированном порядке; `Layout(data, cache, logs, mode, root, reason)` с `.portable` и `.as_dict()`.
- `ensure_layout(layout)` — создаёт три папки или бросает `LayoutError`, который называет папку и переменную-исправление.
- `PORTABLE_MARKER_NAME`, `portable_marker(root)`, `is_portable(root)`, `write_portable_marker(root, ...)`;
  константы `DATA_ENV`, `CACHE_ENV`, `LOGS_ENV`, `PORTABLE_ENV`, `UPDATE_MANIFEST_ENV`.
- `is_writable_dir(path)`, `describe_environment(layout=None)`.

**Ресурсы и worker**
- `frozen()`, `app_root(executable=None)`, `portable_root(executable=None)`,
  `macos_bundle(executable=None)`, `resource_root()`, `resource_path(*parts)`.
- `worker_command(*args, executable=None)`, `child_environment(layout, base=None)`,
  `child_cwd(layout)`.

**Перенос старой папки**
- `legacy_data_roots(layout, *, environ=None, executable=None, package=None) -> tuple[Path, ...]`
- `plan_migration(layout, *, environ=None, executable=None, package=None, source=None, now=None) -> MigrationPlan`
  (`items`, `total_bytes`, `missing` — конфликты по содержимому, `backup`, `empty`, `as_dict()`)
- `apply_migration(plan, *, execute=False) -> MigrationResult` (`applied`, `copied`, `skipped`,
  `backup`, `receipt`, `errors`, `ok`, `as_dict()`); пишет `data/migration-receipt.json`.

**Обновление**
- `UpdateManifest` (`.from_dict`, `.from_file`, `.as_dict`), `read_manifest(path_or_payload)`,
  `compare_versions(a, b)`, `find_update(current_version, manifest, *, channel=None) -> UpdateNotice | None`.
- `data_schema_version(data)` (читает `PRAGMA user_version`), `supported_schema_version()`
  (читает `db.SCHEMA_VERSION`, fallback `FALLBACK_SCHEMA_VERSION`),
  `schema_compatible(data, manifest) -> SchemaCompatibility`.
- `verify_artifact(path, manifest, *, platform=None, runner=None, finder=None) -> Verification`.
- `plan_update(layout, manifest, artifact, *, executable=None, platform=None, runner=None, now=None) -> UpdatePlan`,
  `apply_update(plan, *, execute=False) -> UpdateResult`, `rollback(plan, *, execute=False) -> UpdateResult`.
- `sha256_file(path)`, константа `UPDATE_STATE_NAME`.

**Подпись и нотаризация**
- `signing_status(environ=None, *, platform=None, runner=None, finder=None) -> SigningStatus`
- `notarization_status(environ=None, *, platform=None, runner=None, finder=None) -> NotarizationStatus`
- `signature_of(path, *, platform=None, runner=None, finder=None) -> SignatureInfo`
- `publish_label(status, notarized=False) -> str` — единственная формулировка,
  которую можно ставить рядом с артефактом.
- `runner`/`finder` — точки подмены внешних команд, поэтому проверки
  подписи тестируются без настоящих ключей и без сети.

**Хост**
- `main(argv=None) -> int` — тонкий хост: `--print-paths`, `--portable`,
  `--update-notice`, всё остальное передаётся в `gui.main` с `--data`.

**Сборка** (`packaging/`)
- `proxy-workbench-macos.spec` (onedir `.app`, arm64), `proxy-workbench-windows-gui.spec`
  (`console=False`), `proxy-workbench-windows-cli.spec` (`console=True`),
  `windows-installer.iss` (per-user), `desktop_launcher.py`, `cli_launcher.py`.
- `build_macos.py`, `build_windows.py`, `release_manifest.py`, `verify_release.py`.

---

## 3. Совместимость

**Что ломается, если §1.1 не внести.** Ничего не сломается — и это проблема.
`desktop.py` будет жить рядом с `paths.default_data()`, который продолжит
возвращать `data/` рядом с `.exe`, и R19 останется открытым: собранный `.app`
не сможет создать свою папку данных, потому что bundle read-only. Ни один
тест этого не поймает, если не вызвать `paths.default_data()` при `frozen=True`.

**Что НЕ ломается.** `data/` в checkout, `Start.bat`, `run.sh`, `Start.command`,
`python -m proxy_workbench`, wheel, pipx, Docker, `gui.py` и `proxytool.py` в
текущем виде. `packaging/smoke.py` не менялся. `packaging/proxy-workbench.spec`
не менялся. Существующие тесты не трогаются.

**Известное ограничение моей части.** `desktop.py` импортирует
`proxy_workbench.i18n.tr` (для сообщений терминала) и `proxy_workbench.paths.PACKAGE`
(для ресурсов). Оба — только на чтение, чужие функции не меняются. `paths.PACKAGE`
импортируется на уровне модуля, поэтому появление `desktop` в `sys.modules`
не тянет за собой `db`, `gui` или `proxytool`.

---

## 4. Проверки

Запускались из корня репозитория, только свои тесты:

| Команда | Результат |
| --- | --- |
| `.venv/bin/python -m unittest tests.test_desktop_layout` | Ran 24 tests — OK |
| `.venv/bin/python -m unittest tests.test_desktop_migration` | Ran 14 tests — OK |
| `.venv/bin/python -m unittest tests.test_desktop_update` | Ran 34 tests — OK |
| `.venv/bin/python -m unittest tests.test_desktop_signing` | Ran 27 tests — OK |
| `.venv/bin/python -m unittest tests.test_packaging_release` | Ran 25 tests — OK |
| `.venv/bin/python packaging/build_macos.py --dist /tmp/pw-dist --pyinstaller /tmp/pwbuild-venv/bin/pyinstaller` | собраны `Proxy Workbench.app`, `…-macos-arm64.zip`, `…-macos-arm64.dmg`, манифест; встроенная проверка запуска приложения прошла |
| `.venv/bin/python packaging/verify_release.py /tmp/pw-dist/proxy-workbench-2.2.1-macos-arm64.manifest.json` | `"ok": true`, exit 0 |
| `codesign --display --verbose=4 "Proxy Workbench.app"` | `Signature=adhoc` |
| `spctl -a -vvv "Proxy Workbench.app"` | `rejected` |
| `.venv/bin/python -c "… desktop.signing_status()"` | `available: false` — в связке ключей нет кодовых идентичностей |
| `.venv/bin/python -c "… desktop.notarization_status()"` | `available: false` — нет `APPLE_ID`/`APPLE_PASSWORD`/`APPLE_TEAM_ID` |

**Что осталось непрочитанным / невыполненным:**
- `python -m unittest discover -s tests` — не запускался (по условию задачи его
  выполняет интегратор).
- `packaging/build_windows.py` — не запускался: нет Windows и нет `iscc`.
  Проверены только дескрипторы (`tests/test_packaging_release.py`).
- Inno Setup-скрипт не компилировался: `iscc` на этой машине отсутствует.
- `packaging/smoke.py` не прогонялся против новых артефактов: macOS-сборка
  запускается с временным `HOME` и проверяется на своей странице, а полный
  сценарий smoke требует CLI-бинарника, который на этой ОС не собирается.
- Подписанный и нотарифицированный артефакт **не собран**: ключей нет.
  Пайплайн проверен на отказ (код 3) и на ad-hoc-детект, но не на успешном
  подписании.

---

## 5. Открытые вопросы

1. **Кто владелец `packaging/proxy-workbench.spec` после перехода на два
   бинарника.** Сейчас файл вне моей зоны по условию задачи, и я его не
   трогал. Он собирает один `console=True` бинарник, который больше не
   нужен, если применить §1.3. Удаление — решение владельца.
2. **Windows-сборка не проверена ничем, кроме чтения.** Сборщик написан по
   документации PyInstaller и Inno Setup, но ни разу не выполнялся на
   Windows. До первого прогона в CI его следует считать непроверенным, а
   release notes — не упоминающим Windows-поставку как готовую.
3. **Автообновление при запуске не делал.** Есть проверка, backup, проверка
   схемы, откат и `--update-notice`. Автоматическая загрузка и применение
   требуют решения владельца релизов: откуда, с каким каналом, что делать при
   `channel != stable`. F23 требует «update notice и управляемое обновление» —
   notice и управляемое обновление есть, автоматического запуска нет.
4. **F22 (tray, фоновый режим) не входил в задачу** и не реализован. Если
   desktop-поставка считается закрытой только вместе с фоновой работой, это
   отдельная работа по этому же модулю.
