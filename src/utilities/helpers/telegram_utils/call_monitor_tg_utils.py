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
Integrates with SipManager to answer or disconnect SIP calls.
Logs states clearly as [CALL_STATE] RINGING, etc., in both console and log file.
Enhanced with debugging to identify missing state transitions.
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
    "DEBUG": "\033[90m",        # Gray for debug messages
    "RESET": "\033[0m"          # Reset to default
}

def colorize(state_str: str, message: str) -> str:
    """
    Wraps a given message in ANSI color codes based on the state name.
    """
    color = COLOR_CODES.get(state_str, COLOR_CODES["RESET"])
    return f"{color}[{state_str}] {message}{COLOR_CODES['RESET']}"

TAGS = [
    "tgvoip:V", "tgvoip:D", "tgvoip:I", "tgvoip:W", "tgvoip:E",
    "MediaFocusControl:I", "MediaFocusControl:D",
    "AudioManager:I", "AudioManager:D",
    "Telecom:I", "Telecom:D", "Telecom:V",
    "VoIPService:D", "VoIPService:I",
    "VoIPBaseService:D",
    "VoIPController:D",
    "CallAudioRouteStateMachine:I",
    "ConnectionService:D",
    "AudioService:I",
    "AudioFlinger:D", "AudioFlinger:I",
    "ActivityTaskManager:I", "ActivityManager:I", "ActivityManager:D",
    "webrtc_voice_engine:I", "webrtc_voice_engine:D",
    "EncryptedConnection:I", "EncryptedConnection:D",
    "ReflectorPort:I", "ReflectorPort:D", "ReflectorPort:W"
]

PATTERNS = {
    "RINGING": re.compile(
        r"(?P<event>START\s+u0\s+\{act=voip.*cmp=org\.telegram\.messenger/.*|"
        r"tgvoip.*(Initiating call|Call ringing|set network type:.*active interface)|"
        r"Telecom.*(INCOMING_CALL|CALL_RINGING))",
        re.IGNORECASE
    ),
    "CONNECTING": re.compile(
        r"(?P<event>requestAudioFocus.*USAGE_VOICE_COMMUNICATION|"
        r"VoIPService.*startOutgoingCall|"
        r"Telecom.*NEW_OUTGOING_CALL|"
        r"tgvoip.*(Connecting|Starting connection|Bound to local UDP port|Receive thread starting|Sending UDP ping)|"
        r"webrtc_voice_engine.*(AddSendStream|AddRecvStream|SetSenderParameters|SetReceiverParameters)|"
        r"EncryptedConnection.*(SEND:empty|processSignalingData)|"
        r"ReflectorPort.*(sending ping)|"
        r"ActivityManager.*(Starting activity: Intent.*org\.telegram\.messenger))",
        re.IGNORECASE
    ),
    "ANSWERED": re.compile(
        r"(?P<event>tgvoip.*(First audio packet - setting state to ESTABLISHED|Call state changed to 3|Call established|Call connected)|"
        r"AudioFlinger.*(thread.*ready to run|Track created successfully|start output|audio stream started)|"
        r"AudioManager.*MODE_IN_COMMUNICATION|"
        r"MediaFocusControl.*AUDIOFOCUS_GAIN)",
        re.IGNORECASE
    ),
    "DISCONNECTED": re.compile(
        r"(?P<event>abandonAudioFocus|"
        r"tgvoip.*(Call ended|Call rejected|Call terminated)|"
        r"Telecom.*(CALL_DISCONNECTED|CALL_REJECTED)|"
        r"MediaFocusControl.*AUDIOFOCUS_LOSS|"
        r"AudioManager.*MODE_NORMAL)",
        re.IGNORECASE
    )
}

def write_state_to_log(file, state: str, timestamp: str, duration: float = None, event: str = None):
    """
    Write call state to the log file in a clear format.
    """
    state_line = f"[{timestamp}] [CALL_STATE] {state}"
    if duration is not None:
        state_line += f" (Duration: {duration:.1f}s)"
    if event:
        state_line += f" | {event}"
    file.write(f"{state_line}\n")
    file.flush()

