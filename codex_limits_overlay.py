import json
import locale
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QSettings, QPoint
from PySide6.QtGui import QAction, QColor, QCursor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
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
WINDOW_WIDTH = 250
CONTENT_MARGIN_X = 10
CONTENT_WIDTH = WINDOW_WIDTH - (CONTENT_MARGIN_X * 2)
TITLE_ICON_SIZE = 16


TEXT = {
    "ru": {
        "title": "Оставшийся лимит",
        "starting": "запуск…",
        "refreshing": "обновление…",
        "error": "ошибка",
        "unknown_account": "аккаунт неизвестен",
        "no_limits": "лимиты не вернулись",
        "five_hours": "5ч",
        "week": "Неделя",
        "show_hide": "Показать / скрыть",
        "refresh_now": "Обновить сейчас",
        "restart": "Переподключить",
        "quit": "Выйти",
    },
    "en": {
        "title": "Remaining limit",
        "starting": "starting…",
        "refreshing": "refresh…",
        "error": "error",
        "unknown_account": "unknown account",
        "no_limits": "no limits returned",
        "five_hours": "5h",
        "week": "Week",
        "show_hide": "Show / Hide",
        "refresh_now": "Refresh now",
        "restart": "Restart connection",
        "quit": "Quit",
    },
}

MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

