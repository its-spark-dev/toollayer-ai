# One image for all three Python services. They differ only in the command they are started
# with, and building one image keeps them provably on the same code and the same contract
# version — which is exactly the drift the contract tests exist to prevent.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies are installed from the project metadata alone before the source is copied, so
# a source-only change does not invalidate the dependency layer.
COPY pyproject.toml README.md ./
COPY packages/contracts/toollayer_contracts/__init__.py packages/contracts/toollayer_contracts/__init__.py
RUN python -m pip install --upgrade pip \
    && python -c "import tomllib,subprocess,sys; \
d=tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']; \
sys.exit(subprocess.call([sys.executable,'-m','pip','install',*d]))"

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

CMD ["uvicorn", "control_plane.main:app", "--host", "0.0.0.0", "--port", "8080"]
