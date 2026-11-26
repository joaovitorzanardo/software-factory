FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system .

# /data guarda o SQLite de uso de tokens (volume nomeado, fora do OneDrive/drvfs)
RUN useradd --create-home --uid 1000 app && mkdir /data && chown app /data
USER app

EXPOSE 8000
CMD ["software-factory"]
