import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QSettings, QPoint
from PySide6.QtGui import QAction, QCursor, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMenu,
    QProgressBar,
    QSystemTrayIcon,
    QStyle,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
)

APP_NAME = "Codex Limits Overlay"
POLL_INTERVAL_MS = 20_000


class CodexRpcError(RuntimeError):
    pass


class CodexAppServerClient:
    def __init__(self):
        self.proc = None
        self.next_id = 1
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.stderr_tail = []
        self.alive = False
        self._start()
        self._initialize()

    def _find_codex_command(self):
        codex = shutil.which("codex.cmd") or shutil.which("codex.exe") or shutil.which("codex")
        if not codex:
            raise CodexRpcError("Не найден codex в PATH. Проверь: where codex")
        return codex

    def _start(self):
        codex = self._find_codex_command()
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        if codex.lower().endswith((".cmd", ".bat")):
            comspec = os.environ.get("ComSpec", "cmd.exe")
            args = [comspec, "/d", "/s", "/c", f'call "{codex}" app-server']
        else:
            args = [codex, "app-server"]

        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self.alive = True

        threading.Thread(target=self._stdout_reader, daemon=True).start()
        threading.Thread(target=self._stderr_reader, daemon=True).start()

    def _initialize(self):
        init_id = 0
        with self.pending_lock:
            event = threading.Event()
            self.pending[init_id] = {"event": event, "msg": None}

        self._send({
            "method": "initialize",
            "id": init_id,
            "params": {
                "clientInfo": {
                    "name": "codex_limits_overlay",
                    "title": "Codex Limits Overlay",
                    "version": "0.1.0",
                }
            },
        })
        self._send({"method": "initialized", "params": {}})

        msg = self._wait_for(init_id, timeout=15)
        if "error" in msg:
            raise CodexRpcError(f"initialize error: {msg['error']}")

    def _stdout_reader(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue

                msg_id = msg.get("id")
                if msg_id is not None:
                    with self.pending_lock:
                        slot = self.pending.get(msg_id)
                        if slot:
                            slot["msg"] = msg
                            slot["event"].set()
        finally:
            self.alive = False

    def _stderr_reader(self):
        try:
            for line in self.proc.stderr:
                line = line.strip()
                if line:
                    self.stderr_tail.append(line)
                    self.stderr_tail = self.stderr_tail[-8:]
        finally:
            pass

    def _send(self, obj):
        if not self.proc or self.proc.poll() is not None:
            raise CodexRpcError("codex app-server не запущен")

        data = json.dumps(obj, ensure_ascii=False) + "\n"
        with self.write_lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def _wait_for(self, msg_id, timeout=15):
        with self.pending_lock:
            slot = self.pending.get(msg_id)

        if not slot:
            raise CodexRpcError(f"internal error: pending id {msg_id} not found")

        if not slot["event"].wait(timeout):
            stderr = "\n".join(self.stderr_tail)
            raise CodexRpcError(f"timeout waiting for response id={msg_id}\n{stderr}")

        with self.pending_lock:
            msg = slot["msg"]
            self.pending.pop(msg_id, None)

        return msg

    def request(self, method, params=None, timeout=20):
        with self.pending_lock:
            msg_id = self.next_id
            self.next_id += 1
            event = threading.Event()
            self.pending[msg_id] = {"event": event, "msg": None}

        payload = {"method": method, "id": msg_id}
        if params is not None:
            payload["params"] = params

        self._send(payload)
        msg = self._wait_for(msg_id, timeout=timeout)

        if "error" in msg:
            raise CodexRpcError(f"{method} error: {msg['error']}")

        return msg.get("result", {})

    def close(self):
        self.alive = False
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


class Overlay(QWidget):
    data_ready = Signal(dict)
    error_ready = Signal(str)

    def __init__(self):
        super().__init__()

        self.client = None
        self.refreshing = False
        self.drag_pos = None
        self.settings = QSettings("ti-watsky", "codex-limits-overlay")

        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self.root = QWidget(self)
        self.root.setObjectName("root")

        self.title_label = QLabel("Codex limits")
        self.title_label.setObjectName("title")

        self.status_label = QLabel("запуск…")
        self.status_label.setObjectName("muted")

        self.account_label = QLabel("")
        self.account_label.setObjectName("account")

        self.buckets = QVBoxLayout()
        self.buckets.setSpacing(5)
        self.buckets.setContentsMargins(0, 2, 0, 0)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.title_label)
        header.addStretch(1)
        header.addWidget(self.status_label)

        layout = QVBoxLayout(self.root)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)
        layout.addLayout(header)
        layout.addWidget(self.account_label)
        layout.addLayout(self.buckets)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.root)

        self.setStyleSheet("""
            QWidget#root {
                background: rgba(22, 22, 24, 230);
                border: 1px solid rgba(255, 255, 255, 35);
                border-radius: 10px;
            }
            QLabel {
                color: #eeeeee;
                font-family: Segoe UI;
                font-size: 12px;
            }
            QLabel#title {
                color: #ffffff;
                font-weight: 650;
                font-size: 12px;
            }
            QLabel#muted {
                color: #9a9a9a;
                font-size: 10px;
            }
            QLabel#account {
                color: #bdbdbd;
                font-size: 10px;
            }
            QProgressBar {
                height: 5px;
                border: none;
                border-radius: 2px;
                background: rgba(255, 255, 255, 35);
                text-align: center;
            }
            QProgressBar::chunk {
                border-radius: 2px;
                background: #d6d6d6;
            }
        """)

        self.resize(255, 70)
        self.restore_position()

        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))

        menu = QMenu()
        show_action = QAction("Show / Hide", self)
        show_action.triggered.connect(self.toggle_visible)
        refresh_action = QAction("Refresh now", self)
        refresh_action.triggered.connect(self.refresh)
        restart_action = QAction("Restart connection", self)
        restart_action.triggered.connect(self.restart_connection)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit_app)

        menu.addAction(show_action)
        menu.addAction(refresh_action)
        menu.addAction(restart_action)
        menu.addSeparator()
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

        self.timer = QTimer(self)
        self.timer.setInterval(POLL_INTERVAL_MS)
        self.timer.timeout.connect(self.refresh)

        self.data_ready.connect(self.apply_data)
        self.error_ready.connect(self.apply_error)

        QTimer.singleShot(150, self.refresh)
        self.timer.start()

    def restore_position(self):
        x = self.settings.value("x", None)
        y = self.settings.value("y", None)
        if x is not None and y is not None:
            self.move(int(x), int(y))
        else:
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(screen.right() - self.width() - 30, screen.top() + 80)

    def save_position(self):
        self.settings.setValue("x", self.x())
        self.settings.setValue("y", self.y())

    def clear_buckets(self):
        while self.buckets.count():
            item = self.buckets.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def make_bucket_widget(self, name, bucket):
        primary = bucket.get("primary") or {}
        used = primary.get("usedPercent")
        duration = primary.get("windowDurationMins")
        resets_at = primary.get("resetsAt")

        if used is None:
            text = f"{name}: нет данных"
            percent = 0
        else:
            percent = max(0, min(100, int(round(float(used)))))
            left = max(0, 100 - percent)
            reset_text = "?"
            if resets_at:
                reset_text = datetime.fromtimestamp(int(resets_at)).strftime("%H:%M")
            dur_text = f"{duration}m" if duration else "?"
            text = f"{name}: {percent}% used · {left}% left · reset {reset_text} · {dur_text}"

        box = QWidget()
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(0, 0, 0, 0)
        box_layout.setSpacing(2)

        label = QLabel(text)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(percent)
        bar.setTextVisible(False)

        box_layout.addWidget(label)
        box_layout.addWidget(bar)
        return box

    def get_client(self):
        if self.client is None or not self.client.alive:
            if self.client:
                self.client.close()
            self.client = CodexAppServerClient()
        return self.client

    def refresh(self):
        if self.refreshing:
            return

        self.refreshing = True
        self.status_label.setText("refresh…")

        def worker():
            try:
                client = self.get_client()
                account = client.request("account/read", {"refreshToken": False}, timeout=20)
                limits = client.request("account/rateLimits/read", timeout=25)
                self.data_ready.emit({"account": account, "limits": limits})
            except Exception as exc:
                self.error_ready.emit(str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def apply_data(self, data):
        self.refreshing = False

        account_obj = (data.get("account") or {}).get("account") or {}
        email = account_obj.get("email") or "unknown account"
        plan = account_obj.get("planType")
        self.account_label.setText(f"{email}" + (f" · {plan}" if plan else ""))

        limits = data.get("limits") or {}
        by_id = limits.get("rateLimitsByLimitId")
        single = limits.get("rateLimits")

        self.clear_buckets()

        if isinstance(by_id, dict) and by_id:
            for limit_id, bucket in sorted(by_id.items()):
                name = bucket.get("limitName") or bucket.get("limitId") or limit_id
                self.buckets.addWidget(self.make_bucket_widget(name, bucket))
        elif isinstance(single, dict):
            name = single.get("limitName") or single.get("limitId") or "codex"
            self.buckets.addWidget(self.make_bucket_widget(name, single))
        else:
            label = QLabel("лимиты не вернулись")
            self.buckets.addWidget(label)

        credits = limits.get("credits")
        if credits:
            credits_label = QLabel(f"credits: {json.dumps(credits, ensure_ascii=False)[:120]}")
            credits_label.setObjectName("muted")
            self.buckets.addWidget(credits_label)

        self.status_label.setText(datetime.now().strftime("%H:%M:%S"))
        self.adjustSize()
        self.save_position()

    def apply_error(self, text):
        self.refreshing = False
        self.clear_buckets()
        self.account_label.setText("ошибка")
        msg = QLabel(text[:260])
        msg.setWordWrap(True)
        self.buckets.addWidget(msg)
        self.status_label.setText("error")
        self.adjustSize()

    def restart_connection(self):
        if self.client:
            self.client.close()
            self.client = None
        self.refresh()

    def toggle_visible(self):
        self.setVisible(not self.isVisible())

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.toggle_visible()

    def quit_app(self):
        self.save_position()
        if self.client:
            self.client.close()
        QApplication.quit()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        elif event.button() == Qt.RightButton:
            self.tray.contextMenu().popup(QCursor.pos())

    def mouseMoveEvent(self, event):
        if self.drag_pos and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_pos = None
        self.save_position()

    def closeEvent(self, event):
        event.ignore()
        self.hide()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    overlay = Overlay()
    overlay.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()