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

from PySide6.QtCore import Qt, QObject, QTimer, Signal, QSettings
from PySide6.QtGui import QAction, QActionGroup, QColor, QCursor, QIcon, QPainter, QPixmap
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
AUTH_CHECK_INTERVAL_MS = 20_000
REFRESH_INTERVALS_MS = (10_000, 20_000, 30_000, 60_000, 300_000)
WINDOW_SAFETY_GAP = 6
DEFAULT_SIZE_PRESET = "small"
SIZE_PRESETS = {
    "small": {
        "width": 220,
        "margin_x": 8,
        "margin_top": 7,
        "margin_bottom": 8,
        "title_font": 12,
        "account_font": 11,
        "limit_font": 11,
        "muted_font": 10,
        "icon_size": 13,
        "progress_height": 4,
        "bucket_spacing": 6,
        "row_spacing": 2,
    },
    "medium": {
        "width": 270,
        "margin_x": 10,
        "margin_top": 9,
        "margin_bottom": 10,
        "title_font": 14,
        "account_font": 12,
        "limit_font": 12,
        "muted_font": 11,
        "icon_size": 16,
        "progress_height": 5,
        "bucket_spacing": 8,
        "row_spacing": 3,
    },
    "large": {
        "width": 340,
        "margin_x": 14,
        "margin_top": 11,
        "margin_bottom": 12,
        "title_font": 16,
        "account_font": 14,
        "limit_font": 14,
        "muted_font": 13,
        "icon_size": 18,
        "progress_height": 7,
        "bucket_spacing": 10,
        "row_spacing": 5,
    },
    "giant": {
        "width": 410,
        "margin_x": 16,
        "margin_top": 13,
        "margin_bottom": 15,
        "title_font": 18,
        "account_font": 16,
        "limit_font": 16,
        "muted_font": 15,
        "icon_size": 22,
        "progress_height": 9,
        "bucket_spacing": 13,
        "row_spacing": 6,
    },
}
THEME_MODES = ("dark", "light", "auto")


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
        "clone_window": "Клонировать окно",
        "delete_clone": "Удалить клон",
        "refresh_interval": "Интервал обновления",
        "interval_10000": "10 секунд",
        "interval_20000": "20 секунд",
        "interval_30000": "30 секунд",
        "interval_60000": "1 минута",
        "interval_300000": "5 минут",
        "window_size": "Размер окна",
        "size_small": "Маленький",
        "size_medium": "Средний",
        "size_large": "Большой",
        "size_giant": "Гигантский",
        "theme": "Тема",
        "theme_dark": "Тёмная",
        "theme_light": "Светлая",
        "theme_auto": "Авто",
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
        "clone_window": "Clone window",
        "delete_clone": "Delete clone",
        "refresh_interval": "Refresh interval",
        "interval_10000": "10 seconds",
        "interval_20000": "20 seconds",
        "interval_30000": "30 seconds",
        "interval_60000": "1 minute",
        "interval_300000": "5 minutes",
        "window_size": "Window size",
        "size_small": "Small",
        "size_medium": "Medium",
        "size_large": "Large",
        "size_giant": "Giant",
        "theme": "Theme",
        "theme_dark": "Dark",
        "theme_light": "Light",
        "theme_auto": "Auto",
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
    def __init__(self, notification_callback=None):
        self.proc = None
        self.next_id = 1
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.stderr_tail = []
        self.alive = False
        self.notification_callback = notification_callback
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
                else:
                    self._handle_notification(msg)
        finally:
            self.alive = False

    def _handle_notification(self, msg):
        if msg.get("method") != "account/rateLimits/updated":
            return
        if not self.notification_callback:
            return
        try:
            self.notification_callback(msg)
        except Exception:
            pass

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


