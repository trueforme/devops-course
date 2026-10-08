# Отчёт по домашнему заданию

**Стенд:** Linux 6.14, x86_64
**Инструменты:** Docker Engine 29.4.0 (build 9d7ad9f), Docker Compose v5.1.2,
storage driver `overlay2`, dive 0.13.1, git 2.43.0, uv (локально, для генерации `uv.lock`).

**Оговорка по формулировке задания.** ДЗ говорит «продолжите Flask-приложение первой
пары». Приложение первой пары у меня написано на **FastAPI**, поэтому «продолжение»
понято буквально: взят существующий код и расширен. Ни одно требование задания
(healthcheck, зависимости по готовности, тома, сети, переменные, dive) от выбора
веб-фреймворка не зависит.

**Статус разделов.** Заполняется по ходу работы.

| № | Раздел                                     | Статус    |
|---|--------------------------------------------|-----------|
| 1 | Базовый стек, healthcheck, порядок запуска  | готово    |
| 2 | Том БД и сохранность данных                 | не начато |
| 3 | Холодный бэкап и восстановление             | не начато |
| 4 | Изоляция сетей                              | не начато |
| 5 | Переменные, `.env`, имя проекта, `down -v`  | не начато |
| 6 | dive до и после оптимизации                 | не начато |
| 7 | * Автоприменение кода (`override` / `watch`)| не начато |
| 8 | * dive в GitHub Actions                     | не начато |

---

## 1. Базовый стек, healthcheck и порядок запуска

### 1.1. Состав

Четыре сервиса в `compose.yaml`:

| Сервис  | Образ               | Сети           | Публикация |
|---------|---------------------|----------------|------------|
| `web`   | `nginx:1.30-alpine` | `front`        | `18080→80` |
| `app`   | локальная сборка    | `front`,`back` | нет        |
| `db`    | `postgres:18`       | `back`         | нет        |
| `cache` | `redis:8.8-alpine`  | `back`         | нет        |

Приложение обращается к зависимостям по именам сервисов — `db` и `cache`.
Встроенный DNS Compose резолвит их внутри общей сети, адреса нигде не зашиты.

### 1.2. Закрепление зависимостей

Версии зафиксированы точными `==` в `pyproject.toml` и дополнительно сведены
в `uv.lock` (18 пакетов вместе с транзитивными):

```toml
dependencies = [
    "fastapi==0.120.4",
    "uvicorn==0.38.0",
    "psycopg[binary]==3.2.12",
    "redis==6.4.0",
]
```

В образе ставится строго из lock-файла:

```dockerfile
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev
```

`--frozen` запрещает uv пересчитывать резолюцию: если `uv.lock` разошёлся
с `pyproject.toml`, сборка падает, а не тихо ставит другие версии.
`--no-dev` отсекает dev-зависимости.

### 1.3. Отделение зависимостей от кода

```dockerfile
COPY pyproject.toml uv.lock ./   # ← меняются редко
RUN ... uv sync --frozen --no-dev
...
COPY --chown=appuser:appuser app.py .   # ← меняется часто
```

Код копируется **после** установки зависимостей. Правка `app.py` инвалидирует
только последний слой; слой с библиотеками берётся из кеша.

### 1.4. Healthcheck

Обе зависимости сообщают о готовности сами:

```yaml
db:
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB"]
    interval: 2s
    timeout: 2s
    retries: 15
    start_period: 3s

cache:
  healthcheck:
    test: ["CMD", "redis-cli", "ping"]
    interval: 2s
    timeout: 2s
    retries: 15
```

Двойной `$$` в `CMD-SHELL` — экранирование для Compose: он не должен подставлять
переменную на этапе чтения YAML, её обязан раскрыть шелл **внутри** контейнера `db`,
где `POSTGRES_USER` уже определён.

`start_period: 3s` нужен, потому что официальный образ postgres при первом запуске
инициализирует кластер и успевает на короткое время поднять временный сервер.
Без `start_period` неудачные попытки в этом окне засчитывались бы в `retries`.

### 1.5. Ожидание готовности

```yaml
app:
  depends_on:
    db:
      condition: service_healthy
      required: true
    cache:
      condition: service_healthy
      required: true
```

Это ключевой момент: `service_started` означал бы «процесс запущен», чего
недостаточно — приложение при старте создаёт таблицу `notes` и упало бы на
неготовой базе. `service_healthy` ждёт именно прохождения healthcheck.

Создание таблицы вынесено в `lifespan` FastAPI:

```python
@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_table()
    yield
```

### 1.6. Подтверждение готовности

```bash
$ docker compose up -d --build --wait
 Container notes-app-db-1 Waiting
 Container notes-app-cache-1 Healthy
 Container notes-app-db-1 Healthy
 Container notes-app-app-1 Starting
 Container notes-app-app-1 Started
 Container notes-app-web-1 Started
```

Порядок в выводе — прямое доказательство работы `depends_on`: `app` переходит
в `Starting` только после того, как **обе** зависимости отметились как `Healthy`.

```bash
$ docker compose ps
NAME                IMAGE               SERVICE   STATUS                   PORTS
notes-app-app-1     notes-app-app       app       Up (healthy)             8000/tcp
notes-app-cache-1   redis:8.8-alpine    cache     Up (healthy)             6379/tcp
notes-app-db-1      postgres:18         db        Up (healthy)             5432/tcp
notes-app-web-1     nginx:1.30-alpine   web       Up (healthy)             0.0.0.0:18080->80/tcp, [::]:18080->80/tcp
```

Порт на хосте есть только у `web`. У `app`, `db` и `cache` в колонке `PORTS`
стоит внутренний порт без маппинга — снаружи они недоступны.

### 1.7. Запрос к приложению

```bash
$ curl -fsS localhost:18080/
Docker notes app - visits: 1
$ curl -fsS localhost:18080/
Docker notes app - visits: 2

$ curl -fsS --get --data-urlencode 'text=контрольная заметка' localhost:18080/notes/add
added: контрольная заметка
$ curl -fsS localhost:18080/notes
1: контрольная заметка

$ curl -s -o /dev/null -w '%{http_code}\n' --get --data-urlencode 'text= ' localhost:18080/notes/add
400
```

Счётчик растёт (Redis отвечает), заметка записалась и читается (PostgreSQL отвечает),
валидация пустого значения работает. Весь трафик шёл через Nginx на порт `18080`.

### 1.8. Путь тома БД

```bash
$ docker compose exec db sh -c 'echo PGDATA=$PGDATA; ls /var/lib/postgresql'
PGDATA=/var/lib/postgresql/18/docker
18
```

Том подключён к `/var/lib/postgresql`, а не к `/var/lib/postgresql/data`.
Это особенность `postgres:18`; разбор — в разделе 2.

---

## 2. Том БД и сохранность данных

*Не начато.*

## 3. Холодный бэкап и восстановление

*Не начато.*

## 4. Изоляция сетей

*Не начато.*

## 5. Переменные, `.env`, имя проекта, `down` против `down -v`

*Не начато.*

## 6. dive до и после оптимизации

*Не начато.*

## 7. * Автоприменение кода

*Не начато.*

## 8. * dive в GitHub Actions

*Не начато.*
