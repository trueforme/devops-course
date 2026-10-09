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
| 2 | Том БД и сохранность данных                 | готово    |
| 3 | Холодный бэкап и восстановление             | готово    |
| 4 | Изоляция сетей                              | готово    |
| 5 | Переменные, `.env`, имя проекта, `down -v`  | готово    |
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

### 2.1. Путь монтирования соответствует версии образа

```yaml
db:
  image: postgres:18
  volumes:
    - pgdata:/var/lib/postgresql
```

Том подключён к `/var/lib/postgresql`, **а не к `/var/lib/postgresql/data`**.
Это требование конкретной версии образа:

```bash
$ docker image inspect postgres:18 --format 'Volumes={{json .Config.Volumes}}'
Volumes={"/var/lib/postgresql":{}}

$ docker compose exec db sh -c 'echo PGDATA=$PGDATA; ls /var/lib/postgresql'
PGDATA=/var/lib/postgresql/18/docker
18
```

Сравнение с предыдущей мажорной версией:

| Образ                | `VOLUME` в образе            |
|----------------------|------------------------------|
| `postgres:16-alpine` | `/var/lib/postgresql/data`   |
| `postgres:18`        | `/var/lib/postgresql`        |

В 18-й версии точка монтирования переехала: том подключается на уровень выше,
а кластер лежит в подкаталоге с номером мажорной версии. Сделано ради
`pg_upgrade --link` — обе версии должны находиться в одной файловой системе,
иначе жёсткие ссылки между ними невозможны.

Проверено, что ошибиться незаметно нельзя: при монтировании `postgres:18`
по старому пути контейнер **не стартует**, а падает с кодом 1 даже на пустом томе:

```text
Error: in 18+, these Docker images are configured to store database data in a
       format which is compatible with "pg_ctlcluster" ...
       Counter to that, there appears to be PostgreSQL data in:
         /var/lib/postgresql/data (unused mount/volume)
       The suggested container configuration for 18+ is to place a single mount
       at /var/lib/postgresql ...
```

### 2.2. Контрольная заметка

```bash
$ curl -fsS --get --data-urlencode \
    'text=КОНТРОЛЬНАЯ-2026-10-09 том переживает замену контейнера' \
    localhost:18080/notes/add
added: КОНТРОЛЬНАЯ-2026-10-09 том переживает замену контейнера
```

### 2.3. Замена контейнера БД с прежним томом

```bash
$ docker inspect notes-app-db-1 --format '{{.Id}}' | cut -c1-12
02d4f84e55b0

$ docker compose rm -sf db        # останов и удаление ТОЛЬКО контейнера db
 Container notes-app-db-1 Removed

$ docker compose up -d --wait db  # новый контейнер, том тот же
 Container notes-app-db-1 Healthy

$ docker inspect notes-app-db-1 --format '{{.Id}}' | cut -c1-12
1b44f40877b2
```

Идентификатор изменился — это другой контейнер. `docker compose rm -sf` удаляет
контейнер, но не именованный том: том объявлен на уровне проекта, а не контейнера.

### 2.4. Подтверждение сохранности — оба способа

**SQL напрямую в новом контейнере:**

```bash
$ docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At \
    -c "SELECT id, text FROM notes ORDER BY id;"'
1|контрольная заметка
2|КОНТРОЛЬНАЯ-2026-10-09 том переживает замену контейнера
```

**Через приложение:**

```bash
$ curl -fsS localhost:18080/notes
1: контрольная заметка
2: КОНТРОЛЬНАЯ-2026-10-09 том переживает замену контейнера
```

### 2.5. Какое состояние сохраняется, а какое допускается потерять

Задание требует описать это явно.

| Состояние | Где | Том | Судьба |
|-----------|-----|-----|--------|
| **Заметки** | PostgreSQL | `pgdata` | **Сохраняется.** Переживает пересоздание контейнера, `docker compose down`, перезагрузку хоста. Потеря недопустима — это единственные пользовательские данные. |
| **Счётчик посещений** | Redis | **нет** | **Допускается потерять.** Сбрасывается при любом пересоздании контейнера `cache`. |

Решение по Redis осознанное, а не упущение: `cache` описан в `compose.yaml`
без секции `volumes`, с комментарием в файле. Счётчик — справочная величина,
его обнуление не ломает ни одного сценария работы приложения и не приводит
к потере того, что пользователь вводил руками.

