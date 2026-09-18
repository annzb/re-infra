# syntax=docker/dockerfile:1.7
#
# Retribalize Lambda parent image (linux/amd64), shipping the rc_dynamo package.
# Child service images build FROM this image pinned by digest and provide their
# own CMD; this image has no handler. Only locked production dependencies and
# rc_dynamo are installed: tests, uv, and build tooling never reach the final
# image. The test image is built on top of this one - see tests.Dockerfile.

FROM public.ecr.aws/datadog/lambda-extension:98 AS datadog
FROM ghcr.io/astral-sh/uv:0.12.15 AS uv

# Multi-arch index digest of public.ecr.aws/lambda/python:3.11; build with
# --platform linux/amd64. Bump through Dependabot or by re-resolving:
#   docker buildx imagetools inspect public.ecr.aws/lambda/python:3.11
FROM public.ecr.aws/lambda/python:3.11@sha256:cc940054e77d4a160c611a20c09cb8ab9325c09d72555a164329eb3e46957d80

# Image-native Datadog Lambda Extension, disabled by default exactly as the
# service images configure it. The datadog-lambda Python wrapper stays in the
# service images' own requirements.
COPY --from=datadog /opt/. /opt/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=${LAMBDA_TASK_ROOT} \
    ENABLE_DATADOG=false \
    DD_TRACE_ENABLED=false \
    DD_LOGS_INJECTION=false \
    DD_SERVERLESS_LOGS_ENABLED=false \
    DD_LLMOBS_ENABLED=false \
    DD_SERVERLESS_FLUSH_STRATEGY=periodically,1

# Dependency layer. Only pyproject.toml and uv.lock feed its cache key, so
# source-only changes reuse it.
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=bind,source=pyproject.toml,target=/build/pyproject.toml \
    --mount=type=bind,source=uv.lock,target=/build/uv.lock \
    cd /build \
    && UV_NO_CACHE=1 uv export --frozen --no-dev --no-emit-project \
        --format requirements-txt --output-file /tmp/requirements.txt \
    && UV_NO_CACHE=1 uv pip install --system --python /var/lang/bin/python3.11 \
        --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Package layer: builds and installs rc_dynamo (and its console scripts)
# without dependencies. The bind mount is discarded, so no source tree remains.
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=bind,target=/build,rw \
    UV_NO_CACHE=1 uv pip install --system --python /var/lang/bin/python3.11 --no-deps /build

WORKDIR ${LAMBDA_TASK_ROOT}

# Last, so per-build values never invalidate the layers above.
ARG SOURCE_REPOSITORY=""
ARG CREATED=""
LABEL org.opencontainers.image.title="rc-dynamo" \
      org.opencontainers.image.description="Retribalize Lambda parent image with the rc_dynamo DynamoDB framework" \
      org.opencontainers.image.source="${SOURCE_REPOSITORY}" \
      org.opencontainers.image.created="${CREATED}" \
      ai.retribalize.python.version="3.11"
