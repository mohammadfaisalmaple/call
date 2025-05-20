#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
call_monitor_tg_utils.py
------------------------
Monitors Telegram call logs and SIP events.
"""

from infrastructure.logging.logger import logger
from baresip_utils import BaresipManager

def monitor_telegram_calls(sip_manager: BaresipManager, emulator_port: str, output_file: str = None) -> None:
    logger.info(f"[monitor_telegram_calls] Monitoring calls for emulator_port={emulator_port}")
    if sip_manager.ensure_connected(timeout=20):
        logger.info("[monitor_telegram_calls] SIP connected, waiting for call events")
        sip_manager.wait_incoming_call_end()
        logger.info("[monitor_telegram_calls] Call monitoring completed, auto-answer triggered if incoming call detected")
    else:
        logger.error("[monitor_telegram_calls] SIP connection failed, registration status: %s", sip_manager.is_registered())