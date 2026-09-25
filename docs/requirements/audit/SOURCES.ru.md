# Источники прокси: разбор каталога и план улучшения

Дата: 25 сентября 2026. Основа — все 55 строк `proxy_workbench/sources.json` в рабочем дереве 2.2.1. Ссылки в инвентаризации взяты из кода. Их доступность, содержимое и реальные прокси в этом аудите не проверялись.

Изучение отдельных README поставщиков даёт сведения об их заявленных форматах; не подтверждает число живых прокси, качество или актуальность каждой ленты. Ни один источник ниже не объявлен мёртвым и не удалён.

## 1. Что есть сейчас

| Показатель | Значение | Значение для разработки |
| --- | --- | --- |
| Всего URL | 55 | Это не 55 независимых наборов прокси |
| Точные дубли строк | 0 | Нужна дедупликация содержимого и семейства, а не только URL |
| GitHub Raw | 43, около 78% ссылок | Один инфраструктурный сбой затрагивает большую часть каталога |
| jsDelivr для GitHub | 2 | CDN-зеркало не является независимым поставщиком |
| ProxyScrape API | 5 | Есть v2/v4 и разные протоколы/параметры; требуется сравнение полезности |
| Остальные | Proxyspace, три HTML-страницы, Geonode API | Другая инфраструктура, но независимость самих данных ещё нужно измерять |
| Форматы | 38 http, 2 http-fields, 6 socks5, 5 socks4, 3 text, 1 geonode | JSON/CSV metadata почти не используются |

Распределение — по подсказке parser в конфигурации, а не по фактическим протоколам загруженных адресов. В обычном текстовом источнике явная схема URI имеет приоритет.

Уже представлены известные семейства TheSpeedX, Monosans, Proxifly, IPLocate, Hookzof, ProxyScrape и Geonode. Главный следующий шаг — лучше использовать эти данные и измерить уникальный вклад.

## 2. Полный реестр текущих 55 ссылок

ID S01–S55 соответствуют порядку массива; строка исходного файла равна номеру ID + 1. Действие в последней колонке — рекомендация для разработки, не вердикт о работоспособности.

