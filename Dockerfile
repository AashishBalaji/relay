FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30 AS builder
WORKDIR /app
COPY pyproject.toml ./
COPY relay ./relay
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/usr/local/bin:$PATH
WORKDIR /app
COPY --from=builder /install /usr/local
RUN useradd --create-home --no-log-init --shell /usr/sbin/nologin appuser
USER appuser
ENTRYPOINT ["relay"]
CMD ["--help"]
