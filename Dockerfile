FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so code changes don't invalidate this layer.
COPY requirements.lock ./
RUN pip install -r requirements.lock \
    && useradd --system --create-home --uid 1000 app

COPY docker-entrypoint.py /usr/local/bin/docker-entrypoint.py
COPY bot ./bot

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION} \
    DATA_DIR=/data
VOLUME ["/data"]

ENTRYPOINT ["python", "/usr/local/bin/docker-entrypoint.py"]
CMD ["python", "-m", "bot"]
