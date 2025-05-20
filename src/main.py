#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py
-------
Entry point to initiate a Telegram call.
"""

import os
from dotenv import load_dotenv
from src.make_call_tg_utils import make_telegram_call
from src.baresip_utils import BaresipManager
from src.infrastructure.logging.logger import logger

def main():
    load_dotenv(dotenv_path="config/.env")
    sip_manager = BaresipManager(node_id="node1", user_id="user1", instance_id="inst1")
    try:
        result = make_telegram_call(
            adb_port="emulator-5556",
            phone="+962788542246",
            contact_name="Test Contact",
            sip_manager=sip_manager,
            rabbit_queue_name="call_queue",
            worker_name="worker1",
            request_id="req123"
        )
        logger.info(f"Call result: {'Success' if result else 'Failed'}")
    finally:
        sip_manager.stop()

if __name__ == "__main__":
    main()