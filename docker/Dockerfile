# One image for all three Python services. They differ only in the command they are started
# with, and building one image keeps them provably on the same code and the same contract
# version — which is exactly the drift the contract tests exist to prevent.
#
# The base image is pinned to a patch version rather than a floating `3.12-slim`. A floating
# tag makes a rebuild silently different from the build that was tested. docs/deployment.md
# records how the pin is updated, so pinning does not turn into never patching.

FROM python:3.14.0-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies come from the lockfile, with hashes, before any source is copied. Two reasons,
# and the second is the important one: a source-only change does not invalidate this layer,
# and the image is built from the exact dependency graph the tests ran against rather than
# from whatever the version ranges resolved to on the day of the build.
#
# `--require-hashes` is implied by the presence of hashes and is what makes this a
# reproducible install rather than a pinned one: a package whose contents changed under a
# fixed version number fails here.
COPY requirements.lock ./
RUN python -m pip install --upgrade pip \
    && python -m pip install --require-hashes --no-deps -r requirements.lock

COPY pyproject.toml README.md ./
COPY packages/ packages/
COPY apps/ apps/
COPY examples/ examples/
COPY scripts/ scripts/

ENV PYTHONPATH=/app/packages/contracts:/app/packages/openapi-converter:/app/packages/policy-engine:/app/packages/mock-llm:/app/apps/control-plane/backend:/app/apps/runtime:/app/apps/demo-api

# Services run as a non-root user. Nothing here needs to write outside its data volume.
RUN useradd --create-home --uid 10001 toollayer \
    && mkdir -p /data \
    && chown -R toollayer:toollayer /app /data
USER toollayer

EXPOSE 8080 8081 8082

# A default health probe for `docker run` of this image on its own. Compose overrides the
# port per service; without this, a standalone container reports healthy the moment the
# process starts, whether or not it can answer.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=6 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1

CMD ["uvicorn", "control_plane.main:app", "--host", "0.0.0.0", "--port", "8080"]
