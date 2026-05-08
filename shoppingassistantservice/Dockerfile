# Copyright 2024 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

ARG BUILDPLATFORM=linux/amd64

# ---------- RUNTIME BASE ----------
FROM --platform=$BUILDPLATFORM python:3.12-slim AS base

# Runtime dependencies for PostgreSQL connectivity
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ---------- BUILD STAGE ----------
FROM base AS builder

# Build dependencies for compiling psycopg2 & pgvector C-extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.in requirements.txt

# Install packages to an isolated prefix to avoid copying build tools
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------- FINAL IMAGE ----------
FROM base

# 🔒 Security: Run as non-root user
RUN useradd --create-home --shell /bin/bash appuser && \
    mkdir -p /shoppingassistantservice && \
    chown -R appuser:appuser /shoppingassistantservice /home/appuser

WORKDIR /shoppingassistantservice
USER appuser

# Copy compiled Python packages
COPY --from=builder --chown=appuser:appuser /install /usr/local

# Copy application code
COPY --chown=appuser:appuser . .

ENV PORT=8080
EXPOSE 8080

ENTRYPOINT ["python", "shoppingassistantservice.py"]
