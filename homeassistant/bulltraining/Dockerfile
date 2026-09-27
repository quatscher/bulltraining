# Basisimage fest vorgegeben: der Supervisor übergibt als BUILD_FROM sein Alpine-Basisimage (ohne Python/pip).
# python:3.12-slim ist ein Multi-Arch-Image; Docker wählt die Architektur des Pi (aarch64) selbst.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

CMD ["python", "-m", "bulltraining.cli", "addon"]
