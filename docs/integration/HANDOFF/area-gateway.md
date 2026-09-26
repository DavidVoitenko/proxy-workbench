# Handoff: area gateway (F16 / F17, дефекты 1, 2, 3 и 16-19)

**Требования:** F16, F17, F04 (путь «импорт → worker check → scoped gateway»), дефекты списка
области 1 (шлюз не умеет передавать учётные данные), 2 (привязка к пулу из GUI не применяется),
3 (`--lan` мёртв), плюс сквозная проверка дефектов 16, 17, 18, 19. Приёмка §7 сценарии 14 и 17.
**Контракт:** `docs/integration/CONTRACTS.ru.md` §1.2(1) (доступ — не endpoint), §5.1
(`upstream_credential` — четвёртая личность, отдельная от `gateway_password`), §4.2/§4.4.
**База:** ветка `integration/ultra-2026-09-25`, ревизии остальных файлов не трогал.
**Мои файлы:** `proxy_workbench/gateway.py`, `tests/test_area_gateway_credentials.py`,
`tests/test_area_gateway_f16.py`, `tests/test_area_gateway_f17.py`.

---

## 1. Что воспроизведено живым прогоном (до правки)

Подняты реальные сокеты на loopback: мок HTTP-прокси с Basic-аутентификацией, мок SOCKS5 с
паролем, мок SOCKS4. Проверка — чтение байтов, а не чтение кода.

**1.1 HTTP CONNECT без `Proxy-Authorization`.** `gateway.open_tunnel` отправлял ровно это:

```
CONNECT 127.0.0.1:80 HTTP/1.1 | Host: 127.0.0.1:80 |  |
  -> UpstreamError(CONNECT_REFUSED)
```

Мок, требующий Basic, отвечал 407, прокси-адрес навсегда уходил в rest. Поле `Proxy-Authorization`
не формировалось нигде в модуле.

**1.2 SOCKS5 предлагал только «без аутентификации».**

```
method offer the gateway sent: ['00']      (05 01 00 = version 5, 1 method, NO AUTH)
  -> UpstreamError(SOCKS5_AUTH)
upstream verdict: refused, the gateway never offered 0x02
```

То есть `SOCKS5_AUTH` был не редким случаем, а постоянным для любого SOCKS5 с паролем.

**1.3 Связка `api.public_row` → `Pool.source_rows`.** Экспорт на диске несёт `access_id` и
`access_revision` (`exportsvc.ROW_FIELDS`), но `api.public_row` (`api.py:89-161`) собирает
фиксированный словарь и **оба поля теряются**. То есть даже будь у шлюза резолвер, строка не
сообщала бы, какая личность доступа ей соответствует. Это отдельный пункт в §3.1.

**1.4 `--lan`.** `Bind(host='127.0.0.1', lan=True)` давал `listen_host == '127.0.0.1'`: флаг
доходил, а сокет оставался локальным. И `published_host` считался от `host`, а не от адреса
bind, поэтому гипотетический wildcard-случай публиковал бы `127.0.0.1` телефону.

**1.5 Привязка из GUI.** `App.gateway_binding()` (gui.py:3582) строит `gateway.Binding`, но
`App.start_gateway()` (gui.py:1141) вызывает `gateway.Background(self.data, host, port,
token=...)` — без `bind=`, поэтому `Gateway` получал пустой `Binding()` и выбор пула в интерфейсе
не влиял ни на что.

---

## 2. Что сделано в `proxy_workbench/gateway.py`

### 2.1 Учётные данные восходящего прокси (F04, дефект 1 области)

