FROM signaldeck:dev

WORKDIR /app
COPY pyproject.toml README.md ./
COPY plugins ./plugins

# The base image already includes signaldeck-sdk and signaldeck-plugin-main.
# Demo configuration is mounted from ./config; it is not part of the wheel.
RUN pip install --no-cache-dir \
    "mcp>=1.30,<2" \
    "uvicorn>=0.35,<1" \
    "httpx>=0.27,<1" \
    "jsonschema>=4.26,<5" \
    && pip install --no-cache-dir --no-deps .

ENV PYTHONPATH=/plugins
