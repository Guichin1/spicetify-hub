"""
qt_thread_bridge.py

Ponte entre threads cruas (threading.Thread, como as usadas por
SpicetifyManager.run_async) e a thread principal do Qt.

QTimer.singleShot chamado de dentro de uma threading.Thread comum
NUNCA dispara — confirmado empiricamente, não é suposição documentada
de memória. QTimer depende de um loop de eventos rodando NA MESMA
thread que o criou, e uma threading.Thread pura não tem loop de
eventos nenhum processando seus timers, mesmo com a thread principal
girando normalmente via QApplication.exec(). O timer simplesmente
nunca dispara — sem erro, sem exceção, silêncio total.

Sinais Qt são a exceção que resolve isso: emitir um Signal de
qualquer thread enfileira a chamada no loop de eventos do QObject
receptor (Qt.AutoConnection vira Qt.QueuedConnection automaticamente
quando emissor e receptor estão em threads diferentes) — e o receptor
aqui vive na thread principal, que de fato está rodando.

Uso:
    self._invoker = MainThreadInvoker(self)  # parent = algo na UI thread
    ...
    # dentro de um callback chamado pelo SpicetifyManager.run_async,
    # ou seja, rodando na worker thread:
    self._invoker.call_in_main_thread(lambda: self.algum_widget.setText("x"))
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, Signal


class MainThreadInvoker(QObject):
    _invoke_requested = Signal(object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._invoke_requested.connect(self._invoke)

    def _invoke(self, fn: Callable[[], None]) -> None:
        fn()

    def call_in_main_thread(self, fn: Callable[[], None]) -> None:
        self._invoke_requested.emit(fn)
