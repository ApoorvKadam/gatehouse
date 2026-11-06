"""Entry point: uvicorn desk.main:app --port 8000"""
import logging

from .api import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
app = create_app()
