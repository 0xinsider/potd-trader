# Run `watch` on a small always-on box. Mount the configuration folder at /app/data;
# live trading requires /app/data/.env so live off can stop existing watchers.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev
VOLUME ["/app/data"]
ENV POTD_TRADER_HOME=/app/data
ENTRYPOINT ["uv", "run", "--no-sync", "potd-trader"]
CMD ["watch"]