Подтверждение поведения на практике — после полного `down` / `up`:

```bash
$ docker compose exec cache redis-cli GET visits
(ключа нет)                     # счётчик сброшен

$ curl -fsS localhost:18080/notes
1: контрольная заметка          # заметки на месте
2: КОНТРОЛЬНАЯ-2026-10-09 том переживает замену контейнера
```

Если бы счётчик потребовалось сохранять, Redis нужно было бы запускать
с `appendonly yes` и отдельным томом. Для этой задачи такая цена не оправдана.

---

## 3. Холодный бэкап и восстановление

«Холодный» означает, что копия снимается с **остановленной** БД. Копирование
файлов работающего PostgreSQL даёт несогласованный снимок: часть страниц ещё
в буферах, WAL активен, контрольная точка не завершена. Такой архив может
не восстановиться или восстановиться с повреждениями.

### 3.1. Остановка всех сервисов, которые могут писать в БД

```bash
$ docker compose down
 Container notes-app-web-1 Removed
 Container notes-app-app-1 Removed
 Container notes-app-cache-1 Removed
 Container notes-app-db-1 Removed
 Network notes-app_front Removed
 Network notes-app_back Removed
```

`down` **без `-v`** — удаляет контейнеры и сети, том сохраняет.

Останавливается не только `db`, но и `app`: иначе он продолжал бы открывать
соединения и писать заметки в момент снятия копии.

Проверка:

```bash
$ docker ps -a --filter name=notes-app --format '{{.Names}}'
                                       # пусто — никто не пишет в БД
$ docker volume ls --filter name=^notes-app_pgdata$ --format '{{.Name}}'
notes-app_pgdata                       # том на месте
```

### 3.2. Архивирование через временный контейнер

```bash
$ mkdir -p backup
$ docker run --rm \
    -v notes-app_pgdata:/data:ro \
    -v "$PWD/backup:/backup" \
    alpine:3.24 tar czf /backup/pgdata-$(date +%Y%m%d-%H%M%S).tgz -C /data .
```

Разбор команды:

- **`--rm`** — контейнер нужен только как «руки», он не часть стека.
- **`-v notes-app_pgdata:/data:ro`** — источник, смонтирован **только на чтение**:
  гарантия, что процедура снятия копии не изменит данные.
- **`-v "$PWD/backup:/backup"`** — приёмник, каталог на хосте. **Архив создаётся
  вне исходного тома** — иначе его удаление унесло бы и копию.
- **`-C /data .`** — относительные пути внутри архива (`./18/docker/...`),
  благодаря чему он распаковывается в любой целевой каталог.

Контейнер нужен потому, что `/var/lib/docker/volumes` доступен только root,
а внутри тома лежат файлы с uid пользователя `postgres`.

Результат:

```bash
$ ls -lh backup/
-rw-r--r-- 1 root root 6,8M pgdata-20261009-045944.tgz

$ sha256sum backup/pgdata-20261009-045944.tgz
1a8a327cee83eb57c82b6d25552591bd11ba515fe211a3acb2a5de18436ec3da

$ tar tzf backup/pgdata-20261009-045944.tgz | wc -l
1310                                  # = 1280 файлов + 30 каталогов

$ tar tzf backup/pgdata-20261009-045944.tgz | grep -E 'PG_VERSION|base/$' | head -3
./18/docker/PG_VERSION
./18/docker/base/
./18/docker/base/4/PG_VERSION
```

Архив принадлежит root — он создан процессом контейнера, работавшим от root
(через границу контейнера передаётся числовой uid, см. `notes/02-storage.md`).

### 3.3. Удаление тома

Выполняется **только после** того, как архив создан и его состав проверен.

```bash
$ docker volume rm notes-app_pgdata
notes-app_pgdata

$ docker volume ls --filter name=^notes-app_pgdata$ --format '{{.Name}}'
                                       # пусто — тома нет
```

Удалён именно том собственного проекта, по точному имени. `docker volume prune`
не применялся: фильтр по метке не гарантирует, что под него не попадут
посторонние тома.

### 3.4. Восстановление

