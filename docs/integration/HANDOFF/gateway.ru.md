# Handoff: gateway

**Требования:** F16, дефекты 16, 17, 18, 19; R11, R12, R13
**Контракт:** `docs/integration/CONTRACTS.ru.md` §1.2(3,5), §2.3, §4.4, §5.1, §5.5, §6.3, §7.1, §7.2 (версия 1)
**База:** ветка `integration/ultra-2026-09-25`, HEAD `a877c2c`; `db.py` (SCHEMA_VERSION 14), `core.py`, `pools.py`, `reputation.py` уже в дереве
**Мои файлы:** `proxy_workbench/gateway.py`, `tests/gateway_support.py`,
`tests/test_gateway.py` (переписан под новый контракт — см. §3),
`tests/test_gateway_reservation.py`, `tests/test_gateway_health.py`, `tests/test_gateway_bind.py`,
`tests/test_gateway_denylist.py`, `tests/test_gateway_rotation.py`, `tests/test_gateway_transports.py`

`reputation.py` я **не менял**: шлюзу достаточно `Denylist.match()`, `Denylist.from_file()` и `Denylist.digest`, и это снимает конфликт двух писателей из `HANDOFF/README.ru.md` §1.2/§8 п.1.

---

## 0. Что изменилось по контракту (это ломает потребителей — читать первым)

| Что | Было | Стало | Кого затрагивает |
| --- | --- | --- | --- |
| `gateway.start(host=...)` без LAN | поднимался на любом адресе, `0.0.0.0` — умолчание GUI | не-loopback адрес **отказывается**, пока не сказано `lan=True` или `Bind(lan=True)` | `gui.py`, `proxytool.py run_gateway` |
| `Gateway` / `Background` параметр `token` | один и тот же секрет на GUI, API и шлюз | только пароль шлюза; GUI-токен больше не подставляется, а при отсутствии генерируется **свой** (`new_gateway_token()`) | `gui.py`, `proxytool.py` |
| `Pool.pick()` | читал экспорт с диска | чистая работа с памятью; чтение — `refresh()` / `await arefresh()` | `proxytool.py:2174` (`pool.refresh()` в `run_gateway` — работает без изменений) |
| `Pool.snapshot()` | читал экспорт с диска | только счётчики, файлов не касается; для свежего состояния — `await asnapshot()` | `gui.py:303` (`runner.server.gateway.pool.snapshot(top=5)` — работает, теперь без ввода-вывода) |
| `Pool.acquire()` | основной способ занять слот | `reserve()` возвращает `Lease`, который освобождается ровно один раз | прямых внешних вызовов нет |
| `client_options(user)` | `(request, session)` | `(request, session, pool)` | только `gateway.py` |
| `SUPPORTED` | `http, socks4, socks5` | `http, https, socks4, socks4a, socks5, socks5h` | `exportsvc`/`apiv1`, если перечисляют транспорты |
| `STRATEGIES` | `round-robin, random` | + `health-aware` | GUI-переключатель, если он есть |
| `Gateway.relay()` | `(client_reader, client_writer, proxy, upstream, first=b'')` | `(client_reader, client_writer, lease, upstream, first=b'', kind='tunnel')` | внутренний метод |

Позиционный порядок аргументов `start()` и `Background()` сохранён, поэтому существующие вызовы продолжают работать.

---

## 1. Прошу внести в чужие файлы

### 1.1 `proxy_workbench/gui.py`: LAN — явный opt-in, отдельный токен (дефект 18, R12)

**Где:** `gui.py:1089-1094` (`--gateway-host`, `--gateway-token`) и `gui.py:1128-1146` (запуск `gateway.Background`).

**Сейчас:**
```python
parser.add_argument('--gateway-host', default=os.environ.get('PROXY_WORKBENCH_GATEWAY_HOST', '0.0.0.0'), ...)
...
if not api.is_loopback(args.gateway_host) and not gateway_token:
    gateway_token = server.app.token          # <- секрет GUI уходит в QR телефона
```

**Прошу заменить на:**

