# Handoff: probes

**Требования:** F01, F07, F20 (корректность измерений), дефекты 13, 14, 15; R08, R09, R10.
**Контракт:** CONTRACTS.ru.md §1.2, §2.1, §4.4, §5.4, §5.5, §6.3, §7.1 (F01/F07/F20), §7.2 (13, 14, 15) — версия 1.
**База:** `integration/ultra-2026-09-25`, HEAD `47229d0` плюс незакоммиченный `proxy_workbench/probes.py` и `tests/test_probes_*.py` этого исполнителя.

## 1. Прошу внести в чужие файлы

Моё задание ограничило владение одним новым файлом `proxy_workbench/probes.py` и моими тестами, поэтому `anonymity.py` и `reputation.py` я **не правил**, хотя HANDOFF/README.ru.md §1.2 отдаёт их исполнителю `probes.py`. Ниже — конкретные правки, которые нужны их владельцу (или интегратору) для закрытия дефектов 13 и 14; всё нужное для этого уже лежит в `probes.py` и покрыто тестами.

### 1.1 `proxy_workbench/anonymity.py` — дефект 13, R08

Файл: `proxy_workbench/anonymity.py`, функции `classify` (строка 99) и `extract_public_ips` (строка 85).

- **Что заменить.** `classify(body, own_ips)` сейчас возвращает `elite` любому ответу без нашего IP и без заголовков прокси. Заменить тело функции на делегирование `probes.classify_echo`:

  ```python
  from . import probes

  def classify(body, own_ips, *, judge_verified=True):
      outcome = probes.classify_echo(body, own_ips, judge_verified=judge_verified)
      return {'level': outcome.level, 'signals': list(outcome.signals), 'code': outcome.code}
  ```

  Что это меняет в поведении (всё проверено в `tests/test_probes_anonymity.py`):
  - пустой ответ → `unknown` + код `JUDGE_INVALID` (сигнал `empty_body`);
  - страница CAPTCHA/JS-challenge → `unknown` + `JUDGE_CHALLENGE`;
  - ответ без единого адреса → `unknown` + `JUDGE_INVALID` (сигнал `no_echo_address`) — **это тот случай, который сейчас даёт ложный `elite`**;
  - `elite` только при `judge_verified=True` **и** непустом `own_ips` **и** распознанном адресе; иначе `unknown` + `JUDGE_UNVERIFIED`;
  - `transparent`/`anonymous` по-прежнему ставятся по факту наблюдения и считаются подтверждёнными.
- **Почему здесь.** CONTRACTS §5.4 (коды измерения) и §7.2 дефект 13: «валидация judge до elite». Дефект живёт в этом файле; владелец `proxytool.py` (вызов в `judge_proxy`, `proxytool.py:1215-1229`) должен передавать `judge_verified` из результата прямого bootstrap-анонимного запроса (`detect_own_ips`, `proxytool.py:1196`).
- **Отдельно про фильтр.** `extract_public_ips` (строка 85) отбрасывает не-глобальные адреса (`ip.is_global`). Для публичного judge это верно, для self-hosted judge (127.0.0.1, адрес LAN) — нет: F01 прямо требует поддержать пользовательский/self-hosted контракт. В `probes.py` это решено функцией `extract_addresses`, которая берёт любые корректные адреса, а «свой или нет» решает bootstrap-множество.
- **Ошибка вместо тихого сброса.** Требование anonymity без нужной пробы должно давать `probes.ProbeError` с кодом `E_VALIDATION_ANONYMITY_REQUIRED` (`probes.require_anonymity`), а не `min_anonymity → any`. Сегодня это поведение закреплено тестом `tests/test_anonymity.py:206-210` (`test_min_anonymity_is_ignored_without_judge`) — по CONTRACTS §8.3 этот тест переписывается владельцем теста **под правильное поведение**, а не удаляется. `anonymity.validate_min_level` может остаться как есть: список уровней совпадает.

### 1.2 `proxy_workbench/reputation.py` — дефект 14, R09

Файл: `proxy_workbench/reputation.py`, функции `reverse_ip` (строка 180) и `check_dnsbl` (строка 224).

