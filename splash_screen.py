"""
splash_screen.py

Tela de entrada do Spicetify Hub. Não pergunta nada ao usuário — só
observa o sistema e decide sozinha para onde navegar. O diagnóstico
roda numa QThread dedicada (não uso SpicetifyManager.run_async aqui
de propósito: preciso emitir texto de status por ETAPA, e
threading.Thread + callback simples não tem um jeito limpo de disparar
sinais intermediários de volta pra thread da UI — QThread com sinais
Qt resolve isso nativamente).

Regra de decisão para DASHBOARD (as três precisam ser verdadeiras):
  status.installed == True
  status.source == "apt"
  cli_found == True
Qualquer coisa fora disso (Spotify ausente, Snap, ou CLI ausente) vai
para o INSTALLER.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel

from app_state import AppState
from spicetify_manager import SpicetifyManager, SpotifyStatus
from spicetify_hub_ui import Palette


MIN_SPLASH_DURATION_MS = 2000


@dataclass
class DiagnosticsResult:
    status: SpotifyStatus
    cli_found: bool
    permissions_ok: bool

    @property
    def dashboard_ready(self) -> bool:
        return (
            self.status.installed
            and self.status.source == "apt"
            and self.cli_found
        )


# --------------------------------------------------------------------- #
# Worker — roda o diagnóstico fora da thread de UI e avisa cada etapa.
# --------------------------------------------------------------------- #
class DiagnosticsWorker(QThread):
    stage_changed = Signal(str)
    finished_diagnostics = Signal(object)  # DiagnosticsResult

    def __init__(self, manager: SpicetifyManager, parent: QWidget | None = None):
        super().__init__(parent)
        self._manager = manager

    def run(self) -> None:
        self.stage_changed.emit("Verificando integridade do Spotify...")
        status = self._manager.check_spotify_installation()

        self.stage_changed.emit("Checando permissões...")
        permissions_ok = self._check_permissions(status)

        self.stage_changed.emit("Checando Spicetify CLI...")
        cli_found = shutil.which("spicetify") is not None

        result = DiagnosticsResult(
            status=status,
            cli_found=cli_found,
            permissions_ok=permissions_ok,
        )
        self.finished_diagnostics.emit(result)

    def _check_permissions(self, status: SpotifyStatus) -> bool:
        # Checagem local e barata (os.access), sem subprocess/pkexec —
        # aqui é só diagnóstico, não é hora de pedir senha de root.
        # A correção de permissões de verdade (fix_permissions) fica
        # para o Installer, se for necessário.
        if status.source != "apt":
            return False
        paths = [p for p in self._manager.SPOTIFY_APT_PATHS if os.path.exists(p)]
        if not paths:
            return False
        return all(os.access(p, os.W_OK | os.R_OK) for p in paths)


# --------------------------------------------------------------------- #
# Spinner discreto — sem asset externo, desenhado via QPainter.
# --------------------------------------------------------------------- #
class Spinner(QWidget):
    def __init__(self, parent: QWidget | None = None, diameter: int = 28):
        super().__init__(parent)
        self._angle = 0
        self.setFixedSize(diameter, diameter)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)  # ~60fps, giro suave e discreto

    def _tick(self) -> None:
        self._angle = (self._angle + 6) % 360
        self.update()

    def stop(self) -> None:
        self._timer.stop()

    def paintEvent(self, event) -> None:  # noqa: N802 (nome exigido pelo Qt)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect().adjusted(3, 3, -3, -3)
        pen = QPen(QColor(Palette.GREEN))
        pen.setWidth(3)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)

        span_angle = 100 * 16  # Qt usa 1/16 de grau; arco curto = discreto
        start_angle = int(-self._angle * 16)
        painter.drawArc(rect, start_angle, span_angle)


# --------------------------------------------------------------------- #
# Tela de splash
# --------------------------------------------------------------------- #
class SplashScreen(QWidget):
    # Carrega (AppState, DiagnosticsResult | None) — o resultado vai
    # junto para quem for receber não precisar rodar o diagnóstico de
    # novo (o Installer, por exemplo, já sabe o que falta corrigir).
    navigate_requested = Signal(object, object)

    def __init__(self, manager: SpicetifyManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.manager = manager

        self._diagnostics_result: DiagnosticsResult | None = None
        self._diagnostics_done = False
        self._min_time_elapsed = False
        self._started = False

        self._build_ui()

        self._worker = DiagnosticsWorker(self.manager, self)
        self._worker.stage_changed.connect(self._on_stage_changed)
        self._worker.finished_diagnostics.connect(self._on_diagnostics_finished)

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(20)

        # Placeholder de logo: sem asset externo, um "selo" com a
        # paleta do app. Trocar por QPixmap quando houver arte real.
        logo = QLabel("S")
        logo.setObjectName("splashLogo")
        logo.setFixedSize(72, 72)
        logo.setAlignment(Qt.AlignCenter)

        self._spinner = Spinner(self)

        self._status_label = QLabel("Iniciando...")
        self._status_label.setObjectName("splashStatusLabel")
        self._status_label.setAlignment(Qt.AlignCenter)

        layout.addWidget(logo, alignment=Qt.AlignCenter)
        layout.addWidget(self._spinner, alignment=Qt.AlignCenter)
        layout.addWidget(self._status_label)

        self.setStyleSheet(f"""
            QLabel#splashLogo {{
                background-color: {Palette.BG_CARD};
                color: {Palette.GREEN};
                border: 2px solid {Palette.GREEN};
                border-radius: 36px;
                font-size: 28px;
                font-weight: 800;
            }}
            QLabel#splashStatusLabel {{
                color: {Palette.TEXT_SECONDARY};
                font-size: 13px;
            }}
        """)

    # ------------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # Garante que o diagnóstico só dispara uma vez, mesmo que o
        # usuário volte a ver esta tela (ex: reinício manual do fluxo).
        if not self._started:
            self._started = True
            self._start_diagnostics()

    def _start_diagnostics(self) -> None:
        self._worker.start()
        QTimer.singleShot(MIN_SPLASH_DURATION_MS, self._on_min_time_elapsed)

    # ------------------------------------------------------------------
    def _on_stage_changed(self, text: str) -> None:
        self._status_label.setText(text)

    def _on_diagnostics_finished(self, result: DiagnosticsResult) -> None:
        self._diagnostics_result = result
        self._diagnostics_done = True
        self._maybe_navigate()

    def _on_min_time_elapsed(self) -> None:
        self._min_time_elapsed = True
        self._maybe_navigate()

    def _maybe_navigate(self) -> None:
        # Só sai do splash quando as DUAS condições valerem: diagnóstico
        # pronto E tempo mínimo de exibição cumprido. Isso evita tanto
        # flicker (splash sumir em 80ms num SSD rápido) quanto travar a
        # UI numa tela de loading além do necessário.
        if not (self._diagnostics_done and self._min_time_elapsed):
            return

        self._spinner.stop()
        result = self._diagnostics_result
        assert result is not None  # garantido por _diagnostics_done

        next_state = AppState.DASHBOARD if result.dashboard_ready else AppState.INSTALLER
        self.navigate_requested.emit(next_state, result)
