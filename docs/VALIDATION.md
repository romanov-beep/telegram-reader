# Проверки

После установки закреплённых зависимостей из [requirements.txt](../requirements.txt) запустите из корня репозитория:

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python run.py --help
sh -n scripts/build-tdlib.sh
```

Unit-тесты используют тестовые данные и подмены TDLib. Они проверяют политику доступа, MCP-инструменты, чтение фотографий и форму ответов. Для проверки установленного экземпляра вызовите `telegram_status`, затем сделайте пробный запрос к разрешённому контрольному чату.

## Архив исходников

Из клона Reader запустите `python3 scripts/package-source.py /tmp/telegram-reader.zip`. В архив попадёт дерево текущего коммита, включая лицензию и документы.
