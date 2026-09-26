# Handoff: area-probes (probes / reputation / anonymity / geoip)

**Владею:** `proxy_workbench/probes.py`, `proxy_workbench/reputation.py`,
`proxy_workbench/anonymity.py`, `proxy_workbench/geoip.py`,
`tests/probearea_support.py`, `tests/test_probearea_*.py`.
**Требования:** F01, F07, F08 (частично), F20, дефекты 13, 14, 15.
**Ветка:** `integration/ultra-2026-09-25`.
**Правило проверки:** функция считается работающей, только если её НАСТОЯЩИЙ
путь пройден на локальных сокетах и возвращает правильный результат. Публичные
прокси, реальные DNSBL-зоны и сторонние сервисы не опрашивались ни разу.

Все живые прогоны воспроизводимы:

```
.venv/bin/python tests/test_probearea_modes.py         # F01, 7 режимов + приёмка
.venv/bin/python tests/test_probearea_options.py       # F07, параметры и пресеты
.venv/bin/python tests/test_probearea_capabilities.py  # F20 + дефект 15
.venv/bin/python tests/test_probearea_judge_dnsbl.py   # дефекты 13 и 14
.venv/bin/python tests/test_probearea_geoip.py         # F08
```

Тесты: `Ran 93 tests … OK` (пять моих модулей).

---

## 1. Что сделано, чем доказано

### F01 — семь режимов, каждый запущен

Файл `tests/test_probearea_modes.py`, отчёт `live_report()`:

```
1 collect_only : evidence=collected network=False -> вердикта не даёт
2 tcp          : ok=True stage=tcp code=None connect_ms=25.13
3 handshake    : ok=True stage=handshake code=None
2b tcp-only    : ok=False evidence=none is_working=False  <- TCP-only НЕ передающий прокси
4 basic        : ok=True evidence=transfer_ok bytes=95 is_working=True targets=1
5 services     : ok=True evidence=transfer_ok targets=1
6 custom       : ok=True evidence=transfer_ok targets=1
7 recheck      : base=basic evidence=transfer_ok digest(base)==digest(recheck): True
7b monitor     : mode=basic monitoring=True evidence=transfer_ok <- режим задания, не новый вердикт
```

- **Приёмка «найти 5 рабочих, URL не вводится»** — пройдена: 7 кандидатов
  (5 рабочих CONNECT-туннелей, 2 TCP-only), `найдено рабочих: 5/7`, у каждой
  строки точный уровень `transfer_ok` или `none`, `is_working()` совпадает.
- **«Отказ нейтрального target отличается от отказа всех прокси»** — пройдена:

```
все прокси упали, прямая проверка цели тоже упала: code=TARGET_UNAVAILABLE target_unavailable=True
прокси мёртвые, цель не проверялась: code=None (вывода о цели нет: True)
одна исправная + одна мёртвая: target_failure_signal → None (никто не виноват)
```

- **Мониторинг — режим задания, не новый вердикт**: `Mode.requested`/`PlanOutcome.requested_mode`
  хранят, что выбрал пользователь, `PlanOutcome.base` — какая лестница дала
  доказательство, `monitoring=True` — флаг для планировщика и пула.
  `resolve_mode('monitor', base='collect_only')` → ошибка «нечем измерять».

**Найденный и починенный дефект (был незаметен):** `run_plan` записывал в
`PlanOutcome.mode` имя `recheck`, которого нет в `_MODE_SPECS`, поэтому
`evidence_for()` возвращала `none` — **перепроверка с реальной передачей
записывалась как «доказательств нет»**. Теперь `PlanOutcome.base` несёт базовый
режим, а `evidence_for()` его разворачивает; строка не может оказаться с
уровнем, за которым нет лестницы.

### F07 — параметры, единицы, лимиты, одна валидация

**Найденный молча игнорируемый параметр: `max_body_bytes` в `ProbeOptions`.**
Он валидировался, показывался в плане и в `describe()`, и **не участвовал ни в
одном запросе**: `run_probe` всегда брал `target.max_body_bytes`. Пользователь,
поставивший потолок 4 КБ, получал 400 КБ.

