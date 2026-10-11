# jobhunter image (spec 018, C3). Build:
#   podman build --build-arg BUILD_COMMIT=<short commit> \
#     -t localhost/jobhunter:<short commit> -t localhost/jobhunter:current .
#
# Digest pins: every FROM carries @sha256:. A digest of 64 zeros is a placeholder. Run
# scripts/pin-image-digests.sh (needs network and podman or skopeo) to resolve the tags to
# real digests, review the diff and commit it as a chore: PR. Bump the same way.
#
# Nothing personal or secret is copied: only pyproject.toml, uv.lock, README.md and src/
# (hatchling needs README.md). See .containerignore.

FROM ghcr.io/astral-sh/uv:0.12.5@sha256:0000000000000000000000000000000000000000000000000000000000000000 AS uv

FROM docker.io/library/python:3.13-slim-trixie@sha256:0000000000000000000000000000000000000000000000000000000000000000 AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
# Dependency layer: cached until pyproject.toml or uv.lock change.
RUN uv sync --locked --no-dev --no-install-project
COPY src/ src/
RUN uv sync --locked --no-dev --no-editable

FROM docker.io/library/python:3.13-slim-trixie@sha256:0000000000000000000000000000000000000000000000000000000000000000 AS runtime
ARG BUILD_COMMIT=unknown
LABEL org.opencontainers.image.title="jobhunter" \
      org.opencontainers.image.description="Single-user job-search system (console and CLI)" \
      org.opencontainers.image.source="https://github.com/DanielJoyce/jobber" \
      org.opencontainers.image.revision=$BUILD_COMMIT
RUN groupadd -g 1000 jobhunter \
 && useradd -u 1000 -g 1000 -m -d /home/jobhunter jobhunter \
 && mkdir -p /data /cache /config \
 && chown 1000:1000 /data /cache
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    JOBHUNTER_CONTAINER=1 \
    PYTHONUNBUFFERED=1 \
    JOBHUNTER_BUILD_COMMIT=$BUILD_COMMIT
USER 1000:1000
EXPOSE 8808
ENTRYPOINT ["jobhunter"]
CMD ["console"]

# Added when 017 phase 2 starts (D14, user decision 2026-10-10: not built now):
# FROM builder AS builder-browser   ->  uv sync --locked --no-dev --no-editable --extra browser
# FROM runtime AS browser           ->  that venv, ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw,
#                                       playwright install --with-deps --only-shell chromium
#                                       (clean apt lists; budget 1.1 GB)
