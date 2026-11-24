FROM python:3.12.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.11.26
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-dev --no-install-project
COPY abrl /app/abrl
COPY migrations /app/migrations
COPY alembic.ini /app/alembic.ini
RUN useradd --uid 10001 --create-home abrl && mkdir -p /app/.abrl && chown -R abrl:abrl /app/.abrl
USER 10001
EXPOSE 8400
CMD ["uvicorn", "abrl.api:app", "--host", "0.0.0.0", "--port", "8400", "--no-access-log"]
FROM base AS browser
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN playwright install --with-deps chromium && chmod -R a+rX /opt/browsers
USER 10001
CMD ["python", "-m", "abrl.session_runner", "--once", "--pool", "standard"]

# The containerized manual Chrome adapter requires amd64 and a display.
FROM browser AS manual
USER root
ARG GOOGLE_CHROME_VERSION=""
RUN test "$(dpkg --print-architecture)" = amd64 \
 && apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates xvfb x11-utils \
 && mkdir -p /etc/apt/keyrings \
 && curl --fail --silent --show-error https://dl.google.com/linux/linux_signing_key.pub -o /etc/apt/keyrings/google.asc \
 && printf '%s\n' 'deb [arch=amd64 signed-by=/etc/apt/keyrings/google.asc] https://dl.google.com/linux/chrome/deb/ stable main' > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends "google-chrome-stable${GOOGLE_CHROME_VERSION:+=$GOOGLE_CHROME_VERSION}" \
 && rm -rf /var/lib/apt/lists/*
COPY --chmod=755 scripts/browser-entrypoint.sh /app/scripts/browser-entrypoint.sh
ENV DISPLAY=:99
USER 10001
ENTRYPOINT ["/app/scripts/browser-entrypoint.sh"]
CMD ["python", "-m", "abrl.remote_browser", "browser"]
