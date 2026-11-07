FROM python:3.12-slim AS app

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=America/New_York \
    DESK_FMS_BASE_URL=http://127.0.0.1:8100 \
    DESK_DATABASE_URL=sqlite:////app/data/desk.db SIM_DATABASE_URL=sqlite:////app/data/fmssim.db
WORKDIR /app

COPY pyproject.toml README.md ./
COPY desk ./desk
COPY fmssim ./fmssim
RUN pip install --no-cache-dir ".[chat]" && mkdir -p /app/data
# every build runs the evaluation against its own throwaway simulator; a red line that is not zero fails the build,
# and the report is what /evaluation shows
RUN python -m desk.evaluate --out /app/desk/static/evaluation.json

COPY start.sh ./
RUN useradd --create-home --uid 1000 gatehouse && chown -R gatehouse /app/data
USER gatehouse

# the simulator stays on 127.0.0.1:8100 inside the container; only Gatehouse's port is published
EXPOSE 8000
CMD ["bash", "start.sh"]

# The test suite, run inside the image that ships, so a dependency missing from it fails here and not in
# production: docker build --target test -t gatehouse:test . && docker run --rm gatehouse:test
FROM app AS test
USER root
RUN pip install --no-cache-dir "pytest>=8"
COPY tests ./tests
USER gatehouse
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

# what a plain `docker build` produces, and what the host runs
FROM app
