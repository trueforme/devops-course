# Отчёт по домашнему заданию

Linux 6.14 x86_64, storage driver `overlay2`. Docker Engine 29.4.0 (9d7ad9f),
Docker Compose v5.1.2, dive 0.13.1, git 2.43.0, uv 0.9.9 (в сборке).
Образы: python:3.14-slim, postgres:18, redis:8.8-alpine, nginx:1.30-alpine.

Приложение первой пары написано на FastAPI — продолжено оно, а не Flask.

## Сводка

| Требование задания | Раздел | Результат |
|---|---|---|
| Заметки в PostgreSQL, счётчик в Redis, Nginx на входе | 1 | 4 сервиса, `up --wait` проходит |
| Зависимости закреплены | 1.2 | `pyproject.toml` + `uv.lock`, `uv sync --frozen` |
| Установка зависимостей отделена от копирования кода | 1.3 | `COPY pyproject.toml` → `uv sync` → `COPY app.py` |
| Healthcheck БД и кеша, ожидание через `service_healthy` | 1.4 | `pg_isready`, `redis-cli ping` |
| Подтверждение: `docker compose ps` + запрос | 1.5 | все `healthy`, `curl` отвечает |
| Том по пути версии образа | 2.1 | `pgdata:/var/lib/postgresql` (postgres:18) |
| Контрольная заметка переживает замену контейнера БД | 2.3 | `02d4f84e55b0` → `1b44f40877b2`, заметка на месте |
| Описано сохраняемое и теряемое состояние | 2.4 | заметки — том; счётчик Redis — без тома |
| Холодный бэкап: команды остановки, архива, удаления, восстановления | 3 | 6.8 MB, 1280 файлов |
| Контрольная заметка после удаления тома | 3.5 | подтверждена запросом |
| Две сети, публикуется только вход | 4.1 | `front` / `back`, порт только у `web` |
| Из Nginx: нет DNS БД, нет TCP по IP | 4.2 | `имя НЕ найдено`, таймаут |
| Из приложения: доступ к БД и кешу есть | 4.3 | TCP + SQL + `PING` |
| Объяснение, почему `app` не маршрутизатор | 4.4 | нет маршрута через него + изоляция мостов |
| `.env.example` в Git, `.env` исключён | 5.1 | `git check-ignore` подтверждает |
| Итог `docker compose config` без секретов | 5.1 | пароли вырезаны |
| Имя проекта: источник, приоритет, имена ресурсов | 5.2 | `-p` > `COMPOSE_PROJECT_NAME` > `name:` > каталог |
| Различие `down` и `down -v` | 5.3 | том сохранён / удалён, данные восстановлены |
| dive до и после: размеры, метрики, вердикт | 6 | 201 MB → 153 MB, оба PASS |
| Какие слои изменились, почему поведение сохранилось | 6.2–6.3 | uv убран из рантайма, код и lock не менялись |

Задания со звёздочкой — разделы 7 и 8.

---

## 1. Базовый стек

### 1.1. Состав

| Сервис  | Образ               | Сети           | Порт на хосте |
|---------|---------------------|----------------|---------------|
| `web`   | `nginx:1.30-alpine` | `front`        | `18080→80`    |
| `app`   | локальная сборка    | `front`,`back` | —             |
| `db`    | `postgres:18`       | `back`         | —             |
| `cache` | `redis:8.8-alpine`  | `back`         | —             |

Обращение к зависимостям — по именам сервисов `db` и `cache`.

### 1.2. Закрепление зависимостей и порядок слоёв

Версии закреплены точными `==` в `pyproject.toml`, транзитивные — в `uv.lock`
(18 пакетов). В образе: `uv sync --frozen --no-dev`; `--frozen` роняет сборку,
если lock разошёлся с `pyproject.toml`.

Порядок в `Dockerfile`: `COPY pyproject.toml uv.lock` → `uv sync` →
`COPY app.py`. Правка кода инвалидирует только последний слой.

### 1.3. Healthcheck и ожидание готовности

| Сервис  | Проверка                                     | Параметры |
|---------|----------------------------------------------|-----------|
| `db`    | `pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB` | `interval 2s`, `retries 15`, `start_period 3s` |
| `cache` | `redis-cli ping`                             | `interval 2s`, `retries 15` |