def start_logcat(emulator_port=None, call_id=None):
    """
    Starts a filtered ADB logcat process for certain tags.
    Returns:
        - proc: The subprocess.Popen object for the logcat process
        - call_id_to_use: The chosen call_id for logging
    """
    if call_id:
        call_id_to_use = call_id
    else:
        call_id_to_use = f"monitor-{emulator_port}" if emulator_port else "monitor-unknown"

    trace_logger = logger.bind(call_trace=True, call_id=call_id_to_use)
    trace_logger.info("[start_logcat] Clearing old logcat buffers.")

    adb_cmd = ["adb"]
    if emulator_port:
        adb_cmd += ["-s", f"emulator-{emulator_port}"]
    try:
        subprocess.run(adb_cmd + ["logcat", "-c"], check=True, capture_output=True, text=True)
        trace_logger.info("[start_logcat] Logcat buffers cleared successfully")
    except subprocess.CalledProcessError as e:
        trace_logger.error(f"[start_logcat] Failed to clear logcat buffers: {e.stderr}")
        raise

    trace_logger.info("[start_logcat] Starting new logcat process with tags: %s", TAGS)
    cmd = adb_cmd + ["logcat", "-v", "time", "-s"] + TAGS
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        trace_logger.info("[start_logcat] Logcat process started with PID %s", proc.pid)
        return proc, call_id_to_use
    except Exception as e:
        trace_logger.error(f"[start_logcat] Failed to start logcat: {str(e)}")
        raise