```python
parser.add_argument('--gateway-host', default=os.environ.get('PROXY_WORKBENCH_GATEWAY_HOST', '127.0.0.1'),
                    help=tr('адрес шлюза; по умолчанию только этот компьютер',
                            'gateway bind address; this computer only by default'))
parser.add_argument('--gateway-lan', action='store_true',
                    help=tr('разрешить доступ с локальной сети (нужен отдельный пароль шлюза)',
                            'allow access from the local network (a separate gateway password is required)'))
parser.add_argument('--gateway-interface',
                    help=tr('какой адрес показывать телефону, если их несколько',
                            'which LAN address to publish when there is more than one'))
```

и при старте:
```python
try:
    server.app.gateway = gateway.Background(
        args.data, args.gateway_host, args.gateway_port,
        lan=args.gateway_lan, interface=args.gateway_interface,
        # НЕ передавать сюда server.app.token: это секрет GUI, а не шлюза.
        token=args.gateway_token)
except (OSError, ValueError) as exc:
    ...   # уже есть, печатает «Ротирующий прокси не запущен»
```

**Почему:** дефект 18 и R12 прямо называют `gui.py:1089` и `gui.py:1134` точкой правки. `gateway.Bind` теперь сам отказывает не-loopback адресу без `lan=True`, так что неправильный дефолт больше не приводит к тихому LAN-режиму: приложение напечатает понятное сообщение и продолжит работать без шлюза. `lan_interfaces()` и `Bind.published_host` дают выбор интерфейса; `Background.state()['interfaces']` — список для GUI.

**Что НЕ ломается:** `--no-gateway`, локальный режим, печать адреса и QR. `api.is_loopback` импортируется как раньше.

### 1.2 `proxy_workbench/gui.py`: `gateway_state()` — показать новое состояние (F16)

**Где:** `gui.py:299-320` (`App.gateway_state`).

Сейчас возвращаются `snapshot`, `address`, `copy_address`, `bind_host`, `mobile_ready`, `username`, `password`, `proxies`.
`runner.server.gateway.pool.snapshot(top=5)` продолжает работать (теперь без файлового ввода-вывода), но в ответе не хватает:

```python
state = dict(snapshot)
state.update(
    lan=runner.lan,                                  # LAN включён или нет
    interface=runner.bind.published_host,            # какой адрес показан телефону
    token_origin=runner.token_origin,                # 'none' | 'explicit' | 'generated'
    interfaces=gateway.lan_interfaces(),             # выбор интерфейса в GUI
    pools=snapshot['binding'],                       # pool_id/generation/profile/policy
    strategies=gateway.STRATEGIES,
    sticky_modes=gateway.STICKY_MODES,
    revoke_policies=gateway.REVOKE_POLICIES,
    on_deny=snapshot['on_deny'],
    denied=snapshot['denied'],
)
```

**Почему:** F16 требует видимого состояния привязки и «понятной политики» для долгих соединений и shutdown; CONTRACTS §4.3/§4.4 требуют, чтобы причина отказа была видна пользователю, а не молчала.

**Секреты:** `state()` и `snapshot()` не содержат значения пароля — это проверено тестом `test_gateway_bind.py::test_gateway_state_exposes_no_password`. В `gateway_state()` пароль уже кладётся в `copy_address`/`password` намеренно (это телефонное подключение, не управление), и он остаётся паролем **шлюза**, а не GUI.

### 1.3 `proxy_workbench/gui.py`: кнопка «запретить» — отзыв, а не только запрет вперёд (дефект 19, R13)

**Где:** место, где GUI сохраняет `denylist.txt` (`gui.py:272`, `core.atomic(self.data/'denylist.txt', ...)`).

**Прошу:** после записи файла, если запрос шёл из массовой операции «запретить выбранные», сказать об этом пользователю явно, потому что теперь есть **два разных действия**:

- **запретить впредь** — правило попадёт в `data/denylist.txt`, шлюз подхватит его при следующем обновлении пула (`refresh_interval`, по умолчанию 2 с) и **отзовёт новые допуски**;
- **отозвать из активного пула** — дополнительно вызвать `gateway.set_denylist(...)` у живого `Background`, чтобы отзыв был немедленным, а не по таймеру.