* `class AccessCredentials(store, *, lock=None)` — единственная точка «строка → байты в сокет».
  Внутри только `secrets.AccessStore.resolve()` и
  `secrets.transport_credentials(access, resolved, scheme=scheme)`. Второго места, которое
  умеет отформатировать пароль, в модуле нет.
  Выбор личности:
  * строка называет `access_id` — берётся он, и ревизия сверяется
    (`resolve(access_id, access_revision=row['access_revision'])`), поэтому Superseded-ревизия
    **не отправляется**, а новый пароль не наследует доказательство старого;
  * строка называет только `endpoint_id` (это то, что отдаёт `public_row` сегодня) — берётся
    личность, **только если она ровно одна** пригодная; несколько → `SecretConflictError`,
    credential не отправляется вовсе: две учётки одного адреса это две личности
    (CONTRACTS §1.2(1)), сливать их хуже, чем не аутентифицироваться;
  * несовпадение схемы (например `MODE_SOCKS5` против `socks4`), запертый vault, отсутствующий
    секрет — `SecretError`, который превращается в «credential нет», а не в исключение в пути
    клиента.
  `ResolvedAccess` скрабится в `finally`, `TransportCredentials` живёт только до записи запроса.
* `Gateway._credentials_for(proxy)` ищет строку выбранного адреса (`Pool.row_for`, O(1)-индекс,
  который перестраивается вместе с `rows`, то есть всегда в границах того, что реально
  сервируется) и **вызывает резолвер вне event loop** (`asyncio.to_thread`, синхронные и
  асинхронные callable поддержаны). Запертый OS-keychain не может встать на loop. Всё это внутри
  `connect_timeout` и общего handshake-deadline.
* `open_tunnel(..., credentials=None)`:
  * `http`/`https` CONNECT получает строку `Proxy-Authorization` (RFC 7617);
  * `http`/`https` forward: заголовок добавляет релей при сборке запроса — туннель был
    аутентифицирован, а запрос ушёл бы анонимным, если бы это забыли;
  * `socks5`/`socks5h`: приветствие берётся из доступа (`05 01 02` при наличии учётки, `05 01 00`
    без неё — предложение никогда не расширяется), при выборе 0x02 пишется RFC 1929 и читается
    ответ `\x01\x00`;
  * `socks4`/`socks4a` не получают ничего: F04 не разрешает изобретать SOCKS4-учётку.
* `Lease.credentials` живёт ровно до первой записи запроса и обнуляется в `Lease.release()` и в
  `finally` релея.
* Резолвер — необязательный аргумент `Gateway(..., credentials=...)` / `start(..., credentials=)`;
  `None` означает «пул открытых прокси», и это поведение по умолчанию.

Фактический байтовый обмен после правки (живой прогон):

```
1. HTTP CONNECT через прокси, требующий Basic
  client   <- HTTP/1.1 200 Connection established
  upstream <- CONNECT 127.0.0.1:49317 HTTP/1.1 | Host: ... |
              Proxy-Authorization: Basic YWxpY2U6Y2FuYXJ5LWNyZWRlbnRpYWwtcDRzcw== |  |
  tunnel   <- b'the real local target'

2. plain HTTP forward (запрос пишет сам релей)
  client   <- HTTP/1.1 200 OK | b'the real local target'
  upstream <- GET http://127.0.0.1:49337/forwarded HTTP/1.1 | Host: 127.0.0.1 |
              Proxy-Authorization: Basic YWxpY2U6Y2FuYXJ5LWNyZWRlbnRpYWwtcDRzcw== |
              Connection: close |  |

3. SOCKS5 через прокси, требующий username/password
  client offer      : 05 01 02
  gateway chose     : 0502
  gateway auth      : 0100      (01 00 = granted)
  upstream offer saw: 02        (05 01 02 - только 0x02, 0x00 не предлагался)
  upstream got      : ('alice', 'canary-credential-p4ss')
  tunnel carried    : b'the real local target'
```

### 2.2 Безопасность: «нет пароля» ≡ «неверный пароль» ≡ «прокси мёртв»

Требование: отказ 407 не должен превращаться в канал утечки «пароль есть, но не подошёл».

Закрыто следующее.

