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
| 6 | dive до и после оптимизации                 | готово    |
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

**Версия инструмента:** dive 0.13.1. Пороги — в `.dive-ci` (в репозитории).

### 6.1. Что сравнивается

| Файл            | Образ          | Суть                                            |
|-----------------|----------------|-------------------------------------------------|
| `Dockerfile.v1` | `notes-app:v1` | Исходный: одна стадия, образ `uv` как рантайм   |
| `Dockerfile`    | `notes-app:v2` | Multi-stage, рантайм `python:3.14-slim`, non-root |

### 6.2. Итоговые метрики

```bash
CI=true dive notes-app:v1 --ci-config .dive-ci
CI=true dive notes-app:v2 --ci-config .dive-ci
```

| Образ | Размер | efficiency | wastedBytes | userWastedPercent | Вердикт |
|-------|--------|------------|-------------|-------------------|---------|
| `v1`  | 201 MB | 98.11 %    | 5.5 MB      | 4.35 %            | PASS    |
| `v2`  | **153 MB** | 97.43 % | 5.7 MB      | 7.67 %            | PASS    |

Размер уменьшился на **48 MB (−24 %)**, обе версии проходят пороги.

### 6.3. Какие слои изменились и почему

**Убран `uv` из рантайма.** В `v1` базовым образом служил
`ghcr.io/astral-sh/uv:python3.12-bookworm-slim` — то есть инструмент установки
зависимостей ехал в продакшен. В `v2` uv копируется одним бинарём
(`COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /bin/uv`) **только в стадию build**;
финальная стадия стоит на чистом `python:3.14-slim`.

Проверка в работающем контейнере:

```bash
$ docker compose exec app sh -c 'id; which uv || echo "uv в рантайме отсутствует"'
uid=10001(appuser) gid=10001(appuser) groups=10001(appuser)
uv в рантайме отсутствует
```

**Сменилась база: `python:3.12-slim-bookworm` → `python:3.14-slim` (trixie).**
Это не косметика, обоснование — в 6.4.

**Добавлен непривилегированный пользователь.** `v1` работал от root, `v2` —
от `appuser` (uid 10001), что видно в выводе `id` выше.

**Разделение слоёв сохранено в обеих версиях:** `COPY pyproject.toml uv.lock` →
`uv sync --frozen` → `COPY app.py`. Правка кода не инвалидирует слой зависимостей.

### 6.4. Промежуточный результат: первая попытка оптимизации провалила пороги

Первая версия `v2` собиралась на `python:3.12-slim-bookworm` и дала:

| Образ | Размер | wastedBytes | userWastedPercent | Вердикт |
|-------|--------|-------------|-------------------|---------|
| `v1`  | 201 MB | 5.5 MB      | 4.35 %            | PASS    |
| `v2` (bookworm) | 163 MB | **19 MB** | **22.15 %** | **FAIL** (2 из 3) |

Образ стал меньше — и **перестал** проходить проверку. Разбор:

```
Inefficient Files (v2 на bookworm):
Count  Wasted Space  File Path
    2        7.6 MB  /usr/bin/perl
    3        2.3 MB  /var/cache/debconf/templates.dat
    2        1.3 MB  /usr/lib/x86_64-linux-gnu/perl-base/auto/re/re.so
```

Ни один из этих файлов не упомянут в моём Dockerfile. Проверка базового образа:

```bash
$ CI=true dive python:3.12-slim-bookworm --ci-config .dive-ci
  wastedBytes: 19412027 bytes (19 MB)
  userWastedPercent: 34.6067 %
Result:FAIL [Total:3] [Passed:1] [Failed:2]
```

`v2` wasted = `19418940`, база = `19412027`. Разница — **6913 байт**: собственные
слои не насорили практически ничем, все 19 MB унаследованы от базы, которая сама
не проходит пороги.

**Исправление было в выборе базы, а не в подкручивании порогов.** `python:3.14-slim`
собран на Debian trixie и несёт 5.65 MB потерь вместо 19.4 MB. После перехода:
153 MB и PASS по всем трём правилам. Пороги `.dive-ci` не менялись.