Публичный API для этого уже есть и не требует правок шлюза:
```python
closed = server.app.gateway.set_denylist(Denylist.from_file(path, normalizer=core.normalize))
# closed — список прокси, чьи открытые потоки закрыты; пусто при on_deny='keep'
```

**Судьба уже открытых потоков — отдельная и явная настройка** `on_deny`:
- `'keep'` (по умолчанию) — открытый поток не рвётся: байты уже в проводе, и обрывать его должен пользователь, а не побочный эффект добавления правила;
- `'close'` — шлюз закрывает потоки запрещённых адресов и возвращает их список.

**Почему:** R13 требует различить «запретить впредь» и «отозвать», и говорит, что закрытие уже существующих потоков — отдельная явно выбранная политика. Текущий текст кнопки обещает только будущие проверки; теперь можно обещать и отзыв.

### 1.4 `proxy_workbench/proxytool.py`: отдельный токен в CLI (дефект 18)

**Где:** `proxytool.py:2076-2085` (флаги `serve`/`gateway`), `proxytool.py:2161-2172` (`run_gateway`).

**Сейчас** один `--api-token` обслуживает и API, и шлюз.

**Прошу:** добавить `--gateway-token` (и `PROXY_WORKBENCH_GATEWAY_TOKEN`) и передавать его в `gateway.start(..., token=args.gateway_token)`. Если флаг не задан, а адрес не loopback, шлюз сам сгенерирует себе пароль и напечатает его один раз в stdout — сейчас `start()` возвращает `server.gateway.token` и `server.gateway.token_origin`, этого достаточно:

```python
server = await gateway.start(args.data, args.host, port, args.gateway_token, filters, args.rotate,
                             max(0, args.max_per_proxy), max(0.0, args.session_ttl) * 60,
                             sticky=args.gateway_sticky, on_deny=args.gateway_deny_open_streams)
if server.gateway.token_origin == 'generated':
    print(tr(f'Пароль шлюза (показывается один раз): {server.gateway.token}', ...))
```

Также прошу добавить `--gateway-sticky {failover,strict}` и `--gateway-deny-open-streams {keep,close}`.

**Почему:** R12 и CONTRACTS §5.1 — четыре разные identity, ни одна не подставляется вместо другой.

### 1.5 `proxy_workbench/apiv1.py`: маршруты шлюза (F16, F29)

В `apiv1.py:1328-1356` уже объявлены `/v1/gateway/bindings`, `/v1/gateway/listeners`, `/v1/gateway/sessions`, `/v1/gateway/config`. Им не хватает данных, которые теперь есть:

| Маршрут | Что отдавать | Откуда |
| --- | --- | --- |
| `GET /v1/gateway/listeners` | `Bind.as_dict()` + `token_origin` + `authenticated` | `Background.bind`, `Background.state()` |
| `GET /v1/gateway/bindings` | `pool_id, generation, profile_id, profile_revision, policy` | `Pool.state()['binding']`, `Pool.state()['bindings']` |
| `GET /v1/gateway/config` | `strategies, sticky_modes, supported, revoke_policies, handshake_timeout, connect_timeout, idle_timeout, max_session, max_clients, on_deny` | `Gateway.state()` |
| `GET /v1/gateway/sessions` | число и TTL, **без имён сессий** (имя сессии приходит от клиента) | `Pool.snapshot()['sessions']` |

**Почему:** CONTRACTS §5.2/§5.3 — права `gateway.read`/`gateway.write` объявлены, но нечем наполнить; §5.4 — `E_GATEWAY_NO_UPSTREAM`, `E_GATEWAY_DEADLINE`, `E_GATEWAY_SLOT_UNAVAILABLE`, `E_GATEWAY_TRANSPORT_UNSUPPORTED` должны быть видимы клиенту. `Gateway.state()` уже отдаёт эти поля и **не содержит секретов**.

### 1.6 `proxy_workbench/diagnostics.py`: коды причин шлюза (CONTRACTS §5.4)

