# Версия «после оптимизации». Сравнение с Dockerfile.v1 — в REPORT.md.
#
# Стадия build: здесь живёт uv и всё, что нужно для установки зависимостей.
# Рантайм: чистый python:3.12-slim-bookworm, в него переезжает только готовый .venv.
# База у обеих стадий общая (образ uv собран поверх python:3.12-slim-bookworm),
# поэтому интерпретатор внутри .venv указывает на существующий путь.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

# UV_COMPILE_BYTECODE — разложить .pyc заранее, чтобы не писать их в слой записи
# контейнера при первом запуске. UV_LINK_MODE=copy — не делать hardlink на кеш,
# которого в финальном образе не будет.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Зависимости отдельным слоем: правка app.py не инвалидирует uv sync.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.12-slim-bookworm

RUN useradd --create-home --uid 10001 appuser

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=build --chown=appuser:appuser /app/.venv /app/.venv
COPY --chown=appuser:appuser app.py .

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
