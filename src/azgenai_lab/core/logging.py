import logging


def configure_logging(level: str = "INFO") -> None:
    # force=True: pytest (and other callers) may already have installed
    # handlers on the root logger, which would make a plain basicConfig()
    # call a silent no-op. This must actually take effect every time.
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