| ID | Источник и URL | Parser сейчас | Действие |
| --- | --- | --- | --- |
| S01 | [MuRongPIG/Proxy-Master · http.txt](https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt) | `http` | Дать паспорт; измерить свежесть и уникальный вклад. |
| S02 | [proxyspace.pro/http.txt](https://proxyspace.pro/http.txt) | `http` | Кандидат на разнообразие инфраструктуры; история ошибок и полезности. |
| S03 | [sunny9577/proxy-scraper · http_proxies.txt](https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt) | `http` | Объединить family с S31; проверить пересечение общего и HTTP feed. |
| S04 | [ProxyScrape · v4 · http](https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=http&proxy_format=ipport&format=text) | `http` | Рассмотреть основным HTTP API family ProxyScrape; сравнить с S30/S34. |
| S05 | [rdavydov/proxy-list · http.txt](https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt) | `http` | Паспорт, last-change, лицензия и измерение вклада. |
| S06 | [Zaeem20/FREE_PROXIES_LIST · https.txt](https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/https.txt) | `http` | HTTPS-name трактовать по CONNECT/capabilities; общая family с S28. |
| S07 | [iplocate/free-proxy-list · http.txt](https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/http.txt) | `http` | Общая карточка IPLocate с S45/S50; сохранить протокольное разделение. |
| S08 | [zloi-user/hideip.me · https.txt](https://raw.githubusercontent.com/zloi-user/hideip.me/main/https.txt) | `http-fields` | Специализированный fields-adapter и fixtures; общая family с S32. |
| S09 | [ProxyScrape · CDN · HTTPS](https://cdn.jsdelivr.net/gh/proxyscrape/free-proxy-list@main/proxies/protocols/https/data.txt) | `http` | CDN/feed family ProxyScrape; сопоставить protocol dataset, не давать двойную заслугу. |
| S10 | [relayglass/free-proxy-list · https.txt](https://raw.githubusercontent.com/relayglass/free-proxy-list/main/protocol/https/https.txt) | `http` | Проверить смысл HTTPS label; записать protocol hint и parser fixture. |
| S11 | [dinoz0rg/proxy-list · http.txt](https://raw.githubusercontent.com/dinoz0rg/proxy-list/main/checked_proxies/http.txt) | `http` | Паспорт и сопоставимость заявленных checked-данных с локальным профилем. |
| S12 | [databay-labs/free-proxy-list · http.txt](https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/http.txt) | `http` | Паспорт, freshness и overlap; решение о default по измерениям. |
| S13 | [Vann-Dev/proxy-list · http.txt](https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/http.txt) | `http` | Паспорт, freshness и overlap; решение о default по измерениям. |
| S14 | [ProxyScrape · CDN · HTTP](https://cdn.jsdelivr.net/gh/proxyscrape/free-proxy-list@main/proxies/protocols/http/data.txt) | `http` | CDN/feed family ProxyScrape; использовать как соответствующий fallback после сравнения. |
| S15 | [Anonym0usWork1221/Free-Proxies · http_proxies.txt](https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt) | `http` | Паспорт, freshness и overlap; доп. протоколы — только по документированным URL. |
| S16 | [ObcbO/getproxy · http.txt](https://raw.githubusercontent.com/ObcbO/getproxy/master/file/http.txt) | `http` | Проверить формат/обновляемость и уникальную полезность. |
| S17 | [casals-ar/proxy-list · http](https://raw.githubusercontent.com/casals-ar/proxy-list/main/http) | `http` | Явный parser для файла без расширения; контролировать content-type/формат. |
| S18 | [zevtyardt/proxy-list · http.txt](https://raw.githubusercontent.com/zevtyardt/proxy-list/main/http.txt) | `http` | Паспорт, freshness и overlap; решение о default по измерениям. |
| S19 | [hproxy-com/free-proxy-list · https.txt](https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/https.txt) | `http` | Общая family с S20; сравнить HTTP/HTTPS datasets по endpoint/capabilities. |
| S20 | [hproxy-com/free-proxy-list · http.txt](https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/http.txt) | `http` | Общая family с S19; не удваивать оценку совпадений. |
| S21 | [TheSpeedX/PROXY-List · http.txt](https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt) | `http` | Сверить canonical repo/aliases с README; общая family с S42/S47. |
| S22 | [monosans/proxy-list · http.txt](https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt) | `http` | Перспективен JSON adapter Monosans с provenance metadata; общая family S44/S48. |
| S23 | [proxifly/free-proxy-list · data.txt](https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt) | `http` | Перспективен JSON adapter Proxifly; общая family S33/S43/S49. |
| S24 | [ShiftyTR/Proxy-List · http.txt](https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt) | `http` | Изучить доп. протоколы по README; сначала оценить текущий HTTP feed. |
| S25 | [clarketm/proxy-list · proxy-list-raw.txt](https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt) | `http` | Проверять freshness/уникальный вклад; auto-quarantine только с объяснением. |
| S26 | [jetkai/proxy-list · proxies-http.txt](https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt) | `http` | Изучить документированные metadata/доп. протоколы; сохранить parser fixtures. |
| S27 | [roosterkid/openproxylist · HTTPS_RAW.txt](https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt) | `http` | HTTPS_RAW не считать автоматически TLS-to-proxy; проверить семантику. |
| S28 | [Zaeem20/FREE_PROXIES_LIST · http.txt](https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/http.txt) | `http` | Общая family с S06; оценить объединение пересекающихся endpoint. |
| S29 | [vakhov/fresh-proxy-list · http.txt](https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt) | `http` | Паспорт, freshness и overlap; название fresh не доказательство свежести. |
| S30 | [ProxyScrape · v2 · http](https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=5000) | `http` | Legacy ProxyScrape v2: сравнить с S04; затем решить primary/fallback. |
| S31 | [sunny9577/proxy-scraper · proxies.txt](https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt) | `http` | Общая family с S03; проверить mixed/explicit schemes и нужду в auto. |
| S32 | [zloi-user/hideip.me · http.txt](https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt) | `http-fields` | Общий fields-adapter HideIP с S08; корректно обрабатывать вариации полей. |
| S33 | [proxifly/free-proxy-list · data.txt](https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/https/data.txt) | `http` | Общая family Proxifly; разделить CONNECT и TLS-to-proxy metadata. |
| S34 | [ProxyScrape · v2 · http](https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all) | `http` | Вторая HTTP v2-выборка ProxyScrape; измерить отличие от S30/S04. |
| S35 | [prxchk/proxy-list · http.txt](https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt) | `http` | Паспорт, freshness и overlap; решение о default по измерениям. |
| S36 | [elliottophellia/yakumo · http_checked.txt](https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/http/global/http_checked.txt) | `http` | Checked/global — hint источника; локальная перепроверка и метаданные возраста. |
| S37 | [gproxynet/free-proxy-list · http.txt](https://raw.githubusercontent.com/gproxynet/free-proxy-list/main/http.txt) | `http` | Паспорт, freshness и overlap; решение о default по измерениям. |
| S38 | [ErcinDedeoglu/proxies · http.txt](https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt) | `http` | Проверить документированные дополнительные feeds; оценить уникальность. |
| S39 | [dpangestuw/Free-Proxy · http_proxies.txt](https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/http_proxies.txt) | `http` | Паспорт, freshness и overlap; решение о default по измерениям. |
| S40 | [ALIILAPRO/Proxy · http.txt](https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/http.txt) | `http` | Паспорт, freshness и overlap; решение о default по измерениям. |
| S41 | [hookzof/socks5_list · proxy.txt](https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt) | `socks5` | Отдельный SOCKS5 источник; измерять вклад и устойчивость по этому протоколу. |
| S42 | [TheSpeedX/PROXY-List · socks5.txt](https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt) | `socks5` | TheSpeedX family; canonical alias review, не дублировать publisher. |
| S43 | [proxifly/free-proxy-list · data.txt](https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt) | `socks5` | Proxifly SOCKS5; сравнить возможный отдельный SOCKS5 repo как ту же family. |
| S44 | [monosans/proxy-list · socks5.txt](https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt) | `socks5` | Monosans SOCKS5; по возможности получать JSON metadata общим adapter. |
| S45 | [iplocate/free-proxy-list · socks5.txt](https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/socks5.txt) | `socks5` | IPLocate SOCKS5; общая family с S07/S50. |
| S46 | [ProxyScrape · v4 · socks5](https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=socks5&proxy_format=ipport&format=text) | `socks5` | ProxyScrape SOCKS5; одна family с HTTP/SOCKS4 и CDN. |
| S47 | [TheSpeedX/PROXY-List · socks4.txt](https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt) | `socks4` | TheSpeedX SOCKS4; оставить отдельную capability внутри общей family. |
| S48 | [monosans/proxy-list · socks4.txt](https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt) | `socks4` | Monosans SOCKS4; общий adapter и полная provenance. |
| S49 | [proxifly/free-proxy-list · data.txt](https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt) | `socks4` | Proxifly SOCKS4; общий adapter, protocol hint не заменяет проверку. |
| S50 | [iplocate/free-proxy-list · socks4.txt](https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/socks4.txt) | `socks4` | IPLocate SOCKS4; общая family, отдельная статистика протокола. |
| S51 | [ProxyScrape · v4 · socks4](https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=socks4&proxy_format=ipport&format=text) | `socks4` | ProxyScrape SOCKS4; общие limits publisher и отдельный feed. |
| S52 | [free-proxy-list.net/](https://free-proxy-list.net/) | `text` | HTML-table adapter вместо общего regex; content-changed и schema fixtures. |
| S53 | [www.sslproxies.org/](https://www.sslproxies.org/) | `text` | HTML adapter; CONNECT metadata; проверить общность dataset с S52/S54. |
| S54 | [www.us-proxy.org/](https://www.us-proxy.org/) | `text` | HTML adapter; географическая подсказка не заменяет observed exit-country. |
| S55 | [Geonode · API](https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&protocols=http%2Chttps%2Csocks4%2Csocks5) | `geonode` | Пагинация, lastChecked/metadata, total budget; обновить country resolver после collect. |

## 3. Как переработать существующие семейства

| Семейство | Текущие записи | Предлагаемая работа |
| --- | --- | --- |
| ProxyScrape | S04/S09/S14/S30/S34/S46/S51 | Одна family; основные feeds по протоколу, отдельные соответствующие fallback URLs. Сравнить v2/v4 перед переводом старых в резерв |
| Proxifly | S23/S33/S43/S49 | Использовать структурный JSON там, где полезны поля; HTTP/HTTPS labels трактовать по фактическому протоколу, не расширению файла |
| Monosans | S22/S44/S48 | JSON adapter с metadata и provenance; собственные локальные замеры остаются основой рейтинга |
| IPLocate | S07/S45/S50 | Единая карточка на три протокола; profile-specific yield, freshness и вклад в разнообразие |
| TheSpeedX | S21/S42/S47 | Сверить canonical repository/aliases: README ссылается на SOCKS-List; не добавлять alias как нового независимого поставщика |
| Hookzof | S41 | Отдельный SOCKS5-oriented source с timestamp и собственной метрикой полезности |
| Sunny | S03/S31 | Измерить пересечение generated-http и общего списка; проверить, нужен ли mixed adapter |
| Zaeem20 | S06/S28 | Сохранить различие feed/capability; не удваивать score совпадающих endpoint |
| HideIP | S08/S32 | Один специализированный adapter вместо разрастания форматов верхнего уровня; fixtures реальных вариантов полей без чувствительных данных |
| hproxy | S19/S20 | Выяснить отличие HTTP/HTTPS наборов и сохранить protocol hints; дедупликация endpoint отдельно |
| Geonode | S55 | Метаданные/lastChecked, incremental refresh, pagination budget; обновление country resolver сразу после collect |
| HTML lists | S52–S54 | Раздельные adapters/fixtures; проверить общность оператора и данных, устойчивость к смене HTML |
| Остальные GitHub feeds | S01/S05/S10–S13/S15–S18/S24–S27/S29/S35–S40 | Автоматическая история качества и пересечений. Решение о default/quarantine после измерений, не по звёздам репозитория |
| Proxyspace | S02 | Сохранять как кандидата на инфраструктурное разнообразие; качество определять локальными наблюдениями |

У Monosans документированы TXT/JSON, exit-IP, ASN и geolocation. У Proxifly документированы TXT/JSON/CSV. Для TheSpeedX важно проверить именно различие имени текущего репозитория и download-links в README. [Monosans](https://github.com/monosans/proxy-list), [Proxifly](https://github.com/proxifly/free-proxy-list/blob/main/README.md), [TheSpeedX](https://github.com/TheSpeedX/PROXY-List).

## 4. Где искать дополнительный полезный охват

Сначала расширять возможности известных источников, затем добавлять новые:

1. **Metadata feeds:** форматы JSON/CSV уже используемых Monosans/Proxifly/ProxyScrape. Это может повысить точность предварительного отбора без увеличения количества запросов к мёртвым адресам. [ProxyScrape repository](https://github.com/ProxyScrape/free-proxy-list).
2. **Неиспользованные протоколы существующих publisher:** проверить документированные SOCKS-наборы у уже подключённых HTTP-источников. Не придумывать raw paths; фиксировать существующие ссылки после review README.
3. **Собственные файлы/подписки пользователя:** полезны независимо от состояния бесплатных списков; для provider endpoints добавить hostname/auth и secret store.
4. **Собственные внутренние списки:** только в отдельном явно выбранном trusted scope; публичный discovery продолжает блокировать private/metadata адреса.
5. **Новые открытые репозитории:** вводить как experimental, с паспортом, fixture и измерением пересечений.

### Конкретные кандидаты на experimental-каталог

| Кандидат | Что проверено по публичному описанию | Что необходимо выяснить до включения |
| --- | --- | --- |
| [Thordata/awesome-free-proxy-list](https://github.com/Thordata/awesome-free-proxy-list) | Заявлены несколько протоколов, CSV/JSON и исторические/географические поля | Canonical download paths, условия данных, степень копирования существующих feeds, реальный уникальный вклад |
| [proxmint/free-proxy-list](https://github.com/proxmint/free-proxy-list) | Заявлены TXT/JSON, собственная валидация и CC BY 4.0 с attribution | Совместимость metadata, правильная атрибуция, пересечения и различие remote/local checked-at |
| [proxifly/free-socks5-proxies](https://github.com/proxifly/free-socks5-proxies) | Отдельный SOCKS5-репозиторий того же publisher | Даёт ли вообще новые endpoint относительно S43; общий family_id обязателен |
| [Simatwa/free-proxies](https://github.com/Simatwa/free-proxies) | Заявлены HTTP/SOCKS4/SOCKS5; в документации встречаются JSON/timestamp links | Canonical owner/path после возможных переносов, формат, лицензия, обновляемость |
| [webunblocker/free-proxy-list](https://github.com/webunblocker/free-proxy-list) | Описан открытый snapshot с несколькими форматами | История проекта, частота обновлений, provenance, parser и полезность; не включать в default только из-за новизны |

Это предложения для дальнейшей оценки, а не готовый проверенный «список рабочих источников». Автоматическое добавление этих ссылок в текущую программу не выполнялось. Заявления о количестве IP, периодичности обновления и анонимности не переносить в собственный маркетинг как установленный факт.

## 5. Новая структура каталога

Пример схемы, а не реальная активная настройка:

```json
{
  "schema_version": 1,
  "catalog_version": "2026-09-25.1",
  "sources": [{
    "id": "example-http",
    "display_name": "Example / HTTP",
    "publisher_id": "example",
    "family_id": "example-public-dataset",
    "homepage": "https://lists.example.org/",
    "adapter": "text_lines_v1",
    "urls": ["https://lists.example.org/http.txt"],
    "protocol_hint": "http",
    "enabled_by_default": false,
    "recommended_refresh_seconds": 1800,
    "terms_url": "https://lists.example.org/terms",
    "license_status": "needs_review",
    "limits": {"max_bytes": 33554432, "max_candidates": 500000},
    "parser_fixture": "example-http-v1.txt"
  }]
}
```

URL каталога должен указывать на реальное расположение файла. Сейчас это отдельная проблема F01: `branding.SOURCES_URL` смотрит в корень, а bundled файл находится внутри Python-пакета.

Runtime-состояние не хранить в публичном JSON. Для каждого source нужны `enabled_override`, last fetch/change/success, ETag, content hash, error streak, next retry, imported/unique/working counters. Для каждой связи source/proxy — first/last seen и metadata с её происхождением.

Не давать каталогу загружать исполняемый Python/JS. Новый adapter добавляется через проверяемый релиз приложения; удалённый каталог выбирает только известный adapter и data-параметры.

## 6. Метрики полезности

| Метрика | Как считать | Как использовать |
| --- | --- | --- |
| Fetch success | Успешные завершённые загрузки / запланированные попытки за окно | Отличать временно недоступный сайт от плохих прокси |
| Parse validity | Валидные уникальные endpoint / разобранные записи | Замечать смену формата и HTML-заглушки |
| Local pass rate | Прошедшие базовую локальную проверку / реально проверенные | Не смешивать с фильтром страны пользователя |
| Task yield | Прошедшие конкретный профиль / проверенные в сопоставимых условиях | Помогать подобрать источники для конкретной задачи |
| Unique working contribution | Свежие working endpoint, отсутствующие у остальных family | Выбирать источники, которые расширяют пул |
| Survival | Доля тех же endpoint, прошедших следующую проверку через одинаковый интервал | Оценивать длительную полезность, а не однократную удачу |
| Freshness | Возраст remote observation, content и local check отдельно | Не считать новый commit или fetch новым измерением |
| Cost | Байты, requests и время на новый свежий подходящий endpoint | Экономить трафик, батарею и время |
| Overlap | Доля совпадений наборов за одно окно; отдельно endpoint и exit-IP | Выявлять зеркала/агрегаторы, не удаляя разные протоколы одного host |
| Confidence | Размер/свежесть выборки и число независимых запусков | Не делать вывод «плохой источник» по 1–2 проверкам |

Не использовать одну пропорцию «прошли/получены» без условий измерения. Страна пользователя, недоступный target, остановка find-N и разный порядок источников создают смещение выборки. Нужны сопоставимые выборки, отдельный baseline-профиль и ограниченная доля проверки новых источников.

## 7. Политика обновления и отключения

- **304:** оставить dataset и обновить last-fetch; не обновлять local checked-at.
- **429/503:** уважить Retry-After, ограничить запросы к host, отложить повтор.
- **404/410:** показать причину; проверить catalog replacement при следующем обновлении, не стирать историю.
- **Нет новых адресов:** не значит «не работает» — источник может поддерживать полезные стабильные адреса.
- **Нет прошедших задачу:** проверить baseline/фильтры; не удалять источник по одному пользовательскому профилю.
- **Parser-changed:** поместить в quarantine с последним рабочим cache; cache-кандидаты тоже проходят freshness policy.
- **Резкий рост объёма/смена формата:** остановить по budget, сохранить диагностический код и предложить review.
- **Адрес исчез из источника:** отметить last-seen, не удалять из других коллекций; active pool определяется локальной свежестью.
- **Источник disabled:** не загружать; отдельно дать выбор, использовать ли ранее полученные адреса этого scope.
- **Новый каталог:** preview additions/changes/removals; локальные overrides сохраняются; можно откатить.

## 8. Порядок работ по sources

1. Исправить URL каталога, отображаемые имена и country resolver после сбора.
2. Остановить ошибочный auto-prune; временно оставить ручное отключение с объяснением.
3. Ввести source ID/family/provenance и отдельные counters.
4. Добавить fetch cache, host limits, global budgets и объяснимые errors.
5. Реализовать мастер источника и несколько структурных adapters.
6. Сравнить источники на одинаковых fixtures; живую полезность измерять только отдельным явно разрешённым запуском.
7. Сформировать небольшой default-набор по уникальному рабочему вкладу; остальные оставить доступными как расширенный каталог.
8. Добавлять experimental-кандидатов из раздела 4 по тому же процессу.

Результат этой работы должен измеряться временем до нужного свежего пула и его устойчивостью, а не количеством строк URL в `sources.json`.
