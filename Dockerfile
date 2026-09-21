# Run `watch` on a small always-on box. Secrets come from --env-file at run time, never from
# the image.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
VOLUME ["/app/data"]
ENTRYPOINT ["uv", "run", "--no-sync", "potd-trader"]
CMD ["watch"]
