FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=ghcr.io/astral-sh/uv:0.11.0 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY silvercare ./silvercare
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home app
USER 10001
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "silvercare.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]
