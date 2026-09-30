# premium-ai-manager

Безопасный тестовый контур автономного AI-менеджера для Telegram Business.

## Сейчас готово

- webhook Telegram Business;
- ответы от имени подключённого бизнес-аккаунта;
- обязательный секрет webhook;
- allowlist тестовых Telegram chat ID;
- отсутствие токенов и персональных данных в репозитории;
- `/health` для проверки сервера.

ИИ и amoCRM подключаются после согласования провайдеров и проверки каналов клиента.

## Запуск

```bash
cp .env.example .env
pip install -r requirements.txt
uvicorn app:app --reload
```

Секреты задаются только в переменных окружения сервера.
