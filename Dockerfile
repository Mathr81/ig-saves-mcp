FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY ig_saves ig_saves
# REQUIRE_PROXY : cette image refuse de contacter Instagram sans PROXY_URL.
# HF_HOME : le modèle Whisper reste dans le volume au lieu d'être retéléchargé à chaque build.
ENV IG_SAVES_DATA=/data REQUIRE_PROXY=1 HF_HOME=/data/models PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["/app/.venv/bin/python", "-m", "ig_saves.server"]