class OverlayManager(QObject):
    data_ready = Signal(dict)
    limits_ready = Signal(dict)
    error_ready = Signal(str)

    def __init__(self):
        super().__init__()
        self.windows = []
        self.client = None
        self.refreshing = False
        self.last_data = None
        self.settings = QSettings("ti-watsky", "codex-limits-overlay")
        self.is_ru = detect_russian_locale()
        self.text = TEXT["ru" if self.is_ru else "en"]
        self.refresh_interval_ms = self.load_refresh_interval()
        self.size_preset = self.load_size_preset()
        self.theme_mode = self.load_theme_mode()
        self.auth_path = Path(r"C:\Users\user\.codex") / "auth.json"
        self.auth_mtime = self.get_auth_mtime()

        self.timer = QTimer(self)
        self.timer.setInterval(self.refresh_interval_ms)
        self.timer.timeout.connect(self.refresh)

        self.auth_timer = QTimer(self)
        self.auth_timer.setInterval(AUTH_CHECK_INTERVAL_MS)
        self.auth_timer.timeout.connect(self.check_auth_file)

        self.data_ready.connect(self.apply_data_to_windows)
        self.limits_ready.connect(self.apply_limits_to_windows)
        self.error_ready.connect(self.apply_error_to_windows)

    def start(self):
        window = self.create_window()
        window.show()
        QTimer.singleShot(150, self.refresh)
        self.timer.start()
        self.auth_timer.start()

    def load_refresh_interval(self):
        try:
            interval = int(self.settings.value("refresh_interval_ms", POLL_INTERVAL_MS))
        except (TypeError, ValueError):
            interval = POLL_INTERVAL_MS
        if interval not in REFRESH_INTERVALS_MS:
            return POLL_INTERVAL_MS
        return interval

    def load_size_preset(self):
        preset = str(self.settings.value("size_preset", DEFAULT_SIZE_PRESET))
        if preset not in SIZE_PRESETS:
            return DEFAULT_SIZE_PRESET
        return preset

    def load_theme_mode(self):
        mode = str(self.settings.value("theme", "dark"))
        if mode not in THEME_MODES:
            return "dark"
        return mode

    def create_window(self, source_window=None):
        window = Overlay(self, is_primary=not self.windows)
        self.windows.append(window)

        if source_window:
            window.move(source_window.x() + 24, source_window.y() + 24)

        if self.last_data:
            window.apply_data(self.last_data, force_resize=True)

        self.refresh_window_menus()
        return window

    def clone_window(self, source_window):
        window = self.create_window(source_window)
        window.show()

    def remove_window(self, window):
        if len(self.windows) <= 1:
            self.refresh_window_menus()
            return

        if window in self.windows:
            self.windows.remove(window)
        if window.is_primary and self.windows:
            self.windows[0].is_primary = True
        window.force_close = True
        window.tray.hide()
        window.close()
        window.deleteLater()
        self.refresh_window_menus()

    def refresh_window_menus(self):
        for window in self.windows:
            window.rebuild_context_menu()

    def apply_settings_to_windows(self, force_resize=False):
        for window in self.windows:
            window.sync_settings()
            window.apply_window_width()
            window.apply_style()
            window.apply_layout_metrics()
            window.apply_title_icon()
            window.apply_tray_icon()
            window.apply_account_elide()
            if self.last_data:
                window.apply_data(self.last_data, force_resize=True)
            elif force_resize:
                window.adjustSize()
            window.rebuild_context_menu()

    def set_refresh_interval(self, interval_ms):
        if interval_ms not in REFRESH_INTERVALS_MS:
            return
        self.refresh_interval_ms = interval_ms
        self.settings.setValue("refresh_interval_ms", interval_ms)
        self.timer.setInterval(interval_ms)
        self.apply_settings_to_windows()
        self.refresh()

    def set_size_preset(self, preset):
        if preset not in SIZE_PRESETS:
            return
        self.size_preset = preset
        self.settings.setValue("size_preset", preset)
        self.apply_settings_to_windows(force_resize=True)

    def set_theme_mode(self, mode):
        if mode not in THEME_MODES:
            return
        self.theme_mode = mode
        self.settings.setValue("theme", mode)
        self.apply_settings_to_windows()

    def get_auth_mtime(self):
        try:
            return self.auth_path.stat().st_mtime_ns
        except OSError:
            return None

    def get_client(self):
        if self.client is None or not self.client.alive:
            if self.client:
                self.client.close()
            self.client = CodexAppServerClient(self.handle_rate_limits_notification)
        return self.client

    def handle_rate_limits_notification(self, msg):
        limits = self.extract_limits_from_notification(msg)
        if limits:
            self.limits_ready.emit(limits)

    def extract_limits_from_notification(self, msg):
        params = msg.get("params")
        if not isinstance(params, dict):
            return {}

        for key in ("limits", "rateLimits"):
            value = params.get(key)
            if isinstance(value, dict):
                return value

        return params

    def check_auth_file(self):
        current_auth_mtime = self.get_auth_mtime()
        if current_auth_mtime == self.auth_mtime:
            return

        self.auth_mtime = current_auth_mtime
        if self.client:
            self.client.close()
            self.client = None
        self.refresh()

    def refresh(self):
        if self.refreshing:
            return

        self.refreshing = True

        def worker():
            try:
                current_auth_mtime = self.get_auth_mtime()
                if current_auth_mtime != self.auth_mtime:
                    if self.client:
                        self.client.close()
                    self.auth_mtime = current_auth_mtime
                    self.client = None

                client = self.get_client()
                account = client.request("account/read", {"refreshToken": False}, timeout=20)
                limits = client.request("account/rateLimits/read", timeout=25)
                self.data_ready.emit({"account": account, "limits": limits})
            except Exception as exc:
                self.error_ready.emit(str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def apply_data_to_windows(self, data):
        self.refreshing = False
        self.last_data = data
        for window in self.windows:
            window.apply_data(data)

    def apply_limits_to_windows(self, limits):
        if self.last_data:
            self.last_data = {**self.last_data, "limits": limits}
        for window in self.windows:
            window.apply_limits_update(limits)

    def apply_error_to_windows(self, text):
        self.refreshing = False
        for window in self.windows:
            window.apply_error(text)

    def quit_app(self):
        for window in self.windows:
            window.save_position()
        if self.client:
            self.client.close()
        QApplication.quit()


class Overlay(QWidget):
    data_ready = Signal(dict)
    limits_ready = Signal(dict)
    error_ready = Signal(str)

    def __init__(self, manager, is_primary=False):
        super().__init__()

        self.manager = manager
        self.is_primary = is_primary
        self.force_close = False
        self.drag_pos = None
        self.last_data = None
        self.account_text_full = ""
        self.limit_rows = {}
        self.no_limits_label = None
        self.error_label = None
        self.sync_settings()

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
        self.title_icon_label.setAlignment(Qt.AlignCenter)
        self.gauge_icon_path = Path(__file__).with_name("gauge.svg")

        self.title_label = QLabel(self.text["title"])
        self.title_label.setObjectName("title")

        self.account_label = QLabel("")
        self.account_label.setObjectName("account")

        self.buckets = QVBoxLayout()
        self.buckets.setContentsMargins(0, 6, 0, 0)

        self.header = QHBoxLayout()
        self.header.setContentsMargins(0, 0, 0, 0)
        self.header.addWidget(self.title_icon_label, 0, Qt.AlignVCenter)
        self.header.addWidget(self.title_label, 0, Qt.AlignVCenter)
        self.header.addStretch(1)

        self.root_layout = QVBoxLayout(self.root)
        self.root_layout.addLayout(self.header)
        self.root_layout.addWidget(self.account_label)
        self.root_layout.addLayout(self.buckets)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.root)

        self.apply_style()
        self.apply_layout_metrics()
        self.apply_title_icon()
        self.apply_window_width()
        self.resize(self.window_width(), 120)
        if self.is_primary:
            self.restore_position()

        self.tray = QSystemTrayIcon(self)
        self.apply_tray_icon()

        self.context_menu = self.build_context_menu()
        self.tray.setContextMenu(self.context_menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

    def sync_settings(self):
        self.settings = self.manager.settings
        self.is_ru = self.manager.is_ru
        self.text = self.manager.text
        self.refresh_interval_ms = self.manager.refresh_interval_ms
        self.size_preset = self.manager.size_preset
        self.theme_mode = self.manager.theme_mode

    def build_context_menu(self):
        menu = QMenu()

        show_action = QAction(self.text["show_hide"], self)
        show_action.triggered.connect(self.toggle_visible)
        refresh_action = QAction(self.text["refresh_now"], self)
        refresh_action.triggered.connect(self.manager.refresh)
        clone_action = QAction(self.text["clone_window"], self)
        clone_action.triggered.connect(lambda: self.manager.clone_window(self))
        delete_action = QAction(self.text["delete_clone"], self)
        delete_action.setEnabled(len(self.manager.windows) > 1)
        delete_action.triggered.connect(lambda: self.manager.remove_window(self))
        quit_action = QAction(self.text["quit"], self)
        quit_action.triggered.connect(self.manager.quit_app)

        menu.addAction(show_action)
        menu.addAction(refresh_action)
        menu.addAction(clone_action)
        menu.addAction(delete_action)
        menu.addMenu(self.build_refresh_interval_menu())
        menu.addMenu(self.build_size_menu())
        menu.addMenu(self.build_theme_menu())
        menu.addSeparator()
        menu.addAction(quit_action)

        return menu

    def build_refresh_interval_menu(self):
        menu = QMenu(self.text["refresh_interval"], self)
        group = QActionGroup(menu)
        group.setExclusive(True)

        for interval_ms in REFRESH_INTERVALS_MS:
            action = QAction(self.text[f"interval_{interval_ms}"], group)
            action.setCheckable(True)
            action.setChecked(interval_ms == self.refresh_interval_ms)
            action.triggered.connect(lambda checked=False, ms=interval_ms: self.manager.set_refresh_interval(ms))
            menu.addAction(action)

        return menu

    def build_size_menu(self):
        menu = QMenu(self.text["window_size"], self)
        group = QActionGroup(menu)
        group.setExclusive(True)

        for preset in SIZE_PRESETS:
            action = QAction(self.text[f"size_{preset}"], group)
            action.setCheckable(True)
            action.setChecked(preset == self.size_preset)
            action.triggered.connect(lambda checked=False, value=preset: self.manager.set_size_preset(value))
            menu.addAction(action)

        return menu

    def build_theme_menu(self):
        menu = QMenu(self.text["theme"], self)
        group = QActionGroup(menu)
        group.setExclusive(True)

        for mode in THEME_MODES:
            action = QAction(self.text[f"theme_{mode}"], group)
            action.setCheckable(True)
            action.setChecked(mode == self.theme_mode)
            action.triggered.connect(lambda checked=False, value=mode: self.manager.set_theme_mode(value))
            menu.addAction(action)

        return menu

    def rebuild_context_menu(self):
        self.context_menu = self.build_context_menu()
        self.tray.setContextMenu(self.context_menu)

    def restore_position(self):
        x = self.settings.value("x", None)
        y = self.settings.value("y", None)
        if x is not None and y is not None:
            self.move(int(x), int(y))
        else:
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(screen.right() - self.width() - 30, screen.top() + 80)

    def save_position(self):
        if not self.is_primary:
            return
        self.settings.setValue("x", self.x())
        self.settings.setValue("y", self.y())

    def preset(self):
        return SIZE_PRESETS.get(self.size_preset, SIZE_PRESETS[DEFAULT_SIZE_PRESET])

    def window_width(self):
        preset = self.preset()
        if not self.account_text_full:
            return preset["width"]
        return self.limit_block_width() + preset["margin_x"] * 2 + WINDOW_SAFETY_GAP

    def content_width(self):
        preset = self.preset()
        return max(160, self.width() - preset["margin_x"] * 2)

    def preset_content_width(self):
        preset = self.preset()
        return max(160, preset["width"] - preset["margin_x"] * 2)

    def limit_block_width(self):
        max_width = self.preset_content_width()
        if not self.account_text_full:
            return max_width

        account_width = self.account_label.fontMetrics().horizontalAdvance(self.account_text_full)
        return max(150, min(max_width, account_width))

    def apply_window_width(self):
        width = self.window_width()
        self.setFixedWidth(width)
        self.root.setFixedWidth(width)

    def apply_layout_metrics(self):
        preset = self.preset()
        margin_x = preset["margin_x"]
        self.root_layout.setContentsMargins(
            margin_x,
            preset["margin_top"],
            margin_x,
            preset["margin_bottom"],
        )
        self.root_layout.setSpacing(max(3, preset["row_spacing"] + 2))
        self.header.setSpacing(max(4, preset["row_spacing"] + 3))
        self.buckets.setSpacing(preset["bucket_spacing"])
        self.buckets.setContentsMargins(0, preset["bucket_spacing"], 0, 0)
        self.account_label.setFixedWidth(self.content_width())

    def apply_style(self):
        preset = self.preset()
        colors = self.theme_colors()
        progress_height = preset["progress_height"]
        progress_radius = max(2, progress_height // 2)
        self.setStyleSheet(f"""
            QWidget#root {{
                background: {colors["background"]};
                border: 1px solid {colors["border"]};
                border-radius: 16px;
            }}
            QLabel {{
                color: {colors["text"]};
                font-family: Segoe UI, Arial;
                font-size: {preset["limit_font"]}px;
            }}
            QLabel#title {{
                color: {colors["title"]};
                font-weight: 700;
                font-size: {preset["title_font"]}px;
            }}
            QLabel#muted {{
                color: {colors["muted"]};
                font-size: {preset["muted_font"]}px;
            }}
            QLabel#account {{
                color: {colors["account"]};
                font-size: {preset["account_font"]}px;
                font-weight: 650;
            }}
            QLabel#bucketName, QLabel#bucketPercent, QLabel#bucketReset {{
                color: {colors["title"]};
                font-size: {preset["limit_font"]}px;
                font-weight: 700;
            }}
            QLabel#titleIcon {{
                color: {colors["icon"]};
                font-size: {preset["icon_size"]}px;
            }}
            QLabel#bucketPercent {{
                qproperty-alignment: AlignCenter;
            }}
            QLabel#bucketReset {{
                qproperty-alignment: AlignRight;
            }}
            QProgressBar {{
                height: {progress_height}px;
                max-height: {progress_height}px;
                border: none;
                border-radius: {progress_radius}px;
                background: {colors["progress_background"]};
                text-align: center;
            }}
            QProgressBar::chunk {{
                border-radius: {progress_radius}px;
                background: {colors["progress_chunk"]};
            }}
        """)

    def apply_title_icon(self):
        size = self.preset()["icon_size"]
        self.title_icon_label.setFixedSize(size, size)
        if self.gauge_icon_path.exists():
            self.title_icon_label.setPixmap(
                self.load_tinted_icon_pixmap(self.gauge_icon_path, size, self.theme_colors()["icon"])
            )
        else:
            self.title_icon_label.setText("◷")

    def apply_tray_icon(self):
        if self.gauge_icon_path.exists():
            self.tray.setIcon(QIcon(
                self.load_tinted_icon_pixmap(self.gauge_icon_path, 24, self.theme_colors()["icon"])
            ))
        else:
            self.tray.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))

    def effective_theme_mode(self):
        if self.theme_mode != "auto":
            return self.theme_mode

        window_color = QApplication.palette().window().color()
        return "light" if window_color.lightness() >= 128 else "dark"

    def theme_colors(self):
        if self.effective_theme_mode() == "light":
            return {
                "background": "rgba(245, 245, 245, 218)",
                "border": "rgba(0, 0, 0, 32)",
                "text": "#303030",
                "title": "#242424",
                "account": "#363636",
                "muted": "#777777",
                "progress_background": "rgba(0, 0, 0, 28)",
                "progress_chunk": "#454545",
                "icon": "#454545",
            }

        return {
            "background": "rgba(20, 20, 22, 218)",
            "border": "rgba(255, 255, 255, 28)",
            "text": "#d8d8d8",
            "title": "#e8e8e8",
            "account": "#d2d2d2",
            "muted": "#a2a2a2",
            "progress_background": "rgba(255, 255, 255, 28)",
            "progress_chunk": "#d6d6d6",
            "icon": "#d8d8d8",
        }

    def set_account_text(self, email, plan):
        text = f"{email}" + (f" · {plan}" if plan else "")
        changed = text != self.account_text_full
        self.account_text_full = text
        if changed:
            self.apply_window_width()
            self.apply_layout_metrics()
        self.apply_account_elide()
        return changed

    def apply_account_elide(self):
        if not self.account_text_full:
            self.account_label.setText("")
            return

        metrics = self.account_label.fontMetrics()
        text = metrics.elidedText(self.account_text_full, Qt.ElideRight, self.content_width())
        self.account_label.setText(text)
        self.account_label.setToolTip(self.account_text_full)

    def load_tinted_icon_pixmap(self, path, size, color):
        source = QIcon(str(path)).pixmap(size * 4, size * 4)
        if source.isNull():
            return QPixmap()

        white = QPixmap(source.size())
        white.fill(Qt.transparent)

        painter = QPainter(white)
        painter.drawPixmap(0, 0, source)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(white.rect(), QColor(color))
        painter.end()

        image = white.toImage()
        left = image.width()
        top = image.height()
        right = -1
        bottom = -1

        for y in range(image.height()):
            for x in range(image.width()):
                if image.pixelColor(x, y).alpha() > 0:
                    left = min(left, x)
                    top = min(top, y)
                    right = max(right, x)
                    bottom = max(bottom, y)

        if right < left or bottom < top:
            return white.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        trimmed = white.copy(left, top, right - left + 1, bottom - top + 1)
        return trimmed.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)

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

    def limit_row_key(self, name, bucket):
        primary = bucket.get("primary") or {}
        return primary.get("windowDurationMins") or name

    def bucket_values(self, name, bucket):
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

        return {
            "title": self.bucket_title(duration, name),
            "percent": percent_text,
            "reset": reset_text,
            "value": left_percent,
        }

    def make_bucket_widget(self, name, bucket):
        values = self.bucket_values(name, bucket)

        box = QWidget()
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(0, 0, 0, 0)
        box_layout.setSpacing(self.preset()["row_spacing"])

        row_widget = QWidget()
        row_widget.setFixedWidth(self.limit_block_width())

        row = QGridLayout(row_widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setHorizontalSpacing(8)
        row.setColumnStretch(0, 1)
        row.setColumnStretch(1, 1)
        row.setColumnStretch(2, 1)

        title = QLabel(values["title"])
        title.setObjectName("bucketName")

        percent = QLabel(values["percent"])
        percent.setObjectName("bucketPercent")

        reset = QLabel(values["reset"])
        reset.setObjectName("bucketReset")

        row.addWidget(title, 0, 0, alignment=Qt.AlignLeft)
        row.addWidget(percent, 0, 1, alignment=Qt.AlignCenter)
        row.addWidget(reset, 0, 2, alignment=Qt.AlignRight)

        bar = QProgressBar()
        bar.setFixedHeight(self.preset()["progress_height"])
        bar.setFixedWidth(self.limit_block_width())
        bar.setRange(0, 100)
        bar.setValue(values["value"])
        bar.setTextVisible(False)

        box_layout.addWidget(row_widget)
        box_layout.addWidget(bar)
        self.buckets.addWidget(box)

        return {
            "box": box,
            "box_layout": box_layout,
            "row_widget": row_widget,
            "title": title,
            "percent": percent,
            "reset": reset,
            "bar": bar,
        }

    def update_bucket_widget(self, row, name, bucket):
        values = self.bucket_values(name, bucket)

        row["title"].setText(values["title"])
        row["percent"].setText(values["percent"])
        row["reset"].setText(values["reset"])
        row["bar"].setValue(values["value"])
        self.update_bucket_widget_size(row)

    def update_bucket_widget_size(self, row):
        width = self.limit_block_width()
        row["box_layout"].setSpacing(self.preset()["row_spacing"])
        row["row_widget"].setFixedWidth(width)
        row["bar"].setFixedHeight(self.preset()["progress_height"])
        row["bar"].setFixedWidth(width)

    def hide_limit_rows(self):
        changed = False
        for row in self.limit_rows.values():
            if row["box"].isVisible():
                row["box"].hide()
                changed = True
        return changed

    def get_no_limits_label(self):
        if self.no_limits_label is None:
            self.no_limits_label = QLabel(self.text["no_limits"])
            self.buckets.addWidget(self.no_limits_label)
        return self.no_limits_label

    def get_error_label(self):
        if self.error_label is None:
            self.error_label = QLabel("")
            self.error_label.setWordWrap(True)
            self.buckets.addWidget(self.error_label)
        return self.error_label

    def apply_data(self, data, force_resize=False):
        self.last_data = data

        account_obj = (data.get("account") or {}).get("account") or {}
        email = account_obj.get("email") or self.text["unknown_account"]
        plan = account_obj.get("planType")

        account_changed = self.set_account_text(email, plan)
        limits = data.get("limits") or {}
        self.apply_limits(limits, force_resize=force_resize or account_changed)

    def apply_limits_update(self, limits):
        if self.last_data:
            self.last_data = {**self.last_data, "limits": limits}
        self.apply_limits(limits)

    def apply_limits(self, limits, force_resize=False):
        items = self.sorted_limit_items(limits)
        layout_changed = force_resize

        if self.error_label and self.error_label.isVisible():
            self.error_label.hide()
            layout_changed = True

        if items:
            if self.no_limits_label and self.no_limits_label.isVisible():
                self.no_limits_label.hide()
                layout_changed = True

            seen = set()
            for name, bucket in items:
                key = self.limit_row_key(name, bucket)
                seen.add(key)
                row = self.limit_rows.get(key)
                if row is None:
                    row = self.make_bucket_widget(name, bucket)
                    self.limit_rows[key] = row
                    layout_changed = True
                else:
                    self.update_bucket_widget(row, name, bucket)

                if not row["box"].isVisible():
                    row["box"].show()
                    layout_changed = True

            for key, row in self.limit_rows.items():
                if key not in seen and row["box"].isVisible():
                    row["box"].hide()
                    layout_changed = True
        else:
            if self.hide_limit_rows():
                layout_changed = True
            created_label = self.no_limits_label is None
            label = self.get_no_limits_label()
            if created_label or not label.isVisible():
                label.show()
                layout_changed = True

        self.apply_window_width()
        self.apply_layout_metrics()
        self.apply_account_elide()
        for row in self.limit_rows.values():
            self.update_bucket_widget_size(row)

        if layout_changed:
            self.adjustSize()
            self.save_position()

    def apply_error(self, text):
        self.account_label.setText(self.text["error"])
        self.account_text_full = ""
        self.hide_limit_rows()
        if self.no_limits_label:
            self.no_limits_label.hide()
        msg = self.get_error_label()
        msg.setText(text[:260])
        msg.show()
        self.apply_window_width()
        self.adjustSize()

    def toggle_visible(self):
        self.setVisible(not self.isVisible())

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.toggle_visible()

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
        if self.force_close:
            event.accept()
        else:
            event.ignore()
            self.hide()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    manager = OverlayManager()
    manager.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