Починено как потолок бюджета (`probes.body_budget`): цель может только
*уменьшить* лимит, поднять его нельзя. Доказано живым потоком:

```
цель сама просит max_body_bytes=400000
потолок опций  262144 -> body_limit= 262144 прочитано= 262144 ok=True
потолок опций   16384 -> body_limit=  16384 прочитано=  16384 ok=True
потолок опций    4096 -> body_limit=   4096 прочитано=   4096 ok=True
```

`TargetOutcome.body_limit` и `TargetOutcome.to_public()['body_limit']` показывают
применённый лимит, поэтому «какой потолок был» видно в строке результата.

**Пресеты «быстро / баланс / тщательно / экономно»** (`probes.PRESETS`,
алиасы `быстро/баланс/тщательно/экономно` + `quick/normal/detailed/economy/light`):

```
fast      connect=2.0 handshake=3.0 read=4.0 whole=12.0 attempts=1 backoff=0.0×1.0≤0.0 body≤65536  redir≤0 худший=9.0с  помещается=True
balanced  connect=4.0 handshake=6.0 read=8.0 whole=30.0 attempts=2 backoff=0.5×2.0≤8.0 body≤262144 redir≤0 худший=18.0с помещается=True
thorough  connect=6.0 handshake=10.0 read=20.0 whole=90.0 attempts=3 backoff=1.0×2.0≤10.0 body≤1048576 redir≤2 худший=36.0с помещается=True
frugal    connect=3.0 handshake=4.0 read=6.0 whole=20.0 attempts=1 backoff=0.0×1.0≤0.0 body≤32768  redir≤0 худший=13.0с помещается=True
```

Пресет — **полный набор**, а не патч: переключение сценария не оставляет
backoff или лимит редиректов от прошлого (дефект 24). Явно заданные поля
побеждают пресет и видны в плане:

```
пресет=thorough, задано attempts=4 и max_body_bytes=8192 -> {"attempts": 4, "max_body_bytes": 8192}
остальные значения от пресета: {"connect_timeout_s": 6.0, ..., "max_redirects": 2}
plan.to_public()['preset'] == 'thorough'; plan.to_public()['option_limits'] содержит единицы
```

Имя пресета **не входит** в `plan_digest`: `{'preset':'fast'}` и явные значения
`PRESETS['fast']` дают один digest — пресет не меняет измеряемое.

Ни один непредусмотренный параметр не теряется молча:

```
{'timeout': 5}                -> E_VALIDATION_UNKNOWN_FIELD
{'verify': False}             -> E_VALIDATION_UNKNOWN_FIELD
{'max_retry': 3}              -> E_VALIDATION_UNKNOWN_FIELD
{'judge_max_bytes': 1048576}  -> E_VALIDATION_UNKNOWN_FIELD  (у judge своё поле)
{'speed_max_bytes': 1048576}  -> E_VALIDATION_UNKNOWN_FIELD  (у speed-своей цели своё)
```

**POST/body/API-auth только в собственном профиле:**

```
POST на публичной цели        -> E_VALIDATION_TARGET_UNSAFE
body на публичной цели        -> E_VALIDATION_TARGET_UNSAFE
API-auth на публичной цели    -> E_VALIDATION_TARGET_UNSAFE
credential-заголовок          -> E_VALIDATION_TARGET_UNSAFE
собственный профиль           -> ok, публичное представление без значения секрета
повтор небезопасного метода   -> E_VALIDATION_RETRY_UNSAFE
custom CA: check_hostname=True verify_mode=CERT_REQUIRED  (TLS не отключается)
```

Значение секрета передаётся только ссылкой (`auth.secret_ref`), литерал
`value/token/password/secret` отвергается.

### F20 и дефект 15 — новые виды измерений

Каждый вид получил контролируемый endpoint, бюджет, свой словарь исходов и
локальные positive/negative сценарии (вывод `tests/test_probearea_capabilities.py`):