- **Что заменить.** `reverse_ip` для IPv6 делает `"".join(reversed(address.exploded))`. `IPv6Address.exploded` — это `2001:0db8:0000:…:0001`, **с двоеточиями** (проверено на этой ревизии: `python -c "import ipaddress;print(ipaddress.ip_address('2001:db8::1').exploded)"` → `2001:0db8:0000:0000:0000:0000:0000:0001`). Разворот даёт метки вида `0:0`, которых нет ни в одной зоне. Заменить на разворот 32 нибблов упакованной формы: `packed = f'{int(parsed):032x}'`, затем `'.'.join(reversed(packed)) + '.'`. То же в `probes.reverse_ip` уже сделано и проверено roundtrip-тестом (`tests/test_probes_dnsbl.py`, 32 метки, ни одного двоеточия, compressed и expanded дают одинаковый результат).
- **Что заменить в `check_dnsbl`.** `listed = any(item.startswith("127.") for item in addresses)` (строка 237) считает любым `127.*` листингом. Нужна зонная карта ответов, как в `probes.DnsblZone`/`probes.classify_dnsbl`:
  - `127.255.255.0/24` → **не** листинг, а `unknown` с кодом `DNSBL_ACCESS` (запрос заблокирован);
  - коды, которые зона объявляет сама (например «нужен авторизованный запрос»), → `unknown` + `DNSBL_ACCESS`; по умолчанию список пуст, потому что общий стандарт их не задаёт;
  - `NXDOMAIN` → `clear` (либо `unknown`, если зона объявила `nxdomain='unknown'`);
  - `NOERROR` без адресов → `unknown` + `DNSBL_ERROR` (пустой ответ не доказательство отсутствия);
  - `SERVFAIL` → `unknown` + `DNSBL_ERROR`, `REFUSED` → `unknown` + `DNSBL_ACCESS`;
  - прочие `127/8` → `listed`.
- **Квота и доступ.** Транспортная ошибка resolver'а приходит как `probes.DnsQueryError(code)` и даёт `unknown` с кодом `DNSBL_QUOTA` либо `DNSBL_ACCESS`, а не «чисто». Исчерчение лимита запросов (`max_queries`) даёт `BUDGET_EXHAUSTED` на нетронутых зонах с `queried=False`. Сегодняшний код их не различает и в `screen_proxy` (строка 255) объявляет `status='clean'`, если DNSBL включён, даже когда часть зон ответила `unknown` — это ложное «чисто», и его надо убрать: `clean` возможно только когда **все** зоны дали `clear`.
- **Совместимость.** `Denylist`, `make_policy`, `verdict_blocks`, `result_allowed` и их сигнатуры не меняются: `gateway.py` и `proxytool.py` читают `Denylist.match()` и `make_policy()` (HANDOFF §1.2), и им ничего не нужно трогать.

### 1.3 `proxy_workbench/proxytool.py` — подключение (владелец: интегратор)

- `measure_speed` (`proxytool.py:1164-1194`) перестаёт считать сам: окно измеряется от первого байта до последнего (`probes.measure_speed(trace, limits)`), а один чанк или неполная передача дают `state='insufficient'` без числа. Transport-seam, который это делает, описан ниже.
- `check_proxy` (`:1139`) получает режим из `probes.build_plan`, а `scan` (`:1283`) — уровень доказательства из `probes.PlanOutcome.evidence`. Ключевое правило: строка, измеренная в режиме `tcp`/`handshake`, не может быть выдана как передающая; сегодня этого не различает ни один уровень кода.
- `export`/`api.public_row` должны брать `code`/`stage`/`detail` из `TargetOutcome.to_public()`; новые коды измерений перечислены в `probes.MEASUREMENT_CODES` (CONTRACTS §5.4 просит такой справочник, §7.2 его нет).

## 2. Что уже сделано у меня

Публичный API `proxy_workbench/probes.py` (leaf-модуль: своей БД, своего сетевого загрузчика и своего нормализатора прокси у него нет; сокеты только через внедрённый transport — CONTRACTS §1.2, HANDOFF §2.2):

