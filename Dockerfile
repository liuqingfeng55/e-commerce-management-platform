FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml /app/
COPY app /app/app
RUN pip install --no-cache-dir .
COPY migrations /app/migrations
COPY alembic.ini /app/alembic.ini

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