```
POSITIVE websocket  /ws                    state=ok              code=None
NEGATIVE websocket  /ws-none (нет 101)     state=not_upgraded    code=WS_NOT_UPGRADED
NEGATIVE websocket  /ws-silent (нет pong)  state=closed          code=WS_CLOSED
POSITIVE duration   /hold?seconds=2        state=ok              code=None
NEGATIVE duration   /hold-flaky (рвёт)     state=short           code=CONNECTION_CLOSED
NEGATIVE duration   /hold?seconds=0.3      state=short           code=CONNECTION_CLOSED
POSITIVE media      /manifest.m3u8         state=ok              code=None
NEGATIVE media      /manifest-empty        state=no_manifest     code=MEDIA_NO_MANIFEST
NEGATIVE media      /manifest-missing      state=manifest_failed code=HTTP_404
NEGATIVE media      /manifest-bad-segment  state=segment_failed  code=HTTP_404
POSITIVE HTTP API assertions                ok=True bytes=285 body_limit=32768
NEGATIVE degraded API                        ok=False code=JSON_ASSERT :: status != 'ok'
NEGATIVE битый JSON                          ok=False code=JSON_ASSERT :: body is not valid JSON
NEGATIVE пустое тело                         ok=False code=BODY_TOO_SMALL
```

Словари исходов (ни один не может быть выдан за другой):

| вид | endpoint | бюджет | исходы |
| --- | --- | --- | --- |
| `websocket` | URL с апгрейдом 101 и ответом на ping | `handshake_timeout_s`, `ping_timeout_s`, `max_pings`, `max_frame_bytes` | `ok` / `not_upgraded` / `no_pong` / `closed` / `error` |
| `duration` | URL, который держит сокет | `hold_s`, `min_sustained_s`, `min_bytes` | `ok` / `short` / `error` |
| `media` | манифест + первый сегмент | `manifest_max_bytes`, `max_segments`, `segment_max_bytes`, `min_segment_bytes` | `ok` / `no_manifest` / `manifest_failed` / `segment_failed` / `error` |
| `http_api` | JSON-документ + `json_assertions` | `max_body_bytes`, `whole_probe_timeout_s` | `ok` / `JSON_ASSERT` с путём |

**Дефект 15 — окно, минимумы, insufficient:**

```
2 МБ @2 МБ/с            state=ok           mbps=16.15 bytes=2000000 chunks=71 ttfb=25.19ms transfer=990.75ms
2 МБ без троттлинга     state=insufficient mbps=None :: окно передачи 0.010 с короче минимума 0.25 с
один чанк (2 МБ)        state=insufficient mbps=None :: одного чанка недостаточно: нужно минимум 2
два чанка, окно 1 с     state=ok           mbps=16.0  ttfb=500.0ms transfer=1000.0ms
обрыв потока            state=error        mbps=None code=ConnectionReset
100 КБ @200 КБ/с        state=insufficient mbps=None :: мало данных: 131072 байт при минимуме 1048576
```

Окно считается от первого байта до последнего; TTFB, transfer и total — разные
поля. `run_speed_test` пересчитывает число из измеренного окна, тест сверяет
`bytes*8/(transfer_ms/1000)/1e6` с `mbps`.

**Cold и reused не смешиваются:**

```
cold    conn=cold   ttfb=25.19ms transfer=990.75ms total=1015.94ms
reused  conn=reused ttfb=2.0ms   transfer=985.0ms  total=987.0ms
mixed_speed_connections([cold,reused]) = True
comparable_speed(...,'cold')=1  (...,'reused')=1
```

Транспорт, проигнорировавший `reuse=True`, получает честную метку `cold` и
дописку «транспорт открыл новое соединение вместо повторного».

**Нет общего обещания.** В матрице возможностей `udp_transport`,
`http2_or_http3` и `calls_video_any_service` помечены `supported=False` с
объяснением; ни одна строка не обещает «все звонки/видео работают».

### Дефект 13 — judge до elite

`anonymity.classify` теперь делегирует `probes.classify_echo`; историческая
двухключевая форма сохранена, подробная — `anonymity.classify_detail`.

