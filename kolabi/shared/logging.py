from __future__ import annotations

import logging
from typing import Optional

LOGFMT = "%(asctime)s %(threadName)s~%(levelno)s /%(filename)s@%(lineno)s@%(funcName)s/ %(message)s"
LOGNAME = "kola"


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> logging.Logger:
    logger = logging.getLogger(LOGNAME)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOGFMT))
        logger.addHandler(handler)
    logger.setLevel(level.upper())
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(logging.Formatter(LOGFMT))
        logger.addHandler(file_handler)
    return logger
