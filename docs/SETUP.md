# Установка и подключение

## Подготовка

Установите Python-зависимости по [README](../README.md). Для Linux нужна официальная `libtdjson.so` совместимой архитектуры. Скрипт сборки использует закреплённую ревизию TDLib. Те же исходники собирает workflow [Build TDLib](../.github/workflows/tdlib.yml).

```sh
sh scripts/build-tdlib.sh /tmp/reader-tdlib-build /tmp/reader-tdlib-out
```

Создайте приватную конфигурацию вне репозитория. Команда `init` один раз выведет ключи подключения. Сохраните их в менеджере паролей. Для локального stdio HTTPS-адрес используется как значение конфигурации. Для подключения по HTTPS укажите адрес своего сервера.

```sh
umask 077
mkdir -p "$HOME/.config/telegram-reader"
export TG_READER_CONFIG="$HOME/.config/telegram-reader/config.json"
.venv/bin/python run.py init \
  --public-url https://reader.example.org \
  --state-directory "$HOME/.local/share/telegram-reader" \
  --tdjson /tmp/reader-tdlib-out/libtdjson.so
```

Впишите собственные Telegram `api_id` и `api_hash` в созданный файл конфигурации. Получение ключей описано в [документации Telegram](https://core.telegram.org/api/obtaining_api_id). Вход выполняется интерактивно. Коды Telegram вводятся в терминале.

```sh
.venv/bin/python run.py check-config
.venv/bin/python run.py login
```

## Доступ к чатам

Команда `grant-chat` открывает выбранный чат. `--not-before` и `--expires-at` задают допустимые даты сообщений и срок действия доступа. В `--consent-note` укажите основание и границы разрешённого доступа. Эта запись сама по себе не подтверждает право на передачу контента. Перед настройкой доступа проверьте применимость [условий Telegram API](https://core.telegram.org/api/terms) и [условий использования контента](https://telegram.org/tos/content-licensing).

```sh
.venv/bin/python run.py grant-chat --help
.venv/bin/python run.py revoke-all --help
```

## Локальный Codex

Настройте MCP-сервер типа stdio. Команда запуска — абсолютный путь к `.venv/bin/python`, аргументы — абсолютный путь к `run.py` и `stdio`. Передайте процессу `TG_READER_CONFIG` с абсолютным путём к конфигурации. Корневой [mcp.json](../mcp.json) описывает тот же запуск для установки как локального плагина в Linux. [Настройка MCP в Codex](https://learn.chatgpt.com/docs/extend/mcp#connect-codex-to-an-mcp-server).

## Подключение через HTTPS

Используйте `run.py serve`, отдельного непривилегированного пользователя и локальный backend. Примеры службы и TLS-прокси находятся в [deploy](../deploy). Для Codex есть [пример MCP-конфигурации](../deploy/codex.toml.example). Для OAuth зарегистрируйте точный callback клиента через `add-oauth-client`. Команда `package-remote` собирает пакет подключения без секретов.

Конфигурация и сессия TDLib относятся к установленному экземпляру. При обновлении подготовьте выпуск в другом каталоге, проверьте его и переключите команду запуска, сохранив пути к конфигурации и сессии.
