"""Entry point: uvicorn desk.main:app --port 8000"""
import logging
import os

from .api import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)   # one line per FMS call buries everything else
# DESK_RUN_WORKER=0 when a separate worker process reconciles, so two loops never work the same commands
app = create_app(start_worker=os.environ.get("DESK_RUN_WORKER", "1") == "1")
