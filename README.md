# Unofficial Telegram Reader

Telegram Reader даёт Codex и ChatGPT доступ на чтение к разрешённым облачным чатам личного Telegram-аккаунта. По запросу он показывает папки, ищет чаты и сообщения, читает историю и получает фотографии. Секретные чаты, защищённое и исчезающее содержимое исключены.

Каждый экземпляр рассчитан на одного владельца и одну сессию Telegram. Reader обращается к API Telegram через TDLib и предоставляет операции чтения как MCP-инструменты. Владелец входит в аккаунт и задаёт доступ к чатам через локальную CLI. При первом запуске доступ к сообщениям закрыт.

## Запуск

Python 3.11+ и официальная библиотека TDLib. Для локального Codex на Linux доступен MCP через stdio. Для подключения к экземпляру на своём сервере доступен MCP через HTTPS.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py --help
```

- [Установка и подключение](docs/SETUP.md)
- [Архитектура и ограничения](docs/ARCHITECTURE.md)
- [Контракт TDLib](docs/TDLIB.md)
- [Проверки](docs/VALIDATION.md)
- [Задачи](TODO.md)

## Лицензия

Исходники доступны по [PolyForm Noncommercial License 1.0.0](LICENSE). Для коммерческого использования требуется отдельное письменное разрешение [правообладателя](https://github.com/romanov-beep).
