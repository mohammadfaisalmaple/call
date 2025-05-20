import socket
import json

def send_command(cmd):
    msg_str = json.dumps(cmd)
    netstring = f"{len(msg_str)}:{msg_str},"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect(('127.0.0.1', 4444))
        s.sendall(netstring.encode())
        response = s.recv(4096)
        return response.decode()

dial_cmd = {
    "method": "dial",
    "params": {
        "account": "sip:200@135.181.130.186",
        "target": "sip:100@135.181.130.186"
    }
}

resp = send_command(dial_cmd)
print("Response:", resp)
