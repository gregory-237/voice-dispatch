# REST API и CRM

Интерактивная схема доступна по `/docs`, OpenAPI JSON — по `/openapi.json`. Базовый путь `/api/v1`. Серверные интеграции передают `Authorization: Bearer <API_TOKEN>`. Времена передаются в ISO 8601. Входящий webhook использует подпись HMAC.

| Метод и путь | Назначение |
| --- | --- |
| POST /tickets | Регистрация. Обязателен заголовок Idempotency-Key |
| GET /tickets?after=0&limit=50&status=new | Список по возрастанию id. Для следующей страницы укажите последний id в after |
| GET /tickets/{id} | Поля заявки и хронология |
| PATCH /tickets/{id} | Передайте version и изменяемые поля status, priority, assignee |
| POST /tickets/{id}/callback | Тело с due, например `{"due":"2027-01-10T12:00:00+04:00"}`. Нужна выполненная заявка и согласие жителя |
| POST /webhooks/intake | Создание заявки из подписанного события CRM |

```sh
curl -X POST http://localhost:8080/api/v1/tickets \
  -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: crm-request-502' \
  -d '{"address":"ул. Садовая, 18","apartment":"7","name":"Житель","category":"water","description":"Течёт кран в подвале","phone":"+70000000001","consent":false}'
```

Ключ повторной регистрации относится к источнику, максимальная длина — 120 символов. Повтор идентичного запроса вернёт существующий id. Другие данные с тем же ключом дают HTTP 409. Несовместимый переход статуса или устаревшая версия карточки также дают 409.

Категории: `water`, `heat`, `power`, `lift`, `common`, `other`. Приоритеты: `normal`, `high`, `emergency`. Обычный маршрут заявки: `new → assigned → working → done → closed`. Из новой заявки возможен переход сразу в работу, из выполненной и закрытой — возврат в работу.

## Подпись сообщений

Подпись — hex HMAC-SHA256 с ключом WEBHOOK_SECRET от ASCII timestamp, точки и исходных байтов JSON. Заголовки: `X-VD-Time` (Unix seconds), `X-VD-Signature`, `Idempotency-Key`. Для входящего события окно допустимого расхождения часов — 300 секунд. Поле id внутри подписанного тела должно совпадать с ключом.

```python
import hashlib, hmac, json, os, time
import httpx

event = {"id": "crm-502", "ticket": {"address": "ул. Мира, 7", "apartment": "5",
         "name": "Житель", "phone": "+70000000002", "category": "power",
         "description": "Нет света в подъезде", "consent": False}}
body = json.dumps(event, ensure_ascii=False).encode()
stamp = str(int(time.time()))
sig = hmac.new(os.environ["WEBHOOK_SECRET"].encode(), stamp.encode()+b"."+body, hashlib.sha256).hexdigest()
r = httpx.post("http://localhost:8080/api/v1/webhooks/intake", content=body,
               headers={"Content-Type":"application/json", "X-VD-Time":stamp,
                        "X-VD-Signature":sig, "Idempotency-Key":event["id"]})
r.raise_for_status()
```

Для отправки событий из VoiceDispatch задайте WEBHOOK_URL и перезапустите приложение. Изменение заявки, запись истории и запись в исходящую очередь сохраняются в одной транзакции. Получатель получает `{"id":"…","event":"ticket.changed","ticket":{…}}`. При повторной доставке id остаётся прежним. Получатель проверяет подпись, сохраняет обработанный id и отвечает кодом 2xx. Очередь делает до восьми попыток с растущей задержкой. Состояние доставки хранится в таблице deliveries.
