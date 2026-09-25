# Журнал проверки источников

Срез сделан: **2026-09-25T12:24:52Z UTC**. Инструмент: `tools/verify_sources.py`.

## Что и как проверялось

Выполнялись только обычные HTTP GET к публичным файлам списков. Ни один найденный прокси-эндпоинт не использовался для соединения, чужие адреса не сканировались, аккаунты не создавались и оплата не производилась.

Ограничения прохода: дедлайн одного запроса 25 с, лимит тела 4194304 байт, параллельность 8, пауза между запросами к одному хосту 1.0 с.

## Разделение статусов

В этом исследовании различаются пять статусов. Первые четыре подтверждаются ниже, пятый — нет.

| Статус | Что означает | Подтверждён этим исследованием |
| --- | --- | --- |
| Найден по документации | источник описан в чужом README или каталоге | да |
| URL доступен | ответ пришёл, код в диапазоне 2xx, тело непустое | да |
| Формат подтверждён | тело разобрано и содержит распознаваемые адреса или заявленную структуру | да |
| Данные выглядят обновляемыми | есть ETag/Last-Modified либо наблюдаемая смена содержимого | да, косвенно |
| Содержащиеся прокси проверены | реальное соединение через прокси установлено и проверено | **НЕТ, ни для одного источника** |

Надпись «verified» или «checked» на сайте или в имени файла источника — это утверждение поставщика, а не доказательство работоспособности. В этом цикле ни один адрес из списков не проверялся на работоспособность, поэтому соответствующий статус нигде не выставлен.

Частичная загрузка (сбой по лимиту тела или дедлайну) означает «проверено частично», а не «источник не работает».

## Итоги

- Всего кандидатов: **150**.
- URL доступен и отдал непустое тело: **140**.
- Нет пригодного непустого ответа: **10**; в этот счёт входит `cur-37` с HTTP 200 и пустым телом, что не доказывает постоянную недоступность.
- Ответ редиректит: **7**.
- Обрезано по лимиту тела: **0** — по ним выводы о размере набора и о полноте сравнения делать нельзя.
- Ответили, но без распознаваемых адресов: **18**.
- Есть ETag или Last-Modified (можно опрашивать дёшево): **121**.
- Содержит metadata-поля (страна/ASN/анонимность/время проверки): **67**.

## Ответили, но без распознаваемых адресов

| ID | URL | HTTP | Тип ответа | Первые строки |
| --- | --- | --- | --- | --- |
| new-001 | https://www.webshare.io/proxy-server | 200 | text/html; charset=utf-8 | <!DOCTYPE html><!-- Last Published: Thu Sep 24 2026 07:30:36 GMT+0000  |
| new-002 | https://brightdata.com/ | 200 | text/html; charset=UTF-8 | <!DOCTYPE html> |
| new-003 | https://decodo.com/proxies | 200 | text/html; charset=utf-8 | <!DOCTYPE html><html lang="en" class="poppins_70b479f1-module__7L0PUq_ |
| new-005 | https://soax.com/ | 200 | text/html; charset=utf-8 | <!DOCTYPE html><html lang="en" class="fustat_6971d0c4-module__U69ThW__ |
| new-007 | https://www.proxyrack.com/ | 200 | text/html; charset=utf-8 | <!DOCTYPE html><html lang="en"><head><meta charSet="utf-8" data-next-h |
| new-010 | https://advanced.name/freeproxy?country=us | 200 | text/html; charset=UTF-8 | <!DOCTYPE html> |
| new-015 | https://proxy-daily.com/ | 200 | text/html; charset=utf-8 | <!doctype html> |
| new-027 | https://www.kaggle.com/datasets/litportnet/free-proxy-observations | 200 | text/html; charset=utf-8 | <!DOCTYPE html> |
| new-028 | https://www.kaggle.com/datasets/warifp/proxy-socks5-realtime-update-dataset | 200 | text/html; charset=utf-8 | <!DOCTYPE html> |
| new-050 | https://hide.mn/en/proxy-list/countries/ | 200 | text/html; charset=utf-8 | <!DOCTYPE html><html lang="en"><head> |
| new-052 | https://proxy-list.org/ | 200 | text/html | <!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http:/ |
| new-059 | https://rootjazz.com/proxies/proxies.txt | 200 | text/plain | 303.303.303:8888 |
| new-086 | https://raw.githubusercontent.com/gproxynet/free-proxy-list/main/proxies.json | 200 | text/plain; charset=utf-8 | [] |
| new-088 | https://telegram.org/ | 200 | text/html; charset=utf-8 | <!DOCTYPE html> |
| new-089 | https://wiki.metacubex.one/ | 200 | text/html; charset=utf-8 | <!doctype html> |
| new-091 | https://sing-box.sagernet.org/ | 200 | text/html; charset=utf-8 | <!doctype html> |
| new-094 | https://hiddify.com/ | 200 | text/html; charset=utf-8 | <!doctype html> |
| new-095 | https://v2rayn.2dust.link/ | 200 | text/html; charset=utf-8 | <!DOCTYPE html> |

