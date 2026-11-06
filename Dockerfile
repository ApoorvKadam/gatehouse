FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=America/New_York
WORKDIR /app

COPY pyproject.toml README.md ./
COPY desk ./desk
COPY fmssim ./fmssim
RUN pip install --no-cache-dir ".[chat]" && mkdir -p /app/data

RUN useradd --create-home --uid 1000 desk && chown -R desk /app/data
USER desk

EXPOSE 8000 8100
CMD ["uvicorn", "desk.main:app", "--host", "0.0.0.0", "--port", "8000"]
