# Версия «после оптимизации». Сравнение с Dockerfile.v1 — в REPORT.md.
#
# Обе стадии стоят на одной и той же базе python:3.14-slim (Debian trixie).
# uv не является базовым образом: он копируется одним файлом в стадию build
# и в рантайм не попадает.
#
# Почему именно python:3.14-slim, а не python:3.12-slim-bookworm:
# у bookworm-варианта 19 MB унаследованных потерь по dive (в основном двойной
# /usr/bin/perl), у trixie-варианта — 5.7 MB. Разбор в REPORT.md, раздел 6.

FROM python:3.14-slim AS build

# uv приезжает как один статический бинарь, без своего базового образа.
COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /bin/uv

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


FROM python:3.14-slim

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
