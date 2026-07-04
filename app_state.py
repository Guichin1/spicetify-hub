"""
app_state.py

Enum de navegação compartilhado entre MainApplication e as telas
(SplashScreen, InstallerScreen, DashboardScreen). Vive em módulo
próprio de propósito: se ficasse dentro de main_application.py, toda
tela que precisasse emitir um sinal de navegação teria que importar
main_application — e main_application importa as telas. Import
circular na certa.
"""

from enum import Enum, auto


class AppState(Enum):
    SPLASH = auto()
    INSTALLER = auto()
    DASHBOARD = auto()
