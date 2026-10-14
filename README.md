# Premium AI Sales Manager

Полуавтоматический отдел продаж для цветочных магазинов на базе Telegram Business.

## Как работает

1. `/find спб` выполняет три целевых запроса через Yandex Search API, убирает дубли и поднимает выше лидов с Telegram, телефоном и собственным сайтом.
2. `/draft ID` создаёт персональное первое сообщение и кнопку перехода в чат.
3. Владелец аккаунта вручную проверяет и отправляет первое сообщение.
4. Ответившего лида менеджер узнаёт по Telegram username, сохраняет переписку в SQLite и продолжает диалог через Telegram Business.
5. Тёплые и горячие лиды приходят в личный чат владельца с ботом.
6. При запросе созвона, счёта, договора или оплаты автоответы останавливаются до `/resume ID`.

Неизвестным контактам менеджер не отвечает. Массовой автоматической холодной рассылки нет.

## Админ-команды

```text
/add Название | @username | город | заметка
/find спб
/leads [cold|warm|hot]
/draft ID
/sent ID
/lead ID
/pause ID
/resume ID
```

Сначала откройте обычный чат с ботом и нажмите `/start`. Уведомления приходят туда, а не в «Избранное».

## Selectel: обновление существующего сервера

Сохраните текущие секреты и добавьте `ADMIN_CHAT_ID` — свой цифровой Telegram ID. База находится в уже подключённом Docker volume `/data`.

```bash
cd /opt/premium-ai-manager
git pull --ff-only
printf '\nADMIN_CHAT_ID=ВАШ_TELEGRAM_ID\nSALES_DB_PATH=/data/sales.db\n' >> /etc/premium-ai-manager.env
docker build -t premium-ai-manager .
docker rm -f premium-ai-manager
docker run -d --name premium-ai-manager --restart unless-stopped --env-file /etc/premium-ai-manager.env -v premium-ai-manager-data:/data --log-opt max-size=10m --log-opt max-file=3 premium-ai-manager python -u poll.py
docker logs --tail 30 premium-ai-manager
```

Не добавляйте строки повторно, если `ADMIN_CHAT_ID` и `SALES_DB_PATH` уже есть. При необходимости откройте файл командой `nano /etc/premium-ai-manager.env`.

Ожидаемая строка запуска:

```text
Polling started. Dialogue mode: yandex (credentials not yet tested)
```

## Переменные

- `TELEGRAM_BOT_TOKEN` — токен подключённого business-бота;
- `ADMIN_CHAT_ID` — единственный пользователь, которому доступны команды и уведомления;
- `ALLOWED_CHAT_IDS=*` — принимать ответы всех добавленных лидов;
- `SALES_DB_PATH=/data/sales.db` — постоянная SQLite-база;
- `YANDEX_API_KEY`, `YANDEX_MODEL_URI` — YandexGPT для диалогов;
- `YANDEX_SEARCH_FOLDER_ID` — ID каталога для Yandex Search API. Обычно извлекается из `YANDEX_MODEL_URI`, но его можно указать явно.

Токены остаются в `/etc/premium-ai-manager.env` с правами `600` и не попадают в Git.

## Поиск лидов

Используется официальный Yandex Search API из AI Studio, а не парсинг Яндекс Карт. Сервисному аккаунту ключа нужна роль `search-api.webSearch.user` и активный платёжный аккаунт. За один `/find` выполняются три синхронных запроса. Результаты содержат публичные страницы, сайты, телефоны и Telegram, но не гарантируют имя владельца.

## Локальная проверка

```bash
pip install -r requirements.txt
python -m unittest -v
```