```
валидный echo чужого IP (proxy ok)   | elite      | None
валидный echo + заголовки прокси     | anonymous  | None            (via)
утечка своего IP                      | transparent| None            (real_ip)
пустой ответ                          | unknown    | JUDGE_INVALID   (empty_body)
CAPTCHA / JS-challenge                | unknown    | JUDGE_CHALLENGE (challenge_page)
страница без адреса (была elite!)     | unknown    | JUDGE_INVALID   (no_echo_address)
bootstrap не показал наш IP           | unknown    | JUDGE_UNVERIFIED(judge_unverified)
нет baseline (own_ips пуст)           | unknown    | JUDGE_UNVERIFIED(no_baseline)
```

Проверено и на живом self-hosted judge: bootstrap отдаёт `origin: 127.0.0.1`,
`own_ips = {'127.0.0.1'}`, `/challenge` (HTTP 503) → `JUDGE_CHALLENGE`.
Требование анонимности без judge даёт
`E_VALIDATION_ANONYMITY_REQUIRED` с текстом «Молча проверять без judge нельзя —
уровень был бы выдуман», а не тихий сброс в `any`.

### Дефект 14 — DNSBL

`reputation.reverse_ip` для IPv6 даёт 32 точечные ниббла, roundtrip в
упакованную форму и равенство compressed/expanded проверены. Каждый исход
прогнан отдельно:

```
listed 127.0.0.2         -> listed   code=None
clear NXDOMAIN           -> clear    code=None
blocked 127.255.255.254  -> unknown  code=DNSBL_ACCESS
quota                    -> unknown  code=DNSBL_QUOTA
access refused           -> unknown  code=DNSBL_ACCESS
SERVFAIL                 -> unknown  code=DNSBL_ERROR
REFUSED                  -> unknown  code=DNSBL_ACCESS
NOERROR без адресов      -> unknown  code=DNSBL_ERROR
```

Коды, объявленные самой зоной, не угадываются: один и тот же ответ `127.0.0.254`
в общей зоне — `listed`, в зоне с `blocked_codes=('127.0.0.254',)` —
`unknown`/`DNSBL_ACCESS`. `clean` выдаётся только когда **все** зоны ответили
`clear`; иначе `unknown` с кодом первой причины. Бюджет запросов оставляет
нетронутые зоны с `BUDGET_EXHAUSTED` и `queried=False`. Старая форма seam
(`getaddrinfo`-tuple) работает: `listed` / `clear` как раньше.

### F08 — география

`tests/test_probearea_geoip.py` грузит настоящие gz+CSV базы через
`CountryDB.from_file` / `AsnDB.from_file`:

```
загружено диапазонов: страны=3 провайдеры=3 (ZZ, битая строка и развёрнутый диапазон отброшены)
5.9.1.2        страна=DE  asn=24940 'Hetzner Online GmbH'        hosting=True
2.16.255.255   страна=NL  asn=6079  'Leidos Netherlands B.V.'    hosting=False
9.9.9.9        страна=None
read-only фильтр ни одного сокета не открыл: да   (socket.socket подменён на запрет)
```

**Найденный и починенный дефект:** `proxy_host('5.9.1.2:8080')` возвращал `''`
(`partition('://')[2` без схемы), поэтому адрес без схемы молча давал страну
`None` — «неизвестное, выданное за проверенное». Теперь разбираются и URL, и
`host:port`, и `[2001:db8::1]:1080`, и учётные данные до `@`.

---

## 2. Прошу внести в чужие файлы

### 2.1 `proxy_workbench/proxytool.py` — подключить новый transport-seam

Мой модуль сетевого кода не содержит. Интегратор реализует четыре метода:

```python
class Transport:
    async def send(self, request: ProbeRequest, *, options) -> ProbeResponse: ...
    async def download(self, target: SpeedTarget, *, options, reuse=False) -> TransferTrace: ...
    async def websocket(self, request: ProbeRequest, *, options, spec) -> WsTrace: ...
    async def hold(self, request: ProbeRequest, *, options, spec) -> HoldTrace: ...
```

