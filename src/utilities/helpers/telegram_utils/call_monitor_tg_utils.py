
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
call_monitor_tg_utils.py
------------------------
Monitors Telegram VoIP calls by reading ADB logcat for specific tags and detecting call state transitions:
    - RINGING
    - CONNECTING
    - ANSWERED
    - DISCONNECTED
Integrates with SipManager to answer or disconnect SIP calls.
Logs states clearly as [STATE] YYYY-MM-DD HH:MM:SS | MM-DD HH:MM:SS.mmm in console and log file.
Optimized for minimal output, excluding noise (e.g., WebRTC, emulator artifacts).
Supports UTC+3 timezone (Asia/Riyadh) and emulator environments.
"""

import subprocess
import re
import datetime
import time
import sys
from enum import Enum
from pathlib import Path
import pytz

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

# Monitored logcat tags (minimal set for Telegram VoIP)
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
    "ReflectorPort:I", "ReflectorPort:D", "ReflectorPort:W",
    # Added for debugging
    "*:I", "*:D"
]

# Regex patterns for call states (specific to Telegram VoIP)
PATTERNS = {
    "RINGING": re.compile(
        r"ActivityTaskManager.*START\s+u0\s+\{act=voip.*cmp=org\.telegram\.messenger/org\.telegram\.ui\.LaunchActivity\}|"
        r"tgvoip.*(Initiating call|Call ringing)",
        re.IGNORECASE
    ),
    "CONNECTING": re.compile(
        r"MediaFocusControl.*requestAudioFocus.*USAGE_VOICE_COMMUNICATION.*callingPack=org\.telegram\.messenger|"
        r"tgvoip.*(Connecting|Call state changed to 2|Starting connection)|"
        r"Telecom.*NEW_OUTGOING_CALL",
        re.IGNORECASE
    ),
    "ANSWERED": re.compile(
        r"tgvoip.*(First audio packet - setting state to ESTABLISHED|Call state changed to 3|Call established|Call connected)|"
        r"AudioFlinger.*thread.*ready to run",
        re.IGNORECASE
    ),
    "DISCONNECTED": re.compile(
        r"MediaFocusControl.*abandonAudioFocus.*callingPack=org\.telegram\.messenger|"
        r"tgvoip.*(Call ended|Call rejected|Call terminated|User-Initiated Abort)|"
        r"Telecom.*(CALL_DISCONNECTED|CALL_REJECTED)",
        re.IGNORECASE
    )
}

def write_state_to_log(file, state: str, timestamp: str, timestamp_raw: str):
    """
    Write call state to the log file in the format [STATE] YYYY-MM-DD HH:MM:SS | MM-DD HH:MM:SS.mmm.
    """
    state_line = f"[{state}] {timestamp} | {timestamp_raw}"
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
        # Verify logcat is running
        time.sleep(1)
        if proc.poll() is not None:
            stderr_output = proc.stderr.read() if proc.stderr else "No stderr"
            trace_logger.error(f"[start_logcat] Logcat process terminated early: {stderr_output}")
            raise RuntimeError("Logcat process failed to start")
        return proc, call_id_to_use
    except Exception as e:
        trace_logger.error(f"[start_logcat] Failed to start logcat: {str(e)}")
        raise

def process_log_line(line, current_state, start_time, sip_manager: SipManager,
                     last_incall_log_time: float, call_id: str, log_file):
    """
    Parses each log line to detect call state transitions.
    Logs states as [STATE] YYYY-MM-DD HH:MM:SS | MM-DD HH:MM:SS.mmm in console and log file.
    Returns (new_state, new_start_time, new_last_incall_log_time).
    """
    trace_logger = logger.bind(call_trace=True, call_id=call_id)
    new_state = current_state
    new_last_incall_log_time = last_incall_log_time

    # Skip lines without monitored tags or with noise
    if not any(tag in line for tag in TAGS) or any(s in line for s in ["android.hardware.audio", "com.android.systemui", "AudioManager.*Use of stream types is deprecated"]):
        return new_state, start_time, new_last_incall_log_time

    # Extract timestamp (e.g., "05-21 13:34:05.786")
    timestamp_match = re.search(r"(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})", line)
    if not timestamp_match:
        return new_state, start_time, new_last_incall_log_time
    timestamp_raw = timestamp_match.group(1)

    # Parse timestamp (assume 2025, convert to UTC+3)
    try:
        timestamp = datetime.datetime.strptime(f"2025-{timestamp_raw}", "%Y-%m-%d %H:%M:%S.%f")
        timezone = pytz.timezone("Asia/Riyadh")  # UTC+3
        timestamp = timezone.localize(timestamp)
        timestamp_str = timestamp.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return new_state, start_time, new_last_incall_log_time

    # Check for state transitions
    for pattern_state, regex in PATTERNS.items():
        match = regex.search(line)
        if match:
            now_ts = time.time()
            # RINGING
            if pattern_state == "RINGING" and new_state == CallState.IDLE:
                new_state = CallState.RINGING
                start_time = now_ts
                msg = f"{timestamp_str} | {timestamp_raw}"
                print(colorize("RINGING", msg))
                write_state_to_log(log_file, "RINGING", timestamp_str, timestamp_raw)
                trace_logger.info(f"[CALL_STATE] RINGING | {timestamp_raw}")

            # CONNECTING
            elif pattern_state == "CONNECTING" and new_state in (CallState.IDLE, CallState.RINGING):
                new_state = CallState.CONNECTING
                start_time = start_time or now_ts
                msg = f"{timestamp_str} | {timestamp_raw}"
                print(colorize("CONNECTING", msg))
                write_state_to_log(log_file, "CONNECTING", timestamp_str, timestamp_raw)
                trace_logger.info(f"[CALL_STATE] CONNECTING | {timestamp_raw}")

            # ANSWERED
            elif pattern_state == "ANSWERED" and new_state in (CallState.RINGING, CallState.CONNECTING):
                new_state = CallState.ANSWERED
                msg = f"{timestamp_str} | {timestamp_raw}"
                print(colorize("ANSWERED", msg))
                write_state_to_log(log_file, "ANSWERED", timestamp_str, timestamp_raw)
                trace_logger.info(f"[CALL_STATE] ANSWERED | {timestamp_raw}")

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
                new_state = CallState.DISCONNECTED
                msg = f"{timestamp_str} | {timestamp_raw}"
                print(colorize("DISCONNECTED", msg))
                write_state_to_log(log_file, "DISCONNECTED", timestamp_str, timestamp_raw)
                trace_logger.info(f"[CALL_STATE] DISCONNECTED | {timestamp_raw}")

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

    # Optional debugging for prolonged CONNECTING or timeouts (suppressed by default)
    """
    now_ts = time.time()
    if new_state == CallState.CONNECTING and start_time and (now_ts - start_time) > 10.0:
        duration = now_ts - start_time
        print(colorize("DEBUG", f"{timestamp_str} | Still in CONNECTING after {duration:.1f}s"))
        trace_logger.debug(f"[call_monitor] Still in CONNECTING after {duration:.1f}s")

    if new_state in (CallState.RINGING, CallState.CONNECTING) and start_time and (now_ts - start_time) > 60.0:
        duration = now_ts - start_time
        msg = f"{timestamp_str} | Timeout after 60s"
        print(colorize("DISCONNECTED", msg))
        write_state_to_log(log_file, "DISCONNECTED", timestamp_str, "Timeout")
        trace_logger.info(f"[CALL_STATE] DISCONNECTED | Timeout after 60s")

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
    """

    return new_state, start_time, new_last_incall_log_time

def monitor_telegram_calls(
    sip_manager: SipManager,
    emulator_port: str = None,
    output_file: str = None,
    call_id: str = None
):
    """
    Monitors Telegram calls by reading ADB logcat for certain tags.
    Logs state transitions as [STATE] YYYY-MM-DD HH:MM:SS | MM-DD HH:MM:SS.mmm in console and log file.
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
        if not result.stdout.strip():
            trace_logger.warning("[monitor_telegram_calls] Emulator check returned no output, ADB connection may be unstable")
    except subprocess.SubprocessError as e:
        trace_logger.error(f"[monitor_telegram_calls] Emulator check failed: {str(e)}")
        return

    # Check if Telegram is in foreground
    try:
        result = subprocess.run(adb_cmd + ["shell", "dumpsys activity | grep mResumedActivity"], capture_output=True, text=True, timeout=5)
        is_foreground = "org.telegram.messenger" in result.stdout
        trace_logger.info("[monitor_telegram_calls] Telegram in foreground: %s", is_foreground)
        if not is_foreground:
            trace_logger.warning("[monitor_telegram_calls] Telegram not in foreground, attempting to bring to foreground")
            subprocess.run(adb_cmd + ["shell", "am start -n org.telegram.messenger/org.telegram.ui.LaunchActivity"], capture_output=True, text=True)
            time.sleep(2)  # Wait for app to launch
    except subprocess.SubprocessError as e:
        trace_logger.error(f"[monitor_telegram_calls] Telegram foreground check failed: {str(e)}")

    proc, used_call_id = start_logcat(emulator_port, call_id=call_id)

    if not output_file:
        ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = f"tg_voip_states_{ts_str}.log"

    trace_logger.info(f"[monitor_telegram_calls] START, logging to {output_file}")
    print(colorize("IDLE", f"{datetime.datetime.now(pytz.timezone('Asia/Riyadh')).strftime('%Y-%m-%d %H:%M:%S')} | Logging started, saving to {output_file}"))

    current_state = CallState.IDLE
    call_start_time = None
    last_incall_log_time = 0.0

    try:
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            for line in proc.stdout:
                line = line.rstrip()
                f.write(line + "\n")
                current_state, call_start_time, last_incall_log_time = process_log_line(
                    line,
                    current_state,
                    call_start_time,
                    sip_manager,
                    last_incall_log_time,
                    used_call_id,
                    log_file=f
                )
    except KeyboardInterrupt:
        trace_logger.info("[monitor_telegram_calls] KeyboardInterrupt => stopping logcat")
        sip_manager.stop()
    except Exception as e:
        trace_logger.error(f"[monitor_telegram_calls] Error in logcat processing: {str(e)}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        trace_logger.info(f"[monitor_telegram_calls] STOP => saved logs to {output_file}")
        print(colorize("IDLE", f"{datetime.datetime.now(pytz.timezone('Asia/Riyadh')).strftime('%Y-%m-%d %H:%M:%S')} | Logcat stopped, saved to {output_file}"))
        sip_manager.stop()
