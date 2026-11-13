# Развёртывание Great Equalizer на yfweb

Версия **0.4.0**, адрес **https://puls-mp.ru/great_equalizer/**.
Это подготовленный пакет; команды ниже выполняются администратором на yfweb.
К серверу пользователя из среды разработки подключение не выполнялось.

## Границы изменений

* Код и venv: `/opt/great_equalizer`, отдельный пользователь `great-equalizer`.
* Данные кампаний без ключей: `/var/lib/great-equalizer`.
* Хеш пароля: `/etc/great-equalizer/login.json`, root:great-equalizer, 0640.
* Процесс: собственная служба `great-equalizer.service`, только `127.0.0.1:8765`.
* Основной сайт продолжает использовать свой `location /` и порт **8787**.
* В существующий HTTP-server добавляется одна строка `include` нашего фрагмента.
  Фрагмент обслуживает только `/great_equalizer/` и HTTP-проверку сертификата ACME.
* HTTPS: отдельный файл сайта на **443**. Другие HTTPS-пути перенаправляются на
  существующий HTTP-сайт; его исходники и приложение не меняются.
* Nginx DataLens с `/var/opt/clickhouse/datalens/nginx.conf` на **8443**,
  Telegram-бот, PostgreSQL и db-vm не изменяются.

Подпуть не является отдельным браузерным источником: если другие приложения
на `https://puls-mp.ru` исполняют недоверенный код, они разделяют тот же origin.
Выбран адрес с подпутём; это не равно изоляции отдельного поддомена.
HSTS здесь не включается: он затронул бы весь домен и текущий HTTP-сайт.

## Вход и ключи

Серверный режим требует HTTPS и хеш Argon2id; без корректной настройки входа
приложение не запускается. Локальный деморежим не используется для публикации.
Сейчас предусмотрен один административный логин. Пароль создаётся скрытым вводом
на сервере (от 16 символов), открытым текстом не сохраняется и в чат не передаётся.

Ограничения: 5 попыток входа с IP за 15 минут, общий предел 30 проверок за минуту,
не более двух проверок Argon2 одновременно; дополнительный лимит Nginx.
Cookie `__Secure-ge_session`: Secure, HttpOnly, SameSite=Strict,
Path=/great_equalizer/, без Domain. На входе токен и CSRF меняются;
API требуют вход, изменения также требуют CSRF и точный Origin.

API-ключ и Client-Id Ozon вводятся после входа, используются только в памяти
Python и при отправке запросов в Ozon. Они не записываются в настройки,
файлы, БД, cookies, localStorage/sessionStorage, URL или диагностический журнал.
Поле ключа всегда имеет type=password, кнопки показа нет. Поля подключения
очищаются после отправки/отмены. Ключ не выводится в таблицах или уведомлениях;
случайное отражение ключа в ответе API убирается до передачи в интерфейс и файлы. В памяти рабочего процесса
ключ необходим для обращения к Ozon; физическое обнуление строк Python не гарантируется.

Выход, 30 минут неактивности, 8 часов с момента входа или перезапуск требуют
повторного ввода ключа. Фоновая очистка истёкших сеансов выполняется раз в 15 секунд;
проверка срока действует также перед API-запросом. Выход блокирует дальнейшие
обращения; уже отправленный запрос Ozon может завершиться, его результат нужно
перечитать после нового входа. Отдельная служба запрещает core dump и swap для
своей cgroup; настройки остальных процессов не меняются.

Сохраняются только настройки кампаний, ожидаемые ссылки, ограничения и состояния
незавершённых операций. Расписание по интервалу 1–168 часов настраивается
в карточке кампании и работает при активном сеансе с ключом.
[Подробные правила](docs/hourly-schedule.md).

Для уже установленной версии 0.3.0: [обновление без изменений Nginx](UPDATE_SERVER.md).
Ниже — первая установка.

## 1. Предварительная проверка

```bash
sudo ss -ltnp | grep -E ':(443|8765)[[:space:]]' || true
sudo systemctl show nginx.service -p MainPID -p FragmentPath
command -v certbot || true
sudo ls -ld /etc/letsencrypt/live/puls-mp.ru 2>/dev/null || true
getent ahostsv4 puls-mp.ru
```

DNS должен указывать на публичный IP роутера, порты 80/443 — на yfweb.
MainPID nginx.service должен соответствовать основному `/usr/sbin/nginx`,
а не процессу DataLens с параметрами `-p /var/opt/clickhouse/datalens/`.
Если это не так, сначала уточните службу: сигналы другому Nginx не отправляйте.
Если порт уже занят или есть существующий HTTPS-конфиг, его нужно изучить до изменений.

## 2. Код и окружение

Устанавливайте только недостающие `python3-venv`, `unzip`, `certbot`;
общего обновления системы и плагина `certbot --nginx` не требуется.
Пример для отсутствующих пакетов:

```bash
sudo apt-get install --no-install-recommends python3-venv unzip certbot
id great-equalizer || sudo useradd --system --home-dir /var/lib/great-equalizer --no-create-home --shell /usr/sbin/nologin great-equalizer
sudo install -d -m 0755 /opt/great_equalizer/releases/0.4.0
curl -fL https://github.com/YourFitPrg/great_equalizer/raw/refs/heads/main/exports/ozon-local-web-0.4.0.zip -o /tmp/great-equalizer-0.4.0.zip
sudo unzip -q /tmp/great-equalizer-0.4.0.zip -d /opt/great_equalizer/releases/0.4.0
sudo python3 -m venv /opt/great_equalizer/venv
sudo /opt/great_equalizer/venv/bin/python -m pip install -r /opt/great_equalizer/releases/0.4.0/requirements-web.txt
sudo ln -s /opt/great_equalizer/releases/0.4.0 /opt/great_equalizer/current
```

