"""Desktop entry point used by the packaged application.

以原生桌面窗口(pywebview)承载界面:
- macOS 使用系统 WKWebView,Windows 使用 Edge WebView2
- 后台线程运行 FastAPI/uvicorn,主线程运行窗口事件循环
"""

import socket
import threading

import uvicorn
import webview

from app import app

WINDOW_TITLE = "Telegram 名片工具"
WINDOW_SIZE = (1180, 780)
MIN_SIZE = (980, 640)


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(port, ready_event: threading.Event):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    # 让 uvicorn 在窗口关闭时能够退出
    server.install_signal_handlers = lambda: None

    def _mark_ready():
        # 轮询等待端口就绪
        import time

        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)
        ready_event.set()

    threading.Thread(target=_mark_ready, daemon=True).start()
    server.run()


def main():
    port = find_free_port()
    ready = threading.Event()
    server_thread = threading.Thread(target=start_server, args=(port, ready), daemon=True)
    server_thread.start()
    ready.wait(timeout=15)

    window = webview.create_window(
        WINDOW_TITLE,
        f"http://127.0.0.1:{port}",
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=MIN_SIZE,
        background_color="#f4f6fb",
    )
    try:
        webview.start()
    finally:
        # 窗口关闭后退出整个进程
        import os
        import signal

        os.kill(os.getpid(), signal.SIGTERM)


if __name__ == "__main__":
    main()