1. **Единый вердикт.** `Gateway.connect` пишет `outcome(proxy, 'handshake_failed',
   detail=UNUSABLE)`. Раньше туда уходило `type(exc).__name__` — и «`ConnectionRefusedError`»
   (прокси мёртв), и «`UpstreamError`» (407 / отказ SOCKS5) были различимы.
2. **Поле `detail` убрано из публикуемой записи.** Оно уходило в `usage[proxy]['detail']`, а
   `Pool.report()` и `snapshot()['top']` отдают этот словарь целиком. В итоге отказ, зафиксированный
   при наборе соединения, отличался от отказа после тоннеля **самим фактом наличия ключа** в
   словаре. Теперь причина живёт в `Pool.reasons` (+ аксессор `Pool.reason(proxy)`) и наружу не
   отдаётся: `report()` и `snapshot()` одинаковы для любого негодного адреса.
3. **Ошибки SOCKS5 сведены к одному коду.** «прокси выбрал не тот метод» и «прокси отверг пароль»
   дают одинаковый `UpstreamError('SOCKS5_AUTH')`.
4. **Клиентский байтовый ответ не зависит от наличия credential.** Живой прогон: с отправленным
   паролем и без него ответ клиенту совпадает до байта (`tests/…
   test_area_gateway_credentials.py::IndistinguishableFailureTests`).
5. **Ничего в снимке не называет credential.** В `Pool.snapshot()` (то, что слушатель отдаёт по
   `GET /status` любому, кто спросит) полей про учётные данные нет вообще. Счётчики
   `resolved`/`unavailable` есть только в `Gateway.state()` — это локальный взгляд оператора,
   и там нет ни значения, ни адреса, а только агрегаты.

Живой прогон — три сценария, одна и та же запись в пуле:

```
credential rejected : {'ok': 0, 'failed': 1, 'target_failed': 0, 'resting': False, 'health': 0.333}
no credential       : {'ok': 0, 'failed': 1, 'target_failed': 0, 'resting': False, 'health': 0.333}
dead proxy          : {'ok': 0, 'failed': 1, 'target_failed': 0, 'resting': False, 'health': 0.333}
all three leave the same record: True
```

**Честно названный остаток.** Отвечающий 407 апстрим и недоступный апстрим клиенту различаются
(`407` против `502`) — это существующее и закреплённое тестом поведение
`tests/test_gateway_health.py::test_upstream_answering_407_is_rested`. Это различие **не**
зависит от наличия secret: «нет пароля» и «неверный пароль» дают один и тот же байт в байт ответ,
а недоступность адреса любой наблюдатель устанавливает и сам, подключившись к нему напрямую.
Более строгий вариант (глушить 407 и отдавать клиенту общий 502) закрыл бы и эту ось, но требует
правки чужого теста — см. §3.4.

### 2.3 Привязка к пулу из интерфейса (дефект 2 области)

* `Gateway(..., binding=None)` — привязка, которую слушает этот listener. Кладётся в
  `pool.default_binding`, поэтому источник истины один: `Gateway.binding` — это свойство,
  возвращающее `pool.default_binding`, а не второе поле.
* `Gateway.set_binding(binding)` / `Gateway.aset_binding(binding)` применяют привязку на живом
  слушателе и возвращают отчёт `{'binding':…, 'rows': N, 'generation':…, 'profile':…}`.
  `rows` — сколько строк новая привязка может отдать **сейчас**: привязка к поколению, которого
  нет в текущем экспорте, даёт `rows: 0`, и это видно сразу, а не как 502 у клиента.
* `start(..., binding=None, …)`. Если переданы и `binding`, и `default_binding` с разным
  содержимым — `ValueError`, а не «победил один».
* `Background(..., binding=None)` пробрасывает то же самое; `Background.set_binding(binding)`
  выполняет это на том loop, который обслуживает клиентов, и возвращает тот же отчёт.
* Клиентские фильтры по-прежнему только сужают (`_bound_rows`): привязка может запретить, но не
  расширить.

### 2.4 `--lan` стал настоящим (дефект 3 области, F17)

