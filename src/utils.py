"""
utils.py
========
Shared helpers: standardised logging and a SQLite connection factory.

Centralised logging means every risk computation leaves an audit trail
(timestamped) on disk and on the console — a regulatory expectation for any
production risk system.
"""

from __future__ import annotations

import logging
import sqlite3
import sys

from config import DB_PATH, LOG_PATH


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger writing to console + the engine log file.

    Idempotent: repeated calls with the same name won't stack handlers.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    try:
        fileh = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
        fileh.setFormatter(fmt)
        logger.addHandler(fileh)
    except OSError:
        # If the log file can't be opened we still want console logging.
        logger.warning("Could not open log file at %s", LOG_PATH)

    logger.propagate = False
    return logger


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Return a SQLite connection with foreign keys enforced.

    Using a single factory keeps connection settings consistent and makes
    it trivial to swap SQLite for another backend later.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn
