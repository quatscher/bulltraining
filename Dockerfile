# Image des Home-Assistant-Add-ons (gebaut von .github/workflows/addon.yml für aarch64 und amd64).
# Basisimage fest: ein Supervisor-BUILD_FROM wäre Alpine ohne Python/pip.
FROM python:3.12-slim-bookworm

ARG BUILD_ARCH=amd64
ARG BUILD_VERSION=dev
LABEL io.hass.type="addon" \
      io.hass.name="bulltraining" \
      io.hass.arch="${BUILD_ARCH}" \
      io.hass.version="${BUILD_VERSION}" \
      org.opencontainers.image.source="https://github.com/quatscher/bulltraining" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .

CMD ["python", "-m", "bulltraining.cli", "addon"]
