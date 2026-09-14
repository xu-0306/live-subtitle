from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from PySide6 import QtCore, QtGui, QtSvg, QtWebSockets, QtWidgets

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui import config_manager
from gui.backend_runner import BackendRunner
from gui.download_worker import ModelDownloadWorker
from gui.vllm_settings import VllmSettingsWidget
from gui.local_model_controller import LocalModelController
from gui.service_catalog_widget import ServiceCatalogWidget
from backend.model_manager import WHISPER_MODEL_URLS, model_filename

MODEL_OPTIONS = [
    ("tiny", "Tiny"),
    ("base", "Base"),
    ("small", "Small"),
    ("medium", "Medium"),
    ("large-v3", "Large v3"),
]
SUBTITLE_MAX_CHARS_DEFAULT = 260
SUBTITLE_MAX_SENTENCES_DEFAULT = 2
SUBTITLE_MAX_SENTENCES_CJK = 1
SUBTITLE_HISTORY_LINES_DEFAULT = 2
SUBTITLE_SHOW_PARTIAL_DEFAULT = True
STALL_TIMEOUT_DEFAULT = 15
STALL_CHECK_INTERVAL_DEFAULT = 5


_ICON_PATHS = {
    "brand": "M12 2 3.5 6.5v11L12 22l8.5-4.5v-11L12 2Zm0 3.1 5.7 3v7.8l-5.7 3-5.7-3V8.1l5.7-3ZM8 10h2v4H8v-4Zm3 0h2v4h-2v-4Zm3 0h2v4h-2v-4Z",
    "server": "M4 4h16v6H4V4Zm0 10h16v6H4v-6Zm3-7h2v1H7V7Zm0 10h2v1H7v-1Z",
    "local": "M5 3h10l4 4v14H5V3Zm9 1v5h5M8 12h8M8 16h8",
    "stt": "M4 10h2v4H4v-4Zm4-4h2v12H8V6Zm4-3h2v18h-2V3Zm4 6h2v9h-2V9Z",
    "tuning": "M4 6h16M4 12h16M4 18h16M8 4v4m8 2v4m-5 4v4",
    "services": "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 4a5 5 0 1 0 0 10 5 5 0 0 0 0-10Zm0 4a1 1 0 1 0 0 2 1 1 0 0 0 0-2Z",
    "settings": "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm0-6 1 2.1a8 8 0 0 1 2 .8L17 3.7l2.3 2.3-1.2 2a8 8 0 0 1 .8 2L21 11v3l-2.1 1a8 8 0 0 1-.8 2l1.2 2-2.3 2.3-2-1.2a8 8 0 0 1-2 .8L12 23l-3-1-.2-2.1a8 8 0 0 1-2-.8l-2 1.2-2.3-2.3 1.2-2a8 8 0 0 1-.8-2L1 14v-3l2.1-1a8 8 0 0 1 .8-2l-1.2-2L5 3.7l2 1.2a8 8 0 0 1 2-.8L9 2h3Z",
    "light": "M12 3v2m0 14v2M3 12h2m14 0h2M5.6 5.6 7 7m10 10 1.4 1.4M5.6 18.4 7 17m10-10 1.4-1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z",
    "dark": "M20 15.3A8 8 0 0 1 8.7 4 8.5 8.5 0 1 0 20 15.3Z",
    "system": "M4 5h16v11H4V5Zm-2 14h20M9 19h6",
    "start": "M7 4v16l13-8L7 4Z",
    "stop": "M6 6h12v12H6V6Z",
    "restart": "M20 11a8 8 0 1 0 1 4M20 5v6h-6",
    "copy": "M8 8h11v12H8V8ZM5 16H4V4h12v1",
    "folder": "M3 6h7l2 2h9v11H3V6Zm0 3h18",
}


def _svg_icon(name: str, color: str = "#2f76e8", size: int = 20) -> QtGui.QIcon:
    path = _ICON_PATHS.get(name, _ICON_PATHS["services"])
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round">'
        f'<path d="{path}"/></svg>'
    )
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(svg.encode("utf-8")))
    image = QtGui.QImage(size, size, QtGui.QImage.Format_ARGB32_Premultiplied)
    image.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(image)
    renderer.render(painter)
    painter.end()
    return QtGui.QIcon(QtGui.QPixmap.fromImage(image))