| Что вызвать | Аргументы | Что возвращает |
| --- | --- | --- |
| `resolve_mode(name, *, base=None, monitoring=False)` | имя режима или алиас | `Mode(name, evidence, network, needs_targets, checks_transfer, monitoring, base)` |
| `build_plan(settings)` | dict настроек GUI/CLI/API | `ProbePlan` (режим + опции + цели + judge + зоны + speed) |
| `validate_options(data)` / `validate_target(data, own_profile=False)` | сырые параметры | `ProbeOptions` / `TargetProfile`; `ProbeError` с кодом `E_*` |
| `plan_digest(plan)` | `ProbePlan` | 20 hex-символов: идентичность **измеряемого**, recheck/monitor новой не создают |
| `run_probe(target, options, transport, *, clock=None)` | цель, опции, transport | `TargetOutcome` (ok/code/stage/status/bytes/ttfb_ms/transfer_ms/total_ms/attempts/detail) |
| `run_stage(stage, address, options, transport)` | `'tcp'` или `'handshake'` | `TargetOutcome` со `stage='tcp'/'handshake'`, никогда не `transfer_ok` |
| `run_plan(plan, transport, *, endpoint='', fail_fast=True, clock=None)` | план, transport | `PlanOutcome` с `evidence` и `is_working(outcome)` |
| `check_targets(transport, targets, options, *, limit=2)` | прямой transport | проверенные цели — доказательство «упала цель, а не прокси» |
| `measure_speed(trace, limits=SPEED_LIMITS)` | `TransferTrace` | `SpeedMeasurement(state='ok'|'insufficient'|'error')`; число только при `state='ok'` |
| `run_speed_test(target, options, transport, *, limits)` | `SpeedTarget`, transport | `SpeedMeasurement` |
| `classify_echo(body, own_ips, *, judge_verified=True, spec=None)` | ответ judge, bootstrap-множество | `AnonymityOutcome(level, signals, code, exit_ip, confirmed)` |
| `require_anonymity(minimum, judge)` | требуемый уровень, judge | `JudgeSpec` либо `ProbeError(E_VALIDATION_ANONYMITY_REQUIRED)` |
| `reverse_ip(address)` / `check_dnsbl(address, zones, *, resolve, max_queries=None, timeout_s=None)` | адрес, зоны, seam резолвера | префикс `ip6.arpa` / `DnsblReport(zones, status, queries, truncated)` |
| `plan_recheck(rows, *, now, max_age_s, limit=0, remeasure_passing=False)` | строки результатов | `RecheckItem(proxy, reason, age_seconds)` с приоритетом: битое время → истёкшие |
| `capability_matrix()` | — | что реально измеряется, а что объявлено неподдерживаемым (F20) |
| `serve_reference_probe(host='127.0.0.1', port=0)` | — | контекст-менеджер с self-hosted эндпоинтом `/health`, `/echo`, `/bytes/N`, `/redirect/N` |
| `reference_probe_targets(base_url)` | base URL | готовые `TargetProfile` для проверки своего профиля локально (F20) |

**Контракт transport-seam** (его и подключает интегратор, сетевого кода в модуле нет):

```python
class Transport:
    async def send(self, request: ProbeRequest, *, options: ProbeOptions) -> ProbeResponse: ...
    async def download(self, target: SpeedTarget, *, options: ProbeOptions) -> TransferTrace: ...
```

`ProbeResponse.code`/`stage` заполняются при неудаче; `stage` различает `tcp`/`handshake`/`target`, поэтому мёртвый прокси не путается с живым проксием и сломанной целью. Пример полной реализации seam на loopback — `tests/test_probes_reference.py`, класс `LoopbackTransport`.

## 3. Совместимость

- **Что ломается, если §1 не внести.** Дефект 13 и дефект 14 остаются открытыми в тех файлах, где они живут: `export(..., min_anonymity='elite')` продолжит выдавать строки без judge, а IPv6-адрес продолжит уходить в DNSBL с метками вида `0:0` и получать ложное «чисто» при ответе зоны «запрос заблокирован». Пользовательский сценарий, который это ловит: «найти прокси с anonymity elite» (F01/F05) и «исключить адреса из DNSBL» (F14/F16).
- **Что НЕ ломается.** Новый файл и новые тесты ничего не меняют в текущем поведении: `proxytool.py`, `api.py`, `gui.py`, `ui/*` не тронуты, чужие тесты не правились, схема БД не менялась, DDL не добавлялся, миграции не требуюся. `probes.py` не импортирует ни `db.py`, ни другие модули волны 2, поэтому его не нужно переписывать при появлении `db.py`; при появлении `access_id` (§1.2 CONTRACTS) меняется только сигнатура транспорта, не план.
- **Единственное намеренное расхождение с текущим кодом:** `max_redirects` стал per-target полем с умолчанием из опций (в `proxytool.py:960-984` такого поля вообще нет), а `whole_probe_timeout_s` — обязательной кросс-полевой проверкой (CONTRACTS §5.5 отмечает его как «не существует», дефект 23).

## 4. Проверки

Команда (только мои тесты, полный набор не гоняю — HANDOFF §5):

```
.venv/bin/python -m unittest tests.test_probes_modes tests.test_probes_options tests.test_probes_run \
    tests.test_probes_speed tests.test_probes_anonymity tests.test_probes_dnsbl tests.test_probes_reference
```

Результат на этой ревизии: `Ran 156 tests in 1.539s — OK`.

Что покрыто и чем (все сценарии локальные: моки, фикстуры и loopback к self-hosted reference probe; публичные прокси, DNSBL и сторонние сервисы не опрашивались):

