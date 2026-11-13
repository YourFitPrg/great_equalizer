# Обновление yfweb с 0.3.0 до 0.4.0

Обновляется только **great-equalizer.service** и её symlink `current`.
Nginx, порты, сертификат, DataLens, PostgreSQL и основной сайт не изменяются.
Текущие `/var/lib/great-equalizer` и `/etc/great-equalizer/login.json` сохраняются.
Зависимости Python совпадают с 0.3.0; apt и pip для этого обновления не нужны.

Перезапуск завершит текущие сеансы и очистит ключи из RAM. После него нужно снова
войти под своим логином (на вашем сервере — `tarle`) и подключить кабинет Ozon.

Запустите на **yfweb**:

```bash
(
set -e
GE_VERSION=0.4.0
GE_STAGE_DIR="$(mktemp -d -t ge-update.XXXXXX)"
GE_RELEASE="/opt/great_equalizer/releases/$GE_VERSION"

if sudo test -e "$GE_RELEASE"; then
  printf 'Папка релиза уже существует. Нужно проверить её содержимое.\n'
  exit 1
fi

curl -fL "https://github.com/YourFitPrg/great_equalizer/raw/refs/heads/main/exports/ozon-local-web-$GE_VERSION.zip" \
  -o "$GE_STAGE_DIR/ozon-local-web-$GE_VERSION.zip"
curl -fL "https://github.com/YourFitPrg/great_equalizer/raw/refs/heads/main/exports/ozon-local-web-$GE_VERSION.zip.sha256" \
  -o "$GE_STAGE_DIR/ozon-local-web-$GE_VERSION.zip.sha256"

(cd "$GE_STAGE_DIR"; sha256sum -c "ozon-local-web-$GE_VERSION.zip.sha256")

sudo install -d -m 0755 "$GE_RELEASE"
sudo unzip -q "$GE_STAGE_DIR/ozon-local-web-$GE_VERSION.zip" -d "$GE_RELEASE"
sudo sh "$GE_RELEASE/deploy/update-release.sh" "$GE_VERSION"
)
```

Скрипт сначала проверяет импорт новой версии. Потом останавливает только нашу
службу, переключает symlink и проверяет локальный health с версией 0.4.0.
При неуспехе возвращает прежний symlink и запускает прежнюю версию приложения.
Предыдущая папка релиза не удаляется. Работа Nginx для обновления не требуется.

Ожидаемый результат: `{"status":"ok","version":"0.4.0"}`.
Обновите страницу **https://puls-mp.ru/great_equalizer/** и проверьте версию внизу.
Для внешней проверки без ввода ключей:

```bash
curl -fsS https://puls-mp.ru/great_equalizer/health
```

В карточке кампании появятся интервал в часах, переключатель и состояние расписания.
По умолчанию оно выключено. Сохранившееся расписание выполняется только при
действующем ключевом сеансе: [правила первого этапа](docs/hourly-schedule.md).

Для ручного возврата сначала остановите нашу службу, переключите `current` на
предыдущую папку релиза и запустите нашу службу. Конфигурации Nginx при этом не трогайте.