Это первая установка: если `current` или папка релиза уже есть, не перезаписывайте
их вслепую. Для обновления используйте новую папку релиза и переключение symlink
после остановки только `great-equalizer.service`. Код и venv принадлежат root;
прав на запись в них у службы нет. Системное окружение основного сайта не используется.

## 3. Создать пароль и запустить собственную службу

```bash
sudo install -d -o root -g great-equalizer -m 0750 /etc/great-equalizer
sudo /opt/great_equalizer/venv/bin/python /opt/great_equalizer/current/configure_server.py --auth-file /etc/great-equalizer/login.json --username admin
sudo chown root:great-equalizer /etc/great-equalizer/login.json
sudo chmod 0640 /etc/great-equalizer/login.json
sudo install -m 0644 /opt/great_equalizer/current/deploy/great-equalizer.service /etc/systemd/system/great-equalizer.service
sudo systemd-analyze verify /etc/systemd/system/great-equalizer.service
sudo systemctl daemon-reload
sudo systemctl enable --now great-equalizer.service
curl -fsS http://127.0.0.1:8765/great_equalizer/health
```

Успех — JSON с версией 0.4.0. Ключи Ozon на этом этапе не нужны.
Статус/ошибки: `sudo systemctl status great-equalizer.service` и
`sudo journalctl -u great-equalizer.service -n 50 --no-pager`.
Смена пароля: остановить нашу службу, повторить configure_server.py и chown/chmod,
затем запустить службу. Существующие сеансы при этом пропадают.

## 4. HTTP и выпуск сертификата

```bash
sudo install -d -m 0755 /var/lib/great-equalizer-acme
sudo install -m 0644 /opt/great_equalizer/current/deploy/nginx-great-equalizer-http.conf /etc/nginx/snippets/great-equalizer-http.conf
GE_NGINX_HTTP="$(readlink -f /etc/nginx/sites-enabled/wb-analytics)"
sudo cp -pL "$GE_NGINX_HTTP" /etc/nginx/wb-analytics.before-great-equalizer
sudoedit "$GE_NGINX_HTTP"
```

Внутрь существующего `server { ... }`, перед его `location /`, добавить только:

```nginx
include /etc/nginx/snippets/great-equalizer-http.conf;
```

Исходный `location /`, proxy_pass 8787 и его заголовки оставить как есть.
Файл резервной копии находится вне sites-enabled и не загружается Nginx.

```bash
sudo nginx -t
sudo systemctl kill --kill-whom=main --signal=HUP nginx.service
```

Сигнал отправляется главному PID проверенной основной службы, а не через
`nginx -s reload`, который может использовать чужой pid-файл.
Если сертификата для puls-mp.ru ещё нет:

```bash
sudo certbot certonly --webroot -w /var/lib/great-equalizer-acme -d puls-mp.ru
```

Certbot запросит email и принятие условий в SSH. Команда не редактирует Nginx.
При уже существующем подходящем сертификате повторно выпускать его не нужно.

## 5. Отдельный HTTPS-конфиг

```bash
sudo install -m 0644 /opt/great_equalizer/current/deploy/nginx-great-equalizer-limits.conf /etc/nginx/conf.d/great-equalizer-limits.conf
sudo install -m 0644 /opt/great_equalizer/current/deploy/nginx-great-equalizer-proxy.conf /etc/nginx/snippets/great-equalizer-proxy.conf
sudo install -m 0644 /opt/great_equalizer/current/deploy/nginx-great-equalizer-https.conf /etc/nginx/sites-available/great-equalizer
sudo ln -s /etc/nginx/sites-available/great-equalizer /etc/nginx/sites-enabled/great-equalizer
sudo nginx -t
sudo systemctl kill --kill-whom=main --signal=HUP nginx.service
```

`conf.d/*.conf` должен подключаться в `http` основного Nginx, как в типовой Ubuntu.
Перед HUP **nginx -t обязан пройти успешно**. Никаких изменений в конфиге DataLens.
TLS-проверка: открыть **https://puls-mp.ru/great_equalizer/**, проверить сертификат,
затем войти. Вводить ключи Ozon нужно только на этой HTTPS-странице.
Основной **http://puls-mp.ru/** должен продолжать работать как до установки.

Для автоматического перечитывания нашего сертификата после продления:

```bash
sudo install -m 0755 /opt/great_equalizer/current/deploy/certbot-reload.sh /etc/letsencrypt/renewal-hooks/deploy/great-equalizer-reload
sudo systemctl list-timers --all 'certbot*'
```

## Откат

Остановить только `great-equalizer.service`, убрать ссылку
`sites-enabled/great-equalizer`, нашу строку include из HTTP-конфига и наш файл
`conf.d/great-equalizer-limits.conf`. Снова `nginx -t`, затем HUP основной службы.
Исходный location / и 8787 не менялись. Данные кампаний и хеш пароля можно сохранить.