## Нет пригодного непустого ответа

| ID | URL | Код | Причина по данным проверки |
| --- | --- | --- | --- |
| cur-37 | https://raw.githubusercontent.com/gproxynet/free-proxy-list/main/http.txt | 200 | HTTP 200, тело пустое на момент проверки |
| new-017 | https://nordvpn.com/ru/free-proxy-list/ | 403 | 403 — доступ запрещён |
| new-018 | https://premproxy.com/socks-list/ | 404 | 404 — файла или страницы нет |
| new-019 | https://www.freeproxylists.net/ | 403 | 403 — доступ запрещён |
| new-020 | https://www.proxy-list.download/SOCKS5 | 502 | 5xx — ошибка на стороне сервера |
| new-037 | https://gitlab.com/dp0148475/ProxyList3/-/raw/main/proxies/https | 404 | 404 — файла или страницы нет |
| new-053 | https://raw.githubusercontent.com/z3a4/free-proxy-list/main/proxies/checked/all/all-proxies.json | 404 | 404 — файла или страницы нет |
| new-062 | https://www.docip.net/data/free.json | 0 | соединение не установлено или истекло: curl: (7) Failed to connect to www.docip.net port 443 after 697 ms: Couldn't connect to se |
| new-082 | http://127.0.0.1:5010/all | 0 | соединение не установлено или истекло: curl: (7) Failed to connect to 127.0.0.1 port 5010 after 0 ms: Couldn't connect to server |
| new-083 | https://proxy.010438.xyz/api/get?respType=json | 0 | соединение не установлено или истекло: curl: (6) Could not resolve host: proxy.010438.xyz |

## Ответили с редиректом или неожиданным типом содержимого

| ID | Исходный URL | Конечный URL | Редиректов | Тип ответа |
| --- | --- | --- | --- | --- |
| cur-53 | https://www.sslproxies.org/ | https://free-proxy-list.net/ssl-proxy.html | 1 | text/html; charset=utf-8 |
| cur-54 | https://www.us-proxy.org/ | https://free-proxy-list.net/us-proxy.html | 1 | text/html; charset=utf-8 |
| new-013 | https://hideip.me/en/proxy6 | https://px6.net/ | 2 | text/html; charset=utf-8 |
| new-025 | https://huggingface.co/datasets/litportnet/free-proxy-observations/resolve/main/all.json | https://huggingface.co/api/resolve-cache/datasets/litportnet/free-proxy-observations/d77d5820aeed497c273346bc5fd9f7d02325bee9/all.json?%2Fdatasets%2Flitportnet%2Ffree-proxy-observations%2Fresolve%2Fmain%2Fall.json=&etag=%220434b345361a0c786ce5bc5b7c219fc14036802f%22 | 1 | text/plain; charset=utf-8 |
| new-026 | https://www.kaggle.com/api/v1/datasets/download/charles1995/free-public-proxies-2026/proxies.csv | https://storage.googleapis.com:443/kagglesdsdata/datasets/10836003/17024305/proxies.csv?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Credential=gcp-kaggle-com%40kaggle-161607.iam.gserviceaccount.com%2F20260925%2Fauto%2Fstorage%2Fgoog4_request&X-Goog-Date=20260925T121033Z&X-Goog-Expires=259200&X-Goog-SignedHeaders=host&X-Goog-Signature=27238d648908694e3ae3ca425df83690a4889998b59877d80c1271cd29d2a99b588b53ae913eab5ad632f0e9b843b3e83b508eb477eeccc3f923e4044678b204fc31dab3e58161a6e4430dcfdbe995c1982f9ad43362d1f3b4f448c0fe13bb538ec5a5de0d6e5bb37fafc4fdd1d3f6e7d0935ffb69833ed4c192b87ec7aa0e4f06098f7e4fdb4f30c756ef90173c6fe39b4bd675d37926afeb154e514dfeed4ad9f5a95d8db5fb70b0ac67be100e3ab591c6497ad6f58ea00833030800ff46ec9acbe7fdd05eb9616ed196b95f25b2565b4b0a2d152a0bcbe69b1cae00fc580a5393179ae03f817a5d6cfe5f515438a7f3e9f8366a4b6686c655734daf5f86af | 1 | application/octet-stream |
| new-052 | https://proxy-list.org/ | https://proxy-list.org/english/index.php | 1 | text/html |
| new-067 | https://free-proxy-list.net/en/us-proxy.html | https://free-proxy-list.net/us-proxy.html | 1 | text/html; charset=utf-8 |

## Полный построчный журнал

