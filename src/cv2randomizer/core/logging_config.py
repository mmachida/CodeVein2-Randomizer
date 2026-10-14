"""Logging setup shared by the application entry points."""

import logging


# Configure logging
def configure_logging() -> None:
    """Set a consistent console logging format for the application."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
