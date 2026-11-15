FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=ghcr.io/astral-sh/uv:0.11.0 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY silvercare ./silvercare
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home app
RUN python -c "import hashlib,pathlib,urllib.request; p=pathlib.Path('/app/models/qwen3-embedding-tokenizer.json'); p.parent.mkdir(); urllib.request.urlretrieve('https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/resolve/97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3/tokenizer.json',p); assert hashlib.file_digest(p.open('rb'),'sha256').hexdigest() == 'def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a'"
USER 10001
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "silvercare.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]

FROM runtime AS cpu-reranker
ARG RERANKER_PRECISION=float32
USER root
RUN uv sync --frozen --no-dev --extra reranker
RUN /app/.venv/bin/python -m silvercare.cpu_reranker prepare --precision "$RERANKER_PRECISION" --output /app/models/bge-reranker-cpu
USER 10001
CMD ["/app/.venv/bin/uvicorn", "silvercare.cpu_reranker:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]

FROM runtime AS app