`app` ждёт обе через `depends_on: condition: service_healthy`.
`$$` не даёт Compose подставить переменную при чтении YAML — её раскрывает шелл
внутри контейнера. `start_period` покрывает первичную инициализацию кластера.

### 1.5. Подтверждение готовности

```bash
$ docker compose up -d --build --wait
 Container notes-app-cache-1 Healthy
 Container notes-app-db-1 Healthy
 Container notes-app-app-1 Starting
 Container notes-app-app-1 Started
 Container notes-app-web-1 Started
```

`app` стартует только после того, как обе зависимости отметились `Healthy`.

```bash
$ docker compose ps
NAME                IMAGE               SERVICE   STATUS         PORTS
notes-app-app-1     notes-app-app       app       Up (healthy)   8000/tcp
notes-app-cache-1   redis:8.8-alpine    cache     Up (healthy)   6379/tcp
notes-app-db-1      postgres:18         db        Up (healthy)   5432/tcp
notes-app-web-1     nginx:1.30-alpine   web       Up (healthy)   0.0.0.0:18080->80/tcp
```

```bash
$ curl -fsS localhost:18080/
Docker notes app - visits: 1
$ curl -fsS --get --data-urlencode 'text=контрольная заметка' localhost:18080/notes/add
added: контрольная заметка
$ curl -fsS localhost:18080/notes
1: контрольная заметка
```

---

## 2. Том БД

### 2.1. Путь монтирования

```bash
$ docker image inspect postgres:18 --format 'Volumes={{json .Config.Volumes}}'
Volumes={"/var/lib/postgresql":{}}

$ docker compose exec db sh -c 'echo PGDATA=$PGDATA'
PGDATA=/var/lib/postgresql/18/docker
```

```yaml
db:
  volumes:
    - pgdata:/var/lib/postgresql
```

В `postgres:18` точка монтирования — `/var/lib/postgresql`, не `.../data`
(в `postgres:16` было `.../data`). Кластер лежит в подкаталоге версии.

### 2.2. Контрольная заметка

```bash
$ curl -fsS --get --data-urlencode \
    'text=КОНТРОЛЬНАЯ-2026-10-09 том переживает замену контейнера' \
    localhost:18080/notes/add
added: КОНТРОЛЬНАЯ-2026-10-09 том переживает замену контейнера
```

### 2.3. Замена контейнера БД

```bash
$ docker inspect notes-app-db-1 --format '{{.Id}}' | cut -c1-12
02d4f84e55b0

$ docker compose rm -sf db && docker compose up -d --wait db
 Container notes-app-db-1 Removed
 Container notes-app-db-1 Healthy

$ docker inspect notes-app-db-1 --format '{{.Id}}' | cut -c1-12
1b44f40877b2
```

Подтверждение сохранности — SQL в новом контейнере:

```bash
$ docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At \
    -c "SELECT id, text FROM notes ORDER BY id;"'
1|контрольная заметка
2|КОНТРОЛЬНАЯ-2026-10-09 том переживает замену контейнера
```

и через приложение:

```bash
$ curl -fsS localhost:18080/notes
1: контрольная заметка
2: КОНТРОЛЬНАЯ-2026-10-09 том переживает замену контейнера
```

### 2.4. Сохраняемое и теряемое состояние

| Состояние | Хранилище | Том | Судьба |
|---|---|---|---|
| Заметки | PostgreSQL | `pgdata` | **Сохраняется** — переживает пересоздание контейнера и `down` |
| Счётчик посещений | Redis | **нет** | **Допускается потерять** — сбрасывается при пересоздании `cache` |

Решение осознанное: `cache` описан без секции `volumes`. Счётчик — справочная
величина, его обнуление не ломает ни один сценарий и не теряет введённых
пользователем данных. Сохранение потребовало бы `appendonly yes` и отдельного
тома — цена не оправдана.

Проверка после полного `down` / `up`:

```bash
$ docker compose exec cache redis-cli GET visits
                                  # ключа нет — счётчик сброшен
$ curl -fsS localhost:18080/notes
1: контрольная заметка                 # заметки на месте
2: КОНТРОЛЬНАЯ-2026-10-09 том переживает замену контейнера
```

---

## 3. Холодный бэкап и восстановление

Копия снимается с остановленной БД: файлы работающего PostgreSQL дают
несогласованный снимок (буферы, активный WAL).

### 3.1. Остановка