`diagnostics.py:356` и `:429` оставляют коды шлюза владельцу шлюза. Прошу добавить в справочник:

| Код | Когда | Действие для пользователя |
| --- | --- | --- |
| `E_GATEWAY_NO_UPSTREAM` | пул пуст или все попытки исчерпаны | «запустите проверку» / «смените пул» |
| `E_GATEWAY_SLOT_UNAVAILABLE` | `max_per_proxy` занят, свободного слота нет | «повторите позже» |
| `E_GATEWAY_DEADLINE` | handshake не уложился в `handshake_timeout` | «проверьте прокси» |
| `E_GATEWAY_TRANSPORT_UNSUPPORTED` | схема апстрима не из `SUPPORTED` | «выберите поддерживаемый протокол» |
| `E_GATEWAY_DENIED` | адрес попал под denylist | «адрес запрещён правилом» |
| `E_GATEWAY_SESSION_STRICT` | strict-сессия осталась без своего адреса | «сессия не может сменить адрес» |

Перевод — существующим `i18n.tr(ru, en)`, как требует §5.4.

---

## 2. Что уже сделано у меня

### 2.1 Публичный API `gateway.py`

```python
# конфигурация слушателя
Bind(host='127.0.0.1', port=8899, lan=False, interface=None)   # .local .published_host .as_dict()
Binding(pool_id='default', generation=None, profile_id=None,
        profile_revision=None, policy={'max_per_proxy': 2, 'sticky': 'strict',
                                       'countries': ('DE',), ...})
resolve_token(bind, token=None) -> (token, origin)   # origin: none|explicit|generated
new_gateway_token() -> str
lan_interfaces() -> list[str]
display_host(bind_host, interface=None) -> str

# пул
Pool(data, filters=None, strategy='round-robin', max_failures=2, cooldown=300,
     max_per_proxy=0, session_ttl=600, *, sticky='failover', denylist=None,
     denylist_path=None, denylist_normalizer=None, on_deny='keep',
     cache_limit=256, bindings=None, default_binding=None, healthy_latency_ms=0)
  .load_denylist() / .set_denylist(denylist) / .denied_proxies() / .allowed(proxy)
  .refresh() -> list[str]            # файловый ввод-вывод, только не с event loop
  await .arefresh() -> list[str]     # off-loop
  .matching(request=None, binding=None) / .available(request=None, now=None, binding=None)
  .pick(...) -> str                  # без слота
  .reserve(...) -> Lease | None      # выбор + слот одним шагом
  .release(proxy) / .active_for(proxy)
  .connected(proxy, connect_ms=0)    # НЕ успех
  .outcome(proxy, kind, detail=None) # response|tunnel_bytes|upstream_unavailable|no_response|
                                     # handshake_failed|upstream_refused|closed_empty
  .ok(proxy) / .failed(proxy)        # совместимые обёртки
  .health_score(proxy, now=None) -> float
  .report(proxy) -> dict
  .snapshot(top=20) -> dict          # без файлов, безопасно из любого потока
  await .asnapshot(top=20) -> dict
  .state() -> dict
  .binding_for(name=None) -> Binding  # 'default' всегда адресуем, иначе KeyError

# шлюз
Gateway(pool, token=None, attempts=3, connect_timeout=8, idle_timeout=300, *,
        handshake_timeout=30, response_timeout=15, max_clients=512, max_session=0,
        bind=None, token_origin=None, drain_timeout=2.0, ssl_context=None, refresh_interval=2.0)
  await .connect(host, port, forward=False, request=None, session=None,
                 binding=None, sticky=None) -> (Lease, (reader, writer))
  .set_denylist(denylist) -> list[str]   # закрытые потоки при on_deny='close'
  await .shutdown(grace=None) -> {'drained': n, 'forced': m, 'closed_sessions': k}
  .state() -> dict
  .refresh_loop(interval=None)

Lease(pool, proxy)  # .release() ровно один раз, .proxy
ReplayGuard()       # .write_once(lease, writer, payload) -> UpstreamError('REPLAY_REFUSED')

async def start(data, host='127.0.0.1', port=8899, token=None, filters=None,
                strategy='round-robin', max_per_proxy=0, session_ttl=600, *, ...)
Background(data, host='127.0.0.1', port=8899, token=None, **options)
  # .bind .lan .token .token_origin .display_host .state() .shutdown_report
```