class _ChevronSpinBox(QtWidgets.QSpinBox):
    """Spin box with theme-safe chevrons over platform button wells."""

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        color = self.palette().color(QtGui.QPalette.Link)
        if not self.isEnabled():
            color = self.palette().color(QtGui.QPalette.Disabled, QtGui.QPalette.Text)
        pen = QtGui.QPen(color, 1.5, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
        painter.setPen(pen)
        x = self.width() - 10
        upper_y = max(7, self.height() // 4)
        lower_y = min(self.height() - 7, (self.height() * 3) // 4)
        painter.drawLine(x - 3, upper_y + 2, x, upper_y - 1)
        painter.drawLine(x, upper_y - 1, x + 3, upper_y + 2)
        painter.drawLine(x - 3, lower_y - 2, x, lower_y + 1)
        painter.drawLine(x, lower_y + 1, x + 3, lower_y - 2)


class MainWindow(QtWidgets.QMainWindow):
    """Desktop control surface with a persistent sidebar and stacked pages.

    ``auto_start`` and ``setup_tray`` are explicit seams for offscreen render
    tests. Production defaults retain the existing tray and automatic startup
    behavior, while tests can create an isolated window without spawning a
    backend or touching the user's config.
    """

    NAV_ITEMS = (
        ("Server", "server"),
        ("Local translation", "local"),
        ("STT models", "stt"),
        ("Tuning", "tuning"),
        ("Translation services", "services"),
        ("Settings", "settings"),
    )
    NAV_TO_TAB = {
        "server": 0,
        "local": 1,
        "stt": 2,
        "tuning": 3,
        "services": 5,
        "settings": 4,
    }

    def __init__(
        self,
        config_path: Path | str | None = None,
        *,
        auto_start: bool | None = None,
        setup_tray: bool | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Live Subtitle")
        self.setMinimumSize(900, 640)

        self.config_path = Path(config_path) if config_path else config_manager.ensure_user_config()
        if self.config_path.exists():
            self.config: Dict[str, Any] = self._load_config_snapshot()
        else:
            self.config = config_manager.load_default_config()
            config_manager.write_config(self.config_path, self.config)
        self.config_revision: Any = getattr(self, "config_revision", None)

        self.backend_runner: Optional[BackendRunner] = None
        self.download_worker: Optional[ModelDownloadWorker] = None
        self.tray: Optional[QtWidgets.QSystemTrayIcon] = None
        self.pending_start = False
        self.restart_pending = False
        self.force_close = False
        self.backend_failed = False
        self.last_backend_error: Optional[str] = None
        self._service_test_socket: Optional[QtWebSockets.QWebSocket] = None
        self._service_test_timer: Optional[QtCore.QTimer] = None
        self._service_test_result_received = False
        self._loading_ui = False
        self._dirty = False
        self._auto_start_enabled = (
            bool(auto_start)
            if auto_start is not None
            else os.getenv("STT_GUI_DISABLE_AUTOSTART", "").strip().lower()
            not in {"1", "true", "yes", "on"}
        )
        self._setup_tray_enabled = (
            bool(setup_tray)
            if setup_tray is not None
            else os.getenv("STT_GUI_DISABLE_TRAY", "").strip().lower()
            not in {"1", "true", "yes", "on"}
        )
        self._ui_font_family = self._ensure_ui_font()

        self._build_ui()
        self._apply_styles(self._theme_mode())
        self._load_config_into_ui(self.config)
        if self._setup_tray_enabled:
            self._setup_tray()
        self._update_ws_url()
        self._update_buttons()

        if self._auto_start_enabled:
            QtCore.QTimer.singleShot(0, self._auto_start_if_valid)

    @staticmethod
    def _ensure_ui_font() -> str:
        """Load the Windows UI font for offscreen/high-DPI rendering too."""

        candidates = (
            Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf",
            Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf",
        )
        for font_path in candidates:
            if font_path.exists():
                font_id = QtGui.QFontDatabase.addApplicationFont(str(font_path))
                if font_id >= 0:
                    families = QtGui.QFontDatabase.applicationFontFamilies(font_id)
                    if families:
                        return str(families[0])
        return "Sans Serif"

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        root = QtWidgets.QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        # Parent the hierarchy before constructing pages. PySide can otherwise
        # collect temporary page/layout wrappers while this large UI is still
        # being assembled, which invalidates child controls on Windows.
        self.setCentralWidget(central)

        self.sidebar = QtWidgets.QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setMinimumWidth(208)
        self.sidebar.setMaximumWidth(240)
        sidebar_layout = QtWidgets.QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(10, 20, 6, 16)
        sidebar_layout.setSpacing(8)

        brand_row = QtWidgets.QHBoxLayout()
        brand_icon = QtWidgets.QLabel()
        brand_icon.setObjectName("brandIcon")
        brand_icon.setAlignment(QtCore.Qt.AlignCenter)
        brand_icon.setPixmap(_svg_icon("brand", "#ffffff", 25).pixmap(25, 25))
        brand_icon.setFixedSize(38, 38)
        brand_row.addWidget(brand_icon)
        brand_text = QtWidgets.QVBoxLayout()
        brand_title = QtWidgets.QLabel("Live Subtitle")
        brand_title.setObjectName("brandTitle")
        brand_subtitle = QtWidgets.QLabel("Live speech and translation")
        brand_subtitle.setObjectName("sidebarMuted")
        brand_text.addWidget(brand_title)
        brand_text.addWidget(brand_subtitle)
        brand_row.addLayout(brand_text, 1)
        sidebar_layout.addLayout(brand_row)
        sidebar_layout.addSpacing(18)

        self.nav_buttons: list[QtWidgets.QToolButton] = []
        for index, (label, key) in enumerate(self.NAV_ITEMS):
            button = QtWidgets.QToolButton()
            button.setObjectName("navButton")
            button.setText(label)
            button.setIcon(_svg_icon(key, "#2f76e8", 20))
            button.setIconSize(QtCore.QSize(20, 20))
            button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            button.setMinimumHeight(42)
            button.clicked.connect(lambda _checked=False, i=index: self._navigate(i))
            sidebar_layout.addWidget(button)
            self.nav_buttons.append(button)
            if index == 0:
                button.setChecked(True)

        sidebar_layout.addStretch(1)
        self.sidebar_hint = QtWidgets.QLabel("Local first · GPU optional")
        self.sidebar_hint.setObjectName("sidebarMuted")
        self.sidebar_hint.setWordWrap(True)
        sidebar_layout.addWidget(self.sidebar_hint)
        self.sidebar_version = QtWidgets.QLabel("Desktop app · private by default")
        self.sidebar_version.setObjectName("sidebarMuted")
        self.sidebar_version.setWordWrap(True)
        sidebar_layout.addWidget(self.sidebar_version)
        root.addWidget(self.sidebar)

        content_root = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(content_root)
        content_layout.setContentsMargins(26, 22, 26, 12)
        content_layout.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        self.page_title = QtWidgets.QLabel("Server")
        self.page_title.setObjectName("pageTitle")
        self.page_subtitle = QtWidgets.QLabel("Start and manage the local STT backend.")
        self.page_subtitle.setObjectName("mutedText")
        self.page_subtitle.setWordWrap(True)
        heading = QtWidgets.QVBoxLayout()
        heading.addWidget(self.page_title)
        heading.addWidget(self.page_subtitle)
        header.addLayout(heading, 1)
        theme_widget = QtWidgets.QWidget(content_root)
        theme_row = QtWidgets.QHBoxLayout(theme_widget)
        theme_row.setContentsMargins(0, 0, 0, 0)
        theme_row.setSpacing(4)
        self.theme_buttons: dict[str, QtWidgets.QToolButton] = {}
        for mode, label in (("light", "☼ Light"), ("dark", "◐ Dark"), ("system", "▣ System")):
            button = QtWidgets.QToolButton()
            button.setObjectName("themeButton")
            button.setText(label.replace("☼ ", "").replace("◐ ", "").replace("▣ ", ""))
            button.setIcon(_svg_icon(mode, "#2f76e8", 17))
            button.setIconSize(QtCore.QSize(17, 17))
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, m=mode: self._set_theme_mode(m))
            theme_row.addWidget(button)
            self.theme_buttons[mode] = button
        header.addWidget(theme_widget)
        content_layout.addLayout(header)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.tabBar().hide()
        self.tabs.setDocumentMode(True)
        self.tabs.setObjectName("pageStack")
        server_page = self._build_server_tab()
        local_panel = QtWidgets.QWidget()
        self.local_models = LocalModelController(
            local_panel,
            self._model_storage_root_from_config(self.config)
            / config_manager.LOCAL_TRANSLATION_MODELS_DIR_NAME,
            lambda: self._collect_config_from_ui() if hasattr(self, "local_models") else self.config,
            self._persist_local_config,
            self,
        )
        self.local_models.activated.connect(self._on_local_model_activated)
        self.local_models.deactivated.connect(self._on_local_model_deactivated)
        self.local_models.failed.connect(lambda message: self._show_notice(message, "error"))
        local_page = self._wrap_scroll_page(local_panel)
        stt_page = self._build_stt_tab()
        tuning_page = self._build_tuning_tab()
        self.service_catalog = ServiceCatalogWidget()
        self.service_catalog.changed.connect(self._mark_dirty)
        self.service_catalog.notice.connect(lambda message: self._show_notice(message, "info"))
        self.service_catalog.test_requested.connect(self._test_translation_profile)
        self.vllm_settings = VllmSettingsWidget()
        self.services_page = self._build_services_page()
        settings_page = self._build_settings_page()
        self.tabs.addTab(server_page, "Server")
        self.tabs.addTab(local_page, "Local translation")
        self.tabs.addTab(stt_page, "STT models")
        self.tabs.addTab(tuning_page, "Tuning")
        self.tabs.addTab(settings_page, "Settings")
        # Keep the established tab order for callers that use tabText() while
        # the sidebar still presents Settings as its final utility item.
        self.tabs.addTab(self.services_page, "Translation services")
        self.tabs.currentChanged.connect(self._on_page_changed)
        content_layout.addWidget(self.tabs, 1)

        footer = QtWidgets.QHBoxLayout()
        self.dirty_label = QtWidgets.QLabel("All changes saved")
        self.dirty_label.setObjectName("mutedText")
        footer.addWidget(self.dirty_label)
        footer.addStretch(1)
        self.restart_notice = QtWidgets.QLabel("Some changes apply on the next backend restart.")
        self.restart_notice.setObjectName("mutedText")
        footer.addWidget(self.restart_notice)
        self.reset_btn = QtWidgets.QPushButton("Reset")
        self.reset_btn.setProperty("kind", "secondary")
        self.save_btn = QtWidgets.QPushButton("Save defaults")
        footer.addWidget(self.reset_btn)
        footer.addWidget(self.save_btn)
        content_layout.addLayout(footer)

        self.status_bar = QtWidgets.QStatusBar()
        self.status_bar.setObjectName("statusBar")
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready to manage the backend")
        self.save_btn.clicked.connect(self._save_defaults)
        self.reset_btn.clicked.connect(self._reset_defaults)
        root.addWidget(content_root, 1)
        self._on_page_changed(0)

    def _build_server_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        content = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(content)
        body.setContentsMargins(0, 4, 4, 20)
        body.setSpacing(14)

        connection = self._card("Server connection")
        form = QtWidgets.QFormLayout()
        connection.layout().addLayout(form)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        self.host_input = QtWidgets.QLineEdit()
        self.host_input.setPlaceholderText("127.0.0.1")
        self.port_input = _ChevronSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setMaximumWidth(160)
        ws_container = QtWidgets.QWidget()
        ws_layout = QtWidgets.QHBoxLayout(ws_container)
        ws_layout.setContentsMargins(0, 0, 0, 0)
        self.ws_url_input = QtWidgets.QLineEdit()
        self.ws_url_input.setReadOnly(True)
        self.copy_ws_btn = QtWidgets.QPushButton("Copy address")
        self.copy_ws_btn.setIcon(_svg_icon("copy"))
        self.copy_ws_btn.setProperty("kind", "secondary")
        ws_layout.addWidget(self.ws_url_input, 1)
        ws_layout.addWidget(self.copy_ws_btn)
        form.addRow("Host", self.host_input)
        form.addRow("Port", self.port_input)
        form.addRow("WebSocket URL", ws_container)
        body.addWidget(connection)

        controls = self._card("Server controls")
        controls_layout = controls.layout()
        control_row = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Start backend")
        self.start_btn.setIcon(_svg_icon("start"))
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.setIcon(_svg_icon("stop"))
        self.stop_btn.setProperty("kind", "secondary")
        self.restart_btn = QtWidgets.QPushButton("Restart")
        self.restart_btn.setIcon(_svg_icon("restart"))
        self.restart_btn.setProperty("kind", "secondary")
        control_row.addWidget(self.start_btn)
        control_row.addWidget(self.stop_btn)
        control_row.addWidget(self.restart_btn)
        control_row.addStretch(1)
        self.server_status = QtWidgets.QLabel("Stopped")
        self.server_status.setObjectName("statusBadge")
        self.server_status.setProperty("status", "idle")
        control_row.addWidget(self.server_status)
        controls_layout.addLayout(control_row)
        self.server_status_detail = QtWidgets.QLabel(
            "Start the local service and confirm it is ready before connecting."
        )
        self.server_status_detail.setObjectName("mutedText")
        self.server_status_detail.setWordWrap(True)
        controls_layout.addWidget(self.server_status_detail)

        error_row = QtWidgets.QHBoxLayout()
        self.error_toggle = QtWidgets.QToolButton()
        self.error_toggle.setText("Details and diagnostics")
        self.error_toggle.setCheckable(True)
        self.error_toggle.setChecked(False)
        self.error_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.logs_btn = QtWidgets.QPushButton("View logs")
        self.logs_btn.setProperty("kind", "secondary")
        error_row.addWidget(self.error_toggle)
        error_row.addStretch(1)
        error_row.addWidget(self.logs_btn)
        controls_layout.addLayout(error_row)
        self.error_details = QtWidgets.QPlainTextEdit()
        self.error_details.setReadOnly(True)
        self.error_details.setMaximumHeight(110)
        self.error_details.setPlaceholderText("Backend diagnostics will appear here after a failed start.")
        self.error_details.setVisible(False)
        controls_layout.addWidget(self.error_details)
        body.addWidget(controls)

        lower = QtWidgets.QHBoxLayout()
        about = self._card("About the backend")
        about_layout = about.layout()
        about_label = QtWidgets.QLabel(
            "The local service exposes WebSocket speech recognition and HTTP translation. "
            "Model downloads and profile saves are safe to manage while the server is stopped."
        )
        about_label.setObjectName("mutedText")
        about_label.setWordWrap(True)
        about_layout.addWidget(about_label)
        tips = self._card("Quick tips")
        tips_layout = tips.layout()
        tips_label = QtWidgets.QLabel(
            "• Keep the port available\n• Model loading happens when a capture selects it\n• Use View logs when a dependency fails\n• Save settings before restarting"
        )
        tips_label.setObjectName("mutedText")
        tips_label.setWordWrap(True)
        tips_layout.addWidget(tips_label)
        lower.addWidget(about, 1)
        lower.addWidget(tips, 1)
        body.addLayout(lower)
        body.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll)

        self.host_input.textChanged.connect(self._update_ws_url)
        self.host_input.textChanged.connect(self._mark_dirty)
        self.port_input.valueChanged.connect(self._update_ws_url)
        self.port_input.valueChanged.connect(self._mark_dirty)
        self.copy_ws_btn.clicked.connect(self._copy_ws_url)
        self.start_btn.clicked.connect(self._start_backend)
        self.stop_btn.clicked.connect(self._stop_backend)
        self.restart_btn.clicked.connect(self._restart_backend)
        self.error_toggle.toggled.connect(self.error_details.setVisible)
        self.error_toggle.toggled.connect(lambda _checked: self.error_toggle.setText("Details and diagnostics"))
        self.logs_btn.clicked.connect(self._show_logs_dialog)
        return tab

    def _build_stt_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        content = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(content)
        body.setContentsMargins(0, 4, 4, 20)
        body.setSpacing(14)

        model_group = self._card("STT model library")
        form = QtWidgets.QFormLayout()
        model_group.layout().addLayout(form)

        self.model_select_combo = QtWidgets.QComboBox()
        self.model_refresh_btn = QtWidgets.QPushButton("Refresh")
        self.model_download_btn = QtWidgets.QPushButton("Download")
        model_select_row = QtWidgets.QWidget()
        model_select_layout = QtWidgets.QHBoxLayout(model_select_row)
        model_select_layout.setContentsMargins(0, 0, 0, 0)
        model_select_layout.addWidget(self.model_select_combo, 1)
        model_select_layout.addWidget(self.model_refresh_btn)
        model_select_layout.addWidget(self.model_download_btn)

        self.download_progress = QtWidgets.QProgressBar()
        self.download_progress.setValue(0)
        self.download_progress.setTextVisible(True)
        self.download_progress.hide()
        self.download_label = QtWidgets.QLabel("")

        form.addRow("Default model", model_select_row)
        form.addRow("Download progress", self.download_progress)
        form.addRow("", self.download_label)
        self.stt_language_input = QtWidgets.QComboBox()
        self.stt_language_input.setEditable(True)
        self.stt_language_input.addItem("Automatic", "auto")
        self.stt_language_input.addItem("English", "en")
        self.stt_language_input.addItem("日本語", "ja")
        self.stt_language_input.addItem("繁體中文", "zh")
        self.stt_language_input.setToolTip("Open language values remain valid; enter a provider-supported code when needed.")
        self.stt_partial_check = QtWidgets.QCheckBox("Show partial recognition updates")
        form.addRow("Default language", self.stt_language_input)
        form.addRow("Recognition", self.stt_partial_check)
        body.addWidget(model_group)

        state_card = self._card("Availability")
        state_layout = state_card.layout()
        self.model_state_label = QtWidgets.QLabel(
            "Installed models are selectable immediately. Missing models stay visible so the server can start without downloading them."
        )
        self.model_state_label.setObjectName("mutedText")
        self.model_state_label.setWordWrap(True)
        state_layout.addWidget(self.model_state_label)
        body.addWidget(state_card)
        body.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll)

        self.model_refresh_btn.clicked.connect(self._refresh_model_list)
        self.model_download_btn.clicked.connect(self._download_selected_model)
        self.model_select_combo.currentIndexChanged.connect(self._update_model_state)
        self.stt_language_input.currentTextChanged.connect(self._mark_dirty)
        self.stt_partial_check.toggled.connect(self._mark_dirty)

        return tab

    def _build_tuning_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        content = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(content)
        body.setContentsMargins(0, 4, 4, 20)
        body.setSpacing(14)

        cleanup_group = QtWidgets.QGroupBox("Subtitle cleanup")
        cleanup_form = QtWidgets.QFormLayout(cleanup_group)
        self.subtitle_max_chars_input = QtWidgets.QSpinBox()
        self.subtitle_max_chars_input.setRange(60, 2000)
        self.subtitle_max_chars_input.setValue(SUBTITLE_MAX_CHARS_DEFAULT)
        self.subtitle_max_sentences_default_input = QtWidgets.QSpinBox()
        self.subtitle_max_sentences_default_input.setRange(1, 6)
        self.subtitle_max_sentences_default_input.setValue(SUBTITLE_MAX_SENTENCES_DEFAULT)
        self.subtitle_max_sentences_cjk_input = QtWidgets.QSpinBox()
        self.subtitle_max_sentences_cjk_input.setRange(1, 6)
        self.subtitle_max_sentences_cjk_input.setValue(SUBTITLE_MAX_SENTENCES_CJK)
        self.subtitle_history_lines_input = QtWidgets.QSpinBox()
        self.subtitle_history_lines_input.setRange(1, 4)
        self.subtitle_history_lines_input.setValue(SUBTITLE_HISTORY_LINES_DEFAULT)
        self.subtitle_show_partial_check = QtWidgets.QCheckBox("Show partial line")
        cleanup_form.addRow("Max chars", self.subtitle_max_chars_input)
        cleanup_form.addRow(
            "Max sentences (Latin)", self.subtitle_max_sentences_default_input
        )
        cleanup_form.addRow(
            "Max sentences (CJK)", self.subtitle_max_sentences_cjk_input
        )
        cleanup_form.addRow("History lines", self.subtitle_history_lines_input)
        cleanup_form.addRow("", self.subtitle_show_partial_check)
        cleanup_group.setObjectName("card")
        body.addWidget(cleanup_group)

        stability_group = QtWidgets.QGroupBox("STT stability")
        stability_form = QtWidgets.QFormLayout(stability_group)
        self.stall_timeout_input = QtWidgets.QSpinBox()
        self.stall_timeout_input.setRange(0, 300)
        self.stall_timeout_input.setValue(STALL_TIMEOUT_DEFAULT)
        self.stall_check_interval_input = QtWidgets.QSpinBox()
        self.stall_check_interval_input.setRange(0, 300)
        self.stall_check_interval_input.setValue(STALL_CHECK_INTERVAL_DEFAULT)
        stability_form.addRow("Stall timeout (sec)", self.stall_timeout_input)
        stability_form.addRow(
            "Stall check interval (sec)", self.stall_check_interval_input
        )
        stability_group.setObjectName("card")
        body.addWidget(stability_group)
        tuning_note = QtWidgets.QLabel("Subtitle cleanup applies to new capture sessions. Saving does not restart an active session.")
        tuning_note.setObjectName("mutedText")
        tuning_note.setWordWrap(True)
        body.addWidget(tuning_note)
        body.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll)

        for widget in (
            self.subtitle_max_chars_input,
            self.subtitle_max_sentences_default_input,
            self.subtitle_max_sentences_cjk_input,
            self.subtitle_history_lines_input,
            self.stall_timeout_input,
            self.stall_check_interval_input,
        ):
            widget.valueChanged.connect(self._mark_dirty)
        self.subtitle_show_partial_check.toggled.connect(self._mark_dirty)

        return tab

    @staticmethod
    def _card(title: str) -> QtWidgets.QFrame:
        card = QtWidgets.QFrame()
        card.setObjectName("card")
        layout = QtWidgets.QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        label = QtWidgets.QLabel(title)
        label.setObjectName("cardTitle")
        layout.addWidget(label)
        return card

    @staticmethod
    def _wrap_scroll_page(widget: QtWidgets.QWidget) -> QtWidgets.QScrollArea:
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setWidget(widget)
        return scroll

    @staticmethod
    def _nav_glyph(key: str) -> str:
        return {
            "server": "▣",
            "local": "▤",
            "stt": "◫",
            "tuning": "☷",
            "services": "◎",
            "settings": "⚙",
        }.get(key, "•")

    def _build_services_page(self) -> QtWidgets.QWidget:
        content = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(0, 4, 4, 20)
        layout.setSpacing(14)
        layout.addWidget(self.service_catalog)
        helper_header = QtWidgets.QLabel("Advanced vLLM launch helper")
        helper_header.setObjectName("cardTitle")
        layout.addWidget(helper_header)
        self.vllm_settings.setMinimumHeight(560)
        layout.addWidget(self.vllm_settings)
        notice = QtWidgets.QLabel(
            "Profiles are saved through the shared settings revision. The selection applies to the next capture."
        )
        notice.setObjectName("mutedText")
        notice.setWordWrap(True)
        layout.addWidget(notice)
        return self._wrap_scroll_page(content)

    def _build_settings_page(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(0, 4, 4, 20)
        layout.setSpacing(14)
        appearance = self._card("Appearance")
        appearance_layout = appearance.layout()
        appearance_layout.addWidget(QtWidgets.QLabel("Choose how the desktop surface follows your display settings."))
        row = QtWidgets.QHBoxLayout()
        for mode, label in (("light", "Light"), ("dark", "Dark"), ("system", "System")):
            button = QtWidgets.QPushButton(label)
            button.setIcon(_svg_icon(mode, "#2f76e8", 16))
            button.setIconSize(QtCore.QSize(16, 16))
            button.setProperty("kind", "secondary")
            button.clicked.connect(lambda _checked=False, m=mode: self._set_theme_mode(m))
            row.addWidget(button)
        row.addStretch(1)
        appearance_layout.addLayout(row)
        self.theme_status = QtWidgets.QLabel("System theme changes are applied when this window receives a palette update.")
        self.theme_status.setObjectName("mutedText")
        self.theme_status.setWordWrap(True)
        appearance_layout.addWidget(self.theme_status)
        layout.addWidget(appearance)

        behavior = self._card("Settings and lifecycle")
        behavior_layout = behavior.layout()
        settings_row = QtWidgets.QWidget()
        settings_row_layout = QtWidgets.QHBoxLayout(settings_row)
        settings_row_layout.setContentsMargins(0, 0, 0, 0)
        self.settings_location = QtWidgets.QLabel()
        self.settings_location.setObjectName("mutedText")
        self.settings_location.setWordWrap(True)
        settings_row_layout.addWidget(self.settings_location, 1)
        self.open_settings_folder_btn = QtWidgets.QToolButton()
        self.open_settings_folder_btn.setObjectName("openSettingsFolderButton")
        self.open_settings_folder_btn.setIcon(_svg_icon("folder", "#2f76e8", 18))
        self.open_settings_folder_btn.setIconSize(QtCore.QSize(18, 18))
        self.open_settings_folder_btn.setToolTip("Open settings folder")
        self.open_settings_folder_btn.setAccessibleName("Open settings folder")
        settings_row_layout.addWidget(self.open_settings_folder_btn, 0, QtCore.Qt.AlignTop)
        behavior_layout.addWidget(settings_row)
        self.restart_explainer = QtWidgets.QLabel(
            "Host, port, model defaults and profile edits are written atomically. Host/port changes apply after Restart; subtitle and language defaults apply to the next capture."
        )
        self.restart_explainer.setObjectName("mutedText")
        self.restart_explainer.setWordWrap(True)
        behavior_layout.addWidget(self.restart_explainer)
        layout.addWidget(behavior)

        storage = self._card("Model storage")
        storage_layout = storage.layout()
        storage_help = QtWidgets.QLabel(
            "One location for downloaded model assets. Whisper weights use Speech models; "
            "the built-in local translation runtime uses Local Translation Models."
        )
        storage_help.setObjectName("mutedText")
        storage_help.setWordWrap(True)
        storage_layout.addWidget(storage_help)
        storage_row = QtWidgets.QWidget()
        storage_row_layout = QtWidgets.QHBoxLayout(storage_row)
        storage_row_layout.setContentsMargins(0, 0, 0, 0)
        self.model_cache_input = QtWidgets.QLineEdit()
        self.model_cache_input.setPlaceholderText(str(config_manager.get_default_model_storage_dir()))
        self.model_cache_input.setAccessibleName("Model storage directory")
        self.model_cache_btn = QtWidgets.QPushButton("Browse")
        storage_row_layout.addWidget(self.model_cache_input, 1)
        storage_row_layout.addWidget(self.model_cache_btn)
        storage_layout.addWidget(storage_row)
        self.model_storage_paths = QtWidgets.QLabel()
        self.model_storage_paths.setObjectName("mutedText")
        self.model_storage_paths.setWordWrap(True)
        storage_layout.addWidget(self.model_storage_paths)
        layout.addWidget(storage)

        self.open_settings_folder_btn.clicked.connect(self._open_settings_folder)
        self.model_cache_btn.clicked.connect(self._browse_model_dir)
        self.model_cache_input.editingFinished.connect(self._model_storage_changed)

        about = self._card("About")
        about_layout = about.layout()
        about_text = QtWidgets.QLabel(
            "Live Subtitle · desktop control app\n"
            "Turn browser audio into live captions with optional translation."
        )
        about_text.setObjectName("mutedText")
        about_layout.addWidget(about_text)
        layout.addWidget(about)
        layout.addStretch(1)
        return self._wrap_scroll_page(tab)

    def _navigate(self, index: int) -> None:
        if not (0 <= index < len(self.NAV_ITEMS)):
            return
        key = self.NAV_ITEMS[index][1]
        self.tabs.setCurrentIndex(self.NAV_TO_TAB.get(key, index))

    def _on_page_changed(self, index: int) -> None:
        if not (0 <= index < self.tabs.count()):
            return
        tab_to_nav = {value: key for key, value in self.NAV_TO_TAB.items()}
        key = tab_to_nav.get(index, "server")
        label = next((label for label, item_key in self.NAV_ITEMS if item_key == key), "Server")
        subtitles = {
            "Server": "Start and manage the local STT backend.",
            "Local translation": "On-device runtime · download GGUF models and run them with the built-in llama.cpp service.",
            "STT models": "Choose defaults and manage downloaded speech recognition weights.",
            "Tuning": "Shape subtitle cleanup and stability for the next capture.",
            "Translation services": "Provider profiles · configure service connections and legacy adapters; managed llama.cpp files live under Local translation.",
            "Settings": "Appearance, persistence and lifecycle preferences.",
        }
        notices = {
            "Server": "Restart to apply host and port.",
            "Local translation": "Install or remove files immediately.",
            "STT models": "Defaults apply to the next capture.",
            "Tuning": "Applies to the next capture.",
            "Translation services": "Applies to the next capture.",
            "Settings": "Theme applies now; saved with revision.",
        }
        self.page_title.setText(label)
        self.page_subtitle.setText(subtitles.get(label, ""))
        if hasattr(self, "restart_notice"):
            self.restart_notice.setText(notices.get(label, ""))
        for i, button in enumerate(self.nav_buttons):
            button.setChecked(self.NAV_ITEMS[i][1] == key)

    def _mark_dirty(self, *_args: object) -> None:
        if self._loading_ui:
            return
        self._dirty = True
        if hasattr(self, "dirty_label"):
            self.dirty_label.setText("Unsaved changes")
            self.dirty_label.setProperty("status", "warning")
            self.dirty_label.style().unpolish(self.dirty_label)
            self.dirty_label.style().polish(self.dirty_label)

    def _show_notice(self, message: str, kind: str = "info") -> None:
        self.status_bar.showMessage(message, 6000)
        if hasattr(self, "server_status_detail") and kind == "error":
            self.server_status_detail.setText(message)

    def _test_translation_profile(self, profile: object) -> None:
        """Run the saved profile through the backend's real test protocol."""

        if not isinstance(profile, dict):
            self._show_notice("Select a saved service before testing it.", "error")
            return
        profile_id = str(profile.get("id") or "").strip()
        if not profile_id:
            self._show_notice("This service has no stable ID to test.", "error")
            return
        if self.backend_runner is None or not self.backend_runner.isRunning() or self.server_status.text() != "Ready":
            self._show_notice("Start the backend and wait for Ready before testing a service.", "error")
            return
        if self._dirty:
            self._show_notice("Save changes before testing this service.", "error")
            return

        old_socket = self._service_test_socket
        if old_socket is not None:
            old_socket.blockSignals(True)
            old_socket.close()
            old_socket.deleteLater()
        socket = QtWebSockets.QWebSocket(
            "",
            QtWebSockets.QWebSocketProtocol.Version.Version13,
            self,
        )
        self._service_test_socket = socket
        self._service_test_result_received = False
        self.service_catalog.set_test_busy(True)
        socket.connected.connect(
            lambda: socket.sendTextMessage(
                json.dumps(
                    {
                        "type": "test",
                        "target": "translation",
                        "version": 2,
                        "translation": {
                            "selection": {"kind": "profile", "id": profile_id}
                        },
                    }
                )
            )
        )
        socket.textMessageReceived.connect(self._on_service_test_message)
        socket.errorOccurred.connect(self._on_service_test_socket_error)
        socket.disconnected.connect(self._on_service_test_disconnected)
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(
            lambda: self._finish_service_test(False, "Service test timed out.")
        )
        self._service_test_timer = timer
        timer.start(10000)
        host = self.host_input.text().strip() or "127.0.0.1"
        if host == "0.0.0.0":
            host = "127.0.0.1"
        elif host in {"::", "[::]"}:
            host = "[::1]"
        elif ":" in host and not host.startswith("["):
            host = f"[{host}]"
        socket.open(QtCore.QUrl(f"ws://{host}:{self.port_input.value()}/asr"))
        self._show_notice(f"Testing {profile.get('name') or profile_id}…")

    def _on_service_test_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        message_type = payload.get("type")
        if message_type == "test_result":
            self._finish_service_test(
                bool(payload.get("ok")),
                str(payload.get("message") or "Service test completed."),
            )
        elif message_type == "error":
            self._finish_service_test(False, str(payload.get("message") or "Service test failed."))

    def _on_service_test_socket_error(self, _error: object = None) -> None:
        socket = self._service_test_socket
        if socket is None or self._service_test_result_received:
            return
        self._finish_service_test(False, socket.errorString() or "Could not connect to the backend.")

    def _on_service_test_disconnected(self) -> None:
        if not self._service_test_result_received:
            self._finish_service_test(False, "Backend closed the service test before returning a result.")

    def _finish_service_test(self, ok: bool, message: str) -> None:
        if self._service_test_result_received:
            return
        self._service_test_result_received = True
        timer = self._service_test_timer
        self._service_test_timer = None
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        socket = self._service_test_socket
        self._service_test_socket = None
        if socket is not None:
            socket.blockSignals(True)
            socket.close()
            socket.deleteLater()
        self.service_catalog.set_test_busy(False)
        self._show_notice(("Service test passed: " if ok else "Service test failed: ") + message, "info" if ok else "error")

    def _show_logs_dialog(self) -> None:
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Backend logs")
        dialog.resize(760, 420)
        layout = QtWidgets.QVBoxLayout(dialog)
        output = QtWidgets.QPlainTextEdit()
        output.setReadOnly(True)
        lines = getattr(self, "_log_lines", [])
        output.setPlainText("\n".join(lines) or "No backend output captured yet.")
        layout.addWidget(output)
        close_btn = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        close_btn.rejected.connect(dialog.reject)
        layout.addWidget(close_btn)
        dialog.exec()

    def _theme_mode(self) -> str:
        gui = self.config.get("gui") if isinstance(self.config, dict) else {}
        gui_map = gui if isinstance(gui, dict) else {}
        value = str(gui_map.get("theme") or "system").strip().lower()
        return value if value in {"light", "dark", "system"} else "system"

    def _effective_theme(self, mode: str) -> str:
        if mode in {"light", "dark"}:
            return mode
        palette = QtWidgets.QApplication.palette()
        return "dark" if palette.color(QtGui.QPalette.Window).lightness() < 150 else "light"

    def _set_theme_mode(self, mode: str, *, persist: bool = True) -> None:
        if mode not in {"light", "dark", "system"}:
            return
        self.config.setdefault("gui", {})["theme"] = mode
        self._apply_styles(mode)
        for key, button in getattr(self, "theme_buttons", {}).items():
            button.setChecked(key == mode)
        if hasattr(self, "theme_status"):
            self.theme_status.setText(
                f"Theme preference: {mode.title()}. System follows the current OS palette."
            )
        if persist and not self._loading_ui:
            self._save_ui_preference()

    def _save_ui_preference(self) -> None:
        """Persist only the selected theme without claiming service activity."""

        cfg = copy.deepcopy(self.config)
        cfg.setdefault("gui", {})["theme"] = self._theme_mode()
        try:
            try:
                from backend import config_store  # type: ignore
            except ImportError:
                config_store = None
            if config_store is not None and callable(getattr(config_store, "save_config", None)):
                self.config_revision = config_store.save_config(
                    self.config_path,
                    cfg,
                    expected_revision=self.config_revision,
                )
            else:
                config_manager.write_config(self.config_path, cfg)
            self.config = cfg
        except Exception as exc:
            self._show_notice(f"Theme preference could not be saved: {exc}", "error")

    def _apply_styles(self, mode: str | None = None) -> None:
        requested = mode or self._theme_mode()
        effective = self._effective_theme(requested)
        if effective == "dark":
            colors = {
                "window": "#0a121d", "sidebar": "#0f1b2a", "card": "#111c2a",
                "input": "#162438", "border": "#2a405d", "text": "#edf4ff",
                "muted": "#95a8c3", "accent": "#3182f6", "accent_hover": "#4b98ff",
                "secondary": "#18293f", "secondary_hover": "#203752", "danger": "#4b2630",
                "success": "#36c98d", "warning": "#f0b35b", "error": "#ff7b86",
            }
        else:
            colors = {
                "window": "#f5f9fe", "sidebar": "#edf4fc", "card": "#ffffff",
                "input": "#fbfdff", "border": "#d7e4f3", "text": "#172a49",
                "muted": "#607696", "accent": "#2f76e8", "accent_hover": "#2465d2",
                "secondary": "#edf4ff", "secondary_hover": "#e1edff", "danger": "#fff0f1",
                "success": "#168d64", "warning": "#a56812", "error": "#c13e4c",
            }
        self.setStyleSheet(
            f"""
            QWidget {{ background: {colors['window']}; color: {colors['text']}; font-family: "{self._ui_font_family}"; font-size: 13px; }}
            QLabel {{ background: transparent; }}
            QMainWindow {{ background: {colors['window']}; }}
            QFrame#sidebar {{ background: {colors['sidebar']}; border-right: 1px solid {colors['border']}; }}
            QLabel#brandIcon {{ background: {colors['accent']}; color: white; border-radius: 11px; font-size: 13px; font-weight: 700; min-width: 38px; min-height: 38px; }}
            QLabel#brandTitle {{ color: {colors['text']}; font-size: 13px; font-weight: 700; }}
            QLabel#sidebarMuted, QLabel#mutedText {{ color: {colors['muted']}; }}
            QLabel#pageTitle {{ color: {colors['text']}; font-size: 29px; font-weight: 700; padding-bottom: 2px; }}
            QLabel#cardTitle {{ color: {colors['text']}; font-size: 16px; font-weight: 700; }}
            QLabel#cardTitleSmall {{ color: {colors['text']}; font-size: 13px; font-weight: 700; }}
            QFrame#card, QFrame#pageHeaderCard, QGroupBox#card {{ background: {colors['card']}; border: 1px solid {colors['border']}; border-radius: 12px; }}
            QGroupBox#card {{ margin-top: 8px; padding: 16px 12px 12px 12px; }}
            QGroupBox#card::title {{ subcontrol-origin: margin; left: 14px; padding: 0 6px; color: {colors['text']}; font-weight: 700; }}
            QToolButton#navButton {{ background: transparent; color: {colors['muted']}; border: 1px solid transparent; border-radius: 9px; text-align: left; padding: 10px 12px; font-size: 13px; }}
            QToolButton#navButton:hover {{ background: {colors['secondary_hover']}; color: {colors['text']}; }}
            QToolButton#navButton:checked {{ background: {colors['secondary']}; color: {colors['accent']}; border-left: 3px solid {colors['accent']}; padding-left: 9px; font-weight: 700; }}
            QToolButton#themeButton {{ background: transparent; color: {colors['muted']}; border: 1px solid {colors['border']}; border-radius: 7px; padding: 7px 9px; }}
            QToolButton {{ background: {colors['secondary']}; color: {colors['muted']}; border: 1px solid {colors['border']}; border-radius: 7px; padding: 6px 10px; }}
            QToolButton#themeButton:checked {{ background: {colors['secondary']}; color: {colors['accent']}; border-color: {colors['accent']}; }}
            QCheckBox, QRadioButton {{ background: transparent; }}
            QPushButton {{ background: {colors['accent']}; color: white; border: 1px solid {colors['accent']}; border-radius: 8px; padding: 8px 14px; min-height: 16px; }}
            QPushButton:hover {{ background: {colors['accent_hover']}; }}
            QPushButton:disabled {{ background: {colors['border']}; color: {colors['muted']}; border-color: {colors['border']}; }}
            QPushButton[kind="secondary"] {{ background: {colors['secondary']}; color: {colors['accent']}; border-color: {colors['border']}; }}
            QPushButton[kind="secondary"]:hover {{ background: {colors['secondary_hover']}; }}
            QPushButton[kind="danger"] {{ background: {colors['danger']}; color: {colors['error']}; border-color: {colors['error']}; }}
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QListWidget {{ background: {colors['input']}; color: {colors['text']}; border: 1px solid {colors['border']}; border-radius: 8px; padding: 7px 9px; selection-background-color: {colors['accent']}; }}
            QLineEdit:read-only {{ background: {colors['secondary']}; }}
            QComboBox::drop-down {{ width: 22px; border: none; border-left: 1px solid {colors['border']}; background: {colors['secondary']}; border-top-right-radius: 7px; border-bottom-right-radius: 7px; }}
            QAbstractSpinBox {{ padding-right: 20px; }}
            QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width: 18px; border: none; border-left: 1px solid {colors['border']}; background: {colors['secondary']}; }}
            QAbstractSpinBox::up-button {{ border-top-right-radius: 7px; }}
            QAbstractSpinBox::down-button {{ border-bottom-right-radius: 7px; }}
            QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover, QComboBox::drop-down:hover {{ background: {colors['secondary_hover']}; }}
            QListWidget::item {{ padding: 9px 7px; border-radius: 7px; }}
            QListWidget::item:selected {{ background: {colors['secondary']}; color: {colors['accent']}; }}
            QPlainTextEdit#commandPreview {{ font-family: "Cascadia Mono", "Consolas"; color: {colors['success']}; }}
            QProgressBar {{ background: {colors['input']}; border: 1px solid {colors['border']}; border-radius: 6px; text-align: center; height: 14px; }}
            QProgressBar::chunk {{ background: {colors['accent']}; border-radius: 6px; }}
            QLabel#statusBadge {{ background: {colors['secondary']}; color: {colors['accent']}; border: 1px solid {colors['border']}; border-radius: 10px; padding: 6px 10px; font-weight: 700; }}
            QLabel#statusBadge[status="running"] {{ color: {colors['success']}; }}
            QLabel#statusBadge[status="error"] {{ color: {colors['error']}; background: {colors['danger']}; }}
            QLabel#runtimeNotice {{ background: {colors['secondary']}; border: 1px solid {colors['border']}; border-radius: 8px; color: {colors['muted']}; padding: 9px; }}
            QFrame#vllmHeader {{ background: {colors['secondary']}; border: 1px solid {colors['border']}; border-left: 4px solid {colors['accent']}; border-radius: 10px; }}
            QLabel#eyebrow {{ color: {colors['accent']}; font-size: 10px; font-weight: 700; }}
            QLabel#sectionTitle {{ color: {colors['text']}; font-size: 20px; font-weight: 700; }}
            QCheckBox#vllmActivation {{ color: {colors['text']}; font-size: 13px; font-weight: 600; }}
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
            QScrollBar::handle:vertical {{ background: {colors['border']}; min-height: 28px; border-radius: 5px; }}
            QScrollBar::handle:vertical:hover {{ background: {colors['accent']}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; background: transparent; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
            QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
            QScrollBar::handle:horizontal {{ background: {colors['border']}; min-width: 28px; border-radius: 5px; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; background: transparent; }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}
            QStatusBar {{ background: {colors['sidebar']}; color: {colors['muted']}; border-top: 1px solid {colors['border']}; }}
            """
        )
        for key, button in getattr(self, "theme_buttons", {}).items():
            button.setChecked(key == requested)

    def _setup_tray(self) -> None:
        icon = self.style().standardIcon(QtWidgets.QStyle.SP_MediaPlay)
        self.tray = QtWidgets.QSystemTrayIcon(icon, self)
        menu = QtWidgets.QMenu()

        open_action = menu.addAction("Open")
        start_action = menu.addAction("Start")
        stop_action = menu.addAction("Stop")
        restart_action = menu.addAction("Restart")
        menu.addSeparator()
        quit_action = menu.addAction("Quit")

        open_action.triggered.connect(self.showNormal)
        start_action.triggered.connect(self._start_backend)
        stop_action.triggered.connect(self._stop_backend)
        restart_action.triggered.connect(self._restart_backend)
        quit_action.triggered.connect(self._quit_from_tray)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        if reason == QtWidgets.QSystemTrayIcon.Trigger:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def _quit_from_tray(self) -> None:
        self.force_close = True
        self.close()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self._service_test_socket is not None:
            self._finish_service_test(False, "Window closing.")
        if not self.local_models.shutdown():
            self.status_bar.showMessage("Waiting for model operation to stop...")
            event.ignore()
            QtCore.QTimer.singleShot(500, self.close)
            return
        self._stop_backend()
        if self.backend_runner and not self.backend_runner.wait(6000):
            event.ignore()
            QtCore.QTimer.singleShot(500, self.close)
            return
        event.accept()
        QtWidgets.QApplication.quit()

    def changeEvent(self, event: QtCore.QEvent) -> None:
        if event.type() == QtCore.QEvent.ApplicationPaletteChange and self._theme_mode() == "system":
            self._apply_styles("system")
        if (
            event.type() == QtCore.QEvent.WindowStateChange
            and self.isMinimized()
            and self.tray
            and self.tray.isVisible()
        ):
            QtCore.QTimer.singleShot(0, self.hide)
            self.tray.showMessage(
                "Live Subtitle",
                "Minimized to tray",
                QtWidgets.QSystemTrayIcon.Information,
                1500,
            )
        super().changeEvent(event)

    def _update_ws_url(self) -> None:
        host = self.host_input.text().strip() or "127.0.0.1"
        port = self.port_input.value()
        self.ws_url_input.setText(f"ws://{host}:{port}/asr")

    def _copy_ws_url(self) -> None:
        QtWidgets.QApplication.clipboard().setText(self.ws_url_input.text())
        self.status_bar.showMessage("WebSocket URL copied", 2000)

    def _update_buttons(self) -> None:
        running = self.backend_runner is not None and self.backend_runner.isRunning()
        stopping = getattr(self, "server_status", None) is not None and self.server_status.text() == "Stopping"
        downloading = self.download_worker is not None and self.download_worker.isRunning()
        self.start_btn.setEnabled(not running and not downloading)
        self.restart_btn.setEnabled(not downloading)
        self.stop_btn.setEnabled(running and not stopping)
        if hasattr(self, "model_download_btn"):
            self.model_download_btn.setEnabled(not downloading)

    def _set_status(self, text: str, status_type: str = "idle") -> None:
        if self.backend_failed and status_type != "error":
            return
        normalized = str(text).strip().lower()
        if status_type == "idle" and normalized in {"starting", "ready", "running"}:
            status_type = "running"
        elif status_type == "idle" and normalized in {"error", "failed", "stopped unexpectedly"}:
            status_type = "error"
        self.server_status.setText(text)
        self.server_status.setProperty("status", status_type)
        self.server_status.style().unpolish(self.server_status)
        self.server_status.style().polish(self.server_status)
        if hasattr(self, "server_status_detail"):
            if text in {"Starting", "Ready", "Stopping", "Stopped"}:
                detail = {
                    "Starting": "Starting the local service…",
                    "Ready": "The local service is ready for connections.",
                    "Stopping": "Stopping the local service…",
                    "Stopped": "The service is stopped. Downloads and settings remain available.",
                }.get(text, text)
                self.server_status_detail.setText(detail)
        self.status_bar.showMessage(text)
        if self.tray:
            self.tray.setToolTip(text)

    def _auto_start_if_valid(self) -> None:
        if not self._validate_server_settings(show_error=False):
            self._set_status("Invalid config", "error")
            return
        self._start_backend()

    def _validate_server_settings(self, show_error: bool = True) -> bool:
        host = self.host_input.text().strip()
        port = self.port_input.value()
        if not host:
            self.host_input.setText("127.0.0.1")
            self._update_ws_url()
        if port < 1 or port > 65535:
            if show_error:
                self._set_status("Port must be 1-65535", "error")
            return False
        return True

    def _resolve_model_target(self) -> tuple[Path, str]:
        model_name = self.model_select_combo.currentData() or "medium"
        cache_dir = self._get_model_dir()
        return cache_dir / model_filename(model_name), model_name

    def _start_download(self, target_path: Path, model_name: str) -> None:
        url = WHISPER_MODEL_URLS.get(model_name)
        if not url:
            self._set_status("Unknown model size", "error")
            return
        self.download_worker = ModelDownloadWorker(url, target_path)
        self.download_worker.progress.connect(self._on_download_progress)
        self.download_worker.finished.connect(self._on_download_finished)
        self.download_progress.show()
        self.download_progress.setValue(0)
        self.download_label.setText(f"Downloading {model_name}...")
        self.download_worker.start()
        self._update_buttons()

    def _on_download_progress(self, downloaded: int, total: int) -> None:
        if total > 0:
            percent = int(downloaded / total * 100)
            self.download_progress.setValue(percent)
            self.download_label.setText(f"{percent}% ({downloaded // (1024*1024)} MB / {total // (1024*1024)} MB)")
        else:
            self.download_progress.setRange(0, 0)
            self.download_label.setText(f"{downloaded // (1024*1024)} MB")

    def _on_download_finished(self, ok: bool, message: str) -> None:
        if self.download_worker:
            self.download_worker.deleteLater()
        self.download_worker = None
        self.download_progress.setRange(0, 100)
        if ok:
            self.download_progress.setValue(100)
            self.download_label.setText("Download complete")
            if self.pending_start:
                self.pending_start = False
                self._start_backend()
        else:
            self.download_label.setText(message or "Download failed")
            self._set_status("Download failed", "error")
            self.pending_start = False
        self._update_buttons()

    def _ensure_model_ready(self) -> bool:
        target_path, model_name = self._resolve_model_target()
        is_standard = model_name in WHISPER_MODEL_URLS
        if target_path.exists():
            return True
        if not is_standard:
            QtWidgets.QMessageBox.warning(
                self,
                "Model missing",
                "Selected model file was not found in the directory.",
            )
            self._set_status("Model missing", "error")
            return False
        reply = QtWidgets.QMessageBox.question(
            self,
            "Model missing",
            f"{model_name}.pt was not found. Download it now?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            self._set_status("Model missing", "error")
            return False
        self.pending_start = True
        self._start_download(target_path, model_name)
        return False

    def _on_runner_status(self, text: str) -> None:
        status_type = {
            "Starting": "running",
            "Ready": "running",
            "Stopping": "idle",
            "Stopped": "idle",
        }.get(text, "idle")
        self._set_status(text, status_type)

    def _on_runner_log(self, line: str) -> None:
        self._log_lines = (getattr(self, "_log_lines", []) + [str(line)])[-200:]
        if hasattr(self, "error_details"):
            self.error_details.setPlainText("\n".join(self._log_lines[-20:]))

    def _start_backend(self) -> None:
        self.backend_failed = False
        self.last_backend_error = None
        try:
            if self.local_models.busy:
                self._set_status("Preparing local translation", "idle")
                return
            if self.backend_runner and self.backend_runner.isRunning():
                self._set_status("Already running", "running")
                return
            if not self._validate_server_settings():
                return
            if not self._save_config_to_disk():
                return
            host = self.host_input.text().strip()
            port = self.port_input.value()
            self.backend_runner = BackendRunner(str(self.config_path), host, port)
            self._log_lines = []
            self.backend_runner.status.connect(self._on_runner_status)
            self.backend_runner.log.connect(self._on_runner_log)
            self.backend_runner.error.connect(self._on_backend_error)
            self.backend_runner.started.connect(lambda: self._set_status("Ready", "running"))
            self.backend_runner.stopped.connect(self._on_backend_stopped)
            self.backend_runner.start()
            self._set_status("Starting", "running")
            self._update_buttons()
        except Exception as exc:
            self._on_backend_error(f"{type(exc).__name__}: {exc}")

    def _stop_backend(self) -> None:
        self.backend_failed = False
        self.last_backend_error = None
        if self.backend_runner and self.backend_runner.isRunning():
            self.backend_runner.stop()
            self._set_status("Stopping", "idle")
        self._update_buttons()

    def _restart_backend(self) -> None:
        if self.backend_runner and self.backend_runner.isRunning():
            self.restart_pending = True
            self._stop_backend()
            return
        self._start_backend()

    def _on_backend_stopped(self) -> None:
        if self.backend_runner:
            self.backend_runner.deleteLater()
        self.backend_runner = None
        if self.restart_pending:
            self.restart_pending = False
            self._start_backend()
            return
        if not self.backend_failed:
            self._set_status("Stopped", "idle")
        self._update_buttons()

    def _on_backend_error(self, message: str) -> None:
        self.backend_failed = True
        self.last_backend_error = message
        if hasattr(self, "error_details"):
            self.error_details.setPlainText("\n".join(getattr(self, "_log_lines", []) + [message]))
            self.error_details.setVisible(self.error_toggle.isChecked())
        self._set_status(message, "error")

    def _browse_model_dir(self) -> None:
        start_dir = self.model_cache_input.text().strip()
        if not start_dir:
            start_dir = str(config_manager.get_default_model_storage_dir())
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select model storage directory",
            start_dir,
        )
        if path:
            self.model_cache_input.setText(path)
            self._model_storage_changed()

    def _open_settings_folder(self) -> None:
        folder = self.config_path.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._show_notice(f"Could not create settings folder: {exc}", "error")
            return
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(folder))):
            self._show_notice(f"Could not open settings folder: {folder}", "error")

    def _model_storage_root_from_config(self, cfg: Dict[str, Any]) -> Path:
        storage = cfg.get("models") if isinstance(cfg.get("models"), dict) else {}
        configured = str(storage.get("root") or "").strip()
        if configured:
            return Path(configured).expanduser()

        stt = cfg.get("stt") if isinstance(cfg.get("stt"), dict) else {}
        stt_dir = str(stt.get("model_cache_dir") or "").strip()
        if stt_dir:
            path = Path(stt_dir).expanduser()
            if path.name.casefold() in {
                config_manager.SPEECH_MODELS_DIR_NAME.casefold(),
                "models",
            }:
                return path.parent

        local = cfg.get("local_llama") if isinstance(cfg.get("local_llama"), dict) else {}
        local_dir = str(local.get("root") or "").strip()
        if local_dir:
            path = Path(local_dir).expanduser()
            if path.name.casefold() in {
                config_manager.LOCAL_TRANSLATION_MODELS_DIR_NAME.casefold(),
                "managed-models",
            }:
                return path.parent

        # An explicitly supplied config path keeps tests and portable builds
        # self-contained; the production config lives in the default app dir.
        return self.config_path.parent

    def _get_model_storage_root(self) -> Path:
        configured = self.model_cache_input.text().strip()
        return Path(configured).expanduser() if configured else config_manager.get_default_model_storage_dir()

    def _get_model_dir(self) -> Path:
        return (
            self._get_model_storage_root()
            / config_manager.SPEECH_MODELS_DIR_NAME
        )

    def _get_local_model_root(self) -> Path:
        return (
            self._get_model_storage_root()
            / config_manager.LOCAL_TRANSLATION_MODELS_DIR_NAME
        )

    def _update_model_storage_paths(self) -> None:
        root = self._get_model_storage_root()
        self.model_storage_paths.setText(
            f"Speech models: {root / config_manager.SPEECH_MODELS_DIR_NAME}\n"
            f"Local translation models: "
            f"{root / config_manager.LOCAL_TRANSLATION_MODELS_DIR_NAME}"
        )

    def _model_storage_changed(self) -> None:
        self.local_models.set_root(self._get_local_model_root())
        self._update_model_storage_paths()
        self._refresh_model_list()
        self._mark_dirty()

    def _scan_model_dir(self, model_dir: Path) -> Dict[str, Path]:
        if not model_dir.exists():
            return {}
        return {path.stem: path for path in model_dir.glob("*.pt") if path.is_file()}

    def _download_selected_model(self) -> None:
        if self.download_worker and self.download_worker.isRunning():
            self._set_status("Download already running", "error")
            return
        target_path, model_name = self._resolve_model_target()
        if target_path.exists():
            self._set_status("Model already present", "idle")
            return
        if model_name not in WHISPER_MODEL_URLS:
            QtWidgets.QMessageBox.warning(
                self,
                "Model download unavailable",
                "Selected model is custom and has no download URL.",
            )
            return
        self._start_download(target_path, model_name)

    def _refresh_model_list(self, preferred: Optional[str] = None) -> None:
        model_dir = self._get_model_dir()
        available = self._scan_model_dir(model_dir)
        current = preferred or self.model_select_combo.currentData() or "medium"

        self.model_select_combo.blockSignals(True)
        self.model_select_combo.clear()

        for key, label in MODEL_OPTIONS:
            suffix = "" if key in available else " (download)"
            self.model_select_combo.addItem(f"{label}{suffix}", key)

        for name in sorted(available.keys()):
            if name in WHISPER_MODEL_URLS:
                continue
            self.model_select_combo.addItem(f"{name} (custom)", name)

        target_index = next(
            (i for i in range(self.model_select_combo.count()) if self.model_select_combo.itemData(i) == current),
            None,
        )
        if target_index is None:
            self.model_select_combo.addItem(f"{current} (missing)", current)
            target_index = self.model_select_combo.count() - 1
        self.model_select_combo.setCurrentIndex(target_index)
        self.model_select_combo.blockSignals(False)
        self._update_model_state()

    def _update_model_state(self, *_args: object) -> None:
        """Show availability of the selected model independently of startup."""

        if not hasattr(self, "model_state_label") or not hasattr(self, "model_select_combo"):
            return
        model_name = str(self.model_select_combo.currentData() or "").strip()
        if not model_name:
            self.model_state_label.setText("Choose a model to see its availability.")
            return
        target = self._get_model_dir() / model_filename(model_name)
        if target.is_file():
            self.model_state_label.setText(
                f"{model_name} · Installed and selectable. Downloading is separate from starting the server."
            )
        else:
            self.model_state_label.setText(
                f"{model_name} · Missing from the model directory. The server can still start; download it before capture."
            )

    def _load_config_snapshot(self) -> Dict[str, Any]:
        """Load through the shared revision store when it is available."""

        try:
            from backend import config_store  # type: ignore
        except ImportError:
            config_store = None
        if config_store is not None and callable(getattr(config_store, "load_snapshot", None)):
            snapshot, revision = config_store.load_snapshot(self.config_path)
            self.config_revision = revision
            return dict(snapshot)
        self.config_revision = None
        return config_manager.load_config(self.config_path)

    def _save_config_to_disk(self) -> bool:
        updated = self._collect_config_from_ui()
        try:
            try:
                from backend import config_store  # type: ignore
            except ImportError:
                config_store = None
            if config_store is not None and callable(getattr(config_store, "save_config", None)):
                self.config_revision = config_store.save_config(
                    self.config_path,
                    updated,
                    expected_revision=self.config_revision,
                )
            else:
                config_manager.write_config(self.config_path, updated)
        except Exception as exc:
            # Revision conflicts are actionable: retain the user's edits in
            # the controls and require a fresh reload instead of overwriting a
            # migration or extension write with a stale snapshot.
            self._show_notice(f"Could not save settings: {exc}", "error")
            return False
        self.config = updated
        self._dirty = False
        if hasattr(self, "dirty_label"):
            self.dirty_label.setText("All changes saved")
        return True

    def _persist_local_config(self, cfg: Dict[str, Any]) -> None:
        try:
            try:
                from backend import config_store  # type: ignore
            except ImportError:
                config_store = None
            if config_store is not None and callable(getattr(config_store, "save_config", None)):
                self.config_revision = config_store.save_config(
                    self.config_path,
                    cfg,
                    expected_revision=self.config_revision,
                )
            else:
                config_manager.write_config(self.config_path, cfg)
        except Exception as exc:
            self._show_notice(f"Could not save local model settings: {exc}", "error")
            raise
        self.config = cfg

    def _on_local_model_activated(self, cfg: Dict[str, Any]) -> None:
        self.config = cfg
        self.vllm_settings.load_from_config(cfg)
        self.service_catalog.load_from_config(cfg)
        self.status_bar.showMessage("Model available in the extension. Refresh desktop services.")

    def _on_local_model_deactivated(self, cfg: Dict[str, Any]) -> None:
        self.config = cfg
        self.vllm_settings.load_from_config(cfg)
        self.service_catalog.load_from_config(cfg)
        self.status_bar.showMessage("Model choices updated; refresh desktop services in the extension.")

    def _save_defaults(self) -> None:
        if self._save_config_to_disk():
            self.status_bar.showMessage("Defaults saved · new capture defaults are ready", 3000)

    def _reset_defaults(self) -> None:
        defaults = config_manager.load_default_config()
        self.config = defaults
        self._load_config_into_ui(defaults)
        self.status_bar.showMessage("Reset to defaults", 2000)

    def _collect_config_from_ui(self) -> Dict[str, Any]:
        cfg = copy.deepcopy(self.config)
        cfg["server"] = dict(cfg.get("server") or {})
        cfg["stt"] = dict(cfg.get("stt") or {})
        cfg["subtitle"] = dict(cfg.get("subtitle") or {})
        cfg["models"] = dict(cfg.get("models") or {})

        cfg["server"]["host"] = self.host_input.text().strip() or "127.0.0.1"
        cfg["server"]["port"] = int(self.port_input.value())

        cfg["stt"].pop("simulstreaming", None)
        storage_root = self._get_model_storage_root()
        model_dir = storage_root / config_manager.SPEECH_MODELS_DIR_NAME
        cfg["models"]["root"] = str(storage_root)
        cfg["stt"]["model_cache_dir"] = str(model_dir)
        cfg["stt"].pop("model_path", None)
        cfg["stt"]["model"] = self.model_select_combo.currentData() or "medium"
        language_index = self.stt_language_input.currentIndex()
        cfg["stt"]["language"] = (
            self.stt_language_input.itemData(language_index)
            if language_index >= 0 and self.stt_language_input.currentText() == self.stt_language_input.itemText(language_index)
            else self.stt_language_input.currentText().strip() or "auto"
        )

        cfg["subtitle"]["max_chars"] = int(self.subtitle_max_chars_input.value())
        cfg["subtitle"]["max_sentences_default"] = int(
            self.subtitle_max_sentences_default_input.value()
        )
        cfg["subtitle"]["max_sentences_cjk"] = int(
            self.subtitle_max_sentences_cjk_input.value()
        )
        cfg["subtitle"]["history_lines"] = int(self.subtitle_history_lines_input.value())
        cfg["subtitle"]["show_partial"] = bool(
            self.subtitle_show_partial_check.isChecked()
        )
        cfg["stt"]["stall_timeout_sec"] = int(self.stall_timeout_input.value())
        cfg["stt"]["stall_check_interval_sec"] = int(
            self.stall_check_interval_input.value()
        )
        translation = cfg.setdefault("translation", {})
        translation["partial"] = bool(self.stt_partial_check.isChecked())
        cfg["gui"] = dict(cfg.get("gui") or {})
        cfg["gui"]["theme"] = self._theme_mode()
        cfg = self.service_catalog.collect_into_config(cfg)
        cfg = self.vllm_settings.collect_into_config(cfg)
        self.local_models.set_root(
            storage_root / config_manager.LOCAL_TRANSLATION_MODELS_DIR_NAME
        )
        cfg = self.local_models.collect_into_config(cfg)
        cfg.setdefault("local_llama", {})["root"] = str(
            storage_root / config_manager.LOCAL_TRANSLATION_MODELS_DIR_NAME
        )
        return cfg

    def _load_config_into_ui(self, cfg: Dict[str, Any]) -> None:
        self._loading_ui = True
        self.config = copy.deepcopy(cfg)
        self.config.setdefault("gui", {})
        self.settings_location.setText(
            f"Settings file\n{self.config_path}\nWrites use an atomic revision check when the shared backend store is available."
        )
        theme = self._theme_mode()
        self._set_theme_mode(theme, persist=False)
        server_cfg = cfg.get("server", {})
        self.host_input.setText(str(server_cfg.get("host", "127.0.0.1")))
        try:
            self.port_input.setValue(int(server_cfg.get("port", 8765)))
        except (TypeError, ValueError):
            self.port_input.setValue(8765)

        stt_cfg = cfg.get("stt", {})
        model_path = stt_cfg.get("model_path")
        selected_model = stt_cfg.get("model", "medium")
        if model_path:
            try:
                selected_model = Path(model_path).stem
            except OSError:
                pass
        storage_root = self._model_storage_root_from_config(cfg)
        self.model_cache_input.setText(str(storage_root))
        self.local_models.set_root(
            storage_root / config_manager.LOCAL_TRANSLATION_MODELS_DIR_NAME
        )
        self._update_model_storage_paths()
        self._refresh_model_list(selected_model)
        model_index = next(
            (i for i in range(self.model_select_combo.count()) if self.model_select_combo.itemData(i) == selected_model),
            None,
        )
        if model_index is not None:
            self.model_select_combo.setCurrentIndex(model_index)

        language = stt_cfg.get("language", "auto")
        lang_index = self.stt_language_input.findData(language)
        if lang_index >= 0:
            self.stt_language_input.setCurrentIndex(lang_index)
        else:
            self.stt_language_input.setCurrentText(str(language))
        translation_cfg = cfg.get("translation", {})
        if not isinstance(translation_cfg, dict):
            translation_cfg = {}
        self.stt_partial_check.setChecked(bool(translation_cfg.get("partial", False)))

        subtitle_cfg = cfg.get("subtitle", {})
        try:
            self.subtitle_max_chars_input.setValue(
                int(subtitle_cfg.get("max_chars", SUBTITLE_MAX_CHARS_DEFAULT))
            )
        except (TypeError, ValueError):
            self.subtitle_max_chars_input.setValue(SUBTITLE_MAX_CHARS_DEFAULT)
        try:
            self.subtitle_max_sentences_default_input.setValue(
                int(
                    subtitle_cfg.get(
                        "max_sentences_default", SUBTITLE_MAX_SENTENCES_DEFAULT
                    )
                )
            )
        except (TypeError, ValueError):
            self.subtitle_max_sentences_default_input.setValue(
                SUBTITLE_MAX_SENTENCES_DEFAULT
            )
        try:
            self.subtitle_max_sentences_cjk_input.setValue(
                int(
                    subtitle_cfg.get(
                        "max_sentences_cjk", SUBTITLE_MAX_SENTENCES_CJK
                    )
                )
            )
        except (TypeError, ValueError):
            self.subtitle_max_sentences_cjk_input.setValue(
                SUBTITLE_MAX_SENTENCES_CJK
            )
        try:
            self.subtitle_history_lines_input.setValue(
                int(subtitle_cfg.get("history_lines", SUBTITLE_HISTORY_LINES_DEFAULT))
            )
        except (TypeError, ValueError):
            self.subtitle_history_lines_input.setValue(SUBTITLE_HISTORY_LINES_DEFAULT)
        self.subtitle_show_partial_check.setChecked(
            bool(subtitle_cfg.get("show_partial", SUBTITLE_SHOW_PARTIAL_DEFAULT))
        )

        try:
            self.stall_timeout_input.setValue(
                int(stt_cfg.get("stall_timeout_sec", STALL_TIMEOUT_DEFAULT))
            )
        except (TypeError, ValueError):
            self.stall_timeout_input.setValue(STALL_TIMEOUT_DEFAULT)
        try:
            self.stall_check_interval_input.setValue(
                int(
                    stt_cfg.get(
                        "stall_check_interval_sec", STALL_CHECK_INTERVAL_DEFAULT
                    )
                )
            )
        except (TypeError, ValueError):
            self.stall_check_interval_input.setValue(STALL_CHECK_INTERVAL_DEFAULT)

        self.vllm_settings.load_from_config(cfg)
        self.service_catalog.load_from_config(cfg)
        self.local_models.load_config(cfg)
        self._update_ws_url()
        self._loading_ui = False
        self._dirty = False
        if hasattr(self, "dirty_label"):
            self.dirty_label.setText("All changes saved")


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