```bash
$ docker volume create notes-app_pgdata
notes-app_pgdata

$ docker run --rm -v notes-app_pgdata:/data alpine:3.24 sh -c 'ls -A /data | wc -l'
0                                      # том пуст

$ docker run --rm \
    -v notes-app_pgdata:/data \
    -v "$PWD/backup:/backup:ro" \
    alpine:3.24 tar xzf /backup/pgdata-20261009-045944.tgz -C /data
```

Приёмник теперь смонтирован на запись, а каталог с архивом — `:ro`.

```bash
$ docker run --rm -v notes-app_pgdata:/data:ro alpine:3.24 \
    sh -c 'ls /data; ls /data/18; find /data -type f | wc -l'
18
docker
1280                                   # совпадает с числом файлов в архиве
```

### 3.5. Запуск того же стека с тем же образом БД

```bash
$ grep -A1 '^  db:' compose.yaml | grep image
    image: postgres:18                 # образ не менялся

$ docker compose up -d --wait
 Container notes-app-cache-1 Healthy
 Container notes-app-db-1 Healthy
 Container notes-app-app-1 Healthy
 Container notes-app-web-1 Healthy

$ docker compose ps --format 'table {{.Service}}\t{{.Status}}'
SERVICE   STATUS
app       Up 6 seconds (healthy)
cache     Up 12 seconds (healthy)
db        Up 12 seconds (healthy)
web       Up Less than a second
```

База приняла восстановленный каталог и прошла healthcheck — значит, снимок
был согласованным.

### 3.6. Ключевой вывод проверки

**Через приложение:**

```bash
$ curl -fsS localhost:18080/notes
1: контрольная заметка
2: КОНТРОЛЬНАЯ-2026-10-09 том переживает замену контейнера
```

**SQL напрямую:**

```bash
$ docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At \
    -c "SELECT count(*) FROM notes;"'
2
```

Контрольная заметка, записанная до снятия копии, доступна после **полного
удаления тома** и восстановления из архива. Цикл завершён: создание копии
подтверждено её успешным восстановлением, а не только фактом появления файла.

### 3.7. Что осталось за рамками

Холодный бэкап требует простоя и копирует файлы кластера целиком. Для рабочей
системы этого недостаточно:

- `pg_dump` / `pg_dumpall` снимают логическую копию с работающей БД и переносятся
  между мажорными версиями, но медленнее и не сохраняют физическое состояние;
- `pg_basebackup` + архивация WAL дают восстановление на момент времени (PITR)
  без остановки.

Здесь выбран холодный вариант, потому что задание требует продемонстрировать
именно работу с томом как с набором файлов, а также потому, что на учебном стенде
простой ничего не стоит.

## 4. Изоляция сетей

### 4.1. Топология

```yaml
networks:
  front:
  back:
```

| Сеть    | Подсеть         | Участники                |
|---------|-----------------|--------------------------|
| `front` | `172.21.0.0/16` | `web`, `app`             |
| `back`  | `172.22.0.0/16` | `app`, `db`, `cache`     |

```bash
$ docker network inspect notes-app_front --format '{{range .Containers}}{{.Name}} {{end}}'
notes-app-app-1 notes-app-web-1
$ docker network inspect notes-app_back --format '{{range .Containers}}{{.Name}} {{end}}'
notes-app-app-1 notes-app-db-1 notes-app-cache-1
```

`app` — единственный сервис в обеих сетях. Входной `web` к `back` **не подключён**;
изоляция не обходится ради прохождения проверок.

### 4.2. Публикуется только входной сервис

```bash
$ docker compose ps --format 'table {{.Service}}\t{{.Ports}}'
SERVICE   PORTS
app       8000/tcp
cache     6379/tcp
db        5432/tcp
web       0.0.0.0:18080->80/tcp, [::]:18080->80/tcp
```

У `app`, `db` и `cache` указан только внутренний порт — маппинга на хост нет.

### 4.3. Из Nginx: DNS-имени БД не существует

```bash
$ docker compose exec web sh -c 'getent hosts db || echo "db: имя НЕ найдено"'
db: имя НЕ найдено
$ docker compose exec web sh -c 'getent hosts cache || echo "cache: имя НЕ найдено"'
cache: имя НЕ найдено
```

Встроенный DNS Compose отвечает только про контейнеры **общей** сети. У `web`
и `db` общей сети нет, поэтому имя не резолвится вообще — это не отказ
в доступе, а отсутствие записи.

