# syntax=docker/dockerfile:1.7
#
# Test image: the published core image plus the test tooling and the suite. It is
# never pushed. Building it on top of the real image means the suite exercises
# rc_dynamo exactly as it ships - the same interpreter, the same installed
# package - instead of a separate editable copy.
#
# Driven by compose-build-test.yaml, which supplies BASE_IMAGE and LocalStack. There is
# deliberately no fallback tag: the base image is always named in full, so the suite
# can only ever run against an image someone asked for.

ARG BASE_IMAGE

FROM ghcr.io/astral-sh/uv:0.12.15 AS uv

FROM ${BASE_IMAGE}

# uv stays in this image so run-checks.sh can verify both lockfiles.
COPY --from=uv /uv /bin/uv

# Tooling layer. Only the test project's metadata and lockfile feed its cache key.
# --no-emit-package rc-dynamo keeps the base image's installed package in place;
# --require-hashes is not used because a local path dependency carries no hash.
RUN --mount=type=bind,source=tests/pyproject.toml,target=/build/pyproject.toml \
    --mount=type=bind,source=tests/uv.lock,target=/build/uv.lock \
    cd /build \
    && UV_NO_CACHE=1 uv export --frozen --no-emit-project \
        --no-emit-package rc-dynamo \
        --format requirements-txt --output-file /tmp/requirements.txt \
    && UV_NO_CACHE=1 uv pip install --system --python /var/lang/bin/python3.11 \
        -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

WORKDIR /app

# pyproject.toml carries the shared ruff and mypy config; src/ is what they check.
COPY pyproject.toml uv.lock /app/
COPY src /app/src
COPY tests /app/tests

# Shed the Lambda runtime interface entrypoint: this container runs a test suite.
ENTRYPOINT []
CMD ["bash", "tests/run-checks.sh"]