### 6.5. Почему `userWastedPercent` у `v2` хуже, чем у `v1`

`v1` — 4.35 %, `v2` — 7.67 %, при том что `v2` меньше на 48 MB и чище. Это не
противоречие, а свойство метрики:

```
userWastedPercent = wastedBytes / (размер образа − размер нижнего слоя)
```

«User» здесь означает «всё, кроме нулевого слоя», а не «слои моего Dockerfile».
Мусор базового образа всегда попадает и в числитель, и в знаменатель.

Абсолютные потери у обеих версий почти равны (5.5 и 5.7 MB) и почти целиком
унаследованы: у `v2` собственных потерь `5658564 − 5651651 = 6913` байт.
Но знаменатель у `v1` больше — он таскает лишние 48 MB, — поэтому та же величина
мусора даёт меньший процент.

**Вывод: метрика-отношение улучшается от раздувания знаменателя.** Поэтому
`userWastedPercent` нельзя читать в отрыве от `wastedBytes` и размера образа.
Это прямая иллюстрация требования задания **не приравнивать PASS к минимальному
размеру**; здесь видно и обратное — PASS не равен качеству.

### 6.6. Почему поведение приложения сохранилось

Менялась только упаковка, не код: `app.py` и `uv.lock` между `v1` и `v2` идентичны,
зависимости ставятся из того же lock-файла с `--frozen`.

Обе стадии `v2` стоят на одной базе `python:3.14-slim`, поэтому интерпретатор,
на который ссылается `/app/.venv`, существует в рантайме по тому же пути —
виртуальное окружение переносится между стадиями корректно.

Проверка импортов в финальном образе:

```bash
$ docker run --rm notes-app:v2 python -c "import fastapi,psycopg,redis,uvicorn;print('imports ok',fastapi.__version__)"
imports ok 0.120.4
```

Функциональная проверка через поднятый стек:

```bash
$ curl -fsS localhost:18080/
Docker notes app - visits: 1
$ curl -fsS localhost:18080/notes
1: контрольная заметка
```

Единственное поведенческое отличие — процесс работает от `appuser`, а не от root.
Приложение слушает порт 8000 (> 1024), привилегии ему не нужны.

### 6.7. Побочная находка: гонка на старте, пойманная при проверке

При первом запуске стека с новым образом первый запрос вернул `502`:

```
curl: (22) The requested URL returned error: 502
```

Причина не в образе: `web` зависел от `app` через `condition: service_started`,
то есть Nginx начинал принимать соединения раньше, чем uvicorn открывал сокет.

Исправлено добавлением healthcheck приложению (в образе нет curl и wget,
поэтому проверка выполняется штатным интерпретатором):

```yaml
app:
  healthcheck:
    test: ["CMD", "python", "-c",
           "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz').read()"]
    interval: 2s
    timeout: 2s
    retries: 15
    start_period: 3s

web:
  depends_on:
    app:
      condition: service_healthy
```

После исправления первый же запрос без `--retry` отдаёт 200:

```bash
$ docker compose up -d --wait
$ curl -fsS localhost:18080/
Docker notes app - visits: 1
$ docker compose ps --format 'table {{.Service}}\t{{.Status}}'
SERVICE   STATUS
app       Up 6 seconds (healthy)
cache     Up 12 seconds (healthy)
db        Up 12 seconds (healthy)
web       Up Less than a second
```

### 6.8. Попутное подтверждение разделения состояния

После `docker compose down` и повторного `up` (том `pgdata` не удалялся):

```bash
$ curl -fsS localhost:18080/        # счётчик: было 3
Docker notes app - visits: 1        # сброшен — Redis без тома
$ curl -fsS localhost:18080/notes
1: контрольная заметка              # заметка на месте — PostgreSQL с томом
```

Заметки пережили пересоздание контейнеров, счётчик обнулился. Это ожидаемое
и сознательно выбранное поведение, подробнее — в разделе 2.

## 7. * Автоприменение кода

*Не начато.*

## 8. * dive в GitHub Actions

*Не начато.*
