#!/usr/bin/env python3
"""
LiteChat Desktop Client
Python + PyQt6，本地SQLite存储全量聊天记录
"""

import sys
import os
import json
import sqlite3
import uuid
import time
import hashlib
import threading
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QTextEdit, QLineEdit, QPushButton,
    QLabel, QSplitter, QStackedWidget, QDialog, QFormLayout,
    QMessageBox, QInputDialog, QSystemTrayIcon, QMenu, QScrollArea,
    QFrame, QSizePolicy
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QSize, pyqtSlot
)
from PyQt6.QtGui import (
    QFont, QColor, QPalette, QIcon, QPixmap, QPainter, QBrush,
    QTextCursor, QKeySequence, QShortcut
)
import websocket  # websocket-client
import requests

# ── 配置 ──────────────────────────────────────────────────
APP_NAME = "LiteChat"
APP_VERSION = "1.0.0"
DATA_DIR = Path.home() / ".litechat"
DB_PATH = DATA_DIR / "messages.db"
CONFIG_PATH = DATA_DIR / "config.json"
DEFAULT_SERVER = os.environ.get("LITECHAT_SERVER", "ws://localhost:8080")

DATA_DIR.mkdir(parents=True, exist_ok=True)


# ── 本地数据库 ─────────────────────────────────────────────
class LocalDB:
    def __init__(self):
        self.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init()

    def _init(self):
        self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id      TEXT    UNIQUE NOT NULL,
                conv_id     TEXT    NOT NULL,   -- username or group_id
                is_group    INTEGER DEFAULT 0,
                from_user   TEXT    NOT NULL,
                to_target   TEXT    NOT NULL,
                content     TEXT    NOT NULL,
                content_type TEXT   DEFAULT 'text',
                timestamp   INTEGER NOT NULL,
                status      TEXT    DEFAULT 'sent'  -- sent/delivered/read
            );
            CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id, timestamp DESC);
            
            CREATE TABLE IF NOT EXISTS conversations (
                conv_id     TEXT PRIMARY KEY,
                is_group    INTEGER DEFAULT 0,
                name        TEXT    NOT NULL,
                last_msg    TEXT,
                last_time   INTEGER DEFAULT 0,
                unread      INTEGER DEFAULT 0
            );
            
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        self.conn.commit()

    def execute(self, sql, params=()):
        with self._lock:
            return self.conn.execute(sql, params)

    def executemany(self, sql, params):
        with self._lock:
            return self.conn.executemany(sql, params)

    def save_message(self, msg_id, conv_id, is_group, from_user, to_target, content,
                     content_type='text', timestamp=None, status='sent'):
        ts = timestamp or int(time.time() * 1000)
        with self._lock:
            try:
                self.conn.execute(
                    """INSERT OR IGNORE INTO messages
                       (msg_id, conv_id, is_group, from_user, to_target, content, content_type, timestamp, status)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (msg_id, conv_id, is_group, from_user, to_target, content, content_type, ts, status)
                )
                self.conn.execute(
                    """INSERT INTO conversations (conv_id, is_group, name, last_msg, last_time, unread)
                       VALUES (?,?,?,?,?,0)
                       ON CONFLICT(conv_id) DO UPDATE SET
                         last_msg=excluded.last_msg, last_time=excluded.last_time""",
                    (conv_id, is_group, conv_id, content[:60], ts)
                )
                self.conn.commit()
                return True
            except Exception as e:
                print(f"[DB] save_message error: {e}")
                return False

    def get_messages(self, conv_id, limit=100, before_ts=None):
        if before_ts:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE conv_id=? AND timestamp<? ORDER BY timestamp DESC LIMIT ?",
                (conv_id, before_ts, limit)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE conv_id=? ORDER BY timestamp DESC LIMIT ?",
                (conv_id, limit)
            ).fetchall()
        return list(reversed(rows))

    def get_conversations(self):
        return self.conn.execute(
            "SELECT * FROM conversations ORDER BY last_time DESC"
        ).fetchall()

    def get_config(self, key, default=None):
        row = self.conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_config(self, key, value):
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO config (key,value) VALUES (?,?)", (key, value))
            self.conn.commit()

    def mark_read(self, conv_id):
        with self._lock:
            self.conn.execute("UPDATE conversations SET unread=0 WHERE conv_id=?", (conv_id,))
            self.conn.commit()

    def increment_unread(self, conv_id):
        with self._lock:
            self.conn.execute(
                "UPDATE conversations SET unread=unread+1 WHERE conv_id=?", (conv_id,)
            )
            self.conn.commit()


# ── WebSocket 工作线程 ────────────────────────────────────
class WSWorker(QThread):
    message_received = pyqtSignal(dict)
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    auth_ok = pyqtSignal(str)
    auth_fail = pyqtSignal(str)

    def __init__(self, server_url, token):
        super().__init__()
        self.server_url = server_url.replace('http://', 'ws://').replace('https://', 'wss://')
        if not self.server_url.startswith('ws'):
            self.server_url = 'ws://' + self.server_url
        self.token = token
        self._ws = None
        self._running = True
        self._reconnect_delay = 2

    def run(self):
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    self.server_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=25, ping_timeout=10)
            except Exception as e:
                print(f"[WS] Connection error: {e}")

            if self._running:
                self.disconnected.emit()
                print(f"[WS] Reconnecting in {self._reconnect_delay}s...")
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30)

    def _on_open(self, ws):
        self._reconnect_delay = 2
        self.connected.emit()
        ws.send(json.dumps({"type": "auth", "token": self.token}))

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
            if msg.get('type') == 'auth_ok':
                self.auth_ok.emit(msg.get('username', ''))
            elif msg.get('type') == 'auth_fail':
                self.auth_fail.emit(msg.get('message', ''))
                self._running = False
                ws.close()
            else:
                self.message_received.emit(msg)
        except Exception as e:
            print(f"[WS] Parse error: {e}")

    def _on_error(self, ws, error):
        print(f"[WS] Error: {error}")

    def _on_close(self, ws, code, msg):
        print(f"[WS] Closed: {code} {msg}")

    def send(self, data: dict):
        if self._ws:
            try:
                self._ws.send(json.dumps(data))
            except Exception as e:
                print(f"[WS] Send error: {e}")

    def stop(self):
        self._running = False
        if self._ws:
            self._ws.close()


# ── 消息气泡组件 ─────────────────────────────────────────
class MessageBubble(QFrame):
    def __init__(self, msg_data: dict, my_username: str, parent=None):
        super().__init__(parent)
        self.is_mine = msg_data['from_user'] == my_username

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(2)

        # 发送者（群聊时显示）
        if not self.is_mine and msg_data.get('is_group'):
            sender_label = QLabel(msg_data['from_user'])
            sender_label.setStyleSheet("color: #888; font-size: 11px;")
            layout.addWidget(sender_label)

        # 内容
        content = QLabel(msg_data['content'])
        content.setWordWrap(True)
        content.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        # 时间
        ts = msg_data.get('timestamp', 0)
        if ts > 1e12:  # ms
            ts = ts / 1000
        time_str = datetime.fromtimestamp(ts).strftime('%H:%M')
        time_label = QLabel(time_str)
        time_label.setStyleSheet("color: #aaa; font-size: 10px;")

        if self.is_mine:
            content.setStyleSheet("""
                background: #0084ff; color: white; border-radius: 12px;
                padding: 8px 12px; font-size: 13px;
            """)
            layout.addWidget(content, alignment=Qt.AlignmentFlag.AlignRight)
            layout.addWidget(time_label, alignment=Qt.AlignmentFlag.AlignRight)
        else:
            content.setStyleSheet("""
                background: #f0f0f0; color: #333; border-radius: 12px;
                padding: 8px 12px; font-size: 13px;
            """)
            layout.addWidget(content, alignment=Qt.AlignmentFlag.AlignLeft)
            layout.addWidget(time_label, alignment=Qt.AlignmentFlag.AlignLeft)


# ── 对话列表项 ────────────────────────────────────────────
class ConvItem(QWidget):
    def __init__(self, conv_id, name, last_msg, unread, is_group=False):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)

        # 头像
        avatar = QLabel()
        avatar.setFixedSize(40, 40)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        color = '#' + hashlib.md5(conv_id.encode()).hexdigest()[:6]
        initial = (name[0] if name else '?').upper()
        avatar.setStyleSheet(f"""
            background: {color}; color: white; border-radius: 20px;
            font-size: 16px; font-weight: bold;
        """)
        avatar.setText(initial)
        layout.addWidget(avatar)

        # 文字
        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        name_label = QLabel(name)
        name_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        preview = QLabel(last_msg or '')
        preview.setStyleSheet("color: #888; font-size: 11px;")
        preview.setMaximumWidth(150)
        text_layout.addWidget(name_label)
        text_layout.addWidget(preview)
        layout.addLayout(text_layout)
        layout.addStretch()

        if unread > 0:
            badge = QLabel(str(unread))
            badge.setStyleSheet("""
                background: #f44336; color: white; border-radius: 10px;
                padding: 1px 6px; font-size: 11px; font-weight: bold;
            """)
            layout.addWidget(badge)


# ── 聊天窗口 ─────────────────────────────────────────────
class ChatPane(QWidget):
    send_message = pyqtSignal(str, str, bool)  # conv_id, content, is_group

    def __init__(self, my_username, db: LocalDB, parent=None):
        super().__init__(parent)
        self.my_username = my_username
        self.db = db
        self.current_conv = None
        self.current_is_group = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题栏
        self.title_bar = QLabel("选择一个对话")
        self.title_bar.setStyleSheet("""
            background: #fff; border-bottom: 1px solid #e0e0e0;
            padding: 12px 16px; font-size: 15px; font-weight: bold;
        """)
        layout.addWidget(self.title_bar)

        # 消息区域
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("border: none; background: #fafafa;")

        self.msg_container = QWidget()
        self.msg_layout = QVBoxLayout(self.msg_container)
        self.msg_layout.setContentsMargins(16, 8, 16, 8)
        self.msg_layout.setSpacing(6)
        self.msg_layout.addStretch()

        self.scroll_area.setWidget(self.msg_container)
        layout.addWidget(self.scroll_area, 1)

        # 输入区
        input_area = QWidget()
        input_area.setStyleSheet("background: #fff; border-top: 1px solid #e0e0e0;")
        input_layout = QHBoxLayout(input_area)
        input_layout.setContentsMargins(12, 8, 12, 8)

        self.input_box = QTextEdit()
        self.input_box.setMaximumHeight(100)
        self.input_box.setPlaceholderText("输入消息... (Enter发送, Shift+Enter换行)")
        self.input_box.setStyleSheet("""
            border: 1px solid #e0e0e0; border-radius: 8px;
            padding: 8px; font-size: 13px;
        """)

        send_btn = QPushButton("发送")
        send_btn.setFixedSize(72, 36)
        send_btn.setStyleSheet("""
            QPushButton { background: #0084ff; color: white; border-radius: 8px; font-size: 13px; }
            QPushButton:hover { background: #0070e0; }
            QPushButton:pressed { background: #005cc0; }
        """)
        send_btn.clicked.connect(self._do_send)

        input_layout.addWidget(self.input_box)
        input_layout.addWidget(send_btn)
        layout.addWidget(input_area)

        # Enter发送
        self.input_box.installEventFilter(self)

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent
        if obj == self.input_box and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Return and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                self._do_send()
                return True
        return super().eventFilter(obj, event)

    def load_conversation(self, conv_id, name, is_group):
        self.current_conv = conv_id
        self.current_is_group = is_group
        self.title_bar.setText(('🏷 ' if is_group else '💬 ') + name)
        self.db.mark_read(conv_id)

        # 清空消息
        while self.msg_layout.count() > 1:
            item = self.msg_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 加载历史
        messages = self.db.get_messages(conv_id, limit=100)
        for row in messages:
            self._add_bubble({
                'from_user': row['from_user'],
                'content': row['content'],
                'timestamp': row['timestamp'],
                'is_group': row['is_group'],
            })

        QTimer.singleShot(100, self._scroll_to_bottom)

    def _add_bubble(self, msg_data):
        bubble = MessageBubble(msg_data, self.my_username)
        # 插到stretch前面
        self.msg_layout.insertWidget(self.msg_layout.count() - 1, bubble)

    def add_incoming(self, msg_data):
        if msg_data.get('conv_id') == self.current_conv:
            self._add_bubble(msg_data)
            QTimer.singleShot(50, self._scroll_to_bottom)

    def _scroll_to_bottom(self):
        bar = self.scroll_area.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _do_send(self):
        if not self.current_conv:
            return
        text = self.input_box.toPlainText().strip()
        if not text:
            return
        self.input_box.clear()
        self.send_message.emit(self.current_conv, text, self.current_is_group)


# ── 登录对话框 ────────────────────────────────────────────
class LoginDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("LiteChat - 登录")
        self.setFixedSize(360, 320)
        self.token = None
        self.username = None
        self.server_url = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(12)

        title = QLabel("LiteChat")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: #0084ff; margin-bottom: 8px;")
        layout.addWidget(title)

        form = QFormLayout()
        self.server_input = QLineEdit(DEFAULT_SERVER)
        self.username_input = QLineEdit()
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("服务器:", self.server_input)
        form.addRow("用户名:", self.username_input)
        form.addRow("密码:", self.password_input)
        layout.addLayout(form)

        btn_layout = QHBoxLayout()
        login_btn = QPushButton("登录")
        register_btn = QPushButton("注册")
        login_btn.setStyleSheet("background: #0084ff; color: white; padding: 8px; border-radius: 6px;")
        register_btn.setStyleSheet("background: #f0f0f0; padding: 8px; border-radius: 6px;")
        login_btn.clicked.connect(self._login)
        register_btn.clicked.connect(self._register)
        btn_layout.addWidget(login_btn)
        btn_layout.addWidget(register_btn)
        layout.addLayout(btn_layout)

        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("color: red;")
        layout.addWidget(self.status_label)

    def _get_http_url(self):
        url = self.server_input.text().strip()
        return url.replace('ws://', 'http://').replace('wss://', 'https://')

    def _login(self):
        username = self.username_input.text().strip()
        password = self.password_input.text()
        if not username or not password:
            self.status_label.setText("请输入用户名和密码")
            return
        try:
            r = requests.post(f"{self._get_http_url()}/api/login",
                              json={"username": username, "password": password}, timeout=5)
            if r.status_code == 200:
                data = r.json()
                self.token = data['token']
                self.username = data['username']
                self.server_url = self.server_input.text().strip()
                self.accept()
            else:
                self.status_label.setText(r.json().get('error', '登录失败'))
        except Exception as e:
            self.status_label.setText(f"连接失败: {e}")

    def _register(self):
        username = self.username_input.text().strip()
        password = self.password_input.text()
        if not username or not password:
            self.status_label.setText("请输入用户名和密码")
            return
        try:
            r = requests.post(f"{self._get_http_url()}/api/register",
                              json={"username": username, "password": password}, timeout=5)
            if r.status_code == 201:
                self.status_label.setStyleSheet("color: green;")
                self.status_label.setText("注册成功，请登录")
            else:
                self.status_label.setStyleSheet("color: red;")
                self.status_label.setText(r.json().get('error', '注册失败'))
        except Exception as e:
            self.status_label.setText(f"连接失败: {e}")


# ── 主窗口 ────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self, username, token, server_url, db: LocalDB):
        super().__init__()
        self.username = username
        self.token = token
        self.server_url = server_url
        self.db = db
        self.ws_worker = None

        self.setWindowTitle(f"LiteChat - {username}")
        self.resize(900, 620)
        self.setMinimumSize(700, 480)

        self._build_ui()
        self._start_ws()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 左侧边栏
        sidebar = QWidget()
        sidebar.setFixedWidth(260)
        sidebar.setStyleSheet("background: #f8f8f8; border-right: 1px solid #e0e0e0;")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)

        # 用户信息栏
        user_bar = QWidget()
        user_bar.setStyleSheet("background: #0084ff; padding: 12px;")
        user_bar_layout = QHBoxLayout(user_bar)
        user_bar_layout.setContentsMargins(12, 10, 12, 10)
        username_label = QLabel(f"👤 {self.username}")
        username_label.setStyleSheet("color: white; font-weight: bold; font-size: 14px;")
        user_bar_layout.addWidget(username_label)
        user_bar_layout.addStretch()

        # 操作按钮
        btn_layout = QHBoxLayout()
        new_chat_btn = QPushButton("💬")
        new_group_btn = QPushButton("👥")
        for btn in [new_chat_btn, new_group_btn]:
            btn.setFixedSize(30, 30)
            btn.setStyleSheet("color: white; background: transparent; font-size: 16px; border: none;")
        new_chat_btn.setToolTip("新建对话")
        new_group_btn.setToolTip("创建/加入群组")
        new_chat_btn.clicked.connect(self._new_chat)
        new_group_btn.clicked.connect(self._group_menu)
        btn_layout.addWidget(new_chat_btn)
        btn_layout.addWidget(new_group_btn)
        user_bar_layout.addLayout(btn_layout)
        sidebar_layout.addWidget(user_bar)

        # 连接状态
        self.status_label = QLabel("⚪ 连接中...")
        self.status_label.setStyleSheet("padding: 4px 12px; font-size: 11px; color: #888;")
        sidebar_layout.addWidget(self.status_label)

        # 对话列表
        self.conv_list = QListWidget()
        self.conv_list.setStyleSheet("""
            QListWidget { border: none; background: transparent; }
            QListWidget::item:selected { background: #e8f4ff; border-radius: 4px; }
            QListWidget::item:hover { background: #f0f0f0; border-radius: 4px; }
        """)
        self.conv_list.setSpacing(2)
        self.conv_list.currentItemChanged.connect(self._on_conv_selected)
        sidebar_layout.addWidget(self.conv_list, 1)

        main_layout.addWidget(sidebar)

        # 右侧聊天区
        self.chat_pane = ChatPane(self.username, self.db)
        self.chat_pane.send_message.connect(self._on_send)
        main_layout.addWidget(self.chat_pane, 1)

        self._refresh_conv_list()

    def _start_ws(self):
        self.ws_worker = WSWorker(self.server_url, self.token)
        self.ws_worker.message_received.connect(self._on_ws_message)
        self.ws_worker.connected.connect(lambda: self.status_label.setText("🟢 已连接"))
        self.ws_worker.disconnected.connect(lambda: self.status_label.setText("🔴 重连中..."))
        self.ws_worker.auth_ok.connect(lambda u: self.status_label.setText(f"🟢 在线"))
        self.ws_worker.auth_fail.connect(lambda m: QMessageBox.critical(self, "认证失败", m))
        self.ws_worker.start()

    @pyqtSlot(dict)
    def _on_ws_message(self, msg):
        msg_type = msg.get('type')

        if msg_type == 'chat':
            conv_id = msg['from']
            self.db.save_message(
                msg['msg_id'], conv_id, False,
                msg['from'], self.username,
                msg['content'], msg.get('content_type', 'text'),
                msg.get('timestamp')
            )
            self.db.increment_unread(conv_id)
            self._refresh_conv_list()
            self.chat_pane.add_incoming({**msg, 'from_user': msg['from'], 'conv_id': conv_id})

        elif msg_type == 'group_chat':
            conv_id = msg['group_id']
            self.db.save_message(
                msg['msg_id'], conv_id, True,
                msg['from'], conv_id,
                msg['content'], msg.get('content_type', 'text'),
                msg.get('timestamp')
            )
            self.db.increment_unread(conv_id)
            self._refresh_conv_list()
            self.chat_pane.add_incoming({**msg, 'from_user': msg['from'], 'conv_id': conv_id, 'is_group': True})

        elif msg_type == 'offline_msgs':
            for offline in msg.get('messages', []):
                self._on_ws_message(offline)

        elif msg_type == 'typing':
            pass  # TODO: show typing indicator

    @pyqtSlot(str, str, bool)
    def _on_send(self, conv_id, content, is_group):
        msg_id = str(uuid.uuid4())
        ts = int(time.time() * 1000)

        if is_group:
            self.ws_worker.send({
                "type": "group_chat",
                "group_id": conv_id,
                "content": content,
                "msg_id": msg_id,
                "timestamp": ts,
            })
        else:
            self.ws_worker.send({
                "type": "chat",
                "to": conv_id,
                "content": content,
                "msg_id": msg_id,
                "timestamp": ts,
            })

        # 立即显示（乐观更新）
        self.db.save_message(msg_id, conv_id, is_group, self.username, conv_id, content, 'text', ts)
        self._refresh_conv_list()
        self.chat_pane.add_incoming({
            'from_user': self.username,
            'content': content,
            'timestamp': ts,
            'is_group': is_group,
            'conv_id': conv_id,
        })

    def _refresh_conv_list(self):
        selected = self.conv_list.currentItem()
        selected_data = selected.data(Qt.ItemDataRole.UserRole) if selected else None

        self.conv_list.clear()
        convs = self.db.get_conversations()
        for conv in convs:
            item = QListWidgetItem()
            widget = ConvItem(
                conv['conv_id'], conv['name'],
                conv['last_msg'], conv['unread'], conv['is_group']
            )
            item.setSizeHint(widget.sizeHint())
            item.setData(Qt.ItemDataRole.UserRole, {
                'conv_id': conv['conv_id'],
                'name': conv['name'],
                'is_group': bool(conv['is_group'])
            })
            self.conv_list.addItem(item)
            self.conv_list.setItemWidget(item, widget)

            if selected_data and selected_data['conv_id'] == conv['conv_id']:
                self.conv_list.setCurrentItem(item)

    def _on_conv_selected(self, item):
        if not item:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            self.chat_pane.load_conversation(data['conv_id'], data['name'], data['is_group'])
            self._refresh_conv_list()

    def _new_chat(self):
        target, ok = QInputDialog.getText(self, "新建对话", "输入用户名:")
        if ok and target.strip():
            target = target.strip()
            if target == self.username:
                QMessageBox.warning(self, "错误", "不能和自己聊天")
                return
            # 添加到会话列表
            with self.db._lock:
                self.db.conn.execute(
                    "INSERT OR IGNORE INTO conversations (conv_id, is_group, name, last_msg, last_time) VALUES (?,0,?,?,0)",
                    (target, target, '')
                )
                self.db.conn.commit()
            self._refresh_conv_list()
            # 选中
            for i in range(self.conv_list.count()):
                item = self.conv_list.item(i)
                if item.data(Qt.ItemDataRole.UserRole)['conv_id'] == target:
                    self.conv_list.setCurrentItem(item)
                    break

    def _group_menu(self):
        menu = QMenu(self)
        create_act = menu.addAction("🆕 创建群组")
        join_act = menu.addAction("🔗 加入群组")
        action = menu.exec(self.cursor().pos())

        if action == create_act:
            name, ok = QInputDialog.getText(self, "创建群组", "群组名称:")
            if ok and name.strip():
                try:
                    http_url = self.server_url.replace('ws://', 'http://').replace('wss://', 'https://')
                    r = requests.post(f"{http_url}/api/group/create",
                                      headers={"Authorization": f"Bearer {self.token}"},
                                      json={"name": name.strip()}, timeout=5)
                    if r.status_code == 201:
                        data = r.json()
                        with self.db._lock:
                            self.db.conn.execute(
                                "INSERT OR IGNORE INTO conversations (conv_id, is_group, name, last_msg, last_time) VALUES (?,1,?,?,0)",
                                (data['group_id'], name.strip(), '')
                            )
                            self.db.conn.commit()
                        self._refresh_conv_list()
                        QMessageBox.information(self, "成功", f"群组创建成功\n群组ID: {data['group_id']}")
                    else:
                        QMessageBox.warning(self, "失败", r.json().get('error', '创建失败'))
                except Exception as e:
                    QMessageBox.critical(self, "错误", str(e))

        elif action == join_act:
            gid, ok = QInputDialog.getText(self, "加入群组", "输入群组ID:")
            if ok and gid.strip():
                try:
                    http_url = self.server_url.replace('ws://', 'http://').replace('wss://', 'https://')
                    r = requests.post(f"{http_url}/api/group/join",
                                      headers={"Authorization": f"Bearer {self.token}"},
                                      json={"group_id": gid.strip()}, timeout=5)
                    if r.status_code == 200:
                        data = r.json()
                        with self.db._lock:
                            self.db.conn.execute(
                                "INSERT OR IGNORE INTO conversations (conv_id, is_group, name, last_msg, last_time) VALUES (?,1,?,?,0)",
                                (gid.strip(), data.get('name', gid.strip()), '')
                            )
                            self.db.conn.commit()
                        self._refresh_conv_list()
                        QMessageBox.information(self, "成功", f"已加入群组: {data.get('name')}")
                    else:
                        QMessageBox.warning(self, "失败", r.json().get('error', '加入失败'))
                except Exception as e:
                    QMessageBox.critical(self, "错误", str(e))

    def closeEvent(self, event):
        if self.ws_worker:
            self.ws_worker.stop()
            self.ws_worker.wait(3000)
        event.accept()


# ── 入口 ─────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)

    # 全局样式
    app.setStyleSheet("""
        QMainWindow { background: #fff; }
        QDialog { background: #fff; }
        QLineEdit {
            border: 1px solid #ddd; border-radius: 6px;
            padding: 6px 10px; font-size: 13px;
        }
        QLineEdit:focus { border-color: #0084ff; }
        QPushButton { border: none; cursor: pointer; }
    """)

    db = LocalDB()

    # 检查保存的登录信息
    saved_token = db.get_config('token')
    saved_username = db.get_config('username')
    saved_server = db.get_config('server', DEFAULT_SERVER)

    token, username, server_url = None, None, None

    if saved_token and saved_username:
        # TODO: 可以加token验证
        reply = QMessageBox.question(
            None, "自动登录",
            f"以 {saved_username} 继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            token, username, server_url = saved_token, saved_username, saved_server

    if not token:
        dialog = LoginDialog()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            sys.exit(0)
        token = dialog.token
        username = dialog.username
        server_url = dialog.server_url
        db.set_config('token', token)
        db.set_config('username', username)
        db.set_config('server', server_url)

    window = MainWindow(username, token, server_url, db)
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