Рабочий пример на loopback через настоящий мокси-прокси:
`tests/probearea_support.py::LoopbackTransport` (proxytool это уже делает для
httpx; там же видно, как выпустить `CONNECT` вручную — `asyncio.open_connection`
не умеет proxy).

Обязательное при заполнении:

| место | что сделать |
| --- | --- |
| `check_proxy` / `scan` | брать режим из `probes.build_plan`, уровень доказательства — из `PlanOutcome.evidence`; строка, измеренная в `tcp`/`handshake`, не может получить `transfer_ok` (`probes.is_working` это уже проверяет) |
| `measure_speed` (`:1394`) | окно и минимумы больше не считать вручную: `probes.measure_speed(trace, limits)`; один чанк даёт `state='insufficient'` без числа |
| `judge_proxy` (`:1460`) | `verdict = anonymity.classify_detail(body, own_ips, judge_verified=bool(own_ips))` и передать `code`/`exit_ip` в `anonymity.result(..., code=…, exit_address=…)`. Сейчас код ответа judge (`JUDGE_INVALID`/`JUDGE_CHALLENGE`/`JUDGE_UNVERIFIED`) в строку не попадает, и «почему unknown» теряется |
| `detect_own_ips` (`:1434`) | для self-hosted judge нужен `anonymity.extract_public_ips(body, global_only=False)`, иначе loopback/LAN-адрес отбрасывается и baseline пустеет |
| `request_once` (`:1196`) | если подключаете `probes.run_probe`, `attempts>1` с неидемпотентным методом уже отвергается `E_VALIDATION_RETRY_UNSAFE` — не обходите это своей попыткой |
| `--min-anonymity` (`:3460`) | уровень без judge обязан давать ошибку; сегодня `export(..., min_anonymity='elite')` без judge просто молча выгружает строки. См. §2.3 |
| публичная строка | `code`/`stage`/`detail` брать из `TargetOutcome.to_public()` и `CapabilityOutcome.to_public()`; справочник кодов — `probes.MEASUREMENT_CODES` |

### 2.2 `proxy_workbench/gui.py` и `api.py` — пресеты и границы измерений

| место | что показать/принять |
| --- | --- |
| настройки проверки | поле `preset` (`fast`/`balanced`/`thorough`/`frugal` + русские алиасы) и **видимые** переопределения; `probes.presets_manifest()` отдаёт все четыре с единицами, лимитами и `fits` |
| отображение результата | `TargetOutcome.body_limit` (применённый потолок тела), `TargetOutcome.connection` (`cold`/`reused`), `CapabilityOutcome.state` |
| валидация | GUI/CLI/API должны звать `probes.validate_options` / `validate_target` / `validate_capability`, а не собственные копии правил: сейчас один и тот же пресет в трёх клиентах разъедется |
| матрица возможностей | `probes.capability_matrix()` и `probes.capabilities_manifest()`; не показывать `supported=False` как «почти работает» |
| basic без URL | `probes.BASIC_PROBES` имеют `verified_at=None`, `probes_manifest()['verified_count'] == 0`. **Нужен один ручной прогон и простановка `verified_at`**, иначе «предустановленные probes» нельзя называть подтверждёнными (MASTER-PROMPT §8). |

### 2.3 `tests/test_probes_reference.py` — устаревшее утверждение (мой файл править не могу)

`tests/test_probes_reference.py:205-226` закрепляет, что `websocket_handshake`,
`long_lived_connection` и `media_manifest_segment` **не** измеряются:

```python
    def test_unmeasured_transports_are_declared_unsupported(self):
        matrix = {item['id']: item for item in pr.capability_matrix()}
        for name in ('websocket_handshake', 'long_lived_connection', 'media_manifest_segment',
                     'udp_transport', 'http2_or_http3'):
            self.assertFalse(matrix[name]['supported'])
```

Три из пяти имён теперь измеряются (см. §1 F20), поэтому тест падает на трёх
subtest. Утверждение надо переписать под правильное поведение — это ровно тот
случай, который MASTER-PROMPT §7.1 запрещает оставлять «под правильное
поведение тест переписывается, а не удаляется». Замена:

