"""Process-local console logging for the application boundary."""

import logging


def configure_console_logging(*, verbose: bool) -> None:
    """Configure concise logs, with detailed application events on request."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("mali_app").setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
