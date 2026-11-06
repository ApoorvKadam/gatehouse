FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=America/New_York
WORKDIR /app

COPY pyproject.toml README.md ./
COPY desk ./desk
COPY fmssim ./fmssim
RUN pip install --no-cache-dir ".[chat]" && mkdir -p /app/data

COPY start.sh ./
RUN useradd --create-home --uid 1000 gatehouse && chown -R gatehouse /app/data
USER gatehouse

# the simulator stays on 127.0.0.1:8100 inside the container; only Gatehouse's port is published
EXPOSE 8000
CMD ["./start.sh"]
