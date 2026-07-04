"""
spicetify_manager.py

Gerencia a detecção do Spotify, migração snap -> apt/oficial, ajuste de
permissões via pkexec e instalação/aplicação do Spicetify em distros
Debian-based, sem travar a thread principal de uma interface gráfica.

Design:
- Cada operação "pesada" (subprocess) roda em uma thread separada
  (threading.Thread) e devolve o resultado via callback, para não
  bloquear o event loop de nenhuma GUI (PyQt, GTK, Tkinter, etc).
  Não amarrei isso a QProcess de propósito: QProcess só existe dentro
  do mundo Qt, e essa classe deve funcionar em qualquer front-end.
  Se você estiver usando PyQt/PySide, veja o wrapper QtSpicetifyWorker
  no final do arquivo, que expõe os mesmos métodos como sinais Qt.
- Toda chamada de subprocess devolve um CommandResult (dataclass) com
  success, message e output — nunca uma exceção "solta" para a UI.
- Comandos que exigem root usam pkexec (prompt gráfico nativo do
  ambiente, sem precisar de terminal/sudo interativo).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("SpicetifyManager")


@dataclass
class CommandResult:
    """Resultado padronizado de qualquer operação da classe."""
    success: bool
    message: str
    output: str = ""
    returncode: Optional[int] = None

    def __bool__(self) -> bool:
        return self.success


@dataclass
class SpotifyStatus:
    installed: bool = False
    source: str = "none"          # "apt" | "snap" | "none"
    path: Optional[str] = None
    detail: str = ""


class SpicetifyManager:
    """
    Encapsula todo o fluxo de preparação de ambiente para o Spicetify
    em sistemas Debian/Ubuntu.

    Uso típico (síncrono, dentro de uma thread que você já controla):
        mgr = SpicetifyManager()
        status = mgr.check_spotify_installation()
        if status.source == "snap":
            mgr.migrate_snap_to_apt()
        mgr.fix_permissions()
        mgr.install_spicetify_cli()
        mgr.apply_spicetify()

    Uso assíncrono (não bloqueia a UI):
        mgr.run_async(mgr.install_spicetify_cli, callback=on_finished)
    """

    SPOTIFY_APT_PATHS = [
        "/usr/share/spotify",
        "/usr/share/spotify/Apps",
    ]

    # Chave rotacionada pela Spotify em fev/2026 — a antiga (C85668DF69375001)
    # expirou e passou a gerar NO_PUBKEY. Se voltar a falhar no futuro, o ID
    # correto aparece na própria mensagem de erro do apt update.
    SPOTIFY_KEYRING_URL = "https://download.spotify.com/debian/pubkey_5384CE82BA52C83A.gpg"
    SPOTIFY_KEYRING_PATH = "/etc/apt/keyrings/spotify.gpg"
    # signed-by é obrigatório: sem isso o apt ignora o keyring em
    # /etc/apt/keyrings e continua marcando o repo como "não assinado".
    SPOTIFY_REPO_LINE = (
        f"deb [signed-by={SPOTIFY_KEYRING_PATH}] "
        f"https://repository.spotify.com stable non-free"
    )
    SPICETIFY_INSTALL_SCRIPT_URL = "https://raw.githubusercontent.com/spicetify/cli/main/install.sh"

    TIMEOUT_SHORT = 20
    TIMEOUT_LONG = 180

    def __init__(self):
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Infra interna
    # ------------------------------------------------------------------ #
    def _run(
        self,
        cmd: list[str],
        timeout: int = TIMEOUT_SHORT,
        input_text: Optional[str] = None,
        shell: bool = False,
    ) -> CommandResult:
        """Wrapper único e seguro para subprocess.run."""
        try:
            logger.info("Executando: %s", " ".join(cmd) if not shell else cmd)
            proc = subprocess.run(
                cmd if not shell else " ".join(cmd),
                shell=shell,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            combined_output = (proc.stdout or "") + (proc.stderr or "")
            success = proc.returncode == 0
            return CommandResult(
                success=success,
                message="Comando concluído com sucesso." if success
                        else f"Comando falhou (código {proc.returncode}).",
                output=combined_output.strip(),
                returncode=proc.returncode,
            )
        except FileNotFoundError as e:
            msg = f"Binário não encontrado: {e}"
            # Repito o motivo em 'output' de propósito: todo método acima
            # desta camada repassa result.output para o CommandResult final,
            # nunca result.message — sem isso o motivo real da falha se
            # perde silenciosamente para quem chamou.
            return CommandResult(False, msg, msg)
        except subprocess.TimeoutExpired as e:
            msg = f"Comando excedeu o tempo limite de {e.timeout:.0f}s (timeout)."
            return CommandResult(False, msg, msg)
        except Exception as e:  # pragma: no cover - defesa extra
            logger.exception("Erro inesperado ao executar comando")
            msg = f"Erro inesperado: {e}"
            return CommandResult(False, msg, msg)

    # ------------------------------------------------------------------ #
    # 1. Detecção do Spotify (apt vs snap)
    # ------------------------------------------------------------------ #
    def check_spotify_installation(self) -> SpotifyStatus:
        """
        Verifica se o Spotify está instalado via APT (dpkg) ou via Snap.
        Snap é incompatível com Spicetify (filesystem confinado/read-only
        squashfs), então é sinalizado explicitamente para acionar a
        migração.
        """
        # Checagem via dpkg (pacote nativo .deb)
        apt_check = self._run(["dpkg", "-s", "spotify-client"])
        if apt_check.success:
            path = "/usr/share/spotify" if os.path.isdir("/usr/share/spotify") else None
            return SpotifyStatus(
                installed=True,
                source="apt",
                path=path,
                detail="Spotify instalado via APT (compatível com Spicetify).",
            )

        # Checagem via snap
        if shutil.which("snap"):
            snap_check = self._run(["snap", "list", "spotify"])
            if snap_check.success and "spotify" in snap_check.output.lower():
                return SpotifyStatus(
                    installed=True,
                    source="snap",
                    path=None,
                    detail=(
                        "Spotify instalado via Snap. Esse formato usa um "
                        "filesystem read-only e NÃO é suportado pelo "
                        "Spicetify. É necessário migrar para a versão APT."
                    ),
                )

        return SpotifyStatus(
            installed=False,
            source="none",
            detail="Spotify não foi encontrado no sistema.",
        )

    # ------------------------------------------------------------------ #
    # 2. Migração snap -> apt (ou instalação limpa se não houver nada)
    # ------------------------------------------------------------------ #
    def migrate_snap_to_apt(self) -> CommandResult:
        """
        Remove a versão Snap do Spotify de forma limpa e prepara a
        instalação via APT (chamando install_spotify_apt no final).
        """
        remove = self._run(["pkexec", "snap", "remove", "spotify"], timeout=self.TIMEOUT_LONG)
        if not remove.success:
            return CommandResult(
                False,
                "Falha ao remover a versão Snap do Spotify.",
                remove.output,
            )

        logger.info("Snap removido com sucesso. Prosseguindo para instalação via APT.")
        return self.install_spotify_apt()

    def install_spotify_apt(self) -> CommandResult:
        """
        Instalação limpa do Spotify via repositório oficial APT.
        Usada tanto para 'não detectado' quanto pós-migração do snap.
        """
        steps: list[tuple[str, list[str]]] = [
            ("Criar diretório de keyrings",
             ["pkexec", "install", "-d", "-m", "0755", "/etc/apt/keyrings"]),
        ]
        for description, cmd in steps:
            result = self._run(cmd, timeout=self.TIMEOUT_SHORT)
            if not result.success:
                return CommandResult(False, f"Falha em: {description}", result.output)

        # Baixa e instala a chave GPG oficial (via pkexec + shell, curl + gpg)
        key_cmd = [
            "pkexec", "bash", "-c",
            f"curl -sS {self.SPOTIFY_KEYRING_URL} | "
            f"gpg --dearmor --yes -o {self.SPOTIFY_KEYRING_PATH}"
        ]
        key_result = self._run(key_cmd, timeout=self.TIMEOUT_SHORT)
        if not key_result.success:
            return CommandResult(False, "Falha ao importar a chave GPG do Spotify.", key_result.output)

        # Adiciona o repositório
        repo_cmd = [
            "pkexec", "bash", "-c",
            f'echo "{self.SPOTIFY_REPO_LINE}" > /etc/apt/sources.list.d/spotify.list'
        ]
        repo_result = self._run(repo_cmd, timeout=self.TIMEOUT_SHORT)
        if not repo_result.success:
            return CommandResult(False, "Falha ao registrar o repositório do Spotify.", repo_result.output)

        # Atualiza índices e instala
        update_result = self._run(["pkexec", "apt-get", "update"], timeout=self.TIMEOUT_LONG)
        if not update_result.success:
            return CommandResult(False, "Falha ao atualizar os índices do APT.", update_result.output)

        install_result = self._run(
            ["pkexec", "apt-get", "install", "-y", "spotify-client"],
            timeout=self.TIMEOUT_LONG,
        )
        if not install_result.success:
            return CommandResult(False, "Falha ao instalar spotify-client via APT.", install_result.output)

        return CommandResult(True, "Spotify instalado com sucesso via APT.", install_result.output)

    # ------------------------------------------------------------------ #
    # 3. Ajuste de permissões (chmod a+wr via pkexec)
    # ------------------------------------------------------------------ #
    def fix_permissions(self) -> CommandResult:
        """
        Aplica chmod a+wr recursivamente nas pastas do Spotify que o
        Spicetify precisa modificar (Apps/, xpui, etc.), solicitando a
        senha de root graficamente via pkexec (polkit).
        """
        existing_paths = [p for p in self.SPOTIFY_APT_PATHS if os.path.exists(p)]
        if not existing_paths:
            return CommandResult(
                False,
                "Nenhum diretório do Spotify encontrado para ajustar permissões.",
                "",
            )

        cmd = ["pkexec", "chmod", "-R", "a+wr", *existing_paths]
        result = self._run(cmd, timeout=self.TIMEOUT_SHORT)
        if result.success:
            return CommandResult(
                True,
                f"Permissões ajustadas em: {', '.join(existing_paths)}",
                result.output,
            )
        return CommandResult(False, "Falha ao ajustar permissões via pkexec.", result.output)

    # ------------------------------------------------------------------ #
    # 4. Instalação do Spicetify CLI (script oficial)
    # ------------------------------------------------------------------ #
    def install_spicetify_cli(self) -> CommandResult:
        """
        Instala o Spicetify CLI via script oficial (curl | sh).
        Executado como usuário normal — o Spicetify CLI não deve
        rodar como root. Não uso pkexec aqui de propósito: o próprio
        script oficial detecta root/sudo e se recusa a instalar,
        mas sai com returncode 0 (falso positivo). Por isso valido
        o texto de saída, não só o código de retorno.
        """
        if os.geteuid() == 0:
            return CommandResult(
                False,
                "Este processo está rodando como root. O Spicetify CLI "
                "precisa ser instalado como usuário normal — não chame "
                "este método via pkexec/sudo.",
                "",
            )

        cmd = [
            "bash", "-c",
            f"curl -fsSL {self.SPICETIFY_INSTALL_SCRIPT_URL} | sh"
        ]
        result = self._run(cmd, timeout=self.TIMEOUT_LONG)

        # O script oficial sai com código 0 mesmo quando se recusa a
        # instalar por detectar root/sudo. Trato isso como falha real.
        if result.success and "ran under sudo or as root" in result.output.lower():
            return CommandResult(
                False,
                "O script de instalação detectou execução com privilégios "
                "de root e se recusou a instalar (saiu com sucesso, mas "
                "sem instalar nada). Rode como usuário normal.",
                result.output,
            )

        if result.success:
            return CommandResult(True, "Spicetify CLI instalado com sucesso.", result.output)
        return CommandResult(False, "Falha ao instalar o Spicetify CLI.", result.output)

    # ------------------------------------------------------------------ #
    # 4.5. Garantir que o Spotify já gerou seu arquivo de config (prefs)
    # ------------------------------------------------------------------ #
    def ensure_spotify_prefs(
        self,
        max_wait_seconds: int = 180,
        poll_interval: float = 2.0,
    ) -> CommandResult:
        """
        O Spicetify precisa do arquivo ~/.config/spotify/prefs. Esse
        arquivo só é criado depois que o usuário efetivamente ABRE o
        Spotify e LOGA na conta — não basta o processo existir por
        alguns segundos. Por isso este método NÃO mata o Spotify
        sozinho: ele abre o app (se não estiver rodando), deixa a
        janela visível para o usuário logar, e fica monitorando a
        criação do arquivo por até `max_wait_seconds`.

        Chame isso via run_async — é bloqueante de propósito (espera o
        humano logar), então rodar na thread principal da UI travaria
        a interface.
        """
        prefs_path = Path.home() / ".config" / "spotify" / "prefs"
        if prefs_path.exists():
            return CommandResult(True, "Arquivo prefs já existe.", str(prefs_path))

        spotify_bin = shutil.which("spotify")
        if not spotify_bin:
            return CommandResult(
                False,
                "Binário 'spotify' não encontrado no PATH. Instale o "
                "Spotify antes de gerar o prefs.",
                "",
            )

        # Só abre se não houver uma instância já rodando (evita duas
        # janelas concorrentes caso o usuário já tenha aberto manualmente).
        already_running = self._run(["pgrep", "-x", "spotify"]).success
        if not already_running:
            logger.info(
                "Abrindo o Spotify para o usuário logar. Aguardando "
                "criação do prefs (até %ss)...", max_wait_seconds
            )
            try:
                subprocess.Popen(
                    [spotify_bin],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,  # não morre se este processo sair
                )
            except Exception as e:
                return CommandResult(False, f"Falha ao abrir o Spotify: {e}", "")

        waited = 0.0
        while waited < max_wait_seconds:
            if prefs_path.exists():
                return CommandResult(
                    True,
                    "Login detectado — prefs criado com sucesso.",
                    str(prefs_path),
                )
            threading.Event().wait(poll_interval)
            waited += poll_interval

        return CommandResult(
            False,
            f"Prefs não apareceu após {max_wait_seconds}s. O usuário "
            "provavelmente ainda não terminou o login. Deixe o Spotify "
            "aberto e tente aplicar o Spicetify novamente depois de logar.",
            "",
        )

    # ------------------------------------------------------------------ #
    # 5. Aplicação do Spicetify (backup + apply)
    # ------------------------------------------------------------------ #
    def apply_spicetify(self) -> CommandResult:
        """
        Executa `spicetify backup apply`. Assume que o binário spicetify
        já está no PATH do usuário (normalmente ~/.spicetify).
        """
        spicetify_bin = shutil.which("spicetify") or str(Path.home() / ".spicetify" / "spicetify")
        if not os.path.exists(spicetify_bin) and shutil.which("spicetify") is None:
            return CommandResult(
                False,
                "Binário do spicetify não encontrado. Instale o CLI antes de aplicar.",
                "",
            )

        result = self._run([spicetify_bin, "backup", "apply"], timeout=self.TIMEOUT_LONG)
        if result.success:
            return CommandResult(True, "Spicetify aplicado com sucesso.", result.output)
        return CommandResult(False, "Falha ao executar 'spicetify backup apply'.", result.output)

    # ------------------------------------------------------------------ #
    # Execução assíncrona genérica (não trava a UI)
    # ------------------------------------------------------------------ #
    def run_async(
        self,
        target: Callable[..., CommandResult | SpotifyStatus],
        callback: Callable[[CommandResult | SpotifyStatus], None],
        *args,
        **kwargs,
    ) -> threading.Thread:
        """
        Roda qualquer método da classe (ex: self.install_spicetify_cli)
        em uma thread separada e entrega o resultado ao 'callback' —
        que você deve fazer disparar de volta na thread da UI (ex: via
        QMetaObject.invokeMethod, root.after() no Tkinter, ou um
        Queue consumido por um timer).

        Exemplo:
            def on_done(result):
                print(result.success, result.message)

            mgr.run_async(mgr.fix_permissions, on_done)
        """
        def _worker():
            with self._lock:
                result = target(*args, **kwargs)
            callback(result)

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        return thread


# ---------------------------------------------------------------------- #
# Wrapper opcional para quem estiver usando PyQt5/PySide6
# ---------------------------------------------------------------------- #
try:
    from PyQt5.QtCore import QThread, pyqtSignal  # type: ignore

    class QtSpicetifyWorker(QThread):
        """
        Roda um método do SpicetifyManager em uma QThread e emite o
        resultado via sinal Qt — útil quando você já tem um event loop
        Qt e quer manter tudo integrado a sinais/slots em vez de
        callbacks manuais.

        Exemplo:
            mgr = SpicetifyManager()
            worker = QtSpicetifyWorker(mgr.install_spicetify_cli)
            worker.finished_result.connect(lambda r: print(r.message))
            worker.start()
        """
        finished_result = pyqtSignal(object)  # emite CommandResult/SpotifyStatus

        def __init__(self, target: Callable[[], CommandResult | SpotifyStatus], parent=None):
            super().__init__(parent)
            self._target = target

        def run(self):
            result = self._target()
            self.finished_result.emit(result)

except ImportError:
    # PyQt5 não instalado: o wrapper simplesmente não existe, e tudo
    # continua funcionando via run_async / threading puro.
    pass


if __name__ == "__main__":
    # Exemplo de fluxo completo, síncrono, para teste manual em terminal.
    mgr = SpicetifyManager()

    status = mgr.check_spotify_installation()
    print(f"[1] Status Spotify: {status}")

    if status.source == "snap":
        print("[2] Migrando de Snap para APT...")
        result = mgr.migrate_snap_to_apt()
        print(result)
    elif not status.installed:
        print("[2] Spotify não encontrado. Instalando via APT...")
        result = mgr.install_spotify_apt()
        print(result)
    else:
        print("[2] Spotify já instalado via APT. Nenhuma ação necessária.")

    print("[3] Ajustando permissões...")
    print(mgr.fix_permissions())

    print("[4] Instalando Spicetify CLI...")
    print(mgr.install_spicetify_cli())

    print("[4.5] Abra o Spotify e faça login — aguardando (até 3 min)...")
    print(mgr.ensure_spotify_prefs())

    print("[5] Aplicando Spicetify...")
    print(mgr.apply_spicetify())
