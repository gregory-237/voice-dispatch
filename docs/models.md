# Локальные модели

Подготовку выполняют один раз на узле с доступом к источникам моделей. Для изолированного контура перенесите каталог `models`, Docker-образы и том Ollama по принятой процедуре. Приложение использует локальные пути. Адрес `OLLAMA_URL` должен указывать на ваш сервис.

```sh
python scripts/models.py --asr small
docker compose up -d ollama
docker compose exec ollama ollama pull qwen2.5:3b
```

| Компонент | Источник и назначение |
| --- | --- |
| Silero VAD | ONNX-веса в пакете silero-vad. Состояние и контекст независимы для каждого звонка |
| faster-whisper | Каталог `models/asr` в формате CTranslate2. Допустимы multilingual tiny, base, small и medium. Установка выбирается через `--asr` |
| Qwen2.5 | Установите нужный тег в Ollama и выберите точное имя в панели. Для малого стенда доступен `qwen2.5:0.5b` |
| Silero TTS | Русский пакет v4_ru по пути `models/silero.pt`. Голоса включают xenia, kseniya, baya, aidar, eugene |

Перезапустите приложение после замены весов на диске и очистите прежние ключи синтеза при изменении версии голоса. Загружайте torch-пакеты из доверенного источника. Параметры потоков CPU и пакет распознавания находятся в `dispatch/models.py`. В разделе «Мониторинг» доступность весов отражает наличие файлов, для Ollama проверяется установленный тег.

[Silero Models](https://github.com/snakers4/silero-models) · [Silero VAD](https://github.com/snakers4/silero-vad) · [faster-whisper](https://github.com/SYSTRAN/faster-whisper) · [Ollama Chat API](https://docs.ollama.com/api/chat)
