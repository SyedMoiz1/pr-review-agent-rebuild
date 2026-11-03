FROM python:3.12-slim AS build
WORKDIR /app 

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock ./

#INSTALL DEPENDENCIES
#--locked -> fail if lockfile is out of date
#--no-dev skip dev dependencies (pytest,mypy,ruff)
#--no-install-project dont install code yet, its not copied
RUN uv sync --locked --no-dev --no-install-project


#COPY CODE
COPY src ./src
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
#RUN UV SYNC AGAIN TO INSTALL PROJECT
RUN uv sync --locked --no-dev



FROM python:3.12-slim AS run
WORKDIR /app 

ENV PATH="/app/.venv/bin:$PATH"

COPY --from=build /app/src ./src
COPY --from=build /app/.venv ./.venv
COPY --from=build /app/alembic.ini ./
COPY --from=build /app/alembic ./alembic



RUN useradd --system appuser

USER appuser
CMD ["uv "]
CMD ["uvicorn", "pr_review_agent_rebuild.webhook:app", "--host", "0.0.0.0", "--port", "8080"]
