# FlowJob backend image (used by Render; works on any Docker host).
FROM python:3.12-slim-bookworm

# WeasyPrint (CV PDFs) needs Pango; Liberation fonts match the Harvard template's Times New Roman fallback.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.11.32
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1

WORKDIR /app
# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project
COPY app ./app
RUN uv sync --locked --no-dev

# Unprivileged user; the app writes nothing to disk.
RUN useradd --create-home --uid 10001 flowjob
USER flowjob

ENV PATH="/app/.venv/bin:$PATH" PORT=8000
EXPOSE 8000
# The platform's proxy (and Vercel in front of it) sets X-Forwarded-For; trust it so login limits count per visitor
# instead of treating every visitor as the proxy's address.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
