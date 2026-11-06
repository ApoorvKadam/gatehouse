"""Entry point for running the simulator as a server: uvicorn fmssim.main:app --port 8100"""
from .app import create_app

app = create_app()