```bash
$ docker compose down
 Container notes-app-web-1 Removed
 Container notes-app-app-1 Removed
 Container notes-app-cache-1 Removed
 Container notes-app-db-1 Removed
 Network notes-app_front Removed
 Network notes-app_back Removed

$ docker ps -a --filter name=notes-app --format '{{.Names}}'
                                  # пусто
$ docker volume ls --filter name=^notes-app_pgdata$ --format '{{.Name}}'
notes-app_pgdata                  # том сохранён (down без -v)
```

Останавливается и `app`: иначе он продолжал бы писать в БД.

### 3.2. Архивирование

```bash
$ mkdir -p backup
$ docker run --rm \
    -v notes-app_pgdata:/data:ro \
    -v "$PWD/backup:/backup" \
    alpine:3.24 tar czf /backup/pgdata-$(date +%Y%m%d-%H%M%S).tgz -C /data .

$ ls -lh backup/
-rw-r--r-- 1 root root 6,8M pgdata-20261009-045944.tgz

$ sha256sum backup/pgdata-20261009-045944.tgz
1a8a327cee83eb57c82b6d25552591bd11ba515fe211a3acb2a5de18436ec3da

$ tar tzf backup/pgdata-20261009-045944.tgz | wc -l
1310                              # 1280 файлов + 30 каталогов
```

Источник смонтирован `:ro`, приёмник — каталог хоста **вне тома**.
`-C /data .` даёт относительные пути.

### 3.3. Удаление тома

```bash
$ docker volume rm notes-app_pgdata
notes-app_pgdata

$ docker volume ls --filter name=^notes-app_pgdata$ --format '{{.Name}}'
                                  # пусто
```

Удалён том собственного проекта по точному имени; `prune` не применялся.

### 3.4. Восстановление

```bash
$ docker volume create notes-app_pgdata
$ docker run --rm -v notes-app_pgdata:/data -v "$PWD/backup:/backup:ro" \
    alpine:3.24 tar xzf /backup/pgdata-20261009-045944.tgz -C /data

$ docker run --rm -v notes-app_pgdata:/data:ro alpine:3.24 find /data -type f | wc -l
1280                              # совпадает с архивом
```

### 3.5. Запуск того же стека и проверка

```bash
$ grep -A1 '^  db:' compose.yaml | grep image
    image: postgres:18            # образ не менялся

$ docker compose up -d --wait
 Container notes-app-db-1 Healthy
 Container notes-app-app-1 Healthy

$ curl -fsS localhost:18080/notes
1: контрольная заметка
2: КОНТРОЛЬНАЯ-2026-10-09 том переживает замену контейнера

$ docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At \
    -c "SELECT count(*) FROM notes;"'
2
```

Контрольная заметка доступна после полного удаления тома.

---

## 4. Изоляция сетей

### 4.1. Топология и публикация

| Сеть    | Подсеть         | Участники            |
|---------|-----------------|----------------------|
| `front` | `172.21.0.0/16` | `web`, `app`         |
| `back`  | `172.22.0.0/16` | `app`, `db`, `cache` |

```bash
$ docker network inspect notes-app_back --format '{{range .Containers}}{{.Name}} {{end}}'
notes-app-app-1 notes-app-db-1 notes-app-cache-1

$ docker compose ps --format 'table {{.Service}}\t{{.Ports}}'
SERVICE   PORTS
app       8000/tcp
cache     6379/tcp
db        5432/tcp
web       0.0.0.0:18080->80/tcp, [::]:18080->80/tcp
```

`web` к `back` не подключён.

### 4.2. Из Nginx: нет ни имени, ни TCP

```bash
$ docker compose exec web sh -c 'getent hosts db || echo "db: имя НЕ найдено"'
db: имя НЕ найдено
$ docker compose exec web sh -c 'getent hosts cache || echo "cache: имя НЕ найдено"'
cache: имя НЕ найдено

$ docker compose exec web sh -c 'nc -z -w3 172.22.0.3 5432 || echo "отказ/таймаут, код $?"'
отказ/таймаут, код 1
$ docker compose exec web sh -c 'nc -z -w3 172.22.0.2 6379 || echo "отказ/таймаут, код $?"'
отказ/таймаут, код 1

$ docker compose exec web sh -c 'ip -o -4 addr show'
lo   127.0.0.1/8
eth0 172.21.0.3/16                # интерфейса в back нет
```

### 4.3. Из приложения: доступ есть

