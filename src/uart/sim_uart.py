"""TCP-based UART simulator for testing without hardware."""
from __future__ import annotations

import socket
import threading

HOST = "127.0.0.1"
PORT = 33333
ACK = b"ACK\n"


def handle_client(conn, addr):
    print("[sim_uart] client:", addr)
    with conn:
        while True:
            data = conn.recv(512)
            if not data:
                break
            print("[sim_uart] recv:", data)
            conn.sendall(ACK)


def main() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen()
        print(f"[sim_uart] listening on {HOST}:{PORT}")
        while True:
            conn, addr = s.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    main()
