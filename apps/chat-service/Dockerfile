# Production Dockerfile for chat-service.
#
# Deliberately does NOT use the uv workspace. Builds chat-service as if it
# were any third-party customer of inferspect-sdk: pulls the SDK + LangChain
# integration extras from PyPI via `pip install`. This proves the chat-service
# integrates with the SDK exactly the way an external customer would.
#
# Build context expected to be the repo root (so `apps/chat-service/...` is
# reachable). The other Python services still use `Dockerfile.python` from
# the workspace because they live next to the SDK in this monorepo.

FROM python:3.11-slim AS runtime

RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy only chat-service. No root pyproject.toml, no workspace config →
# `pip install` resolves `inferspect-sdk[langchain]` from PyPI.
COPY apps/chat-service/pyproject.toml ./pyproject.toml
COPY apps/chat-service/chat_service ./chat_service

# Install the project + its dependencies from PyPI. `inferspect-sdk[langchain]`
# in pyproject.toml pulls the published wheel + the langchain extra.
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["uvicorn", "chat_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
