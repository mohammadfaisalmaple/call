#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
state_manager.py
----------------
Placeholder for logging state changes.
"""

from infrastructure.logging.logger import logger

def log_state(state_code: str, operation: str, action: str, status: str, details: dict, description: str) -> None:
    logger.info(f"[log_state] {state_code}: {operation}/{action} - {status} - {description} - {details}")