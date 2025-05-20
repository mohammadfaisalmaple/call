#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
baresip_utils.py
----------------
Manages SIP communication using baresip CLI soft-phone.
Requires environment variables: ASTERISK_HOST, ASTERISK_PORT, SIP_TRANSPORT_PORT,
USER_INFO_FILE, PULSE_SINK, PULSE_SOURCE, BARESIP_BIN.
"""

from __future__ import annotations
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional
import socket

from infrastructure.logging.logger import logger
from utilities.state_management.state_manager import log_state

class BaresipManager:
    def __init__(self, node_id: str, user_id: str, instance_id: str):
        self.node_id = node_id
        self.user_id = user_id
        self.instance_id = instance_id
        self.asterisk_host = os.getenv("ASTERISK_HOST") or ""
        self.asterisk_port = os.getenv("ASTERISK_PORT") or ""
        self.local_sip_port = os.getenv("SIP_TRANSPORT_PORT") or ""
        self.pulse_sink = os.getenv("PULSE_SINK") or ""
        self.pulse_source = os.getenv("PULSE_SOURCE") or ""
        user_info_file = os.getenv("USER_INFO_FILE") or ""
        self.baresip_bin = os.getenv("BARESIP_BIN") or ""
        if not all([self.asterisk_host, self.asterisk_port, self.local_sip_port,
                    self.pulse_sink, self.pulse_source, user_info_file, self.baresip_bin]):
            raise ValueError("Missing required environment variables")
        self.username, self.password = self._parse_user_info(user_info_file)
        self.cfg_dir = Path.home() / ".baresip"
        self.proc: Optional[subprocess.Popen] = None
        self.stdout_thread: Optional[threading.Thread] = None
        self.cmd_fifo: Optional[str] = None  # Changed to str for ctrl_tcp
        self.running = False
        self.current_call_id: Optional[str] = None
        self.registered = False

    def start(self) -> None:
        """Start Baresip process."""
        if self.running:
            logger.warning("[BaresipManager] Already running")
            return
        self._ensure_config()
        self._spawn_process()
        self.running = True
        self.stdout_thread = threading.Thread(target=self._stdout_reader, name="baresip-stdout", daemon=True)
        self.stdout_thread.start()
        log_state(
            state_code="SIP_ENDPOINT_START", operation="call_mode", action="start_endpoint",
            status="success", details=self._details(), description="baresip endpoint started"
        )

    def stop(self) -> None:
        """Stop Baresip process."""
        if not self.running:
            return
        try:
            self._send_cmd("quit")
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        self.running = False
        self.proc = None
        log_state(
            state_code="SIP_ENDPOINT_STOP", operation="call_mode", action="stop_endpoint",
            status="success", details=self._details(), description="baresip stopped"
        )

    def ensure_connected(self, timeout: int = 10) -> bool:
        """Ensure Baresip is registered with Asterisk."""
        if not self.running:
            self.start()
        return self.wait_registered(timeout=timeout)

    def is_registered(self) -> bool:
        """Check if registered with Asterisk."""
        return self.registered

    def wait_registered(self, timeout: int = 10) -> bool:
        """Wait for registration with Asterisk."""
        for _ in range(timeout * 10):
            if self.registered:
                return True
            time.sleep(0.1)
        return False

    def answer_call(self) -> None:
        """Answer incoming call."""
        if not self.current_call_id:
            logger.warning("[BaresipManager] No current call to answer")
            return
        logger.info("[BaresipManager] Sending answer command for call_id=%s", self.current_call_id)
        self._send_cmd("answer")
        log_state(
            state_code="SIP_CALL_ANSWERED", operation="call_mode", action="answer_call",
            status="success", details=self._details(), description="call answered via baresip"
        )

    def hangup_call(self) -> None:
        """Hang up current call."""
        self._send_cmd("hangup")

    def wait_incoming_call_end(self) -> bool:
        """Wait for incoming call to end."""
        while self.running and self.current_call_id is None:
            logger.info(f"check call id is None : {self.running and self.current_call_id is None}")
            time.sleep(0.5)
        if not self.running:
            logger.info(f"not running")
            return False
        while self.running and self.current_call_id is not None:
            logger.info(f"check call id is not None : {self.running and self.current_call_id is not None}")
            time.sleep(0.5)
        return True

    def _details(self) -> dict:
        return {
            "node_id": self.node_id, "user_id": self.user_id, "instance_id": self.instance_id,
            "sip_uri": f"sip:{self.username}@{self.asterisk_host}:{self.asterisk_port}"
        }

    def _ensure_config(self) -> None:
        """Ensure Baresip config and accounts files exist."""
        self.cfg_dir.mkdir(exist_ok=True)
        acc_path = self.cfg_dir / "accounts"
        if not acc_path.exists():
            account_line = (
                f"<sip:{self.username}@{self.asterisk_host}:{self.asterisk_port}>;"
                f"auth_user={self.username};auth_pass={self.password};answermode=manual;regint=60"
            )
            acc_path.write_text(account_line + "\n", encoding="utf-8")
        cfg_path = self.cfg_dir / "config"
        if not cfg_path.exists():
            cfg_path.write_text("\n", encoding="utf-8")
        cfg_lines = cfg_path.read_text().splitlines()

        def _set(key: str, value: str) -> None:
            prefix = key + "\t"
            for i, line in enumerate(cfg_lines):
                if line.startswith(prefix):
                    cfg_lines[i] = f"{prefix}{value}"
                    break
            else:
                cfg_lines.append(f"{prefix}{value}")

        _set("sip_listen", f"0.0.0.0:{self.local_sip_port}")
        _set("ctrl_tcp_listen", "127.0.0.1:4444")
        _set("audio_source", f"pulse,{self.pulse_source}")
        _set("audio_player", f"pulse,{self.pulse_sink}")
        cfg_path.write_text("\n".join(cfg_lines) + "\n", encoding="utf-8")

    
     

    def _spawn_process(self) -> None:
        """Spawn Baresip subprocess."""
        env = os.environ.copy()
        env["BARESIP_HOME"] = str(self.cfg_dir)
        env["LD_LIBRARY_PATH"] = "/usr/local/lib/baresip/modules:" + env.get("LD_LIBRARY_PATH", "")
        cmd = [self.baresip_bin, "-f", str(self.cfg_dir), "-m", "ctrl_tcp"]
        logger.info("[BaresipManager] Starting baresip with command: %s", cmd)
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env
        )
        logger.info("[BaresipManager] Started PID %s", self.proc.pid)
        
        # التحقق من واجهة ctrl_tcp
        for _ in range(100):  # 10 ثوانٍ
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1)
                s.connect(("127.0.0.1", 4444))
                s.close()
                logger.info("[BaresipManager] ctrl_tcp connected at 127.0.0.1:4444")
                self.cmd_fifo = "tcp:127.0.0.1:4444"
                break
            except (ConnectionRefusedError, socket.timeout) as e:
                logger.debug("[BaresipManager] ctrl_tcp not yet available: %s", str(e))
                time.sleep(0.1)
            finally:
                s.close()
        else:
            logger.error("[BaresipManager] Failed to connect to ctrl_tcp after retries")
            if self.proc:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            raise RuntimeError("ctrl_tcp not available for baresip")
        
        # قراءة إخراج baresip للتحقق من التسجيل
        output = ""
        for _ in range(300):  # قراءة لمدة 30 ثانية
            line = self.proc.stdout.readline() if self.proc.stdout else ""
            if not line:
                break
            output += line
            if "useragent registered successfully" in line.lower():
                logger.info("[BaresipManager] SIP registration successful: %s", line.strip())
                self.registered = True
            elif "connection timed out" in line.lower():
                logger.error("[BaresipManager] SIP registration failed: %s", line.strip())
            elif "ctrl_tcp" in line.lower():
                logger.error("[BaresipManager] ctrl_tcp error: %s", line.strip())
        logger.debug("[BaresipManager] baresip initial output: %s", output)

    def _stdout_reader(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            logger.debug("[baresip] RAW OUTPUT: %s", line)  # تسجيل كل سطر خام
            self._parse_event(line)
            if not self.running:
                break
            time.sleep(0.1)
            
    def _parse_event(self, line: str) -> None:
        """Parse Baresip output events."""
        logger.debug("***** [_parse_event] ***")  # تسجيل كل سطر خام
        lower = line.lower()
        if "registered" in lower and "ua" in lower:
            self.registered = True
            log_state(
                state_code="SIP_REGISTRATION_OK", operation="call_mode", action="register_sip_account",
                status="success", details=self._details(), description="Account registered with Asterisk"
            )
        elif "incoming" in lower and "call" in lower:
            parts = line.split()
            self.current_call_id = parts[1] if len(parts) > 1 else "unknown"
            log_state(
                state_code="SIP_CALL_INCOMING", operation="call_mode", action="incoming_detect",
                status="initiated", details=self._details(), description="incoming call detected"
            )
            # Auto-answer the incoming call
            self.answer_call()
        elif "answered" in lower:
            log_state(
                state_code="SIP_CALL_CONFIRMED", operation="call_mode", action="call_confirmed",
                status="success", details=self._details(), description="call answered/confirmed"
            )
        elif "closed" in lower and "call" in lower:
            self.current_call_id = None
            log_state(
                state_code="SIP_CALL_DISCONNECTED", operation="call_mode", action="call_closed",
                status="success", details=self._details(), description="call closed"
            )

    def _send_cmd(self, cmd: str) -> None:
        """Send command to Baresip via TCP socket."""
        if not self.cmd_fifo or not self.cmd_fifo.startswith("tcp:"):
            logger.error("[BaresipManager] ctrl_tcp not ready")
            return
        try:
            host, port = self.cmd_fifo.replace("tcp:", "").split(":")
            port = int(port)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect((host, port))
                s.sendall((cmd + "\n").encode("utf-8"))
        except Exception as exc:
            logger.exception("[BaresipManager] TCP write failed: %s", exc)

    @staticmethod
    def _parse_user_info(filepath: str) -> tuple[str, str]:
        """Parse SIP_USERNAME and SIP_PASSWORD from user_info file."""
        lines = Path(filepath).read_text().splitlines()
        username, password = None, None
        for line in lines:
            if line.startswith("SIP_USERNAME="):
                username = line.split("=", 1)[1].strip()
            elif line.startswith("SIP_PASSWORD="):
                password = line.split("=", 1)[1].strip()
        if not username or not password:
            raise ValueError("Missing SIP_USERNAME or SIP_PASSWORD in USER_INFO_FILE")
        return username, password