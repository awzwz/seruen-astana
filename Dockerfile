# One image for all 14 services, 9 workers and the collector jobs; the role is chosen by $SERVICE.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY seruen ./seruen
COPY web ./web
# catalog snapshot built by `python -m seruen.ingest.run` — used by the initial load and as a fallback
COPY data/catalog ./data/catalog
ENV DATA_DIR=/app/data SERUEN_MODE=cluster PORT=8000
RUN useradd -u 10001 -m seruen && chown -R seruen /app
USER 10001
EXPOSE 8000
CMD ["python", "-m", "seruen.run"]
