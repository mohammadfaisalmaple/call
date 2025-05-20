import socket
import time

HOST = '127.0.0.1'
PORT = 4444

print("⌛️ انتظار المكالمة ...")
time.sleep(10)  # امنح وقتًا كافيًا للمكالمة لتصل

print("📞 إرسال أمر الرد عبر TCP ...")
try:
    with socket.create_connection((HOST, PORT), timeout=10) as sock:
        sock.sendall(b'call accept\n')
        print("✅ تم إرسال أمر الرد.")
except Exception as e:
    print("❌ فشل الاتصال بـ baresip:", e)