### 2.2 Имя binding в user name клиента

`pool-<id>` в имени пользователя выбирает привязку: `http://pool-de:x@127.0.0.1:8899`. Неизвестный `id` — `400`, а не тихий возврат к пулу по умолчанию. Остальные опции (`country-`, `protocol-`, `latency-`, `anonymity-`, `session-`) только **сужают** привязку, расширить её нельзя.

### 2.3 Политика долгих соединений и shutdown

- `idle_timeout` (по умолчанию 300 с) — бездействие в любую сторону;
- `max_session` (0 = без предела) — верхняя граница жизни одного соединения;
- `handshake_timeout` — один **абсолютный** дедлайн на весь handshake: первый байт, разбор запроса, согласование SOCKS5 и туннель к апстриму;
- `max_clients` — верхняя граница одновременных клиентских соединений, лишние получают `503`;
- `shutdown(grace)` — сначала ждёт до `grace`, потом отменяет остаток и возвращает отчёт.

### 2.4 Модель здоровья (дефект 17)

| Исход | Что означает | Влияние |
| --- | --- | --- |
| `response` | пришёл HTTP-ответ с кодом < 500 | `ok`, снимает серию неудач |
| `tunnel_bytes` | по туннелю пришли байты | `ok` |
| `upstream_unavailable` | апстрим ответил 5xx — не работает цель | **не наказывает прокси** |
| `no_response` | апстрим завис | **не наказывает прокси** |
| `upstream_refused` | 407, не-HTTP мусор, закрылся без ответа | отдых после `max_failures` |
| `closed_empty` | клиент говорил, в ответ тишина | отдых после `max_failures` |
| `handshake_failed` | не соединился / отверг рукопожатие | отдых после `max_failures` |

`connected()` (TCP+handshake) **не** является успехом и **не** снимает серию неудач: это и был исходный дефект.

---

## 3. Тесты, которые пришлось переписать, и почему

`tests/test_gateway.py` закреплял поведение, которое само было дефектом. По `CONTRACTS.ru.md` §8 п.3 такие тесты переписываются **под правильное поведение**, а не удаляются:

| Было | Стало | Причина |
| --- | --- | --- |
| `test_per_proxy_limit` — последовательные `pick`/`acquire` | `test_pick_needs_an_explicit_refresh_and_reservation_is_atomic` + `test_reserve_takes_the_slot_before_the_caller_awaits` + `tests/test_gateway_reservation.py` | последовательный тест не воспроизводит interleaving — прямое замечание R11 |
| `test_pool_filters_and_skips_https_proxies` | `test_pool_keeps_every_supported_transport` | `https://` больше не отбрасывается, а обслуживается (F16) |
| `test_client_options_parsing` (`(dict, str)`) | то же + третий элемент `pool` | изменился контракт `client_options` |
| `Background(..., '0.0.0.0', token=...)` без LAN | `..., lan=True` | LAN стал явным opt-in (дефект 18) |
| — | `test_lan_is_opt_in_and_the_default_stays_loopback` | новое поведение закреплено |

`tests/gateway_support.py` — общие локальные фикстуры: фейковые апстримы (`socks4/4a/5/5h`, `http` в пяти режимах, HTTPS через локальный `openssl`), цель на 127.0.0.1 и ::1, экспорт поколения. Ни одного обращения к публичному прокси, DNSBL или стороннему сервису.

---

## 4. Проверки

Команды запускались из корня репозитория, каждая — свой модуль:

```
.venv/bin/python -m unittest tests.test_gateway                → Ran 13 tests, OK
.venv/bin/python -m unittest tests.test_gateway_reservation    → Ran  9 tests, OK
.venv/bin/python -m unittest tests.test_gateway_health         → Ran 12 tests, OK
.venv/bin/python -m unittest tests.test_gateway_bind           → Ran 12 tests, OK
.venv/bin/python -m unittest tests.test_gateway_denylist       → Ran  8 tests, OK
.venv/bin/python -m unittest tests.test_gateway_rotation       → Ran 22 tests, OK
.venv/bin/python -m unittest tests.test_gateway_transports     → Ran 16 tests, OK
```

