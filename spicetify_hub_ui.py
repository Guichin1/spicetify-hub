"""
spicetify_hub_ui.py

Janela principal do "Spicetify Hub" em PySide6. Dark mode com a paleta
oficial do Spotify (#121212 / #1DB954), status do sistema, progresso
central, ações principais e um log que fica escondido até o usuário
pedir — porque 90% do tempo ninguém quer ver stdout crua, só quando
algo dá errado.

Este arquivo é só a camada visual (placeholders nos sinais). A lógica
real de sistema fica no SpicetifyManager (spicetify_manager.py) — a
integração entre os dois é feita via run_async + callback, que emitem
sinais Qt de volta pra thread principal (ver seção de integração no
final do arquivo).
"""

from __future__ import annotations

import sys
from enum import Enum

from PySide6.QtCore import Qt, QSize, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QProgressBar,
    QTextEdit,
    QFrame,
    QSizePolicy,
)

from spicetify_manager import SpicetifyManager, CommandResult
from qt_thread_bridge import MainThreadInvoker


# --------------------------------------------------------------------- #
# Paleta — centralizada aqui de propósito. Se um dia a Spotify trocar o
# verde de novo (já trocaram o tom umas 3 vezes em anos recentes), você
# edita em um lugar só.
# --------------------------------------------------------------------- #
class Palette:
    BG = "#121212"
    BG_ELEVATED = "#1A1A1A"
    BG_CARD = "#1E1E1E"
    GREEN = "#1DB954"
    GREEN_HOVER = "#1ED760"
    GREEN_PRESSED = "#169C46"
    TEXT_PRIMARY = "#FFFFFF"
    TEXT_SECONDARY = "#B3B3B3"
    BORDER = "#2A2A2A"
    ERROR = "#F15E6C"
    WARNING = "#FFA42B"
    SUCCESS = GREEN
    PENDING = "#535353"


class StatusState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


STATUS_COLOR = {
    StatusState.PENDING: Palette.PENDING,
    StatusState.RUNNING: Palette.WARNING,
    StatusState.SUCCESS: Palette.SUCCESS,
    StatusState.ERROR: Palette.ERROR,
}


# --------------------------------------------------------------------- #
# Indicador de status: bolinha colorida + label. Reutilizado 3x no
# cabeçalho (Spotify / CLI / Permissões) em vez de duplicar código.
# --------------------------------------------------------------------- #
class StatusIndicator(QWidget):
    def __init__(self, label_text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._state = StatusState.PENDING

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._dot = QLabel()
        self._dot.setFixedSize(10, 10)
        self._dot.setObjectName("statusDot")

        self._label = QLabel(label_text)
        self._label.setObjectName("statusLabel")

        layout.addWidget(self._dot)
        layout.addWidget(self._label)
        layout.addStretch()

        self.set_state(StatusState.PENDING)

    def set_state(self, state: StatusState) -> None:
        self._state = state
        color = STATUS_COLOR[state]
        self._dot.setStyleSheet(
            f"background-color: {color}; border-radius: 5px;"
        )

    @property
    def state(self) -> StatusState:
        return self._state


# --------------------------------------------------------------------- #
# Log expansível — some por padrão. Um QPropertyAnimation na
# maxHeight dá a sensação de "gaveta abrindo" em vez de um show/hide
# seco, que parece bug de layout.
# --------------------------------------------------------------------- #
class CollapsibleLog(QWidget):
    COLLAPSED_HEIGHT = 0
    EXPANDED_HEIGHT = 220

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._expanded = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        self.toggle_button = QPushButton("Mostrar log ▾")
        self.toggle_button.setObjectName("logToggleButton")
        self.toggle_button.setCursor(Qt.PointingHandCursor)
        self.toggle_button.clicked.connect(self.toggle)

        self.log_box = QTextEdit()
        self.log_box.setObjectName("logBox")
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(self.COLLAPSED_HEIGHT)
        self.log_box.setFont(QFont("Monospace", 9))

        outer.addWidget(self.toggle_button)
        outer.addWidget(self.log_box)

        self._animation = QPropertyAnimation(self.log_box, b"maximumHeight")
        self._animation.setDuration(220)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)

    def toggle(self) -> None:
        self._expanded = not self._expanded
        start = self.log_box.maximumHeight()
        end = self.EXPANDED_HEIGHT if self._expanded else self.COLLAPSED_HEIGHT
        self.toggle_button.setText("Ocultar log ▴" if self._expanded else "Mostrar log ▾")

        self._animation.stop()
        self._animation.setStartValue(start)
        self._animation.setEndValue(end)
        self._animation.start()

    def append_line(self, text: str) -> None:
        self.log_box.append(text)