### 4.4. Из Nginx: прямое TCP по IP тоже отвергается

DNS можно обойти, подключившись по адресу. Проверено и это:

```bash
$ docker compose exec web sh -c 'nc -z -w3 172.22.0.3 5432 && echo OK || echo "отказ/таймаут, код $?"'
отказ/таймаут, код 1

$ docker compose exec web sh -c 'nc -z -w3 172.22.0.2 6379 && echo OK || echo "отказ/таймаут, код $?"'
отказ/таймаут, код 1
```

Причина видна в интерфейсах:

```bash
$ docker compose exec web sh -c 'ip -o -4 addr show'
lo   127.0.0.1/8
eth0 172.21.0.3/16        ← только front, интерфейса в back нет
```

### 4.5. Из приложения: доступ есть

```bash
$ docker compose exec app python -c "
import socket
for host,port in (('db',5432),('cache',6379)):
    s=socket.create_connection((host,port),3); print(f'TCP {host}:{port} -> {s.getpeername()} OK'); s.close()"
TCP db:5432 -> ('172.22.0.3', 5432) OK
TCP cache:6379 -> ('172.22.0.2', 6379) OK
```

И на прикладном уровне:

```bash
$ docker compose exec app python -c "
import os,psycopg,redis
with psycopg.connect(host='db',dbname=os.environ['POSTGRES_DB'],
                     user=os.environ['POSTGRES_USER'],
                     password=os.environ['POSTGRES_PASSWORD']) as c:
    print('PostgreSQL:', c.execute('select count(*) from notes').fetchone()[0], 'заметок')
print('Redis PING:', redis.Redis(host='cache').ping())"
PostgreSQL: 2 заметок
Redis PING: True
```

### 4.6. Почему `app` в двух сетях не становится IP-маршрутизатором

Вопрос задания. Напрашивающийся ответ — «в контейнере выключена пересылка
пакетов» — **неверен**:

```bash
$ docker compose exec app cat /proc/sys/net/ipv4/ip_forward
1
$ cat /proc/sys/net/ipv4/ip_forward
1
```

Пересылка включена и в контейнере, и на хосте. Настоящих причин две,
и они независимы.

**Причина первая: на `app` никто не маршрутизирует.**

Адреса `app`:

```bash
$ docker inspect notes-app-app-1 --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} -> {{$v.IPAddress}}{{println}}{{end}}'
notes-app_back  -> 172.22.0.4
notes-app_front -> 172.21.0.2
```

Таблица маршрутов `web` и решение ядра для пакета к БД:

```bash
$ docker compose exec web sh -c 'ip route; ip route get 172.22.0.3'
default via 172.21.0.1 dev eth0
172.21.0.0/16 dev eth0 scope link src 172.21.0.3
---
172.22.0.3 via 172.21.0.1 dev eth0  src 172.21.0.3
```

Пакет уходит на `172.21.0.1`. Это **не `app`** (у него во `front` адрес
`172.21.0.2`), а шлюз сети — интерфейс моста на хосте:

```bash
$ ip -o -4 addr show | grep -E '172\.2[12]\.'
br-54a5dc31f036 172.21.0.1/16     ← шлюз front
br-012df8ff82d3 172.22.0.1/16     ← шлюз back
```

У `web` нет ни маршрута `172.22.0.0/16 via 172.21.0.2`, ни причин его завести.
**Маршрутизатором становятся не по факту наличия двух интерфейсов, а потому что
кто-то указал тебя следующим узлом.** Отсутствие маршрута — не запрет, а отсутствие
действия: никто не настраивал `app` шлюзом и не объявлял через него маршруты.

**Причина вторая: хост не пересылает трафик между мостами.**

Пакет доходит до хоста, у которого есть интерфейсы в обеих сетях и включён
`ip_forward`. Технически он мог бы переслать его в `back`. Не пересылает,
потому что Docker устанавливает правила изоляции (цепочки
`DOCKER-ISOLATION-STAGE-1/2`), отбрасывающие трафик между разными
bridge-сетями. Эмпирическое подтверждение — таймаут `nc` из 4.4.

**Что делает `app`.** Он не передаёт чужие пакеты, а **инициирует собственные
соединения** с адресов `172.21.0.2` и `172.22.0.4` соответственно. Для `web`
он обычный HTTP-сервер во `front`; для `db` — обычный клиент в `back`.
Два интерфейса дают ему две роли, а не функцию шлюза между ними.