```python
    def test_measured_capabilities_name_their_endpoint_budget_and_outcome(self):
        matrix = {item['id']: item for item in pr.capability_matrix()}
        for name in ('websocket_handshake', 'long_lived_connection', 'media_manifest_segment',
                     'http_api_assertions'):
            with self.subTest(capability=name):
                self.assertTrue(matrix[name]['supported'])
                self.assertNotEqual(matrix[name]['endpoint'], '—')
                self.assertNotEqual(matrix[name]['budget'], '—')
                self.assertNotEqual(matrix[name]['outcome'], '')
        for name in ('udp_transport', 'http2_or_http3', 'calls_video_any_service'):
            with self.subTest(capability=name):
                self.assertFalse(matrix[name]['supported'])
```

Новую истину я уже закрепил в `tests/test_probearea_capabilities.py`
(`CapabilityMatrixTruth`), поэтому после правки набора не останется.

### 2.4 `tests/test_anonymity.py` — устаревшее ожидание (чужой файл)

`tests/test_anonymity.py:206-210` закрепляет «min_anonymity не мешает без
judge»:

```python
    def test_min_anonymity_is_ignored_without_judge(self):
        self.store('plain', scan_config(judge=None), [result_row('http://11.0.0.1:8080')])
        report = p.export(self.db, 'plain', self.home / 'out', min_success=1, min_anonymity='elite')
        self.assertEqual(report['exported'], 1)
```

Это ровно то «молчаливое сбрасывание в any», которое дефект 13 запрещает.
`probes.require_anonymity` и `probes.build_plan` теперь дают
`E_VALIDATION_ANONYMITY_REQUIRED`; `proxytool.export` этого пока не делает
(см. §2.1). Тест надо переписать **после** правки `proxytool.export`, иначе он
закрепит поведение, которое §3.13 требует убрать.

---

## 3. Совместимость

- `anonymity.classify(body, own_ips)` — прежняя двухключевая форма, все
  вызывающие (`proxytool.py:1460`, `api.py`, `gui.py`, `tests/test_anonymity.py`)
  работают без правок.
- `reputation.Denylist`, `make_policy`, `verdict_blocks`, `result_allowed` и
  `check_dnsbl(address, zones, timeout, resolver=...)` — сигнатуры прежние;
  `screen_proxy` получил необязательный `max_queries=`.
- `reputation.check_dnsbl` возвращает те же ключи (`zone`, `status`,
  `address`) плюс `error`/`queried`, когда исход неоднозначен.
- `probes.validate_options` без `preset` даёт ровно прежние значения
  (`balanced == бывшие DEFAULT_OPTIONS`).
- `TargetProfile.to_public()` теперь можно скормить обратно в
  `validate_target` (раньше `body_bytes`/`auth_configured` падали как
  неизвестные поля).
- Схема БД, DDL, миграции, `db.py`, `core.py`, `gui.py`, `api.py`,
  `exportsvc.py` не тронуты. `probes.py` не импортирует ни `db.py`, ни другие
  модули волны; `anonymity.py` → `probes.py` и `reputation.py` → `probes.py`
  — единственные новые связи внутри пакета, циклов нет.

## 4. Что осталось объективно

1. `verified_at` у `BASIC_PROBES` — нужен ручной прогон внешних ответов;
   условиями задачи внешняя сеть запрещена, поэтому определения честно
   помечены непроверенными (`probes_manifest()['verified_count'] == 0`).
2. `proxytool.export(min_anonymity=…)` без judge всё ещё молчит — см. §2.4.
3. `udp_transport`, `http2_or_http3` не измеряются и объявлены как
   неподдерживаемые; это осознанная граница F20, а не недоделка.
4. `tests.test_extras.SingboxTests.test_config` падает в общем наборе, но
   падает и без моих правок (проверено `git stash`) — это работа другого
   исполнителя в общем дереве.
5. Полный `unittest discover` по 153 модулям: `Ran 3105 tests … failures=3,
   errors=1`; три падения — §2.3, одна ошибка — §4.4. Мои модули:
   `Ran 93 tests … OK`.