# --------------------------------------------------------------------- #
# Janela principal
# --------------------------------------------------------------------- #
class SpicetifyHubWindow(QMainWindow):
    def __init__(self, manager: SpicetifyManager):
        super().__init__()
        # Recebida da MainApplication — nunca instanciada aqui dentro.
        # Uma segunda instância de SpicetifyManager não quebraria nada
        # sozinha, mas romperia a premissa do projeto inteiro: "o
        # manager vive uma vez, lá em cima" (ver main_application.py).
        self.manager = manager
        # Vive na thread principal (parenteado a este QMainWindow) —
        # é o que faz o call_in_main_thread funcionar de verdade.
        self._invoker = MainThreadInvoker(self)

        self.setWindowTitle("Spicetify Hub")
        self.setMinimumSize(QSize(560, 620))

        self._build_ui()
        self._apply_stylesheet()
        self._connect_signals()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(18)

        root.addWidget(self._build_header())
        root.addWidget(self._build_progress_section())
        root.addWidget(self._build_actions_section())
        root.addWidget(self._build_log_section())
        root.addStretch()

    # ------------------------------------------------------------------
    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("headerCard")

        layout = QVBoxLayout(header)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        title_row = QHBoxLayout()
        title = QLabel("Spicetify Hub")
        title.setObjectName("titleLabel")
        subtitle = QLabel("Gerenciador de instalação e customização")
        subtitle.setObjectName("subtitleLabel")

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title_col.addWidget(title)
        title_col.addWidget(subtitle)

        title_row.addLayout(title_col)
        title_row.addStretch()

        status_row = QHBoxLayout()
        status_row.setSpacing(24)
        self.status_spotify = StatusIndicator("Spotify")
        self.status_cli = StatusIndicator("Spicetify CLI")
        self.status_permissions = StatusIndicator("Permissões")
        status_row.addWidget(self.status_spotify)
        status_row.addWidget(self.status_cli)
        status_row.addWidget(self.status_permissions)
        status_row.addStretch()

        layout.addLayout(title_row)
        layout.addLayout(status_row)
        return header

    # ------------------------------------------------------------------
    def _build_progress_section(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.progress_label = QLabel("Aguardando início")
        self.progress_label.setObjectName("progressLabel")
        self.progress_label.setAlignment(Qt.AlignCenter)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("progressBar")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(10)

        layout.addWidget(self.progress_label)
        layout.addWidget(self.progress_bar)
        return container

    # ------------------------------------------------------------------
    def _build_actions_section(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.auto_setup_button = QPushButton("Auto-Setup")
        self.auto_setup_button.setObjectName("primaryButton")
        self.auto_setup_button.setCursor(Qt.PointingHandCursor)
        self.auto_setup_button.setMinimumHeight(48)
        self.auto_setup_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        secondary_row = QHBoxLayout()
        secondary_row.setSpacing(10)

        self.restore_backup_button = QPushButton("Restaurar Backup")
        self.restore_backup_button.setObjectName("secondaryButton")
        self.restore_backup_button.setCursor(Qt.PointingHandCursor)

        self.install_marketplace_button = QPushButton("Instalar Marketplace")
        self.install_marketplace_button.setObjectName("secondaryButton")
        self.install_marketplace_button.setCursor(Qt.PointingHandCursor)

        secondary_row.addWidget(self.restore_backup_button)
        secondary_row.addWidget(self.install_marketplace_button)

        layout.addWidget(self.auto_setup_button)
        layout.addLayout(secondary_row)
        return container

    # ------------------------------------------------------------------
    def _build_log_section(self) -> QWidget:
        self.log_section = CollapsibleLog()
        return self.log_section

    # ------------------------------------------------------------------
    def _connect_signals(self) -> None:
        self.auto_setup_button.clicked.connect(self.on_auto_setup_clicked)
        self.restore_backup_button.clicked.connect(self.on_restore_backup_clicked)
        self.install_marketplace_button.clicked.connect(self.on_install_marketplace_clicked)

    # ------------------------------------------------------------------
    # Handlers reais. Os três seguem o mesmo padrão, por isso existe
    # _run_manager_action: disparar via run_async, jogar cada linha de
    # log no CollapsibleLog, e mostrar progresso HONESTO — QProgressBar
    # com range(0,0) vira automaticamente uma barra "ocupada" (padrão
    # do Qt), porque eu não tenho como saber a porcentagem real de um
    # `curl | sh` ou `spicetify restore`. Fingir um percentual
    # incremental aqui seria pior que não mostrar nada.
    # ------------------------------------------------------------------
    def on_auto_setup_clicked(self) -> None:
        # "Auto-Setup" no Dashboard não é a instalação inicial (quem
        # chega aqui já passou pelo Installer) — é reaplicar o
        # Spicetify, o caso real de uso pós-Dashboard: uma atualização
        # do Spotify sobrescreve o tema e o usuário precisa reaplicar.
        self._run_manager_action(
            action=self.manager.apply_spicetify,
            action_label="Auto-Setup",
            button=self.auto_setup_button,
        )

    def on_restore_backup_clicked(self) -> None:
        self._run_manager_action(
            action=self.manager.restore_spicetify,
            action_label="Restaurar Backup",
            button=self.restore_backup_button,
        )

    def on_install_marketplace_clicked(self) -> None:
        self._run_manager_action(
            action=self.manager.install_marketplace,
            action_label="Instalar Marketplace",
            button=self.install_marketplace_button,
        )

    # ------------------------------------------------------------------
    def _run_manager_action(self, action, action_label: str, button: QPushButton) -> None:
        self.log_section.append_line(f"[{action_label}] Iniciado pelo usuário.")
        self.update_progress(0, f"{action_label}...")
        self.progress_bar.setRange(0, 0)  # modo indeterminado
        self._set_actions_enabled(False)

        def on_line(line: str) -> None:
            # Chega na thread do worker (ver run_async/_run em
            # spicetify_manager.py). QTimer.singleShot NÃO funciona
            # aqui — testei isoladamente e confirmei que nunca dispara
            # vindo de uma threading.Thread crua. O invoker usa Signal,
            # que é thread-safe de verdade.
            self._invoker.call_in_main_thread(lambda: self.log_section.append_line(line))

        def on_finished(result: CommandResult) -> None:
            self._invoker.call_in_main_thread(
                lambda: self._on_action_finished(result, action_label, button)
            )

        self.manager.run_async(action, on_finished, on_line_received=on_line)

    def _on_action_finished(self, result: CommandResult, action_label: str, button: QPushButton) -> None:
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100 if result.success else 0)
        self.update_progress(
            100 if result.success else 0,
            f"{action_label} concluído." if result.success else f"{action_label} falhou.",
        )
        status_word = "Concluído" if result.success else "Falhou"
        self.log_section.append_line(f"[{action_label}] {status_word}: {result.message}")
        self._set_actions_enabled(True)

    def _set_actions_enabled(self, enabled: bool) -> None:
        # Desabilito os TRÊS botões durante qualquer ação, não só o
        # clicado. O SpicetifyManager serializa chamadas via lock
        # interno (ver run_async) — duas ações "simultâneas" na
        # prática rodariam uma depois da outra sem avisar ninguém, o
        # que pareceria a UI travada. Bloquear os botões deixa isso
        # explícito em vez de escondido.
        self.auto_setup_button.setEnabled(enabled)
        self.restore_backup_button.setEnabled(enabled)
        self.install_marketplace_button.setEnabled(enabled)

    # ------------------------------------------------------------------
    # API pública para quem for orquestrar (ex: um Controller que ouve
    # os callbacks do SpicetifyManager e chama estes métodos de volta
    # na thread da UI).
    # ------------------------------------------------------------------
    def update_progress(self, value: int, label: str | None = None) -> None:
        self.progress_bar.setValue(value)
        if label is not None:
            self.progress_label.setText(label)

    def update_status(self, which: str, state: StatusState) -> None:
        target = {
            "spotify": self.status_spotify,
            "cli": self.status_cli,
            "permissions": self.status_permissions,
        }.get(which)
        if target is not None:
            target.set_state(state)

    def append_log(self, text: str) -> None:
        self.log_section.append_line(text)

    # ------------------------------------------------------------------
    def _apply_stylesheet(self) -> None:
        self.setStyleSheet(f"""
            QWidget#central {{
                background-color: {Palette.BG};
            }}

            QFrame#headerCard {{
                background-color: {Palette.BG_CARD};
                border-radius: 12px;
                border: 1px solid {Palette.BORDER};
            }}

            QLabel#titleLabel {{
                color: {Palette.TEXT_PRIMARY};
                font-size: 20px;
                font-weight: 700;
            }}

            QLabel#subtitleLabel {{
                color: {Palette.TEXT_SECONDARY};
                font-size: 12px;
            }}

            QLabel#statusLabel {{
                color: {Palette.TEXT_SECONDARY};
                font-size: 12px;
            }}

            QLabel#progressLabel {{
                color: {Palette.TEXT_SECONDARY};
                font-size: 12px;
            }}

            QProgressBar#progressBar {{
                background-color: {Palette.BG_ELEVATED};
                border: none;
                border-radius: 5px;
            }}

            QProgressBar#progressBar::chunk {{
                background-color: {Palette.GREEN};
                border-radius: 5px;
            }}

            QPushButton#primaryButton {{
                background-color: {Palette.GREEN};
                color: #000000;
                font-size: 15px;
                font-weight: 700;
                border: none;
                border-radius: 22px;
                padding: 10px 24px;
            }}
            QPushButton#primaryButton:hover {{
                background-color: {Palette.GREEN_HOVER};
            }}
            QPushButton#primaryButton:pressed {{
                background-color: {Palette.GREEN_PRESSED};
            }}

            QPushButton#secondaryButton {{
                background-color: transparent;
                color: {Palette.TEXT_PRIMARY};
                font-size: 13px;
                font-weight: 600;
                border: 1px solid {Palette.BORDER};
                border-radius: 18px;
                padding: 8px 16px;
            }}
            QPushButton#secondaryButton:hover {{
                border-color: {Palette.TEXT_PRIMARY};
            }}
            QPushButton#secondaryButton:pressed {{
                background-color: {Palette.BG_ELEVATED};
            }}

            QPushButton#logToggleButton {{
                background-color: transparent;
                color: {Palette.TEXT_SECONDARY};
                font-size: 12px;
                border: none;
                text-align: left;
                padding: 4px 0px;
            }}
            QPushButton#logToggleButton:hover {{
                color: {Palette.TEXT_PRIMARY};
            }}

            QTextEdit#logBox {{
                background-color: {Palette.BG_ELEVATED};
                color: {Palette.TEXT_SECONDARY};
                border: 1px solid {Palette.BORDER};
                border-radius: 8px;
                padding: 8px;
            }}
        """)


def main() -> None:
    app = QApplication(sys.argv)
    manager = SpicetifyManager()  # só existe aqui porque este arquivo,
    # rodado standalone, não tem uma MainApplication por cima criando
    # o manager único. Em produção é o main_application.py quem cria
    # e injeta esta instância.
    window = SpicetifyHubWindow(manager)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