* `Bind.listen_host` — адрес, который реально вешается на сокет. Расходится с `host` ровно в
  одном случае, и этот случай и есть смысл opt-in: `lan=True` с loopback-адресом означает
  «открой меня в сеть», поэтому слушатель берёт wildcard. С `--lan` и выбранным интерфейсом
  вешается **только** этот адрес.
* `Bind.local` переведён на `listen_host` (иначе `allow_local_without_auth` не включился бы).
* `Bind.published_host` считается от `listen_host`: иначе LAN-слушатель публиковал бы телефону
  `127.0.0.1`. Wildcard никогда не публикуется.
* `start(..., interface=…)` / `Background(..., interface=…)` — выбор адаптера из
  `gateway.lan_interfaces()`. Интерфейс без явного `lan=True` отвергается, а не игнорируется.
* Пароль LAN — отдельный: `resolve_token` генерирует собственный, если не задан; токены GUI и API
  он не читает и совпасть с ними не может.
* Видимое состояние: `Gateway.state()` добавляет `bind.listen_host`, `bind.local`,
  `reachable_from_lan`, `listen_address()`, `interfaces`, `credentials`.
  `reachable_from_lan = bind.lan and not bind.local and bool(token)`.

Живой вывод:

```
default (local)        : {'host': '127.0.0.1', 'lan': False, 'listen_host': '127.0.0.1',
                          'local': True, 'published_host': '127.0.0.1'}
--lan, no address      : {'host': '127.0.0.1', 'lan': True, 'listen_host': '0.0.0.0',
                          'local': False, 'published_host': '192.168.0.41'}
--lan, chosen interface: {'host': '127.0.0.1', 'lan': True, 'interface': '192.168.0.41',
                          'listen_host': '192.168.0.41', 'published_host': '192.168.0.41'}
bind 0.0.0.0 alone     : refused - the address 0.0.0.0 is reachable from the network: enable LAN explicitly
LAN password           : generated | 32 chars
local password         : (None, 'none')
```

### 2.5 Что уже было и переписано не было

Резерв слота до `await` с освобождением на cancel/error, единый deadline на весь handshake,
health-честность (TCP-открыт ≠ успешный запрос), `ReplayGuard` (запрос не уходит второму
апстриму), раздельные секреты, denylist до prefilter, `on_deny`-политика — не трогал, кроме
пункта 2.2 про `detail`, который и был утечкой. Всё это перепроверено живыми тестами ниже.

---

## 3. Прошу внести в чужие файлы

### 3.1 Владелец `api.py` — `public_row` (блокер для точной привязки доступа)

`api.public_row` (`api.py:89-161`) не переносит в строку `access_id` и `access_revision`, хотя
`exportsvc.ROW_FIELDS` их объявляет. Прошу добавить в возвращаемый словарь:

```python
'access_id': row.get('access_id'),
'access_revision': row.get('access_revision'),
```

Это ровно те два поля, которые `exportsvc.redact_row` оставляет (ссылка, не значение).
С ними `AccessCredentials` начнёт брать именно ту личность, которой строка была измерена, и
сверять `access_revision`; без них работает только безопасный fallback «у endpoint ровно одна
пригодная личность». `access_revision` в строке не публикуется наружу иначе — это номер, а не
секрет.

### 3.2 Владелец `gui.py` — четыре точных места

**(а) `App.start_gateway` (`gui.py:1136-1148`)** — сама привязка:

```python
with self.mutex:
    try:
        self.gateway = gateway.Background(
            self.data, bind['host'], bind['port'],
            token=self.gateway_token,
            bind=self.gateway_binding(),          # ← привязка из POST /api/gateway/config
            lan=bool(self.gateway_lan),          # ← см. (в)
            interface=self.gateway_interface,     # ← см. (в)
        )
```

`gateway_binding()` уже существует (`gui.py:3582`) и уже валидирует `pool_id`; сейчас его
результат просто не передаётся. `Background` берёт и `bind=`, и `interface=`, и `lan=`
одновременно; `lan=None` означает «не указан».

