#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
call_monitor_tg_utils.py
------------------------
Monitors Telegram calls by reading ADB logcat for specific tags and detecting call state transitions:
    - RINGING
    - CONNECTING
    - ANSWERED
    - DISCONNECTED
Also integrates with SipManager to answer or disconnect SIP calls accordingly.
Added feature: Print colored messages in the console using ANSI color codes for better state visibility.
"""

import subprocess
import re
import datetime
import time
import sys
from enum import Enum
from pathlib import Path

from infrastructure.logging.logger import logger
from baresip_utils import BaresipManager as SipManager
from utilities.helpers.steps_wrapper import execute_step

class CallState(Enum):
    IDLE = "IDLE"
    RINGING = "RINGING"
    CONNECTING = "CONNECTING"
    ANSWERED = "ANSWERED"
    DISCONNECTED = "DISCONNECTED"

# ANSI color codes to colorize call states in the console
COLOR_CODES = {
    "IDLE": "\033[90m",         # Gray
    "RINGING": "\033[95m",      # Magenta
    "CONNECTING": "\033[93m",   # Yellow
    "ANSWERED": "\033[92m",     # Green
    "DISCONNECTED": "\033[91m", # Red
    "RESET": "\033[0m"          # Reset to default
}

def colorize(state_str: str, message: str) -> str:
    color = COLOR_CODES.get(state_str, COLOR_CODES["RESET"])
    return f"{color}[{state_str}] {message}{COLOR_CODES['RESET']}"

TAGS = [
    "tgvoip:V", "tgvoip:D", "tgvoip:I", "tgvoip:W", "tgvoip:E",
    "MediaFocusControl:I", "MediaFocusControl:D",
    "AudioManager:I", "AudioManager:D",
    "Telecom:I", "Telecom:D",
    "VoIPService:D", "VoIPService:I",
    "VoIPBaseService:D",
    "VoIPController:D",
    "CallAudioRouteStateMachine:I",
    "ConnectionService:D",
    "AudioService:I",
    "AudioFlinger:D", "AudioFlinger:I",
    "ActivityTaskManager:I",
    "webrtc_voice_engine:I", "webrtc_voice_engine:D",
    "EncryptedConnection:I", "EncryptedConnection:D",
    "ReflectorPort:I", "ReflectorPort:D", "ReflectorPort:W"
]

PATTERNS = {
    "RINGING": re.compile(
        r"(START\s+u0\s+\{act=voip.*cmp=org\.telegram\.messenger/org\.telegram\.ui\.LaunchActivity\}|"
        r"tgvoip.*(Initiating call|Call ringing|set network type:.*active interface))",
        re.IGNORECASE
    ),
    "CONNECTING": re.compile(
        r"(requestAudioFocus.*USAGE_VOICE_COMMUNICATION|"
        r"VoIPService.*startOutgoingCall|"
        r"Telecom.*NEW_OUTGOING_CALL|"
        r"tgvoip.*(Connecting|Starting connection|Bound to local UDP port|Receive thread starting|Sending UDP ping)|"
        r"webrtc_voice_engine.*(AddSendStream|AddRecvStream|SetSenderParameters|SetReceiverParameters)|"
        r"EncryptedConnection.*(SEND:empty|processSignalingData)|"
        r"ReflectorPort.*(sending ping))",
        re.IGNORECASE
    ),
    "ANSWERED": re.compile(
        r"(tgvoip.*(First audio packet - setting state to ESTABLISHED|Call state changed to 3|Call established|Call connected)|"
        r"AudioFlinger.*(thread.*ready to run|Track created successfully|start output|audio stream started)|"
        r"AudioManager.*MODE_IN_COMMUNICATION|"
        r"MediaFocusControl.*AUDIOFOCUS_GAIN)",
        re.IGNORECASE
    ),
    "DISCONNECTED": re.compile(
        r"(abandonAudioFocus|"
        r"tgvoip.*(Call ended|Call rejected|Call terminated)|"
        r"Telecom.*(CALL_DISCONNECTED|CALL_REJECTED)|"
        r"MediaFocusControl.*AUDIOFOCUS_LOSS|"
        r"AudioManager.*MODE_NORMAL)",
        re.IGNORECASE
    )
}

def start_logcat(emulator_port=None, call_id=None):
    adb_cmd = ["adb"]
    if emulator_port:
        adb_cmd += ["-s", f"emulator-{emulator_port}"]
    subprocess.run(adb_cmd + ["logcat", "-c"], check=True)
    cmd = adb_cmd + ["logcat", "-v", "time", "-s"] + TAGS
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
    return proc, (call_id or f"monitor-{emulator_port or 'unknown'}")

def process_log_line(line, current_state, start_time, sip_manager: SipManager,
                     last_incall_log_time: float, call_id: str):
    trace_logger = logger.bind(call_trace=True, call_id=call_id)
    new_state = current_state
    now_ts = time.time()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_last_incall_log_time = last_incall_log_time

    if new_state == CallState.ANSWERED and start_time is not None:
        if (now_ts - last_incall_log_time) >= 5.0:
            in_call_sec = int(now_ts - start_time)
            msg = f"IN CALL => {in_call_sec} seconds so far."
            print(colorize("ANSWERED", msg))
            trace_logger.info(msg)
            new_last_incall_log_time = now_ts

    for pattern_state, regex in PATTERNS.items():
        if regex.search(line):
            if pattern_state == "RINGING" and new_state == CallState.IDLE:
                new_state = CallState.RINGING
                start_time = now_ts
                print(colorize("RINGING", f"{now_str} | {line.strip()}"))

            elif pattern_state == "CONNECTING" and new_state in (CallState.IDLE, CallState.RINGING):
                new_state = CallState.CONNECTING
                if not start_time:
                    start_time = now_ts
                print(colorize("CONNECTING", f"{now_str} | {line.strip()}"))

            elif pattern_state == "ANSWERED" and new_state in (CallState.RINGING, CallState.CONNECTING):
                new_state = CallState.ANSWERED
                duration = (now_ts - start_time) if start_time else 0
                print(colorize("ANSWERED", f"{now_str} | {line.strip()} | Connected after {duration:.1f}s"))
                step_ok = execute_step(
                    step_name="sip_manager.answer_call",
                    step_func=sip_manager.answer_call,
                    step_params={},
                    operation_context={"operation_name": "call_mode", "action": "monitor_answer"},
                    mandatory=True,
                    description="Answering SIP call"
                )
                if not step_ok:
                    sys.exit(1)
                new_last_incall_log_time = now_ts

            elif pattern_state == "DISCONNECTED" and new_state in (CallState.RINGING, CallState.CONNECTING, CallState.ANSWERED):
                old_state = new_state
                new_state = CallState.DISCONNECTED
                duration = (now_ts - start_time) if start_time else 0
                reason_str = "Rejected" if old_state != CallState.ANSWERED else "Ended"
                print(colorize("DISCONNECTED", f"{now_str} | {line.strip()} | {reason_str} after {duration:.1f}s"))
                step_ok = execute_step(
                    step_name="sip_manager.hangup_call",
                    step_func=sip_manager.hangup_call,
                    step_params={},
                    operation_context={"operation_name": "call_mode", "action": "monitor_disconnect"},
                    mandatory=True,
                    description="Disconnecting SIP call"
                )
                if not step_ok:
                    sys.exit(1)
                new_state = CallState.IDLE
                start_time = None
                new_last_incall_log_time = 0.0
            break
    else:
        if any(k in line.lower() for k in ["tgvoip", "webrtc_voice_engine", "encryptedconnection", "reflectorport", "audioflinger", "audiomanager"]):
            if "createTrack_l(): mismatch" not in line:
                print(colorize("IDLE", f"{now_str} | Unmatched (Potential state?): {line.strip()}"))

    if new_state in (CallState.RINGING, CallState.CONNECTING) and start_time and (now_ts - start_time) > 30.0:
        print(colorize("DISCONNECTED", f"{now_str} | TIMEOUT after 30s, resetting to IDLE"))
        step_ok = execute_step(
            step_name="sip_manager.hangup_call",
            step_func=sip_manager.hangup_call,
            step_params={},
            operation_context={"operation_name": "call_mode", "action": "monitor_timeout"},
            mandatory=True,
            description="Timeout disconnect"
        )
        if not step_ok:
            sys.exit(1)
        new_state = CallState.IDLE
        start_time = None
        new_last_incall_log_time = 0.0

    return new_state, start_time, new_last_incall_log_time

def monitor_telegram_calls(
    sip_manager: SipManager,
    emulator_port: str = None,
    output_file: str = None,
    call_id: str = None
):
    proc, used_call_id = start_logcat(emulator_port, call_id=call_id)
    if not output_file:
        ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = f"tg_voip_raw_{ts_str}.log"

    print(colorize("IDLE", f"[*] Monitoring Telegram calls - Logging to: {output_file}"))

    current_state = CallState.IDLE
    call_start_time = None
    last_incall_log_time = 0.0

    try:
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            for line in proc.stdout:
                f.write(line)
                f.flush()
                current_state, call_start_time, last_incall_log_time = process_log_line(
                    line,
                    current_state,
                    call_start_time,
                    sip_manager,
                    last_incall_log_time,
                    used_call_id
                )
    except KeyboardInterrupt:
        print(colorize("IDLE", "[*] KeyboardInterrupt => stopping logcat."))
    finally:
        proc.terminate()
        proc.wait()
        print(colorize("IDLE", f"[*] Logcat stopped. Output saved to: {output_file}"))
