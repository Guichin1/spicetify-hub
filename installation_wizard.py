"""
installation_wizard.py

Wizard de instalação para leigos. A ideia central: transformar uma
sequência de subprocess/pkexec — que pra maioria das pessoas é só
"texto branco assustador passando rápido no terminal" — numa lista de
cards que sobem de estado um de cada vez.

Duas decisões que fogem do que foi pedido literalmente, registradas
aqui em vez de escondidas no meio do código:

1. Adicionei os cards "Login no Spotify" e "Aplicar Tema", que não
   estavam nos 4 citados no prompt. Sem eles o wizard "termina" e o
   Spicetify nunca é de fato aplicado — seria uma barra de progresso
   que mente sobre estar completa.

2. Os cards são construídos dinamicamente a partir do DiagnosticsResult
   vindo do Splash. Se o Spotify já está via APT e o CLI já existe,
   esses cards nascem em estado "já satisfeito" (verde, sem pkexec, sem
   re-executar nada) em vez de fingir trabalho que não é necessário.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, Signal, QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFrame,
    QGraphicsOpacityEffect,
    QSizePolicy,
)

from spicetify_manager import SpicetifyManager, CommandResult
from spicetify_hub_ui import Palette
from splash_screen import Spinner, DiagnosticsResult


# --------------------------------------------------------------------- #
# Estado de cada card
# --------------------------------------------------------------------- #
class CardState:
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


@dataclass
class StepDefinition:
    key: str
    label: str
    action: Callable[[], CommandResult]
    requires_pkexec: bool = False
    overlay: Optional[str] = None  # "login" dispara o overlay de login
    already_satisfied: bool = False  # nasce SUCCESS sem executar nada


# --------------------------------------------------------------------- #
# Card individual de progresso
# --------------------------------------------------------------------- #
class ProgressCard(QFrame):
    def __init__(self, label_text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("progressCard")
        self._state = CardState.PENDING
        self._pulse_animation: QPropertyAnimation | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(12)

        self._icon_slot = QWidget()
        self._icon_slot.setFixedSize(22, 22)
        icon_layout = QVBoxLayout(self._icon_slot)
        icon_layout.setContentsMargins(0, 0, 0, 0)
        icon_layout.setAlignment(Qt.AlignCenter)

        self._icon_label = QLabel("●")
        self._icon_label.setObjectName("cardIconPending")
        self._icon_label.setAlignment(Qt.AlignCenter)
        icon_layout.addWidget(self._icon_label)

        self._spinner: Spinner | None = None

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        self._title_label = QLabel(label_text)
        self._title_label.setObjectName("cardTitlePending")
        self._detail_label = QLabel("")
        self._detail_label.setObjectName("cardDetail")
        self._detail_label.setWordWrap(True)
        self._detail_label.hide()
        text_col.addWidget(self._title_label)
        text_col.addWidget(self._detail_label)

        layout.addWidget(self._icon_slot)
        layout.addLayout(text_col, stretch=1)

        self.set_state(CardState.PENDING)

    # ------------------------------------------------------------------
    def set_state(self, state: str, detail: str = "") -> None:
        self._state = state
        self._stop_pulse()
        self._clear_spinner()

        if state == CardState.PENDING:
            self._icon_label.show()
            self._icon_label.setText("●")
            self._icon_label.setObjectName("cardIconPending")
            self._title_label.setObjectName("cardTitlePending")
            self._detail_label.hide()

        elif state == CardState.RUNNING:
            self._icon_label.hide()
            self._spinner = Spinner(self._icon_slot, diameter=18)
            self._icon_slot.layout().addWidget(self._spinner)
            self._title_label.setObjectName("cardTitleRunning")
            self._start_pulse()
            self._detail_label.hide()

        elif state == CardState.SUCCESS:
            self._icon_label.show()
            self._icon_label.setText("✓")
            self._icon_label.setObjectName("cardIconSuccess")
            self._title_label.setObjectName("cardTitleSuccess")
            self._detail_label.hide()

        elif state == CardState.ERROR:
            self._icon_label.show()
            self._icon_label.setText("✕")
            self._icon_label.setObjectName("cardIconError")
            self._title_label.setObjectName("cardTitleError")
            if detail:
                self._detail_label.setText(detail)
                self._detail_label.show()

        # Reaplica o objectName força o Qt a reler o QSS (setObjectName
        # sozinho não repinta se o widget já estava visível).
        for w in (self._icon_label, self._title_label):
            w.style().unpolish(w)
            w.style().polish(w)

    # ------------------------------------------------------------------
    def _clear_spinner(self) -> None:
        if self._spinner is not None:
            self._spinner.stop()
            self._spinner.deleteLater()
            self._spinner = None

    def _start_pulse(self) -> None:
        effect = QGraphicsOpacityEffect(self._title_label)
        self._title_label.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(900)
        anim.setStartValue(1.0)
        anim.setKeyValueAt(0.5, 0.45)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.InOutSine)
        anim.setLoopCount(-1)
        anim.start()
        self._pulse_animation = anim

    def _stop_pulse(self) -> None:
        if self._pulse_animation is not None:
            self._pulse_animation.stop()
            self._pulse_animation = None
        self._title_label.setGraphicsEffect(None)


# --------------------------------------------------------------------- #
# Wizard
# --------------------------------------------------------------------- #
class InstallationWizard(QWidget):
    setup_finished = Signal()

    def __init__(
        self,
        manager: SpicetifyManager,
        diagnostics: DiagnosticsResult | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.manager = manager
        self.diagnostics = diagnostics

        self._steps = self._build_plan(diagnostics)
        self._cards: list[ProgressCard] = []
        self._current_index = -1
        self._running = False

        self._build_ui()

    # ------------------------------------------------------------------
    def _build_plan(self, diagnostics: DiagnosticsResult | None) -> list[StepDefinition]:
        status = diagnostics.status if diagnostics else None
        cli_found = diagnostics.cli_found if diagnostics else False
        permissions_ok = diagnostics.permissions_ok if diagnostics else False

        steps: list[StepDefinition] = []

        is_snap = bool(status and status.source == "snap")
        is_installed_apt = bool(status and status.installed and status.source == "apt")

        if is_snap:
            # migrate_snap_to_apt já remove o snap E instala via apt
            # internamente — por isso os dois cards a seguir apontam
            # para a MESMA chamada. Ver nota no topo do arquivo.
            steps.append(StepDefinition(
                key="remove_snap",
                label="Remover Snap",
                action=self.manager.migrate_snap_to_apt,
                requires_pkexec=True,
            ))
            steps.append(StepDefinition(
                key="install_apt",
                label="Instalar via APT",
                action=lambda: CommandResult(True, "Incluído na migração acima.", ""),
                already_satisfied=False,
            ))
        elif not is_installed_apt:
            steps.append(StepDefinition(
                key="install_apt",
                label="Instalar via APT",
                action=self.manager.install_spotify_apt,
                requires_pkexec=True,
            ))
        else:
            steps.append(StepDefinition(
                key="install_apt",
                label="Instalar via APT",
                action=lambda: CommandResult(True, "Já instalado.", ""),
                already_satisfied=True,
            ))

        steps.append(StepDefinition(
            key="fix_permissions",
            label="Liberar pastas do Spotify",
            action=self.manager.fix_permissions,
            requires_pkexec=True,
            already_satisfied=permissions_ok,
        ))

        steps.append(StepDefinition(
            key="install_cli",
            label="Instalar Spicetify CLI",
            action=self.manager.install_spicetify_cli,
            already_satisfied=cli_found,
        ))

        steps.append(StepDefinition(
            key="login",
            label="Login no Spotify",
            action=self.manager.ensure_spotify_prefs,
            overlay="login",
        ))

        steps.append(StepDefinition(
            key="apply_theme",
            label="Aplicar tema Spicetify",
            action=self.manager.apply_spicetify,
        ))

        return steps

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        title = QLabel("Configurando o Spicetify")
        title.setObjectName("wizardTitle")
        subtitle = QLabel("Isso leva só alguns minutos. Você não precisa mexer em nada.")
        subtitle.setObjectName("wizardSubtitle")

        root.addWidget(title)
        root.addWidget(subtitle)

        # Banner de pkexec — escondido por padrão.
        self._pkexec_banner = QLabel(
            "🔒  Digite sua senha na janela do sistema para continuar"
        )
        self._pkexec_banner.setObjectName("pkexecBanner")
        self._pkexec_banner.setAlignment(Qt.AlignCenter)
        self._pkexec_banner.hide()
        root.addWidget(self._pkexec_banner)

        # Cards
        cards_container = QFrame()
        cards_container.setObjectName("cardsContainer")
        cards_layout = QVBoxLayout(cards_container)
        cards_layout.setContentsMargins(4, 4, 4, 4)
        cards_layout.setSpacing(8)

        for step in self._steps:
            card = ProgressCard(step.label)
            if step.already_satisfied:
                card.set_state(CardState.SUCCESS)
            cards_layout.addWidget(card)
            self._cards.append(card)

        root.addWidget(cards_container)
        root.addStretch()

        self._action_button = QPushButton("Iniciar Configuração")
        self._action_button.setObjectName("wizardActionButton")
        self._action_button.setCursor(Qt.PointingHandCursor)
        self._action_button.setMinimumHeight(46)
        self._action_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._action_button.clicked.connect(self._on_action_button_clicked)
        root.addWidget(self._action_button)

        self._apply_stylesheet()

        # Overlay de login — widget-filho posicionado por cima de tudo.
        self._login_overlay = self._build_login_overlay()
        self._login_overlay.hide()

    # ------------------------------------------------------------------
    def _build_login_overlay(self) -> QWidget:
        overlay = QWidget(self)
        overlay.setObjectName("loginOverlay")

        layout = QVBoxLayout(overlay)
        layout.setAlignment(Qt.AlignCenter)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(14)

        icon = QLabel("🎧")
        icon.setAlignment(Qt.AlignCenter)
        icon.setObjectName("loginOverlayIcon")

        message = QLabel(
            "O Spotify vai abrir agora.\n\n"
            "Faça login na sua conta e, quando terminar,\n"
            "feche o Spotify para finalizarmos a instalação."
        )
        message.setObjectName("loginOverlayMessage")
        message.setAlignment(Qt.AlignCenter)
        message.setWordWrap(True)

        layout.addWidget(icon)
        layout.addWidget(message)
        return overlay

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Overlay cobre o wizard inteiro — precisa acompanhar o resize
        # manualmente porque não está dentro do layout principal.
        self._login_overlay.setGeometry(self.rect())

    # ------------------------------------------------------------------
    def _on_action_button_clicked(self) -> None:
        if self._running:
            return

        if self._current_index >= len(self._steps):
            # Botão virou "Concluir"
            self.setup_finished.emit()
            return

        self._running = True
        self._action_button.setEnabled(False)
        self._action_button.setText("Configurando...")

        start_index = 0 if self._current_index == -1 else self._current_index
        self._current_index = start_index
        self._run_step(start_index)

    # ------------------------------------------------------------------
    def _run_step(self, index: int) -> None:
        if index >= len(self._steps):
            self._on_plan_finished()
            return

        self._current_index = index
        step = self._steps[index]
        card = self._cards[index]

        if step.already_satisfied:
            card.set_state(CardState.SUCCESS)
            # QTimer.singleShot(0, ...) em vez de recursão direta: dá
            # ao Qt uma chance de repintar o card antes de seguir, e
            # evita empilhar stack em planos totalmente já satisfeitos.
            QTimer.singleShot(0, lambda: self._run_step(index + 1))
            return

        card.set_state(CardState.RUNNING)

        if step.requires_pkexec:
            self._pkexec_banner.show()
        if step.overlay == "login":
            self._login_overlay.setGeometry(self.rect())
            self._login_overlay.show()
            self._login_overlay.raise_()

        self.manager.run_async(
            step.action,
            callback=lambda result: self._on_step_finished(index, result),
        )

    def _on_step_finished(self, index: int, result: CommandResult) -> None:
        # Callback chega numa thread de worker — agendo a atualização
        # de UI via QTimer.singleShot(0, ...) para garantir execução na
        # thread principal do Qt, em vez de tocar widgets diretamente
        # de dentro da thread do SpicetifyManager.
        QTimer.singleShot(0, lambda: self._handle_step_result(index, result))

    def _handle_step_result(self, index: int, result: CommandResult) -> None:
        step = self._steps[index]
        card = self._cards[index]

        self._pkexec_banner.hide()
        if step.overlay == "login":
            self._login_overlay.hide()

        if result.success:
            card.set_state(CardState.SUCCESS)
            self._run_step(index + 1)
        else:
            card.set_state(CardState.ERROR, detail=result.message)
            self._running = False
            self._action_button.setEnabled(True)
            self._action_button.setText("Tentar novamente")
            # current_index fica parado NESTE passo — "Tentar
            # novamente" reexecuta a partir daqui, não do zero.

    def _on_plan_finished(self) -> None:
        self._running = False
        self._current_index = len(self._steps)
        self._action_button.setEnabled(True)
        self._action_button.setText("Concluir")

    # ------------------------------------------------------------------
    def _apply_stylesheet(self) -> None:
        self.setStyleSheet(f"""
            QLabel#wizardTitle {{
                color: {Palette.TEXT_PRIMARY};
                font-size: 20px;
                font-weight: 700;
            }}
            QLabel#wizardSubtitle {{
                color: {Palette.TEXT_SECONDARY};
                font-size: 13px;
            }}

            QLabel#pkexecBanner {{
                background-color: rgba(255, 164, 43, 0.12);
                color: {Palette.WARNING};
                border: 1px solid {Palette.WARNING};
                border-radius: 8px;
                padding: 10px;
                font-size: 13px;
                font-weight: 600;
            }}

            QFrame#cardsContainer {{
                background-color: transparent;
            }}

            QFrame#progressCard {{
                background-color: {Palette.BG_CARD};
                border: 1px solid {Palette.BORDER};
                border-radius: 10px;
            }}

            QLabel#cardIconPending {{
                color: {Palette.PENDING};
                font-size: 12px;
            }}
            QLabel#cardIconSuccess {{
                color: {Palette.GREEN};
                font-size: 15px;
                font-weight: 800;
            }}
            QLabel#cardIconError {{
                color: {Palette.ERROR};
                font-size: 14px;
                font-weight: 800;
            }}

            QLabel#cardTitlePending {{
                color: {Palette.TEXT_SECONDARY};
                font-size: 14px;
            }}
            QLabel#cardTitleRunning {{
                color: {Palette.GREEN};
                font-size: 14px;
                font-weight: 600;
            }}
            QLabel#cardTitleSuccess {{
                color: {Palette.TEXT_PRIMARY};
                font-size: 14px;
                font-weight: 600;
            }}
            QLabel#cardTitleError {{
                color: {Palette.ERROR};
                font-size: 14px;
                font-weight: 600;
            }}
            QLabel#cardDetail {{
                color: {Palette.ERROR};
                font-size: 11px;
            }}

            QPushButton#wizardActionButton {{
                background-color: {Palette.GREEN};
                color: #000000;
                font-size: 15px;
                font-weight: 700;
                border: none;
                border-radius: 20px;
            }}
            QPushButton#wizardActionButton:hover {{
                background-color: {Palette.GREEN_HOVER};
            }}
            QPushButton#wizardActionButton:disabled {{
                background-color: {Palette.PENDING};
                color: {Palette.TEXT_SECONDARY};
            }}

            QWidget#loginOverlay {{
                background-color: rgba(18, 18, 18, 0.94);
            }}
            QLabel#loginOverlayIcon {{
                font-size: 40px;
            }}
            QLabel#loginOverlayMessage {{
                color: {Palette.TEXT_PRIMARY};
                font-size: 15px;
                line-height: 1.4;
            }}
        """)