| ID | Время проверки (UTC) | HTTP | Конечный URL | Тип | Байт | v4:port | v6:port | ETag | Last-Modified | Ограничения |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cur-01 | 2026-09-25T12:23:36Z | 200 | https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt | text | 624613 | 10903 | 0 | да | — | — |
| cur-02 | 2026-09-25T12:23:36Z | 200 | https://proxyspace.pro/http.txt | text | 73176 | 3871 | 0 | да | Fri, 25 Sep 2026 12:20:12 GMT | — |
| cur-03 | 2026-09-25T12:23:36Z | 200 | https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.tx | text | 11369 | 1878 | 0 | да | — | — |
| cur-04 | 2026-09-25T12:23:36Z | 200 | https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=http&p | text | 8826 | 1293 | 0 | — | Fri, 25 Sep 2026 12:22:55 GMT | — |
| cur-05 | 2026-09-25T12:23:36Z | 200 | https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt | text | 4009 | 554 | 0 | да | — | — |
| cur-06 | 2026-09-25T12:23:36Z | 200 | https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/https.txt | text | 4218 | 586 | 0 | да | — | — |
| cur-07 | 2026-09-25T12:23:36Z | 200 | https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/http.txt | text | 5620 | 824 | 0 | да | — | — |
| cur-08 | 2026-09-25T12:23:36Z | 200 | https://raw.githubusercontent.com/zloi-user/hideip.me/main/https.txt | text | 7383 | 902 | 0 | да | — | — |
| cur-09 | 2026-09-25T12:23:36Z | 200 | https://cdn.jsdelivr.net/gh/proxyscrape/free-proxy-list@main/proxies/protocols/https/data. | text | 2072 | 283 | 0 | да | — | — |
| cur-10 | 2026-09-25T12:23:36Z | 200 | https://raw.githubusercontent.com/relayglass/free-proxy-list/main/protocol/https/https.txt | text | 1064 | 129 | 0 | да | — | — |
| cur-11 | 2026-09-25T12:23:36Z | 200 | https://raw.githubusercontent.com/dinoz0rg/proxy-list/main/checked_proxies/http.txt | text | 13751 | 2037 | 0 | да | — | — |
| cur-12 | 2026-09-25T12:23:36Z | 200 | https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/http.txt | text | 22991 | 3443 | 0 | да | — | — |
| cur-13 | 2026-09-25T12:23:37Z | 200 | https://raw.githubusercontent.com/Vann-Dev/proxy-list/main/proxies/http.txt | text | 6619 | 975 | 0 | да | — | — |
| cur-14 | 2026-09-25T12:23:38Z | 200 | https://cdn.jsdelivr.net/gh/proxyscrape/free-proxy-list@main/proxies/protocols/http/data.t | text | 6703 | 953 | 0 | да | — | — |
| cur-15 | 2026-09-25T12:23:38Z | 200 | https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/http_pro | text | 46536 | 7083 | 0 | да | — | — |
| cur-16 | 2026-09-25T12:23:39Z | 200 | https://raw.githubusercontent.com/ObcbO/getproxy/master/file/http.txt | text | 32959 | 4935 | 0 | да | — | — |
| cur-17 | 2026-09-25T12:23:40Z | 200 | https://raw.githubusercontent.com/casals-ar/proxy-list/main/http | text | 1422133 | 10696 | 0 | да | — | — |
| cur-18 | 2026-09-25T12:23:41Z | 200 | https://raw.githubusercontent.com/zevtyardt/proxy-list/main/http.txt | text | 395509 | 10946 | 0 | да | — | — |
| cur-19 | 2026-09-25T12:23:42Z | 200 | https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/https.txt | text | 16406 | 2485 | 0 | да | — | — |
| cur-20 | 2026-09-25T12:23:43Z | 200 | https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/http.txt | text | 22996 | 3889 | 0 | да | — | — |
| cur-21 | 2026-09-25T12:23:44Z | 200 | https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt | text | 20845 | 2907 | 0 | да | — | — |
| cur-22 | 2026-09-25T12:23:45Z | 200 | https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt | text | 1680 | 217 | 0 | да | — | — |
| cur-23 | 2026-09-25T12:23:46Z | 200 | https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/dat | text | 39139 | 5534 | 0 | да | — | — |
| cur-24 | 2026-09-25T12:23:47Z | 200 | https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt | text | 350 | 40 | 0 | да | — | — |
| cur-25 | 2026-09-25T12:23:48Z | 200 | https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt | text | 3022 | 400 | 0 | да | — | — |
| cur-26 | 2026-09-25T12:23:49Z | 200 | https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.t | text | 10685 | 1801 | 0 | да | — | — |
| cur-27 | 2026-09-25T12:23:50Z | 200 | https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt | text | 680 | 82 | 0 | да | — | — |
| cur-28 | 2026-09-25T12:23:51Z | 200 | https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/http.txt | text | 1658 | 214 | 0 | да | — | — |
| cur-29 | 2026-09-25T12:23:52Z | 200 | https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt | text | 2303 | 528 | 0 | да | — | — |
| cur-30 | 2026-09-25T12:23:53Z | 200 | https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=5000 | text | 3230 | 440 | 0 | — | Fri, 25 Sep 2026 12:22:59 GMT | — |
| cur-31 | 2026-09-25T12:23:53Z | 200 | https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt | text | 13847 | 2241 | 0 | да | — | — |
| cur-32 | 2026-09-25T12:23:54Z | 200 | https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt | text | 1721 | 158 | 0 | да | — | — |
| cur-33 | 2026-09-25T12:23:55Z | 200 | https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/https/da | text | 6361 | 981 | 0 | да | — | — |
| cur-34 | 2026-09-25T12:23:56Z | 200 | https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all | text | 6652 | 953 | 0 | — | Fri, 25 Sep 2026 12:23:00 GMT | — |
| cur-35 | 2026-09-25T12:23:56Z | 200 | https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt | text | 478 | 58 | 0 | да | — | — |
| cur-36 | 2026-09-25T12:23:57Z | 200 | https://raw.githubusercontent.com/elliottophellia/yakumo/master/results/http/global/http_c | text | 2321 | 341 | 0 | да | — | — |
| cur-37 | 2026-09-25T12:23:58Z | 200 | https://raw.githubusercontent.com/gproxynet/free-proxy-list/main/http.txt | empty | 0 | 0 | 0 | да | — | пусто на момент проверки |
| cur-38 | 2026-09-25T12:23:59Z | 200 | https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt | text | 398679 | 10937 | 0 | да | — | — |
| cur-39 | 2026-09-25T12:24:00Z | 200 | https://raw.githubusercontent.com/dpangestuw/Free-Proxy/main/http_proxies.txt | text | 31985 | 4357 | 0 | да | — | — |
| cur-40 | 2026-09-25T12:24:01Z | 200 | https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/http.txt | text | 6362 | 924 | 0 | да | — | — |
| cur-41 | 2026-09-25T12:24:02Z | 200 | https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt | text | 87857 | 11219 | 0 | да | — | — |
| cur-42 | 2026-09-25T12:24:03Z | 200 | https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt | text | 19410 | 2869 | 0 | да | — | — |
| cur-43 | 2026-09-25T12:24:04Z | 200 | https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/d | text | 98719 | 7439 | 0 | да | — | — |
| cur-44 | 2026-09-25T12:24:05Z | 200 | https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt | text | 1820 | 354 | 0 | да | — | — |
| cur-45 | 2026-09-25T12:24:06Z | 200 | https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/socks5.txt | text | 5337 | 882 | 0 | да | — | — |
| cur-46 | 2026-09-25T12:24:07Z | 200 | https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=socks5 | text | 8507 | 1645 | 0 | — | Fri, 25 Sep 2026 12:23:01 GMT | — |
| cur-47 | 2026-09-25T12:24:07Z | 200 | https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt | text | 19916 | 2708 | 0 | да | — | — |
| cur-48 | 2026-09-25T12:24:08Z | 200 | https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt | text | 680 | 79 | 0 | да | — | — |
| cur-49 | 2026-09-25T12:24:09Z | 200 | https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/d | text | 4045 | 530 | 0 | да | — | — |
| cur-50 | 2026-09-25T12:24:10Z | 200 | https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/socks4.txt | text | 946 | 110 | 0 | да | — | — |
| cur-51 | 2026-09-25T12:24:11Z | 200 | https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=socks4 | text | 1975 | 241 | 0 | — | Fri, 25 Sep 2026 12:23:02 GMT | — |
| cur-52 | 2026-09-25T12:24:11Z | 200 | https://free-proxy-list.net/ | html | 21405 | 485 | 0 | — | Fri, 25 Sep 2026 12:22:02 GMT | — |
| cur-53 | 2026-09-25T12:24:11Z | 200 | https://free-proxy-list.net/ssl-proxy.html | html | 16416 | 165 | 0 | — | Fri, 25 Sep 2026 12:22:02 GMT | — |
| cur-54 | 2026-09-25T12:24:11Z | 200 | https://free-proxy-list.net/us-proxy.html | html | 18709 | 328 | 0 | — | Fri, 25 Sep 2026 12:22:02 GMT | — |
| cur-55 | 2026-09-25T12:24:12Z | 200 | https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_typ | json | 46447 | 247 | 0 | да | — | — |
| new-001 | 2026-09-25T12:24:12Z | 200 | https://www.webshare.io/proxy-server | html | 29778 | 0 | 0 | — | Thu, 24 Sep 2026 09:26:09 GMT | адресов не найдено |
| new-002 | 2026-09-25T12:24:12Z | 200 | https://brightdata.com/ | html | 48993 | 0 | 0 | — | Thu, 24 Sep 2026 16:47:25 GMT | адресов не найдено |
| new-003 | 2026-09-25T12:24:12Z | 200 | https://decodo.com/proxies | html | 99275 | 0 | 0 | — | — | адресов не найдено |
| new-004 | 2026-09-25T12:24:13Z | 200 | https://iproyal.com/ | html | 68319 | 6 | 0 | — | Fri, 25 Sep 2026 08:46:52 GMT | — |
| new-005 | 2026-09-25T12:24:13Z | 200 | https://soax.com/ | html | 36527 | 0 | 0 | да | Wed, 23 Sep 2026 15:26:04 GMT | адресов не найдено |
| new-006 | 2026-09-25T12:24:13Z | 200 | https://oxylabs.io/pricing | html | 72134 | 2 | 0 | — | Fri, 25 Sep 2026 12:03:37 GMT | — |
| new-007 | 2026-09-25T12:24:13Z | 200 | https://www.proxyrack.com/ | html | 157881 | 0 | 0 | да | — | адресов не найдено |
| new-008 | 2026-09-25T12:24:13Z | 200 | https://raw.githubusercontent.com/xyzs996/free-proxy-health-list/main/proxies/all/data.jso | json | 430962 | 306 | 0 | да | — | — |
| new-009 | 2026-09-25T12:24:13Z | 200 | https://raw.githubusercontent.com/proxio-io/proxy-list/main/all.json | json | 694709 | 257 | 0 | да | — | — |
| new-010 | 2026-09-25T12:24:13Z | 200 | https://advanced.name/freeproxy?country=us | html | 27435 | 0 | 0 | — | — | адресов не найдено |
| new-011 | 2026-09-25T12:24:14Z | 200 | https://www.proxynova.com/proxy-server-list/ | html | 18972 | 2 | 0 | — | — | — |
| new-012 | 2026-09-25T12:24:15Z | 200 | https://spys.one/proxies/ | html | 10002 | 27 | 0 | — | — | — |
| new-013 | 2026-09-25T12:24:15Z | 200 | https://px6.net/ | html | 6550 | 1 | 0 | — | — | — |
| new-014 | 2026-09-25T12:24:15Z | 200 | https://raw.githubusercontent.com/a2u/free-proxy-list/master/free-proxy-list.json | json | 49 | 2 | 0 | да | — | — |
| new-015 | 2026-09-25T12:24:15Z | 200 | https://proxy-daily.com/ | html | 9119 | 0 | 0 | — | — | адресов не найдено |
| new-016 | 2026-09-25T12:24:15Z | 200 | https://www.freeproxy.world/ | html | 7939 | 27 | 0 | — | — | — |
| new-017 | 2026-09-25T12:24:15Z | 403 | https://nordvpn.com/ru/free-proxy-list/ | html | 3325 | 0 | 0 | — | — | — |
| new-018 | 2026-09-25T12:24:16Z | 404 | https://premproxy.com/socks-list/ | html | 1060 | 0 | 0 | да | Sat, 30 May 2026 11:39:29 GMT | — |
| new-019 | 2026-09-25T12:24:16Z | 403 | https://www.freeproxylists.net/ | text | 15 | 0 | 0 | — | — | — |
| new-020 | 2026-09-25T12:24:16Z | 502 | https://www.proxy-list.download/SOCKS5 | html | 6482 | 0 | 1 | — | — | — |
| new-021 | 2026-09-25T12:24:16Z | 200 | https://raw.githubusercontent.com/Moleway/Free-Proxy-List/main/all.txt | text | 9714 | 1575 | 0 | да | — | — |
| new-022 | 2026-09-25T12:24:16Z | 200 | https://raw.githubusercontent.com/VMHeaven/VMHeaven.io-Free-Proxy-List/main/allproxy.txt | text | 73692 | 7618 | 0 | да | — | — |
| new-023 | 2026-09-25T12:24:17Z | 200 | https://raw.githubusercontent.com/themiralay/Proxy-List-World/master/data.json | json | 2271 | 171 | 0 | да | — | — |
| new-024 | 2026-09-25T12:24:17Z | 200 | https://raw.githubusercontent.com/litportnet/free-proxy-list/live/proxies/countries/us/all | json | 37303 | 598 | 0 | да | — | — |
| new-025 | 2026-09-25T12:24:18Z | 200 | https://huggingface.co/api/resolve-cache/datasets/litportnet/free-proxy-observations/d77d5 | json | 123917 | 619 | 0 | да | — | — |
| new-026 | 2026-09-25T12:24:18Z | 200 | https://storage.googleapis.com:443/kagglesdsdata/datasets/10836003/17024305/proxies.csv?X- | text | 164585 | 3932 | 0 | да | Mon, 22 Jun 2026 06:39:23 GMT | — |
| new-027 | 2026-09-25T12:24:19Z | 200 | https://www.kaggle.com/datasets/litportnet/free-proxy-observations | html | 4522 | 0 | 0 | — | — | адресов не найдено |
| new-028 | 2026-09-25T12:24:19Z | 200 | https://www.kaggle.com/datasets/warifp/proxy-socks5-realtime-update-dataset | html | 4326 | 0 | 0 | — | — | адресов не найдено |
| new-029 | 2026-09-25T12:24:20Z | 200 | https://raw.githubusercontent.com/SoliSpirit/proxy-list/main/Countries/http/Afghanistan.tx | text | 191 | 24 | 0 | да | — | — |
| new-030 | 2026-09-25T12:24:21Z | 200 | https://raw.githubusercontent.com/Argh94/Proxy-List/main/singbox.json | json | 13284 | 119 | 10 | да | — | — |
| new-031 | 2026-09-25T12:24:21Z | 200 | https://raw.githubusercontent.com/watchttvv/free-proxy-list/main/proxy.txt | text | 1041 | 27 | 0 | да | — | — |
| new-032 | 2026-09-25T12:24:21Z | 200 | https://raw.githubusercontent.com/proxygenerator1/ProxyGenerator/main/ALL/all.json | json | 458773 | 137 | 0 | да | — | — |
| new-033 | 2026-09-25T12:24:22Z | 200 | https://raw.githubusercontent.com/vmheaven/VMHeaven.io-Free-Proxy-List/main/Country/AE/htt | text | 510 | 161 | 0 | да | — | — |
| new-034 | 2026-09-25T12:24:23Z | 200 | https://codeberg.org/dbarker/public-proxy-list/raw/branch/main/proxies.txt | text | 471 | 20 | 0 | да | Wed, 16 Jul 2025 05:43:01 GMT | — |
| new-035 | 2026-09-25T12:24:23Z | 200 | https://codeberg.org/SirHowitzer/howitzer-socks5-proxies/raw/branch/main/data.txt | text | 10786 | 1210 | 0 | да | Thu, 02 Apr 2026 05:41:56 GMT | — |
| new-036 | 2026-09-25T12:24:24Z | 200 | https://gitlab.com/dp0148475/ProxyList4/-/raw/main/all.txt | text | 9267 | 470 | 0 | да | — | — |
| new-037 | 2026-09-25T12:24:24Z | 404 | https://gitlab.com/dp0148475/ProxyList3/-/raw/main/proxies/https | html | 1344 | 0 | 0 | — | — | — |
| new-038 | 2026-09-25T12:24:25Z | 200 | https://gitlab.com/dp0148475/ProxyList2/-/raw/main/all.txt | text | 479026 | 10536 | 0 | да | — | — |
| new-039 | 2026-09-25T12:24:25Z | 200 | https://gitlab.com/selfinvestwarehouse/PROXY-List/-/raw/master/http.txt | text | 63693 | 3323 | 0 | да | — | — |
| new-040 | 2026-09-25T12:24:26Z | 200 | https://gitea.com/vvon/openproxylist/raw/branch/main/HTTPS_RAW.txt | text | 1361 | 71 | 0 | да | Mon, 10 Nov 2025 03:00:11 GMT | — |
| new-041 | 2026-09-25T12:24:26Z | 200 | https://raw.githubusercontent.com/Ciara-12/PROXY-free/refs/heads/main/http.txt | text | 3517 | 417 | 0 | да | — | — |
| new-042 | 2026-09-25T12:24:26Z | 200 | https://raw.githubusercontent.com/TuanMinPay/live-proxy/master/all.txt | text | 197793 | 10389 | 0 | да | — | — |
| new-043 | 2026-09-25T12:24:27Z | 200 | https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/http.txt | text | 2140520 | 7666 | 0 | да | — | — |
| new-044 | 2026-09-25T12:24:27Z | 200 | https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/http_all.txt | text | 5045 | 670 | 0 | да | — | — |
| new-045 | 2026-09-25T12:24:27Z | 200 | https://databay.com/api/v1/proxy-list?protocol=socks5&country=US&anonymity=elite&limit=100 | json | 1927 | 46 | 0 | — | Fri, 25 Sep 2026 12:24:28 GMT | — |
| new-046 | 2026-09-25T12:24:28Z | 200 | https://proxy-free.com/free-api/proxies/?format=json | json | 11231 | 22 | 0 | — | — | — |
| new-047 | 2026-09-25T12:24:28Z | 200 | https://proxy11.com/ | html | 3186 | 11 | 0 | — | — | — |
| new-048 | 2026-09-25T12:24:28Z | 200 | https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/json/all.j | json | 9027 | 118 | 0 | да | — | — |
| new-049 | 2026-09-25T12:24:28Z | 200 | https://cdn.jsdelivr.net/gh/webunblocker/free-proxy-list@main/proxies/all/data.json | json | 2634 | 104 | 0 | да | — | — |
| new-050 | 2026-09-25T12:24:29Z | 200 | https://hide.mn/en/proxy-list/countries/ | html | 5235 | 0 | 0 | — | Fri, 25 Sep 2026 12:24:30 GMT | адресов не найдено |
| new-051 | 2026-09-25T12:24:30Z | 200 | https://list.proxylistplus.com/ | html | 8280 | 56 | 2 | — | Fri, 25 Sep 2026 12:24:21 GMT | — |
| new-052 | 2026-09-25T12:24:30Z | 200 | https://proxy-list.org/english/index.php | html | 10167 | 0 | 0 | — | — | адресов не найдено |
| new-053 | 2026-09-25T12:24:31Z | 404 | https://raw.githubusercontent.com/z3a4/free-proxy-list/main/proxies/checked/all/all-proxie | text | 14 | 0 | 0 | — | — | — |
| new-054 | 2026-09-25T12:24:31Z | 200 | https://raw.githubusercontent.com/r00tee/Proxy-List/main/Https.txt | text | 34689 | 5020 | 0 | да | — | — |
| new-055 | 2026-09-25T12:24:32Z | 200 | https://raw.githubusercontent.com/mauricegift/free-proxies/master/files/countries/US.json | json | 9094 | 169 | 0 | да | — | — |
| new-056 | 2026-09-25T12:24:33Z | 200 | https://raw.githubusercontent.com/saisuiu/Lionkings-Http-Proxys-Proxies/main/free.txt | text | 4891 | 1000 | 0 | да | — | — |
| new-057 | 2026-09-25T12:24:33Z | 200 | https://api.proxynova.com/proxylist | json | 49308 | 11 | 0 | — | — | — |
| new-058 | 2026-09-25T12:24:33Z | 200 | https://spys.me/proxy.txt | text | 4497 | 400 | 0 | да | Fri, 25 Sep 2026 11:58:02 GMT | — |
| new-059 | 2026-09-25T12:24:34Z | 200 | https://rootjazz.com/proxies/proxies.txt | text | 16 | 0 | 0 | — | Wed, 07 May 2025 16:02:07 GMT | адресов не найдено |
| new-060 | 2026-09-25T12:24:34Z | 200 | https://api.openproxylist.xyz/http.txt | text | 34726 | 6235 | 0 | да | Fri, 25 Sep 2026 12:20:03 GMT | — |
| new-061 | 2026-09-25T12:24:34Z | 200 | https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt | text | 3205 | 440 | 0 | да | — | — |
| new-062 | 2026-09-25T12:24:34Z | 0 | https://www.docip.net/data/free.json | empty | 0 | 0 | 0 | — | — | curl exit 7 |
| new-063 | 2026-09-25T12:24:34Z | 200 | https://roundproxies.com/api/get-free-proxies/?limit=50&page=1&sort_by=lastChecked&sort_ty | json | 4623 | 30 | 0 | — | — | — |
| new-064 | 2026-09-25T12:24:35Z | 200 | https://proxy.scdn.io/get_proxies.php?protocol=&country=&per_page=100&page=1 | json | 4160 | 337 | 0 | — | — | — |
| new-065 | 2026-09-25T12:24:35Z | 200 | https://www.goodips.com/ | html | 5034 | 8 | 0 | — | — | — |
| new-066 | 2026-09-25T12:24:35Z | 200 | https://www.kuaidaili.com/free/inha/1/ | html | 17340 | 14 | 0 | — | — | — |
| new-067 | 2026-09-25T12:24:35Z | 200 | https://free-proxy-list.net/us-proxy.html | html | 18709 | 328 | 0 | — | Fri, 25 Sep 2026 12:22:02 GMT | — |
| new-068 | 2026-09-25T12:24:36Z | 200 | https://raw.githubusercontent.com/3proxy/3proxy/master/cfg/3proxy.cfg.sample | text | 3132 | 1 | 0 | да | — | — |
| new-069 | 2026-09-25T12:24:36Z | 200 | https://github.com/tinyproxy/tinyproxy | html | 44329 | 41 | 0 | да | — | — |
| new-070 | 2026-09-25T12:24:36Z | 200 | https://github.com/squid-cache/squid | html | 41657 | 32 | 0 | да | — | — |
| new-071 | 2026-09-25T12:24:36Z | 200 | https://github.com/XTLS/Xray-core | html | 57842 | 45 | 0 | да | — | — |
| new-072 | 2026-09-25T12:24:37Z | 200 | https://www.wireguard.com/ | html | 8131 | 6 | 0 | да | Sat, 11 Apr 2026 23:56:56 GMT | — |
| new-073 | 2026-09-25T12:24:37Z | 200 | https://check.torproject.org/exit-addresses | text | 127424 | 1272 | 0 | да | Fri, 25 Sep 2026 11:55:56 GMT | — |
| new-074 | 2026-09-25T12:24:37Z | 200 | https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.json | json | 437126 | 1128 | 0 | да | — | — |
| new-075 | 2026-09-25T12:24:38Z | 200 | https://raw.githubusercontent.com/ProxyScrape/free-proxy-list/main/proxies/all/data.json | json | 131552 | 210 | 0 | да | — | — |
| new-076 | 2026-09-25T12:24:38Z | 200 | https://raw.githubusercontent.com/relayglass/free-proxy-list/main/all.json | json | 9018 | 197 | 0 | да | — | — |
| new-077 | 2026-09-25T12:24:39Z | 200 | https://raw.githubusercontent.com/monosans/proxy-list/main/proxies_pretty.json | json | 52719 | 197 | 0 | да | — | — |
| new-078 | 2026-09-25T12:24:39Z | 200 | https://raw.githubusercontent.com/zevtyardt/proxy-list/main/output/all.json | json | 1045950 | 676 | 0 | да | — | — |
| new-079 | 2026-09-25T12:24:40Z | 200 | https://raw.githubusercontent.com/dinoz0rg/proxy-list/main/checked_proxies/http.json | json | 53972 | 1282 | 0 | да | — | — |
| new-080 | 2026-09-25T12:24:41Z | 200 | https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/proxies_ | json | 183295 | 3497 | 0 | да | — | — |
| new-081 | 2026-09-25T12:24:42Z | 200 | https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/json/proxies.json | json | 35224 | 5726 | 0 | да | — | — |
| new-082 | 2026-09-25T12:24:43Z | 0 | http://127.0.0.1:5010/all | empty | 0 | 0 | 0 | — | — | curl exit 7 |
| new-083 | 2026-09-25T12:24:43Z | 0 | https://proxy.010438.xyz/api/get?respType=json | empty | 0 | 0 | 0 | — | — | curl exit 6 |
| new-084 | 2026-09-25T12:24:43Z | 200 | https://raw.githubusercontent.com/webunblocker/free-proxy-list/main/proxies/all/data.json | json | 2724 | 104 | 0 | да | — | — |
| new-085 | 2026-09-25T12:24:44Z | 200 | https://raw.githubusercontent.com/Simatwa/free-proxies/main/files/metadata.json | json | 3358 | 40 | 0 | да | — | — |
| new-086 | 2026-09-25T12:24:45Z | 200 | https://raw.githubusercontent.com/gproxynet/free-proxy-list/main/proxies.json | json | 23 | 0 | 0 | да | — | адресов не найдено |
| new-087 | 2026-09-25T12:24:45Z | 200 | https://onionoo.torproject.org/details?type=relay&running=true&flag=Exit | json-invalid | 4194304 | 78 | 28 | — | Fri, 25 Sep 2026 12:09:10 GMT | curl exit 56 |
| new-088 | 2026-09-25T12:24:47Z | 200 | https://telegram.org/ | html | 6069 | 0 | 0 | — | — | адресов не найдено |
| new-089 | 2026-09-25T12:24:47Z | 200 | https://wiki.metacubex.one/ | html | 10079 | 0 | 0 | да | Mon, 14 Sep 2026 13:51:51 GMT | адресов не найдено |
| new-090 | 2026-09-25T12:24:47Z | 200 | https://github.com/v2fly/v2ray-core | html | 46099 | 41 | 0 | да | — | — |
| new-091 | 2026-09-25T12:24:47Z | 200 | https://sing-box.sagernet.org/ | html | 13198 | 0 | 0 | — | Thu, 24 Sep 2026 11:46:09 GMT | адресов не найдено |
| new-092 | 2026-09-25T12:24:48Z | 200 | https://github.com/tindy2013/subconverter | html | 44879 | 47 | 0 | да | — | — |
| new-093 | 2026-09-25T12:24:48Z | 200 | https://github.com/sub-store-org/Sub-Store | html | 53978 | 41 | 0 | да | — | — |
| new-094 | 2026-09-25T12:24:48Z | 200 | https://hiddify.com/ | html | 22016 | 0 | 0 | да | Fri, 27 Feb 2026 05:36:50 GMT | адресов не найдено |
| new-095 | 2026-09-25T12:24:48Z | 200 | https://v2rayn.2dust.link/ | html | 1877 | 0 | 0 | — | — | адресов не найдено |

## Чего этот журнал не доказывает

1. Он не доказывает, что какой-либо прокси из списков работает: соединения через них не устанавливались.
2. Слово «checked» в имени файла или в описании источника — заявление поставщика, не результат проверки.
3. Отсутствие ETag/Last-Modified не означает, что данные не обновляются: обновление могло идти без заголовков.
4. Единичный срез не позволяет судить о долгоживучести источника или о выживаемости адресов.
5. `has_geo_or_anon_fields` и подобные признаки показывают наличие слов в теле ответа, а не достоверность этих полей.