| Файл | Содержание |
| --- | --- |
| `tests/test_probes_modes.py` | 8 режимов и алиасы, лестница доказательств, TCP-only ≠ передающий прокси, monitor/recheck как производные режимы без нового вердикта, цепочка fallback ограничена 3 пробами, отказ цели ≠ отказ всех прокси (в т.ч. один прокси не доказывает аварию цели), прямой контроль цели, план перепроверки |
| `tests/test_probes_options.py` | каждое поле `LIMITS` на нижней/верхней границе, незнакомые параметры не игнорируются, типы/NaN/bool, общий срок против худшего случая, геометрический backoff с потолком, statuses/sha256/content-type/размер/JSON-утверждения, POST/body/API-auth только в own-профиле и без повторов, значение секрета не принимается, custom CA не выключает TLS (проверено на системном CA-файле), DNS-mode, content-address плана, справочник кодов |
| `tests/test_probes_run.py` | успех и каждая форма отказа (`HTTP_*`, `CONTENT_TYPE`, `BODY_TOO_SMALL/LARGE`, `CONTENT_MISMATCH`, `HASH_MISMATCH`, `JSON_ASSERT`), операторы JSON, редиректы (следование, цикл, лимит, без Location, запрет по умолчанию), повтор только транспортных сбоев, backoff, общий срок пробы на все попытки, этапы tcp/handshake, fail-fast, сериализуемость результата |
| `tests/test_probes_speed.py` | один чанк не даёт Mbps, окно от первого до последнего байта, TTFB/transfer/total раздельно, минимумы объёма/длительности/чанков, `insufficient` vs `error`, потолок замера, обрыв и незавершённость, дедлайн и обрыв транспорта |
| `tests/test_probes_anonymity.py` | валидный echo → elite только при подтверждённом judge, пустой/посторонний/CAPTCHA → unknown с разными кодами, утечка и заголовки, self-hosted judge с приватным адресом, `require_anonymity` вместо тихого сброса в `any`, валидация judge-URL, отбор строк |
| `tests/test_probes_dnsbl.py` | IPv4 и IPv6 (32 точечные нибблы, roundtrip, compressed == expanded, без двоеточий), зонные коды, `DNSBL_ACCESS` против листинга, `SERVFAIL`/`REFUSED`/пустой `NOERROR`, квота, таймаут резолвера, бюджет запросов, rollup статуса, строгий режим |
| `tests/test_probes_reference.py` | сквозные сценарии по loopback к self-hosted reference probe: успех, битое утверждение, 404, недоступный эндпоинт, ограниченные редиректы, judge-bootstrap и подтверждение `elite`, реальный поток с честным окном и `insufficient` на маленьком ответе, матрица возможностей |

**Что осталось непрочитанным/непроверенным.**

- Встроенные basic probes (`example.com` и страница IANA) не сверялись с живыми ответами: по условиям задачи внешняя сеть не используется. Поэтому у них `verified_at=None`, а `probes_manifest()['verified_count'] == 0` — определение помечено как непроверенное, а не как рабочее. **Требуется действие интегратора:** один ручной прогон и простановка `verified_at` в `BASIC_PROBES` (MASTER-PROMPT §8: без этого «предустановленные probes» нельзя объявлять подтверждёнными).
- `websocket`, длительное соединение, media manifest/segment, UDP, HTTP2/3 объявлены в `capability_matrix()` как **неподдерживаемые** с объяснением. Это осознанная граница, а не недоделка: F20 запрещает объявлять их по результату HTTP GET.
- Полный `unittest discover -s tests` не запускался — по HANDOFF §5 это делает интегратор после сборки всех модулей.

## 5. Открытые вопросы

1. **Владение `anonymity.py` и `reputation.py`.** HANDOFF/README.ru.md §1.2 отдаёт их исполнителю `probes.py`, а моё задание ограничило владение одним новым файлом. Я оставил обе правки невыполненными и описал их в §1; если владение подтверждается, они делаются теми же тестами из `tests/test_probes_anonymity.py` и `tests/test_probes_dnsbl.py` (их можно перенести, не переписывая).
2. **Имена новых кодов измерения.** `MEASUREMENT_CODES` вводит `HANDSHAKE_TIMEOUT`, `HANDSHAKE_PROTOCOL`, `CONTENT_TYPE`, `JSON_ASSERT`, `REDIRECT_LOOP`, `REDIRECT_TOO_MANY`, `BODY_TOO_SMALL`, `DNSBL_QUOTA`, `DNSBL_ACCESS`, `INSUFFICIENT_SAMPLE`, `BUDGET_EXHAUSTED`, `TARGET_UNAVAILABLE` в существующий домен `UPSTREAM` §5.4. Новых доменов не заводил. Если владелец контракта предпочитает другие имена — это бамп §5.4, и менять нужно только словарь констант и тесты.
3. **`probes.PlanOutcome.evidence` и `api.public_row`.** Уровень доказательства сейчас поле исхода пробы. Куда именно его положить в публикуемой строке (CONTRACTS §4.4) — решение интегратора; я не трогал `api.py`.