Соседние модули, чтобы убедиться, что правка шлюза ничего не сломала:

```
.venv/bin/python -m unittest tests.test_api  → Ran  8 tests, OK
.venv/bin/python -m unittest tests.test_gui  → Ran 24 tests, FAILED (failures=2)
.venv/bin/python -m unittest tests.test_extras → Ran  5 tests, FAILED (failures=1)
```

Три падения — **не мои и existed до правки**: `test_gui.GuiTests.test_collect_job_dedup_and_empty_profile_scan`,
`test_gui.GuiTests.test_real_worker_checks_two_services_and_stop_button` и
`test_extras.RecommendedTests.test_gui_recommended_sort`. Проверено подменой
`proxy_workbench/gateway.py` на версию из HEAD (`git show HEAD:proxy_workbench/gateway.py`):
все три падают точно так же и без моих изменений. Они относятся к экспорту и GUI-пути
`proxytool.py`/`gui.py`, которыми я не владею.

**Отдельно про воспроизводимость interleaving.** Тест
`test_gateway_reservation.py::test_parallel_handshakes_never_share_a_slot` запускался и
против старой реализации `Gateway.connect` (pick → `await open_tunnel` → `acquire`),
подставленной скриптом. Результат: **пиковая одновременная нагрузка на один апстрим — 3
соединения при `max_per_proxy=1` и двух апстримах**, при этом `pool.active` оставался
пустым всё время (слот вообще не резервировался). С новым кодом на том же сценарии —
пик 2 соединения суммарно и никогда больше одного на апстрим.

**Что осталось непрочитанным/непроверенным:**
- Полный `unittest discover -s tests` не запускался — по условию задачи его гоняют другие
  исполнители и интегратор.
- Реальная доставка (QR, выбор интерфейса в UI, firewall) не проверялась: это `gui.py`/`ui/*`.
- HTTPS-апстрим проверяется сертификатом, который тест создаёт локально через `openssl`;
  если `openssl` недоступен, четыре теста помечаются `skipTest`, а не проходят молча.
  `test_https_upstream_verifies_the_proxy_host_name` (проверка `ssl=`/`server_hostname=`)
  от `openssl` не зависит и выполняется всегда.

---

## 5. Открытые вопросы

1. **Политика закрытия уже открытых потоков при добавлении правила.** Я сделал `'keep'` по
   умолчанию и `'close'` по явному выбору, потому что R13 говорит, что это отдельная
   политика. Если продукт решит, что массовое «запретить» в GUI обязано рвать и открытые
   потоки, дефолт меняется одной строкой (`Pool(..., on_deny='close')`) и текст кнопки в §1.3.
2. **Имя пула против привязки.** Сейчас binding — это `pool_id` + generation + profile +
   policy поверх экспорта. `pools.py` уже умеет именованные пулы в БД (миграция 7), но у
   них другая природа (desired-контроллер, а не снимок строк). Соединить их — работа
   интегратора; шлюз к этому готов через `bindings=` и `Pool.state()['bindings']`.
3. **`access_revision` в привязке.** CONTRACTS §1.2(1) требует, чтобы смена ревизии доступа
   отзывала прошлое доказательство и в шлюзе. Сущности `access` ещё нет (`grep -rn
   "access_revision" proxy_workbench/` даёт ноль совпадений на момент написания), поэтому в
   `Binding` есть только `profile_id`/`profile_revision`. Когда появится `access`, нужен
   ещё один компонент в `_bound_rows`; место для этого — тот же метод.
4. **Таймаут ожидания ответа апстрима (`response_timeout`, 15 с).** Для CONNECT/SOCKS он не
   применяется: там доказательство приходит с первыми байтами клиента. Если для длинных
   CONNECT-запросов нужен свой лимит, это отдельная настройка, а не переиспользование
   `response_timeout`.