**(б) `App.start_gateway` и `main` (`gui.py:4212-4228`)** — LAN должен доезжать до
`Background`. Сейчас `args.lan` печатает предупреждение и уходит в никуда. Точная форма:

```python
server.app.gateway_lan = bool(args.lan) and api.is_loopback(args.gateway_host)
# либо адрес не указан (тогда gateway сам берёт wildcard), либо он уже не loopback
server.app.gateway_interface = args.gateway_interface or None
```

и `gateway.Background(..., lan=server.app.gateway_lan,
interface=server.app.gateway_interface)`. Сейчас в `proxytool.py` есть только `--gateway-host`
и `--gateway-token`; флаг `--lan` в argparse есть, а `--gateway-interface` — нет, его надо
добавить в владельца CLI (`proxytool.py:3498-3521`) со списком значений из
`gateway.lan_interfaces()`.

**(в) `App.gateway_state` (`gui.py:1102-1126`)** — `mobile_ready` считается от `runner.host`,
то есть от **запрошенного** адреса. Для `--lan` с адресом по умолчанию это `127.0.0.1`, и QR
не покажется, хотя слушатель уже в сети. Прошу заменить условие на то, что теперь считает сам
шлюз:

```python
mobile_ready = bool(getattr(runner.gateway, 'reachable_from_lan', False))
```

и брать `host` для показа из `runner.display_host` (уже так и есть), а адрес, на котором
реально висит сокет, показывать отдельным полем из `runner.state()['bind']['listen_host']`.

**(г) `App.gateway_binding` (`gui.py:3582-3599`)** — возвращать `None`, если настройка пуста, и
это уже так. Дополнительно прошу применить привязку **без перезапуска**: `gateway_configure`
(`gui.py:3616-3660`) сейчас делает `stop_gateway()` + `start_gateway()`. Теперь есть

```python
runner.set_binding(self.gateway_binding())   # возвращает {'rows': N, ...}
```

и `rows == 0` — это честный ответ «выбранный пул сейчас ничего не отдаёт», который стоит
показать, вместо того чтобы перезапускать слушатель и получать 502 у клиента.

### 3.3 Владелец `proxytool.py` — CLI-путь `gateway`

`run_gateway` (`proxytool.py:3706-3752`) может, по желанию, передавать
`lan=`/`interface=` в `gateway.start`, чтобы CLI и GUI вели себя одинаково. Не блокер: текущий
вызов с `args.host` по-прежнему работает, а `Bind` сам отвергнет не-loopback без `lan`.

### 3.4 Владелец `tests/test_gateway_health.py` — решение по 407 (нужен выбор владельца)

Сейчас `relay` пересылает клиенту 407 апстрима как есть. Это закреплено
`test_upstream_answering_407_is_rested` (проверяет код ответа 407 у клиента). Сам тест по сути
проверяет откат апстрима, а код ответа в нём — побочный случай.

Рекомендую снять клиентскую разницу (одна строка в `Gateway.relay`: после
`score('upstream_refused')` при `status == 407` обнулить `head`, чтобы сработал общий 502) и
поправить в этом тесте одно утверждение на 502. Что это даст: «нет пароля», «неверный пароль» и
«прокси мёртв» станут неразличимы **на обоих** каналах — и в статистике пула, и в байтах
клиенту. Что это стоит: target, который сам отвечает 407 на plain-HTTP-пути, будет показан как
502 (через CONNECT-тоннель настоящий статус доходит).

Я это **не сделал**, потому что файл не мой: менять закреплённое поведение другого теста без
решения владельца — не моя зона. Внутри пула канал утечки закрыт полностью (§2.2).

---

## 4. Проверки

Все прогоны — на loopback, без публичных прокси и внешних сервисов.

