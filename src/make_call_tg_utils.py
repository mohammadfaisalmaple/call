#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_call_tg_utils.py
---------------------
Orchestrates a Telegram voice-call from 'Call Mode'.
- Validates phone number.
- Ensures contact is in device's Contacts and synced with Telegram.
- Queries Telegram's cache4.db for userId.
- Responds with SIP-like codes (180, 484, 502).
- Uses BaresipManager for SIP call monitoring.
"""

import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Final, Optional, Tuple

from infrastructure.logging.logger import logger
from baresip_utils import BaresipManager
from utilities.helpers.telegram_utils.call_monitor_tg_utils import monitor_telegram_calls
from utilities.helpers.call_responses_publisher import send_call_response

# Constants
ADB: Final = os.getenv("ADB_PATH", "adb")
DB_PATH: Final = "/data/data/org.telegram.messenger/files/cache4.db"
SQLITE_BIN: Final = "sqlite3"
ACCOUNT_TYPE, ACCOUNT_NAME = "com.android.localprofile", "Local"
CALL_UID_FIND_TIMEOUT = float(os.getenv("CALL_UID_FIND_TIMEOUT", "2.0"))
CALL_UID_FIND_INTERVAL = float(os.getenv("CALL_UID_FIND_INTERVAL", "0.2"))

def run(cmd, **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess command with output captured."""
    return subprocess.run(cmd, text=True, capture_output=True, **kwargs)

def valid_phone(p: str) -> bool:
    """Check if phone number matches +<digits> format, 6-15 digits after '+'."""
    return re.fullmatch(r"\+\d{6,15}", p) is not None

def is_rooted(device_id: str) -> bool:
    """Check if Android device is rooted by checking 'uid=0(root)' in 'id' command."""
    trace_logger = logger.bind(call_trace=True, call_id=device_id)
    proc = run([ADB, "-s", device_id, "shell", "id"])
    if proc.returncode != 0:
        trace_logger.info(f"[is_rooted] ADB error => {proc.stderr}")
        return False
    return "uid=0(root)" in proc.stdout

def get_telegram_user_id(device_id: str, phone: str) -> Optional[str]:
    """Query Telegram's cache4.db for userId by phone number."""
    trace_logger = logger.bind(call_trace=True, call_id=device_id)

    def _adb_sql(query: str) -> Optional[str]:
        cmd = f'echo "{query}" | {SQLITE_BIN} {DB_PATH}'
        proc = run([ADB, "-s", device_id, "shell", cmd])
        if proc.returncode != 0:
            trace_logger.info(f"[get_telegram_user_id] SQL error => {proc.stderr}")
            return None
        return proc.stdout.strip()

    exact_query = f"SELECT uid FROM users WHERE name = '{phone}'"
    trace_logger.info(f"[get_telegram_user_id] searching exact name='{phone}'")
    exact_out = _adb_sql(exact_query)
    if exact_out and exact_out.isdigit():
        return exact_out

    partial_query = f"SELECT uid FROM users WHERE name LIKE '%{phone[1:]}%'"
    trace_logger.info(f"[get_telegram_user_id] searching partial name='{phone[1:]}'")
    partial_out = _adb_sql(partial_query)
    if partial_out and partial_out.isdigit():
        return partial_out
    return None

def dump_ui(device_id: str) -> Optional[str]:
    """Dump current UI hierarchy using uiautomator."""
    trace_logger = logger.bind(call_trace=True, call_id=device_id)
    start_t = time.time()
    proc = run([ADB, "-s", device_id, "exec-out", "uiautomator", "dump", "/dev/tty"])
    if proc.returncode != 0:
        trace_logger.info(f"[dump_ui] Failed => {proc.stderr}")
        return None
    xml_content = proc.stdout.split("UI hierarchy dumped to:")[0].strip()
    trace_logger.info(f"[dump_ui] took {time.time() - start_t:.2f}s")
    if xml_content.endswith("</hierarchy>"):
        return xml_content
    match = re.search(r"(.*?</hierarchy>)\s*", xml_content, re.DOTALL)
    return match.group(1) if match else None