```bash
$ docker compose exec app python -c "
import socket
for h,p in (('db',5432),('cache',6379)):
    s=socket.create_connection((h,p),3); print(f'TCP {h}:{p} -> {s.getpeername()} OK'); s.close()"
TCP db:5432 -> ('172.22.0.3', 5432) OK
TCP cache:6379 -> ('172.22.0.2', 6379) OK

$ docker compose exec app python -c "... psycopg.connect(host='db') ... redis.Redis(host='cache').ping()"
PostgreSQL: 2 заметок
Redis PING: True
```

### 4.4. Почему `app` в двух сетях не становится IP-маршрутизатором

```bash
$ docker compose exec app cat /proc/sys/net/ipv4/ip_forward
1                                 # пересылка включена, дело не в ней

$ docker compose exec web sh -c 'ip route get 172.22.0.3'
172.22.0.3 via 172.21.0.1 dev eth0  src 172.21.0.3

$ ip -o -4 addr show | grep -E '172\.2[12]\.'
br-54a5dc31f036 172.21.0.1/16     # шлюз front — интерфейс хоста
br-012df8ff82d3 172.22.0.1/16     # шлюз back — интерфейс хоста
```

Адреса `app`: `172.21.0.2` во `front`, `172.22.0.4` в `back`. Пакет из `web`
уходит на `172.21.0.1` — это мост на хосте, а не `app`: маршрута через `app`
никто не прописывал, а двух интерфейсов для этого мало. Доставленный хосту
пакет дальше не идёт — Docker отбрасывает трафик между мостами правилами
`DOCKER-ISOLATION-STAGE-1/2`. `app` не передаёт чужие пакеты, а инициирует
свои: для `web` он сервер во `front`, для `db` — клиент в `back`.

---

## 5. Переменные, имя проекта, `down` против `down -v`

### 5.1. `.env.example` в Git, `.env` исключён

Переменные в `compose.yaml`: `${WEB_PORT:-18080}`, `${POSTGRES_DB}`,
`${POSTGRES_USER}`, `${POSTGRES_PASSWORD}`.

```bash
$ git ls-files --error-unmatch .env 2>/dev/null || echo "нет, исключён"
нет, исключён
$ git check-ignore -v .env
.gitignore:2:.env	.env
```

`.gitignore` исключает `.env` и `backup/` (архив с данными БД).

Итог `docker compose config`, секреты вырезаны:

```yaml
name: notes-app
services:
  app:
    environment: {POSTGRES_DB: notes, POSTGRES_USER: notes_user, POSTGRES_PASSWORD: «вырезано»}
    mem_limit: "268435456"
    networks: {back: null, front: null}
  db:
    image: postgres:18
    networks: {back: null}
    volumes: [{type: volume, source: pgdata, target: /var/lib/postgresql}]
  web:
    ports: [{target: 80, published: "18080", protocol: tcp}]
    networks: {front: null}
```

### 5.2. Имя проекта и имена ресурсов

```bash
$ docker compose config --format json | jq -r .name
notes-app
$ docker compose -p notes-app-cli config --format json | jq -r .name
notes-app-cli
$ COMPOSE_PROJECT_NAME=notes-app-env docker compose config --format json | jq -r .name
notes-app-env
$ docker compose --project-directory . -f .check/no-name.yaml config --format json | jq -r .name
docker-notes-app
$ COMPOSE_PROJECT_NAME=from-env docker compose -p from-cli config --format json | jq -r .name
from-cli
```

Приоритет: `-p` > `COMPOSE_PROJECT_NAME` > `name:` > имя каталога.

```bash
$ docker compose ps --format '{{.Name}}' && docker network ls --filter name=^notes-app_ \
    --format '{{.Name}}' && docker volume ls --filter name=^notes-app_ --format '{{.Name}}'
notes-app-app-1
notes-app-cache-1
notes-app-db-1
notes-app-web-1
notes-app_back
notes-app_front
notes-app_pgdata
```

Контейнеры — `<проект>-<сервис>-<номер>`, сети и тома — `<проект>_<имя>`.
Имя задано полем `name:`, иначе им стало бы имя каталога, и переименование
папки сменило бы имена всех ресурсов.

### 5.3. `down` против `down -v`

`down` удаляет контейнеры и сети, `down -v` — ещё и именованные тома вместе
с данными.

