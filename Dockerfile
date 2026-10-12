FROM python:3.13-slim
# Install the locked dependencies and this package into the system Python.
# uv and the source are only mounted for this step, so neither ends up in the
# image. rw: the build writes metadata into the source (discarded afterwards).
# uv sync also removes anything not in uv.lock, such as the image's pip.
RUN --mount=from=ghcr.io/astral-sh/uv:0.12.24,source=/uv,target=/bin/uv \
    --mount=type=bind,target=/src,rw \
    cd /src && UV_PROJECT_ENVIRONMENT=/usr/local \
    uv sync --frozen --no-dev --no-editable --no-cache
USER 65534:65534
ENTRYPOINT ["rgw-quota-sync"]
