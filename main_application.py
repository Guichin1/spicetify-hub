"""
main_application.py

Orquestrador de navegação do Spicetify Hub. Um QStackedWidget central
troca de tela sem recriar janela nem perder estado, e o
SpicetifyManager vive uma única vez aqui em cima — as telas recebem
uma referência a ele, nunca instanciam o próprio.

Fiz a janela frameless com uma titlebar customizada mínima, não só
"deixei o estilo escuro e pronto". É mais trabalho (perder decorações
nativas significa reimplementar arrastar, minimizar e fechar na mão),
mas um app que se vende como réplica visual do Spotify com barra de
título padrão do Windows/GNOME quebra a ilusão inteira — a barra de
título é a primeira coisa que o olho vê.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QGraphicsOpacityEffect,
    QSizeGrip,
)
from PySide6.QtCore import QPropertyAnimation, QEasingCurve

from spicetify_manager import SpicetifyManager
from spicetify_hub_ui import Palette, SpicetifyHubWindow  # reaproveita paleta e o dashboard já feito
from app_state import AppState
from splash_screen import SplashScreen, DiagnosticsResult


# --------------------------------------------------------------------- #
# Titlebar customizada — necessária porque a janela é frameless.
# Sem isso não tem como mover ou fechar a janela.
# --------------------------------------------------------------------- #
class TitleBar(QWidget):
    def __init__(self, parent: QMainWindow):
        super().__init__(parent)
        self._window = parent
        self._drag_offset: QPoint | None = None
        self.setFixedHeight(40)
        self.setObjectName("titleBar")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 8, 0)
        layout.setSpacing(8)

        title = QLabel("Spicetify Hub")
        title.setObjectName("titleBarLabel")

        self.minimize_button = QPushButton("—")
        self.minimize_button.setObjectName("titleBarButton")
        self.minimize_button.setFixedSize(32, 28)
        self.minimize_button.clicked.connect(self._window.showMinimized)

        self.close_button = QPushButton("✕")
        self.close_button.setObjectName("titleBarCloseButton")
        self.close_button.setFixedSize(32, 28)
        self.close_button.clicked.connect(self._window.close)

        layout.addWidget(title)
        layout.addStretch()
        layout.addWidget(self.minimize_button)
        layout.addWidget(self.close_button)

    # Arrastar a janela clicando na titlebar — o SO não faz isso
    # sozinho quando não há decoração nativa.
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self._window.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self._window.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        super().mouseReleaseEvent(event)


# --------------------------------------------------------------------- #
# Tela placeholder — Installer. O Splash real vive em splash_screen.py;
# o Dashboard reaproveita a janela já construída no módulo anterior.
# --------------------------------------------------------------------- #
class InstallerScreen(QWidget):
    def __init__(self, manager: SpicetifyManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.manager = manager
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        label = QLabel("Tela de instalação (placeholder)")
        label.setObjectName("installerLabel")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)


# --------------------------------------------------------------------- #
# Janela principal / orquestrador
# --------------------------------------------------------------------- #
class MainApplication(QMainWindow):
    def __init__(self):
        super().__init__()

        # Instanciado uma única vez, aqui em cima. Todas as telas
        # recebem esta mesma referência — nunca criam a própria, senão
        # cada uma teria seu próprio estado de instalação desalinhado.
        self.manager = SpicetifyManager()

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setMinimumSize(600, 680)

        self._build_ui()
        self._apply_stylesheet()
        self._register_screens()

        self.switch_screen(AppState.SPLASH)

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("appRoot")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.title_bar = TitleBar(self)
        root.addWidget(self.title_bar)

        self.stack = QStackedWidget()
        self.stack.setObjectName("screenStack")
        root.addWidget(self.stack)

        # Grip de redimensionamento no canto — sem borda nativa, o
        # usuário perderia a única forma óbvia de esticar a janela.
        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 4, 4)
        grip_row.addStretch()
        grip_row.addWidget(QSizeGrip(self), alignment=Qt.AlignBottom | Qt.AlignRight)
        root.addLayout(grip_row)

    # ------------------------------------------------------------------
    def _register_screens(self) -> None:
        self.splash_screen = SplashScreen(self.manager)
        self.splash_screen.navigate_requested.connect(self._on_splash_navigate)

        self._screens: dict[AppState, QWidget] = {
            AppState.SPLASH: self.splash_screen,
            AppState.INSTALLER: InstallerScreen(self.manager),
            # Dashboard é a janela do módulo 2 reaproveitada como
            # widget — evita reescrever cabeçalho/progresso/log aqui.
            AppState.DASHBOARD: self._build_dashboard_screen(),
        }
        for widget in self._screens.values():
            self.stack.addWidget(widget)

    def _on_splash_navigate(self, next_state: AppState, result: DiagnosticsResult) -> None:
        # O diagnóstico já rodou no Splash — não peço pro Installer
        # nem pro Dashboard refazer as mesmas checagens de subprocess.
        if next_state is AppState.INSTALLER:
            self.installer_diagnostics = result  # a InstallerScreen real (próximo passo) vai ler isto
        self.switch_screen(next_state)

    def _build_dashboard_screen(self) -> QWidget:
        # SpicetifyHubWindow é um QMainWindow; para viver dentro do
        # QStackedWidget eu uso o centralWidget dele como a tela, em
        # vez de aninhar uma QMainWindow dentro de outra (não suportado
        # de forma limpa pelo Qt).
        dashboard_window = SpicetifyHubWindow()
        dashboard_widget = dashboard_window.centralWidget()
        dashboard_widget.setParent(None)
        # Guardo a referência da janela original para acessar seus
        # atributos (progress_bar, status_spotify etc.) por fora.
        self.dashboard = dashboard_window
        return dashboard_widget

    # ------------------------------------------------------------------
    def switch_screen(self, state: AppState) -> None:
        """
        Troca de tela com um fade-in suave. Não é um crossfade real
        (as duas telas visíveis ao mesmo tempo, uma sumindo e outra
        aparecendo) porque o QStackedWidget só mostra um widget por
        vez — um crossfade de verdade exigiria capturar um snapshot
        (QPixmap) da tela atual e sobrepor como QLabel animado. Para
        uma troca de tela de app desktop, o fade-in simples já resolve
        a sensação de "não é um corte seco" sem essa complexidade extra.
        """
        widget = self._screens.get(state)
        if widget is None:
            raise ValueError(f"Estado sem tela registrada: {state}")

        self.stack.setCurrentWidget(widget)

        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)

        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(220)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        # Guarda referência para o GC não matar a animação no meio.
        self._current_animation = animation
        animation.start()

    # ------------------------------------------------------------------
    def _apply_stylesheet(self) -> None:
        self.setStyleSheet(f"""
            QWidget#appRoot {{
                background-color: {Palette.BG};
                border-radius: 12px;
            }}

            QWidget#titleBar {{
                background-color: {Palette.BG_ELEVATED};
                border-top-left-radius: 12px;
                border-top-right-radius: 12px;
            }}

            QLabel#titleBarLabel {{
                color: {Palette.TEXT_SECONDARY};
                font-size: 12px;
                font-weight: 600;
            }}

            QPushButton#titleBarButton {{
                background-color: transparent;
                color: {Palette.TEXT_SECONDARY};
                border: none;
                border-radius: 4px;
                font-size: 14px;
            }}
            QPushButton#titleBarButton:hover {{
                background-color: {Palette.BORDER};
            }}

            QPushButton#titleBarCloseButton {{
                background-color: transparent;
                color: {Palette.TEXT_SECONDARY};
                border: none;
                border-radius: 4px;
                font-size: 14px;
            }}
            QPushButton#titleBarCloseButton:hover {{
                background-color: {Palette.ERROR};
                color: #FFFFFF;
            }}

            QWidget#screenStack {{
                background-color: {Palette.BG};
            }}

            QLabel#installerLabel {{
                color: {Palette.TEXT_PRIMARY};
                font-size: 22px;
                font-weight: 700;
            }}
        """)


def main() -> None:
    import sys

    app = QApplication(sys.argv)
    window = MainApplication()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
