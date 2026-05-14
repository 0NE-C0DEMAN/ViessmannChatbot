"""Entry point: Flask chat server.

Run from the repository root:

    python chat_server.py

Listens on `CHAT_PORT` (default 8081). Implementation lives in
`viessmann_rag/chat/server.py`.
"""
from viessmann_rag.chat.server import run

if __name__ == "__main__":
    run()