| Что | Как | Где |
| --- | --- | --- |
| Учётные данные: байты на проводе | мок HTTP с Basic, мок SOCKS5 с паролем, мок SOCKS4; CONNECT, forward, SOCKS5, SOCKS5h, открытый прокси | `tests/test_area_gateway_credentials.py` (18 тестов) |
| Выбор личности | одна личность у endpoint, две личности (не сливаются), ротация пароля, superseded-ревизия, запертый vault | там же, `IdentityTests` |
| Канал утечки | три сценария дают одну запись в пуле; в снимке нет слов auth/credential/password; ответ клиенту идентичен | там же, `IndistinguishableFailureTests` |
| Паритет транспортов через listener | http, socks4, socks4a, socks5, socks5h, https-to-proxy (свой CA через openssl), IPv6, оба DNS-режима | `tests/test_area_gateway_f16.py::ParityThroughTheListenerTests` |
| Ротация / sticky / TTL | round-robin, random, health-aware, failover против strict, истечение сессии | `tests/test_area_gateway_f16.py::RotationTests` |
| Параллельные лимиты, cancel, deadline, долгие соединения, shutdown | до 2 одновременных соединений к одному апстриму пиком, третий отказ; отмена handler-задачи внутри handshake; deadline; cap длины; forced/drained в отчёте | `tests/test_area_gateway_f16.py::ConcurrencyTests` |
| Снимок вне loop, bounded cache, смена поколения, удаление пула, изоляция клиентов | поток, в котором читался экспорт, проверяется напрямую; `cache_limit`; pinned generation; удалённый экспорт; `pool-<id>` | `tests/test_area_gateway_f16.py::VisibilityTests` |
| Привязка и LAN | `start(binding=…)`, два разных binding → отказ, сужение scope; `listen_host`, интерфейс, отказ без opt-in | `tests/test_area_gateway_f16.py::ListenerBindingTests`, `LanTests` |
| F17: рецепт и QR | реальный `Background`, опубликованные поля соединяют клиента по HTTP и SOCKS5; LAN-слушатель на реальном адаптере; QR кодируется кодом страницы под node и декодируется обратно | `tests/test_area_gateway_f17.py` (9 тестов) |
| Регрессии | 125 существующих gateway-тестов + мои 57 | `python -m unittest tests.test_gateway* tests.test_qr tests.test_web_connect tests.test_area_gateway_*` → **OK, 182 теста** |

Известная нестабильность, **не моя**: `tests/test_gateway_rotation.py::test_random_spreads_over_every_candidate`
падает примерно в 1 прогоне из 5 (проверял и на исходном `gateway.py` через `git stash` — там
та же картина). Остальные падения полного прогона (`db_migrations`, `desktop_signing`,
`probes_reference`, `db_retention`, `extras`, `paths`, `sourcedesk_compare`, `areajobs_scheduler`,
`selection`) существуют без моих правок — проверил тем же stash-способом: 11 падений до и после.

---

## 5. Открытые вопросы

1. **407 клиенту** — оставляем прозрачный релей (сейчас) или глушим в общий 502 (§3.4)?
   Решение за владельцем контракта; внутри пула утечки нет в обоих вариантах.
2. **Поле интерфейса в CLI** — добавлять ли `--gateway-interface` в `proxytool.py`, или
   интерфейс выбирается только в GUI (§3.2б)?
3. **Защищённый пул в `Pool.source_rows`.** Сейчас селектор выбирает строки, а учётные данные
   берутся по `endpoint_id`; если один адрес имеет несколько пригодных личностей, шлюз не
   авторизуется вовсе. Когда `public_row` начнёт нести `access_id` (§3.1), вопрос снимется.
4. **Отдельный credential на прокси в LAN.** Для `access_id` публикации и для
   `secrets.AccessStore` нужен доступ из процесса GUI к тому же vault, которым пользуется
   worker-проверка. Если это не так, шлюз честно не аутентифицируется и показывает
   `credentials.unavailable` в `state()` — это диагностика, а не ошибка.
