#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
logger.py
---------
Logging module for the Telegram call project.
"""

import logging

class Logger:
    def __init__(self):
        self.logger = logging.getLogger("telegram_call")
        self.logger.setLevel(logging.DEBUG)
        # Clear existing handlers to prevent duplicates
        self.logger.handlers = []
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        handler.setLevel(logging.INFO)  # Set to INFO to reduce debug clutter
        self.logger.addHandler(handler)

    def bind(self, **kwargs):
        return self  # Simplified; real implementation may add context

    def info(self, msg, *args, **kwargs):
        self.logger.info(msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self.logger.warning(msg, *args, **kwargs)

    def debug(self, msg, *args, **kwargs):
        self.logger.debug(msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self.logger.error(msg, *args, **kwargs)

    def exception(self, msg, *args, **kwargs):
        self.logger.exception(msg, *args, **kwargs)

logger = Logger()