### 4.7. Граница изоляции: с хоста БД доступна

Честная оговорка, без которой картина неполна:

```bash
$ nc -z -w3 172.22.0.3 5432
Connection to 172.22.0.3 5432 port [tcp/postgresql] succeeded!
```

С **хоста** база доступна напрямую по внутреннему адресу, хотя её порт не
опубликован. Причина не в ошибке конфигурации: хост имеет интерфейс
`br-012df8ff82d3` с адресом `172.22.0.1/16` в сети `back`, то есть находится
в ней и маршрутизирует туда напрямую, без пересылки между мостами.

Что это означает на практике:

- отсутствие `ports:` защищает от **внешней сети** и от **других
  compose-сетей**, но не от процессов на самом хосте Docker;
- «не опубликован» не равно «недоступен» — равно «недоступен снаружи хоста».

Для учебного стенда это приемлемо. В рабочей среде доступ к хосту Docker
и так равносилен доступу ко всем контейнерам, поэтому отдельной меры здесь
не требуется — важно лишь не считать неопубликованный порт самостоятельной
границей безопасности.

---

## 5. Переменные, `.env`, имя проекта, `down` против `down -v`

### 5.1. Изменяемые значения вынесены в переменные

```yaml
web:
  ports:
    - "${WEB_PORT:-18080}:80"
app:
  environment:
    POSTGRES_DB: ${POSTGRES_DB}
    POSTGRES_USER: ${POSTGRES_USER}
    POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
```

Синтаксис `${WEB_PORT:-18080}` задаёт значение по умолчанию: стек поднимется
даже без `.env`, но с паролем БД такой трюк намеренно не используется —
отсутствие пароля должно быть ошибкой, а не молча подставленным значением.

### 5.2. `.env.example` в Git, рабочий `.env` исключён

`.env.example` (отслеживается Git):

```bash
POSTGRES_DB=notes
POSTGRES_USER=notes_user
POSTGRES_PASSWORD=change_me_locally
WEB_PORT=18080
```

Значения валидные, но демонстративно временные: `change_me_locally` очевидно
требует замены и не является ничьим настоящим паролем.

> **Честная оговорка.** На этом стенде рабочий `.env` получен простым
> `cp .env.example .env`, то есть пароль в нём совпадает с примером. Для
> одноразового локального стенда без опубликованного порта БД это приемлемо;
> в любой общей среде значение обязано отличаться. Почему смена пароля
> не сводится к правке `.env` — см. 5.8.

`.gitignore`:

```bash
.env
.check/
backup/
__pycache__/
*.py[cod]
.venv/
```

Проверка, что рабочий файл действительно не попадает в репозиторий:

```bash
$ git ls-files --error-unmatch .env 2>/dev/null || echo "нет, исключён"
нет, исключён
$ git check-ignore -v .env
.gitignore:2:.env	.env
```

`backup/` исключён отдельно: там лежит архив с реальными данными БД (6.8 MB),
ему не место в истории репозитория.

### 5.3. Итог `docker compose config`

Пароли вырезаны из отчётного вывода (`sed -E 's/(PASSWORD): .*/\1: «вырезано»/'`).
**Сама команда печатает их в открытом виде** — это и есть её назначение:
показать итоговую конфигурацию после всех подстановок.

```yaml
name: notes-app
services:
  app:
    build:
      context: /home/trueforme/devops/docker-notes-app
      dockerfile: Dockerfile
    depends_on:
      cache:
        condition: service_healthy
        required: true
      db:
        condition: service_healthy
        required: true
    environment:
      DB_HOST: db
      DB_PORT: "5432"
      POSTGRES_DB: notes
      POSTGRES_PASSWORD: «вырезано»
      POSTGRES_USER: notes_user
      REDIS_HOST: cache
      REDIS_PORT: "6379"
    mem_limit: "268435456"
    networks:
      back: null
      front: null
  db:
    environment:
      POSTGRES_DB: notes
      POSTGRES_PASSWORD: «вырезано»
      POSTGRES_USER: notes_user
    healthcheck:
      test: [CMD-SHELL, 'pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB']
      interval: 2s
      retries: 15
      start_period: 3s
    image: postgres:18
    networks:
      back: null
    volumes:
      - type: volume
        source: pgdata
        target: /var/lib/postgresql
```

Видно, что `mem_limit: 256m` развернулся в `"268435456"`, а `${POSTGRES_DB}`
подставился значением из `.env`.

### 5.4. Разовая подмена переменной

```bash
$ WEB_PORT=18081 docker compose up -d --wait web
$ curl -fsS localhost:18081/
Docker notes app - visits: 1
$ docker compose ps web --format '{{.Ports}}'
0.0.0.0:18081->80/tcp, [::]:18081->80/tcp
```

Но переменная живёт **только один вызов команды**:

```bash
$ docker compose up -d --wait web          # уже без WEB_PORT
$ docker compose ps web --format '{{.Ports}}'
0.0.0.0:18080->80/tcp, [::]:18080->80/tcp  ← вернулось значение из .env
$ curl -o /dev/null -w '%{http_code}' --max-time 3 localhost:18081/
000
```

Следующий `up` пересоздал контейнер по `.env`. Чтобы изменение было постоянным,
правят `.env`, а не окружение команды.

### 5.5. Источник имени проекта и его приоритет

```bash
$ docker compose config --format json | jq -r .name
notes-app
$ docker compose -p notes-app-cli config --format json | jq -r .name
notes-app-cli
$ COMPOSE_PROJECT_NAME=notes-app-env docker compose config --format json | jq -r .name
notes-app-env
$ sed '/^name:/d' compose.yaml > .check/no-name.yaml
$ docker compose --project-directory . -f .check/no-name.yaml config --format json | jq -r .name
docker-notes-app
```

| Приоритет | Источник                     | Результат          |
|-----------|------------------------------|--------------------|
| 1 (высший)| флаг `-p` / `--project-name` | `notes-app-cli`    |
| 2         | `COMPOSE_PROJECT_NAME`       | `notes-app-env`    |
| 3         | поле `name:` в `compose.yaml`| `notes-app`        |
| 4 (низший)| имя каталога                 | `docker-notes-app` |

Верхняя строка проверена прямым конфликтом:

```bash
$ COMPOSE_PROJECT_NAME=from-env docker compose -p from-cli config --format json | jq -r .name
from-cli
```

В проекте задано поле `name: notes-app` — без него именем стало бы
`docker-notes-app` (имя каталога), и переименование папки меняло бы имена
всех ресурсов.

### 5.6. Имена контейнеров, сетей и томов

Имя проекта — префикс всего:

```bash
$ docker compose ps --format 'table {{.Name}}\t{{.Service}}'
NAME                SERVICE
notes-app-app-1     app
notes-app-cache-1   cache
notes-app-db-1      db
notes-app-web-1     web

$ docker network ls --filter name=^notes-app_ --format 'table {{.Name}}\t{{.Driver}}'
NAME              DRIVER
notes-app_back    bridge
notes-app_front   bridge

$ docker volume ls --filter name=^notes-app_ --format 'table {{.Name}}'
VOLUME NAME
notes-app_pgdata
```

Правила именования:

| Ресурс    | Шаблон                        | Пример              |
|-----------|-------------------------------|---------------------|
| Контейнер | `<проект>-<сервис>-<номер>`   | `notes-app-db-1`    |
| Сеть      | `<проект>_<имя сети>`         | `notes-app_back`    |
| Том       | `<проект>_<имя тома>`         | `notes-app_pgdata`  |

Номер в имени контейнера — порядковый для масштабирования (`--scale`).
Разделитель различается: дефис у контейнеров, подчёркивание у сетей и томов.

**Практическое следствие: одинаковое имя проекта = общие ресурсы.** Два стека,
запущенные с одним именем, будут управлять одними и теми же контейнерами
и томом. Поэтому имя задано явно полем `name:`, а не оставлено на усмотрение
имени каталога.

### 5.7. `down` против `down -v`

Демонстрация выполнена на данных собственного стенда, восстановимых из архива
раздела 3.

**A. `docker compose down` — том сохраняется:**

```bash
$ curl -fsS localhost:18080/notes
1: контрольная заметка
2: КОНТРОЛЬНАЯ-2026-10-09 том переживает замену контейнера

$ docker compose down            # в выводе нет ни одной строки про volume
$ docker volume ls --filter name=^notes-app_pgdata$ --format '{{.Name}}'
notes-app_pgdata                 ← том на месте

$ docker compose up -d --wait && curl -fsS localhost:18080/notes
1: контрольная заметка
2: КОНТРОЛЬНАЯ-2026-10-09 том переживает замену контейнера
```