MONTHS_EN = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def detect_russian_locale():
    candidates = []
    for key in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(key)
        if value:
            candidates.append(value)

    locale_categories = [locale.LC_CTYPE, locale.LC_TIME]
    lc_messages = getattr(locale, "LC_MESSAGES", None)
    if lc_messages is not None:
        locale_categories.append(lc_messages)

    for category in locale_categories:
        try:
            current = locale.getlocale(category)[0]
        except Exception:
            current = None
        if current:
            candidates.append(current)

    for value in candidates:
        normalized = str(value).lower()
        if normalized.startswith("ru") or "russian" in normalized or "рус" in normalized:
            return True

    return False


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

    def _build_codex_args(self):
        appdata = os.environ.get("APPDATA")
        node = shutil.which("node.exe") or shutil.which("node")

        if appdata and node:
            codex_js = (
                Path(appdata)
                / "npm"
                / "node_modules"
                / "@openai"
                / "codex"
                / "bin"
                / "codex.js"
            )
            if codex_js.exists():
                return [node, str(codex_js)]

        codex = shutil.which("codex.exe") or shutil.which("codex.cmd") or shutil.which("codex")
        if not codex:
            raise CodexRpcError("Не найден codex в PATH. Проверь: where codex")

        return [codex]

    def _start(self):
        args = self._build_codex_args() + ["app-server"]

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        env = os.environ.copy()
        env["CODEX_HOME"] = r"C:\Users\user\.codex"
        env["NO_COLOR"] = "1"

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
            env=env,
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
        self.is_ru = detect_russian_locale()
        self.text = TEXT["ru" if self.is_ru else "en"]
        self.auth_path = Path(r"C:\Users\user\.codex") / "auth.json"
        self.auth_mtime = self.get_auth_mtime()

        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self.root = QWidget(self)
        self.root.setObjectName("root")

        self.title_icon_label = QLabel()
        self.title_icon_label.setObjectName("titleIcon")
        self.title_icon_label.setFixedSize(TITLE_ICON_SIZE, TITLE_ICON_SIZE)
        gauge_icon_path = Path(__file__).with_name("gauge.svg")
        if gauge_icon_path.exists():
            self.title_icon_label.setPixmap(self.load_white_icon_pixmap(gauge_icon_path, TITLE_ICON_SIZE))
        else:
            self.title_icon_label.setText("◷")

        self.title_label = QLabel(self.text["title"])
        self.title_label.setObjectName("title")

        self.status_label = QLabel(self.text["starting"])
        self.status_label.setObjectName("muted")

        self.account_label = QLabel("")
        self.account_label.setObjectName("account")

        self.buckets = QVBoxLayout()
        self.buckets.setSpacing(8)
        self.buckets.setContentsMargins(0, 6, 0, 0)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(5)
        header.addWidget(self.title_icon_label)
        header.addWidget(self.title_label)
        header.addSpacing(6)
        header.addWidget(self.status_label)
        header.addStretch(1)

        layout = QVBoxLayout(self.root)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(5)
        layout.addLayout(header)
        layout.addWidget(self.account_label)
        layout.addLayout(self.buckets)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.root)

        self.setStyleSheet("""
            QWidget#root {
                background: rgba(20, 20, 22, 235);
                border: 1px solid rgba(255, 255, 255, 35);
                border-radius: 16px;
            }
            QLabel {
                color: #eeeeee;
                font-family: Segoe UI, Arial;
                font-size: 13px;
            }
            QLabel#title {
                color: #ffffff;
                font-weight: 700;
                font-size: 13px;
            }
            QLabel#muted {
                color: #b2b2b2;
                font-size: 11px;
            }
            QLabel#account {
                color: #e2e2e2;
                font-size: 12px;
                font-weight: 650;
            }
            QLabel#bucketName, QLabel#bucketPercent, QLabel#bucketReset {
                color: #ffffff;
                font-size: 12px;
                font-weight: 700;
            }
            QLabel#titleIcon {
                color: #eeeeee;
                font-size: 13px;
            }
            QLabel#bucketPercent {
                qproperty-alignment: AlignCenter;
            }
            QLabel#bucketReset {
                qproperty-alignment: AlignRight;
            }
            QProgressBar {
                height: 4px;
                max-height: 4px;
                border: none;
                border-radius: 2px;
                background: rgba(255, 255, 255, 35);
                text-align: center;
            }
            QProgressBar::chunk {
                border-radius: 2px;
                background: #eeeeee;
            }
        """)

        self.setFixedWidth(WINDOW_WIDTH)
        self.root.setFixedWidth(WINDOW_WIDTH)
        self.resize(WINDOW_WIDTH, 120)
        self.restore_position()

        self.tray = QSystemTrayIcon(self)
        if gauge_icon_path.exists():
            self.tray.setIcon(QIcon(self.load_white_icon_pixmap(gauge_icon_path, 24)))
        else:
            self.tray.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))

        menu = QMenu()
        show_action = QAction(self.text["show_hide"], self)
        show_action.triggered.connect(self.toggle_visible)
        refresh_action = QAction(self.text["refresh_now"], self)
        refresh_action.triggered.connect(self.refresh)
        restart_action = QAction(self.text["restart"], self)
        restart_action.triggered.connect(self.restart_connection)
        quit_action = QAction(self.text["quit"], self)
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

    def load_white_icon_pixmap(self, path, size):
        source = QIcon(str(path)).pixmap(size, size)
        if source.isNull():
            return QPixmap()

        result = QPixmap(source.size())
        result.fill(Qt.transparent)

        painter = QPainter(result)
        painter.drawPixmap(0, 0, source)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(result.rect(), QColor("#eeeeee"))
        painter.end()

        return result

    def get_auth_mtime(self):
        try:
            return self.auth_path.stat().st_mtime_ns
        except OSError:
            return None

    def clear_buckets(self):
        while self.buckets.count():
            item = self.buckets.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def bucket_title(self, duration, fallback):
        if duration == 300:
            return self.text["five_hours"]
        if duration == 10080:
            return self.text["week"]
        if duration:
            if duration % 1440 == 0:
                days = duration // 1440
                return f"{days}d" if not self.is_ru else f"{days}д"
            if duration % 60 == 0:
                hours = duration // 60
                return f"{hours}h" if not self.is_ru else f"{hours}ч"
            return f"{duration}m" if not self.is_ru else f"{duration}м"
        return fallback or "Codex"

    def format_reset(self, resets_at, duration):
        if not resets_at:
            return "?"

        dt = datetime.fromtimestamp(int(resets_at))
        if duration and duration >= 1440:
            if self.is_ru:
                return f"{dt.day} {MONTHS_RU[dt.month - 1]}"
            return f"{dt.day} {MONTHS_EN[dt.month - 1]}"

        return dt.strftime("%H:%M")

    def sorted_limit_items(self, limits):
        items = []
        by_id = limits.get("rateLimitsByLimitId")

        source_buckets = []
        if isinstance(by_id, dict) and by_id:
            source_buckets = list(by_id.values())
        else:
            single = limits.get("rateLimits")
            if isinstance(single, dict):
                source_buckets = [single]

        for bucket in source_buckets:
            name = bucket.get("limitName") or bucket.get("limitId") or "codex"

            primary = bucket.get("primary")
            if isinstance(primary, dict):
                items.append((name, {"primary": primary}))

            secondary = bucket.get("secondary")
            if isinstance(secondary, dict):
                items.append((name, {"primary": secondary}))

        return sorted(
            items,
            key=lambda item: ((item[1].get("primary") or {}).get("windowDurationMins") or 999999999),
        )

    def make_bucket_widget(self, name, bucket):
        primary = bucket.get("primary") or {}
        used = primary.get("usedPercent")
        duration = primary.get("windowDurationMins")
        resets_at = primary.get("resetsAt")

        if used is None:
            left_percent = 0
            percent_text = "?"
            reset_text = "?"
        else:
            used_percent = max(0, min(100, int(round(float(used)))))
            left_percent = max(0, 100 - used_percent)
            percent_text = f"{left_percent}%"
            reset_text = self.format_reset(resets_at, duration)

        box = QWidget()
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(0, 0, 0, 0)
        box_layout.setSpacing(3)

        row_widget = QWidget()
        row_widget.setFixedWidth(CONTENT_WIDTH)

        row = QGridLayout(row_widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setHorizontalSpacing(8)
        row.setColumnStretch(0, 1)
        row.setColumnStretch(1, 1)
        row.setColumnStretch(2, 1)

        title = QLabel(self.bucket_title(duration, name))
        title.setObjectName("bucketName")

        percent = QLabel(percent_text)
        percent.setObjectName("bucketPercent")

        reset = QLabel(reset_text)
        reset.setObjectName("bucketReset")

        row.addWidget(title, 0, 0, alignment=Qt.AlignLeft)
        row.addWidget(percent, 0, 1, alignment=Qt.AlignCenter)
        row.addWidget(reset, 0, 2, alignment=Qt.AlignRight)

        bar = QProgressBar()
        bar.setFixedHeight(4)
        bar.setFixedWidth(CONTENT_WIDTH)
        bar.setRange(0, 100)
        bar.setValue(left_percent)
        bar.setTextVisible(False)

        box_layout.addWidget(row_widget)
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
        self.status_label.setText(self.text["refreshing"])

        def worker():
            try:
                current_auth_mtime = self.get_auth_mtime()
                if current_auth_mtime != self.auth_mtime:
                    if self.client:
                        self.client.close()
                    self.client = None
                    self.auth_mtime = current_auth_mtime

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
        email = account_obj.get("email") or self.text["unknown_account"]
        plan = account_obj.get("planType")
        self.account_label.setText(f"{email}" + (f" · {plan}" if plan else ""))

        limits = data.get("limits") or {}

        self.clear_buckets()

        items = self.sorted_limit_items(limits)
        if items:
            for name, bucket in items:
                self.buckets.addWidget(self.make_bucket_widget(name, bucket))
        else:
            label = QLabel(self.text["no_limits"])
            self.buckets.addWidget(label)

        self.status_label.setText(datetime.now().strftime("%H:%M:%S"))
        self.setFixedWidth(WINDOW_WIDTH)
        self.root.setFixedWidth(WINDOW_WIDTH)
        self.adjustSize()
        self.save_position()

    def apply_error(self, text):
        self.refreshing = False
        self.clear_buckets()
        self.account_label.setText(self.text["error"])
        msg = QLabel(text[:260])
        msg.setWordWrap(True)
        self.buckets.addWidget(msg)
        self.status_label.setText(self.text["error"])
        self.setFixedWidth(WINDOW_WIDTH)
        self.root.setFixedWidth(WINDOW_WIDTH)
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