def find_element_center(xml_content: str, text: str) -> Optional[Tuple[int, int]]:
    """Find element by text or content-desc and return center coordinates."""
    trace_logger = logger.bind(call_trace=True)
    try:
        root = ET.fromstring(xml_content)
        for node in root.iter("node"):
            node_text = node.get("text", "")
            node_desc = node.get("content-desc", "")
            if text.lower() in (node_text + node_desc).lower():
                bounds_str = node.get("bounds", "")
                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
                if m:
                    l, t, r, b = map(int, m.groups())
                    return ((l + r) // 2, (t + b) // 2)
    except ET.ParseError as e:
        trace_logger.info(f"[find_element_center] XML parse error => {e}")
    return None

def wait_for_element(device_id: str, text: str, timeout: float = 12.0, max_attempts: int = 40) -> Optional[Tuple[int, int]]:
    """Poll UI for element by text or content-desc, return center coords."""
    trace_logger = logger.bind(call_trace=True, call_id=device_id)
    trace_logger.info(f"[wait_for_element] searching '{text}' with timeout={timeout}s")
    start_time = time.time()
    attempts = 0
    while (time.time() - start_time) < timeout and attempts < max_attempts:
        xml = dump_ui(device_id)
        if xml:
            coords = find_element_center(xml, text)
            if coords:
                trace_logger.info(f"[wait_for_element] found '{text}' => {coords}, attempt={attempts+1}")
                return coords
        time.sleep(0.05)
        attempts += 1
    trace_logger.info(f"[wait_for_element] timed out => '{text}' not found")
    return None

def tap(device_id: str, x: int, y: int) -> None:
    """Perform ADB tap at (x, y)."""
    trace_logger = logger.bind(call_trace=True, call_id=device_id)
    trace_logger.info(f"[tap] => x={x}, y={y}")
    run([ADB, "-s", device_id, "shell", "input", "tap", str(x), str(y)])

def is_telegram_in_foreground(device_id: str) -> bool:
    """Check if Telegram is in the foreground."""
    proc = run([ADB, "-s", device_id, "shell", "dumpsys", "window", "windows"])
    return "org.telegram.messenger" in proc.stdout

def make_telegram_call(
    adb_port: str,
    phone: str,
    contact_name: str,
    sip_manager: BaresipManager,
    rabbit_queue_name: str,
    worker_name: str,
    request_id: str
) -> bool:
    """Main function to initiate Telegram call with Call -> End Call -> Call sequence."""
    trace_logger = logger.bind(call_trace=True, call_id=phone)
    trace_logger.info(f"=== make_telegram_call START phone={phone}, contact={contact_name}, request_id={request_id} ===")

    device_id = adb_port if adb_port.startswith("emulator-") else f"emulator-{adb_port}"
    emulator_port = device_id.replace("emulator-", "")

    # Step 1: Validate phone
    trace_logger.info("[make_telegram_call] Step 1: Validate phone")
    if not valid_phone(phone):
        trace_logger.info("[make_telegram_call] phone invalid => respond 484")
        send_call_response(response="484", request_id=request_id, worker_name=worker_name, phone=phone)
        return False

    # Ensure device is rooted
    trace_logger.info("[make_telegram_call] Checking device root status")
    if not is_rooted(device_id):
        trace_logger.info("[make_telegram_call] device not rooted => trying 'adb root'")
        run([ADB, "-s", device_id, "root"])
        time.sleep(1)

    # Step 2: Insert contact
    trace_logger.info("[make_telegram_call] Step 2: adding contact")
    shell_script = f"""#!/system/bin/sh
set -e
NAME="{contact_name}"
PHONE="{phone}"
ACC_TYPE="{ACCOUNT_TYPE}"
ACC_NAME="{ACCOUNT_NAME}"
exists=$(content query --uri content://com.android.contacts/data \
  --projection data1 \
  --where "data1 LIKE '%$PHONE%' AND mimetype='vnd.android.cursor.item/phone_v2'" 2>&1)
if echo "$exists" | grep -q "Error"; then
  echo "? query error => $exists"
  exit 1
fi
if echo "$exists" | grep -q "Row:.*data1=$PHONE"; then
  echo "? contact exists"
else
  echo "? inserting contact => $NAME ($PHONE)"
  ins=$(content insert --uri content://com.android.contacts/raw_contacts \
      --bind account_type:s:$ACC_TYPE \
      --bind account_name:s:$ACC_NAME)
  RAW_ID=$(content query --uri content://com.android.contacts/raw_contacts \
    --projection _id | tail -n1 | sed -En 's/.*_id=([0-9]+).*/\\1/p')
  if [ -z "$RAW_ID" ]; then
    echo "? can't find RAW_ID => fail."
    exit 1
  fi
  content insert --uri content://com.android.contacts/data \
    --bind raw_contact_id:i:$RAW_ID \
    --bind mimetype:s:vnd.android.cursor.item/structured_name \
    --bind data1:s:"$NAME"
  content insert --uri content://com.android.contacts/data \
    --bind raw_contact_id:i:$RAW_ID \
    --bind mimetype:s:vnd.android.cursor.item/phone_v2 \
    --bind data1:s:"$PHONE" \
    --bind data2:i:2
fi
am force-stop org.telegram.messenger
sleep 0.3
am start -n org.telegram.messenger/org.telegram.ui.LaunchActivity
"""
    contact_proc = run([ADB, "-s", device_id, "shell"], input=shell_script)
    if contact_proc.returncode != 0:
        trace_logger.info("[make_telegram_call] contact insertion error => respond 502")
        send_call_response(response="502", request_id=request_id, worker_name=worker_name, phone=phone)
        return False
    trace_logger.info("[make_telegram_call] contact insertion success or contact exists")

    # Step 3: Search userId in Telegram DB
    trace_logger.info("[make_telegram_call] Step 3: searching for userId")
    found_uid = None
    start_time_find_uid = time.time()
    while (time.time() - start_time_find_uid) < CALL_UID_FIND_TIMEOUT:
        found_uid = get_telegram_user_id(device_id, phone)
        if found_uid:
            break
        time.sleep(CALL_UID_FIND_INTERVAL)
    if not found_uid:
        trace_logger.info("[make_telegram_call] userId not found => respond 484")
        send_call_response(response="484", request_id=request_id, worker_name=worker_name, phone=phone)
        return False

    # Step 3.1: Respond 180 (Ringing)
    trace_logger.info(f"[make_telegram_call] userId={found_uid} => respond 180")
    send_call_response(response="180", request_id=request_id, worker_name=worker_name, phone=phone)

    # Step 4: Open Telegram chat
    trace_logger.info("[make_telegram_call] Step 4: open Telegram contact screen")
    run([
        ADB, "-s", device_id, "shell", "am", "start",
        "-n", "org.telegram.messenger/org.telegram.ui.LaunchActivity",
        "-a", "com.tmessages.openchat",
        "--el", "userId", found_uid,
        "--ez", "startInBubble", "false",
        "--ez", "open_keyboard", "true"
    ])

    # Call -> End Call -> Call sequence
    def find_and_tap(text_label: str) -> bool:
        coords = wait_for_element(device_id, text_label, timeout=8.0, max_attempts=80)
        if not coords:
            trace_logger.info(f"[make_telegram_call] '{text_label}' not found => respond 502")
            send_call_response(response="502", request_id=request_id, worker_name=worker_name, phone=phone)
            return False
        tap(device_id, coords[0], coords[1])
        dump_ui(device_id)
        if not is_telegram_in_foreground(device_id):
            trace_logger.info("[make_telegram_call] Telegram lost focus => respond 502")
            send_call_response(response="502", request_id=request_id, worker_name=worker_name, phone=phone)
            return False
        return True


    def find_element(text_label: str) -> bool:
        coords = wait_for_element(device_id, text_label, timeout=8.0, max_attempts=80)
        if not coords:
            return False
        return True

    def is_keyboard_open(device_id: str) -> bool:
        """Check if the soft keyboard is open using dumpsys input_method."""
        trace_logger = logger.bind(call_trace=True, call_id=device_id)
        proc = run([ADB, "-s", device_id, "shell", "dumpsys", "input_method"])
        if proc.returncode != 0:
            trace_logger.info(f"[is_keyboard_open] dumpsys error => {proc.stderr}")
            return False
        return "mInputShown=true" in proc.stdout

    trace_logger.info("[make_telegram_call] Step: FIRST 'Call' button")
    if find_element("Attach media"):
        if is_keyboard_open(device_id):
            run([ADB, "-s", device_id, "shell", "input", "keyevent", "4"])

    trace_logger.info("[make_telegram_call] Step: FIRST 'Call' button")
    if not find_and_tap("Call"):
        return False
    trace_logger.info("[make_telegram_call] Step: 'End Call' button")
    if not find_and_tap("End Call"):
        return False
    

    trace_logger.info("[make_telegram_call] Step: SECOND 'Call' button")
    if not find_and_tap("Call"):
        return False

    # Monitor calls using BaresipManager
    trace_logger.info("[make_telegram_call] Monitoring Telegram call logs")
    sip_manager.ensure_connected()
    monitor_telegram_calls(sip_manager, emulator_port=emulator_port, output_file=None)
    trace_logger.info("[make_telegram_call] Done")
    return True