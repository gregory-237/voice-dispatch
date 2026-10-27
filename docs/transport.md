# Работа с симулятором

Подготовьте короткую русскую реплику в WAV PCM16 mono 8000 Гц. Приложение и модели должны быть запущены. Симулятор присваивает каждому соединению UUID, посылает речь 20-миллисекундными кадрами и параллельно принимает ответ сервера.

```sh
python scripts/simulate.py resident.wav
python scripts/simulate.py resident.wav --load
```

Второй режим последовательно открывает 1, 4, 8 и 12 одновременных сессий. Каждая строка содержит UUID, время транспорта и объём принятого PCM. Используйте UUID для связи с реальными замерами в таблице metrics. События asr и tts измеряют длительность вызова движка с ожиданием очереди, llm — время до полного валидного результата, tts_cache — получение готовой реплики. На время влияют модель, кэш и доступные ресурсы.

```sql
SELECT call_id, stage, count(*), round(avg(ms)::numeric,1) AS avg_ms,
       round(percentile_cont(0.95) WITHIN GROUP (ORDER BY ms)::numeric,1) AS p95_ms
FROM metrics
WHERE created >= (now() AT TIME ZONE 'UTC') - interval '1 day'
GROUP BY call_id, stage ORDER BY call_id, stage;
```

Журнал событий и таблица calls позволяют отличить завершение абонентом, передачу диспетчеру и ошибку движка. WAV можно открыть в карточке звонка.