```bash
$ docker compose down && docker volume ls --filter name=^notes-app_pgdata$ --format '{{.Name}}'
notes-app_pgdata
$ docker compose up -d --wait && curl -fsS localhost:18080/notes
1: контрольная заметка
2: КОНТРОЛЬНАЯ-2026-10-09 том переживает замену контейнера

$ docker compose down -v
 Volume notes-app_pgdata Removed
$ docker compose up -d --wait && curl -fsS localhost:18080/notes
(no notes)

$ docker volume create notes-app_pgdata && docker run --rm \
    -v notes-app_pgdata:/data -v "$PWD/backup:/backup:ro" \
    alpine:3.24 tar xzf /backup/pgdata-20261009-045944.tgz -C /data
$ docker compose up -d --wait && curl -fsS localhost:18080/notes
1: контрольная заметка
2: КОНТРОЛЬНАЯ-2026-10-09 том переживает замену контейнера
```

---

## 6. dive до и после оптимизации

**dive 0.13.1**, пороги в `.dive-ci`: `lowestEfficiency: 0.95`,
`highestWastedBytes: 20MB`, `highestUserWastedPercent: 0.10`.

| Файл            | Образ          | Суть                                               |
|-----------------|----------------|----------------------------------------------------|
| `Dockerfile.v1` | `notes-app:v1` | одна стадия, рантайм — образ `uv`, работа от root  |
| `Dockerfile`    | `notes-app:v2` | multi-stage, рантайм `python:3.14-slim`, non-root  |

### 6.1. Размеры и метрики

```bash
$ CI=true dive notes-app:v1 --ci-config .dive-ci
$ CI=true dive notes-app:v2 --ci-config .dive-ci
```

| Образ | Размер     | efficiency | wastedBytes | userWastedPercent | Вердикт |
|-------|------------|------------|-------------|-------------------|---------|
| `v1`  | 201 MB     | 98.11 %    | 5.5 MB      | 4.35 %            | PASS    |
| `v2`  | **153 MB** | 97.43 %    | 5.7 MB      | 7.67 %            | PASS    |

Размер уменьшился на 48 MB (−24 %), оба образа проходят пороги.

PASS и минимальный размер не отождествляются: `v1` меньше по
`userWastedPercent`, потому что эта метрика — отношение, и у более крупного
образа знаменатель больше. Абсолютные потери (`wastedBytes`) у обоих
практически равны и почти целиком унаследованы от базового образа:
собственных потерь у `v2` — 6913 байт.

### 6.2. Какие слои изменились

**`uv` убран из рантайма.** В `v1` базой служил
`ghcr.io/astral-sh/uv:python3.12-bookworm-slim`, то есть инструмент установки
зависимостей ехал в финальный образ. В `v2` он копируется одним бинарём
(`COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /bin/uv`) только в стадию build;
в финальную стадию переезжает лишь готовый `/app/.venv`.

```bash
$ docker compose exec app sh -c 'id; which uv || echo "uv в рантайме отсутствует"'
uid=10001(appuser) gid=10001(appuser) groups=10001(appuser)
uv в рантайме отсутствует
```

**Сменилась база** — `python:3.12-slim-bookworm` → `python:3.14-slim`:
у первой 19 MB унаследованных потерь по dive, у второй 5.7 MB.

**Добавлен непривилегированный пользователь** `appuser` (uid 10001) вместо root.

**Разделение слоёв сохранено** в обеих версиях: зависимости ставятся до
копирования кода.

### 6.3. Почему поведение сохранилось

`app.py` и `uv.lock` между версиями идентичны, зависимости ставятся из того же
lock-файла с `--frozen`. Обе стадии `v2` стоят на одной базе `python:3.14-slim`,
поэтому интерпретатор, на который ссылается `/app/.venv`, существует в рантайме
по тому же пути.

```bash
$ docker run --rm notes-app:v2 python -c "import fastapi,psycopg,redis,uvicorn;print('imports ok',fastapi.__version__)"
imports ok 0.120.4

$ curl -fsS localhost:18080/
Docker notes app - visits: 1
$ curl -fsS localhost:18080/notes
1: контрольная заметка
2: КОНТРОЛЬНАЯ-2026-10-09 том переживает замену контейнера
```

Единственное поведенческое отличие — процесс работает от `appuser`.
Приложение слушает порт 8000 (> 1024), привилегии ему не нужны.

---

## 7. * Автоприменение кода

*Не выполнено.*

## 8. * dive в GitHub Actions

*Не выполнено.*
