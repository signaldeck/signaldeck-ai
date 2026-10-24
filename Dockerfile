FROM signaldeck:dev

WORKDIR /app
COPY pyproject.toml README.md ./
COPY plugins ./plugins
RUN pip install --no-cache-dir .

ENV PYTHONPATH=/plugins
