# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# UID/GID 1000 works with the common local Linux user while also avoiding a
# root process inside Docker Desktop environments.
RUN groupadd --gid 1000 wayback \
    && useradd --uid 1000 --gid wayback --no-create-home --shell /usr/sbin/nologin wayback \
    && mkdir -p /output \
    && chown wayback:wayback /output

USER wayback

ENTRYPOINT ["wayback-tool"]
CMD ["--help"]
