# Установка и подключение

## Подготовка и вход

Установите Python-зависимости. Для Linux нужна официальная `libtdjson.so` совместимой архитектуры. Скрипт сборки использует закреплённую ревизию TDLib. [README.md](../README.md#установка-с-нуля) · [Build TDLib](../.github/workflows/tdlib.yml)

На Debian или Ubuntu установите системные зависимости сборки.

```sh
sudo apt-get update
sudo apt-get install ca-certificates git cmake g++ gperf libssl-dev zlib1g-dev make
```

```sh
sh scripts/build-tdlib.sh \
  "$HOME/.cache/telegram-reader/tdlib-build" \
  "$HOME/.local/lib/telegram-reader"
```

Создайте приватную конфигурацию своей установки вне репозитория. Команда `init` один раз выведет ключи подключения. Сохраните их в менеджере паролей. Для локального stdio подойдёт HTTPS-адрес из примера. Для подключения по HTTPS укажите адрес своего сервера.

Личные заметки об установке можно хранить в папке `local/`, исключённой из Git.

```sh
umask 077
mkdir -p "$HOME/.config/telegram-reader"
export TG_READER_CONFIG="$HOME/.config/telegram-reader/config.json"
.venv/bin/python run.py init \
  --public-url https://reader.example.org \
  --state-directory "$HOME/.local/share/telegram-reader" \
  --tdjson "$HOME/.local/lib/telegram-reader/libtdjson.so"
```

Впишите собственные Telegram `api_id` и `api_hash` в созданный файл конфигурации. Получение ключей описано в [документации Telegram](https://core.telegram.org/api/obtaining_api_id). Вход выполняется интерактивно. Коды Telegram вводятся в терминале.

```sh
.venv/bin/python run.py check-config
.venv/bin/python run.py login
```

## Доступ к чатам

Владелец задаёт права через локальную CLI Reader после входа в Telegram. Владелец определяет доступ к содержимому, а настройки MCP-клиента управляют вызовом инструментов. Выданные права действуют для всех агентов, подключённых к одному экземпляру Reader.

Команда `grant-chat` разрешает чтение одного чата. Подставьте его TDLib ID, даты в формате ISO 8601 и основание доступа. `--not-before` и `--expires-at` задают и период действия разрешения, и допустимые даты сообщений. `--consent-note` фиксирует основание и границы доступа. Проверка применимых условий Telegram и согласия участников остаётся за владельцем. [Условия Telegram API](https://core.telegram.org/api/terms) · [Условия использования контента](https://telegram.org/tos/content-licensing)

```sh
.venv/bin/python run.py grant-chat \
  --chat-id="$CHAT_ID" --not-before "$NOT_BEFORE" \
  --expires-at "$EXPIRES_AT" --consent-note "$CONSENT_NOTE"
```

Для отмены разрешения используйте `revoke-chat --chat-id="$CHAT_ID"`. Чтобы закрыть доступ к содержимому всех чатов, используйте `revoke-all`.

## Подключение агента

### Локальный запуск на Linux через stdio

В MCP-клиенте добавьте локальный сервер. Укажите абсолютный путь к `.venv/bin/python` как команду, абсолютный путь к `run.py` и `stdio` как аргументы, а путь к конфигурации передайте через `TG_READER_CONFIG`.

- **Codex CLI.** Запустите команду из каталога Reader с заданной ранее переменной `TG_READER_CONFIG`.

  ```sh
  codex mcp add telegram-reader \
    --env "TG_READER_CONFIG=$TG_READER_CONFIG" \
    -- "$PWD/.venv/bin/python" "$PWD/run.py" stdio
  ```

  В настольном клиенте тот же сервер можно добавить через Settings → MCP servers. [Документация Codex по MCP](https://learn.chatgpt.com/docs/extend/mcp#connect-codex-to-an-mcp-server)
- **ZCode.** В Settings → MCP Servers нажмите New, выберите stdio и внесите команду, аргументы и переменную окружения. [Документация ZCode по MCP](https://zcode.z.ai/en/docs/mcp-services#create-an-mcp-server)

Для локальной установки Reader как плагина конфигурация запуска указана в [mcp.json](../mcp.json). После подключения вызовите `telegram_status` и проверьте состояние авторизации. Затем запросите тестовое сообщение из чата, которому вы выдали разрешение.

### Подключение к Linux-серверу через HTTPS

Используйте `run.py serve`, отдельного непривилегированного пользователя и локальный backend. Примеры службы и TLS-прокси находятся в [deploy](../deploy). Для Codex есть [пример MCP-конфигурации](../deploy/codex.toml.example). Для OAuth зарегистрируйте точный callback клиента через `add-oauth-client`. Команда `package-remote` собирает пакет подключения без секретов.

Конфигурация и сессия TDLib относятся к установленному экземпляру. При обновлении подготовьте выпуск в другом каталоге, проверьте его и переключите команду запуска, сохранив пути к конфигурации и сессии.