**B. `docker compose down -v` — том удаляется:**

```bash
$ docker compose down -v
 Volume notes-app_pgdata Removing
 Volume notes-app_pgdata Removed

$ docker volume ls --filter name=^notes-app_pgdata$ --format '{{.Name}}'
                                 ← пусто

$ docker compose up -d --wait && curl -fsS localhost:18080/notes
(no notes)                       ← база инициализирована заново, пустая
```

| Команда     | Контейнеры | Сети | Именованные тома | Данные |
|-------------|-----------|------|------------------|--------|
| `down`      | удаляет   | удаляет | **сохраняет** | сохраняются |
| `down -v`   | удаляет   | удаляет | **удаляет**   | **теряются** |

**C. Восстановление после `down -v`:**

```bash
$ docker volume create notes-app_pgdata
$ docker run --rm -v notes-app_pgdata:/data -v "$PWD/backup:/backup:ro" \
    alpine:3.24 tar xzf /backup/pgdata-20261009-045944.tgz -C /data
$ docker compose up -d --wait
$ curl -fsS localhost:18080/notes
1: контрольная заметка
2: КОНТРОЛЬНАЯ-2026-10-09 том переживает замену контейнера
```

Данные вернулись — это второе независимое подтверждение, что архив из раздела 3
рабочий.

`down -v` необратим: подтверждения он не спрашивает и выполняется мгновенно.
Единственная защита — наличие проверенной резервной копии.

### 5.8. Побочная находка: `POSTGRES_PASSWORD` не меняет пароль существующей БД

При попытке привести рабочий `.env` в соответствие с 5.2 обнаружилось поведение,
которое стоит зафиксировать отдельно.

**Что сделано:** изменено значение в `.env` и выполнен `up`.

```bash
$ sed -i 's/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=<новое>/' .env
$ docker compose up -d --wait
dependency failed to start: container notes-app-app-1 is unhealthy

$ curl -fsS localhost:18080/notes
curl: (22) The requested URL returned error: 502
```

**Что в логах:**

```text
app-1 | psycopg.OperationalError: connection failed: ... FATAL:
        password authentication failed for user "notes_user"
app-1 | ERROR:    Application startup failed. Exiting.
```

**Причина.** `POSTGRES_PASSWORD` обрабатывается точкой входа образа **только при
первичной инициализации кластера**, когда том пуст. Том уже существовал, initdb
не запускался, и в базе остался прежний пароль. Переменная окружения и реальное
состояние БД разошлись.

**Самое интересное — состояние сервисов в этот момент:**

```bash
$ docker compose ps -a --format 'table {{.Service}}\t{{.Status}}'
SERVICE   STATUS
app       Restarting (3) 4 seconds ago
cache     Up 2 minutes (healthy)
db        Up 24 seconds (healthy)      ← «здорова»!
web       Up 2 minutes
```

**`db` имеет статус `healthy`, хотя её единственный клиент не может войти.**
Причина в том, что проверяет healthcheck:

```bash
$ docker compose exec db pg_isready -U notes_user -d notes
/var/run/postgresql:5432 - accepting connections
```

`pg_isready` отвечает на вопрос «принимает ли сервер соединения», а не
«работают ли учётные данные». Для задачи «дождаться запуска БД» этого достаточно
и это правильный выбор. Но healthcheck в принципе не является проверкой того,
что зависимый сервис сможет работать: `app` падал на аутентификации, пока `db`
рапортовала о здоровье.

**Правильное исправление** — менять пароль в самой БД (`ALTER USER ... WITH
PASSWORD`) и синхронно в `.env`, а не только в `.env`. В данном прогоне эта
команда не выполнялась: передача пароля аргументом командной строки оставляет
его в истории шелла и в списке процессов. Для стенда значение возвращено
к исходному.

**Вывод для эксплуатации.** Смена пароля БД в compose-стеке — не правка
переменной, а двухшаговая операция. Если забыть первый шаг, стек ломается
в момент, когда `up` отрапортует об успешном запуске двух из четырёх сервисов.

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
