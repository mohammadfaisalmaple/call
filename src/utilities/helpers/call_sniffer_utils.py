import subprocess
import re
import datetime
import time
from enum import Enum
from pathlib import Path

class CallState(Enum):
    IDLE = "IDLE"
    RINGING = "RINGING"
    CONNECTING = "CONNECTING"
    ANSWERED = "ANSWERED"
    DISCONNECTED = "DISCONNECTED"

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
    "RINGING": re.compile(r"act=voip.*cmp=org\.telegram\.messenger/org\.telegram\.ui\.LaunchActivity", re.IGNORECASE),
    "CONNECTING": re.compile(r"requestAudioFocus.*USAGE_VOICE_COMMUNICATION|VoIPService.*startOutgoingCall", re.IGNORECASE),
    "ANSWERED": re.compile(r"AudioFlinger.*ready to run|AUDIOFOCUS_GAIN|Call established", re.IGNORECASE),
    "DISCONNECTED": re.compile(r"abandonAudioFocus|Call ended|AUDIOFOCUS_LOSS|MODE_NORMAL", re.IGNORECASE),
}

COLORS = {
    "RINGING": "\033[95m",
    "CONNECTING": "\033[93m",
    "ANSWERED": "\033[92m",
    "DISCONNECTED": "\033[91m",
    "DEBUG": "\033[90m",
    "RESET": "\033[0m"
}

def colorize(state, message):
    return f"{COLORS.get(state, '')}[{state}] {message}{COLORS['RESET']}"

def start_call_sniffer_process(emulator_port: str, output_file: str = None):
    """
    Starts a background logcat monitor for Telegram VoIP call states.
    Should be called BEFORE initiating the UI call.
    Returns the subprocess handle (you should terminate it later).
    """
    adb_cmd = ["adb"]
    if emulator_port:
        adb_cmd += ["-s", f"emulator-{emulator_port}"]
    subprocess.run(adb_cmd + ["logcat", "-c"], check=True)

    cmd = adb_cmd + ["logcat", "-v", "time", "-s"] + TAGS
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_file or f"tg_voip_sniff_{ts}.log"
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def _background_sniffer():
        state = CallState.IDLE
        start_time = None
        with open(output_file, "w", encoding="utf-8") as f:
            for line in proc.stdout:
                f.write(line)
                ts_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                line_lower = line.lower()
                for key, pattern in PATTERNS.items():
                    if pattern.search(line):
                        print(colorize(key, f"{ts_str} | {line.strip()}"))
                        break
                if "tgvoip" in line_lower or "webrtc" in line_lower:
                    if "createTrack_l(): mismatch" not in line:
                        print(colorize("DEBUG", f"{ts_str} | Unmatched? {line.strip()}"))

    import threading
    thread = threading.Thread(target=_background_sniffer, name="call-sniffer", daemon=True)
    thread.start()

    return proc