def process_log_line(line, current_state, start_time, sip_manager: SipManager,
                     last_incall_log_time: float, call_id: str):
    """
    Parses each log line to detect call state transitions.
    Logs states clearly as [CALL_STATE] RINGING, etc., in console and log file.
    Returns (new_state, new_start_time, new_last_incall_log_time).
    """
    trace_logger = logger.bind(call_trace=True, call_id=call_id)
    new_state = current_state
    now_ts = time.time()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_last_incall_log_time = last_incall_log_time

    # Check for state transitions
    for pattern_state, regex in PATTERNS.items():
        match = regex.search(line)
        if match:
            event = match.group("event")
            # RINGING
            if pattern_state == "RINGING" and new_state == CallState.IDLE:
                new_state = CallState.RINGING
                start_time = now_ts
                msg = f"{now_str} | {event}"
                print(colorize("RINGING", msg))
                write_state_to_log(f, "RINGING", now_str, event=event)
                trace_logger.info(f"[CALL_STATE] RINGING | {event}")

            # CONNECTING
            elif pattern_state == "CONNECTING" and new_state in (CallState.IDLE, CallState.RINGING):
                new_state = CallState.CONNECTING
                start_time = start_time or now_ts
                msg = f"{now_str} | {event}"
                print(colorize("CONNECTING", msg))
                write_state_to_log(f, "CONNECTING", now_str, event=event)
                trace_logger.info(f"[CALL_STATE] CONNECTING | {event}")

            # ANSWERED
            elif pattern_state == "ANSWERED" and new_state in (CallState.RINGING, CallState.CONNECTING):
                new_state = CallState.ANSWERED
                duration = (now_ts - start_time) if start_time else 0
                msg = f"{now_str} | {event} | Connected after {duration:.1f}s"
                print(colorize("ANSWERED", msg))
                write_state_to_log(f, "ANSWERED", now_str, duration, event)
                trace_logger.info(f"[CALL_STATE] ANSWERED | {event} (Duration: {duration:.1f}s)")

                step_ok = execute_step(
                    step_name="sip_manager.answer_call",
                    step_func=sip_manager.answer_call,
                    step_params={},
                    operation_context={"operation_name": "call_mode", "action": "monitor_answer"},
                    mandatory=True,
                    description="Answering SIP call"
                )
                if not step_ok:
                    trace_logger.error("[call_monitor] answer_call failed => exit")
                    sys.exit(1)

                new_last_incall_log_time = now_ts

            # DISCONNECTED
            elif pattern_state == "DISCONNECTED" and new_state in (CallState.RINGING, CallState.CONNECTING, CallState.ANSWERED):
                old_state = new_state
                new_state = CallState.DISCONNECTED
                duration = (now_ts - start_time) if start_time else 0
                reason_str = "Rejected" if old_state != CallState.ANSWERED else "Ended"
                msg = f"{now_str} | {event} | {reason_str} after {duration:.1f}s"
                print(colorize("DISCONNECTED", msg))
                write_state_to_log(f, "DISCONNECTED", now_str, duration, event)
                trace_logger.info(f"[CALL_STATE] DISCONNECTED | {event} ({reason_str}, Duration: {duration:.1f}s)")

                step_ok = execute_step(
                    step_name="sip_manager.hangup_call",
                    step_func=sip_manager.hangup_call,
                    step_params={},
                    operation_context={"operation_name": "call_mode", "action": "monitor_disconnect"},
                    mandatory=True,
                    description="Disconnecting SIP call"
                )
                if not step_ok:
                    trace_logger.error("[call_monitor] hangup_call failed => exit")
                    sys.exit(1)

                new_state = CallState.IDLE
                start_time = None
                new_last_incall_log_time = 0.0

            break
    else:
        # Log unmatched lines with relevant keywords
        relevant_keywords = [
            "tgvoip", "webrtc_voice_engine", "encryptedconnection", "reflectorport",
            "audioflinger", "audiomanager", "telecom", "voipservice", "activitymanager"
        ]
        if any(k in line.lower() for k in relevant_keywords):
            if "createTrack_l(): mismatch" not in line:
                print(colorize("DEBUG", f"{now_str} | Unmatched (Potential state?): {line.strip()}"))
                trace_logger.debug(f"[call_monitor] Unmatched line with relevant keywords: {line.strip()}")

    # Debug prolonged CONNECTING state
    if new_state == CallState.CONNECTING and start_time and (now_ts - start_time) > 10.0:
        duration = now_ts - start_time
        print(colorize("DEBUG", f"{now_str} | Still in CONNECTING after {duration:.1f}s"))
        trace_logger.debug(f"[call_monitor] Still in CONNECTING after {duration:.1f}s")

    # Handle timeout after 30 seconds in RINGING/CONNECTING
    if new_state in (CallState.RINGING, CallState.CONNECTING) and start_time and (now_ts - start_time) > 30.0:
        duration = now_ts - start_time
        msg = f"{now_str} | Timeout after 30s"
        print(colorize("DISCONNECTED", msg))
        write_state_to_log(f, "DISCONNECTED", now_str, duration, "Timeout")
        trace_logger.info(f"[CALL_STATE] DISCONNECTED | Timeout after 30s")

        step_ok = execute_step(
            step_name="sip_manager.hangup_call",
            step_func=sip_manager.hangup_call,
            step_params={},
            operation_context={"operation_name": "call_mode", "action": "monitor_timeout"},
            mandatory=True,
            description="Timeout disconnect"
        )
        if not step_ok:
            trace_logger.error("[call_monitor] timeout hangup_call failed => exit")
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
    """
    Monitors Telegram calls by reading ADB logcat for certain tags.
    Logs state transitions clearly as [CALL_STATE] RINGING, etc., in console and log file.
    """
    trace_logger = logger.bind(call_trace=True, call_id=call_id or f"monitor-{emulator_port}")
    trace_logger.info("[monitor_telegram_calls] Starting call monitoring for emulator_port=%s, call_id=%s", emulator_port, call_id)

    # Verify emulator is running
    adb_cmd = ["adb"]
    if emulator_port:
        adb_cmd += ["-s", f"emulator-{emulator_port}"]
    try:
        result = subprocess.run(adb_cmd + ["shell", "getprop ro.boot.emulator"], capture_output=True, text=True, timeout=5)
        trace_logger.info("[monitor_telegram_calls] Emulator check: %s", result.stdout.strip() or "No output")
    except subprocess.SubprocessError as e:
        trace_logger.error(f"[monitor_telegram_calls] Emulator check failed: {str(e)}")
        return

    # Check if Telegram is in foreground
    try:
        result = subprocess.run(adb_cmd + ["shell", "dumpsys activity | grep mResumedActivity"], capture_output=True, text=True, timeout=5)
        is_foreground = "org.telegram.messenger" in result.stdout
        trace_logger.info("[monitor_telegram_calls] Telegram in foreground: %s", is_foreground)
    except subprocess.SubprocessError as e:
        trace_logger.error(f"[monitor_telegram_calls] Telegram foreground check failed: {str(e)}")

    proc, used_call_id = start_logcat(emulator_port, call_id=call_id)

    if not output_file:
        ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = f"tg_voip_raw_{ts_str}.log"

    trace_logger.info(f"[monitor_telegram_calls] START, logging to {output_file}")
    print(colorize("IDLE", f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Logging started, saving to {output_file}"))

    current_state = CallState.IDLE
    call_start_time = None
    last_incall_log_time = 0.0

    try:
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            for line in proc.stdout:
                f.write(line)
                current_state, call_start_time, last_incall_log_time = process_log_line(
                    line,
                    current_state,
                    call_start_time,
                    sip_manager,
                    last_incall_log_time,
                    used_call_id
                )
    except KeyboardInterrupt:
        trace_logger.info("[monitor_telegram_calls] KeyboardInterrupt => stopping logcat.")
    except Exception as e:
        trace_logger.error(f"[monitor_telegram_calls] Error in logcat processing: {str(e)}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        trace_logger.info(f"[monitor_telegram_calls] STOP => saved logs to {output_file}")
        print(colorize("IDLE", f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Logcat stopped, saved to {output_file}"))