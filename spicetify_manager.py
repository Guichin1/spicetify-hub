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
- _run usa Popen (não subprocess.run) e lê stdout/stderr linha a linha
  em tempo real. Métodos que executam comandos longos (apt update,
  curl do Spicetify, etc.) aceitam um on_line_received opcional —
  cada linha chega assim que é impressa pelo processo, não só no
  final. O CommandResult final ainda carrega o log inteiro em
  'output', para quem quiser consultar depois do fato.
- Comandos que exigem root usam pkexec (prompt gráfico nativo do
  ambiente, sem precisar de terminal/sudo interativo).
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
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
    # Repositório atual é spicetify/marketplace — o nome antigo
    # (spicetify/spicetify-marketplace) ainda aparece em tutoriais
    # desatualizados espalhados pela internet, mas foi o que a própria
    # documentação oficial (spicetify.app/docs) mostrou na consulta.
    SPICETIFY_MARKETPLACE_INSTALL_URL = (
        "https://raw.githubusercontent.com/spicetify/marketplace/main/resources/install.sh"
    )

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
        on_line_received: Optional[Callable[[str], None]] = None,
    ) -> CommandResult:
        """
        Wrapper único para execução de subprocess. Usa Popen (não
        subprocess.run) e consome stdout/stderr LINHA A LINHA conforme
        elas chegam — cada linha vai imediatamente para
        on_line_received (se fornecido), em vez de esperar o processo
        inteiro terminar. O CommandResult final ainda carrega o log
        completo em 'output' (todas as linhas juntas), para histórico.

        stderr é fundido em stdout (STDOUT em vez de PIPE separado) de
        propósito: se eu ler dois pipes separados um de cada vez, um
        deles pode encher o buffer do SO e travar o processo (deadlock
        clássico de subprocess com dois PIPEs). Fundir os streams evita
        isso e ainda preserva a ordem cronológica real das linhas.

        subprocess.run tinha timeout nativo; Popen não tem para leitura
        linha a linha, então implemento na mão com threading.Timer:
        se o tempo estourar, mato o processo, o que fecha o stdout e
        naturalmente encerra o loop de leitura abaixo.
        """
        lines: list[str] = []
        timeout_flag = {"hit": False}
        proc: Optional[subprocess.Popen] = None

        try:
            logger.info("Executando: %s", " ".join(cmd) if not shell else cmd)
            proc = subprocess.Popen(
                cmd if not shell else " ".join(cmd),
                shell=shell,
                stdin=subprocess.PIPE if input_text is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # line-buffered
                start_new_session=True,  # cria um novo grupo de processos
            )

            if input_text is not None and proc.stdin is not None:
                proc.stdin.write(input_text)
                proc.stdin.close()

            def _kill_on_timeout() -> None:
                timeout_flag["hit"] = True
                # proc.kill() só mata o processo direto (ex: bash) e
                # NÃO seus filhos (ex: o "sleep" ou "curl" que ele
                # disparou). Se o filho ainda tiver o fd do stdout
                # aberto, o pipe nunca fecha e o timeout vira decorativo
                # — foi exatamente o que aconteceu no meu primeiro
                # teste (timeout de 1s levou 5s pra retornar). Por isso
                # mato o GRUPO inteiro de processos, não só o líder.
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        proc.kill()
                    except Exception:
                        pass

            timer = threading.Timer(timeout, _kill_on_timeout)
            timer.daemon = True
            timer.start()

            try:
                assert proc.stdout is not None
                for raw_line in proc.stdout:
                    line = raw_line.rstrip("\n")
                    lines.append(line)
                    if on_line_received is not None:
                        try:
                            on_line_received(line)
                        except Exception:
                            # Um erro no callback da UI não pode
                            # derrubar a execução do comando em si.
                            logger.exception(
                                "Erro no callback on_line_received; ignorado."
                            )
                returncode = proc.wait()
            finally:
                timer.cancel()

            combined_output = "\n".join(lines)

            if timeout_flag["hit"]:
                msg = f"Comando excedeu o tempo limite de {timeout}s (timeout)."
                return CommandResult(False, msg, combined_output or msg, returncode)

            success = returncode == 0
            return CommandResult(
                success=success,
                message="Comando concluído com sucesso." if success
                        else f"Comando falhou (código {returncode}).",
                output=combined_output.strip(),
                returncode=returncode,
            )

        except FileNotFoundError as e:
            msg = f"Binário não encontrado: {e}"
            # Repito o motivo em 'output' de propósito: todo método acima
            # desta camada repassa result.output para o CommandResult final,
            # nunca result.message — sem isso o motivo real da falha se
            # perde silenciosamente para quem chamou.
            return CommandResult(False, msg, msg)
        except Exception as e:  # pragma: no cover - defesa extra
            logger.exception("Erro inesperado ao executar comando")
            msg = f"Erro inesperado: {e}"
            return CommandResult(False, msg, "\n".join(lines) or msg)

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
    def migrate_snap_to_apt(
        self, on_line_received: Optional[Callable[[str], None]] = None
    ) -> CommandResult:
        """
        Remove a versão Snap do Spotify de forma limpa e prepara a
        instalação via APT (chamando install_spotify_apt no final).
        """
        remove = self._run(
            ["pkexec", "snap", "remove", "spotify"],
            timeout=self.TIMEOUT_LONG,
            on_line_received=on_line_received,
        )
        if not remove.success:
            return CommandResult(
                False,
                "Falha ao remover a versão Snap do Spotify.",
                remove.output,
            )

        logger.info("Snap removido com sucesso. Prosseguindo para instalação via APT.")
        return self.install_spotify_apt(on_line_received=on_line_received)

    def install_spotify_apt(
        self, on_line_received: Optional[Callable[[str], None]] = None
    ) -> CommandResult:
        """
        Instalação limpa do Spotify via repositório oficial APT.
        Usada tanto para 'não detectado' quanto pós-migração do snap.
        """
        steps: list[tuple[str, list[str]]] = [
            ("Criar diretório de keyrings",
             ["pkexec", "install", "-d", "-m", "0755", "/etc/apt/keyrings"]),
        ]
        for description, cmd in steps:
            result = self._run(cmd, timeout=self.TIMEOUT_SHORT, on_line_received=on_line_received)
            if not result.success:
                return CommandResult(False, f"Falha em: {description}", result.output)

        # Baixa e instala a chave GPG oficial (via pkexec + shell, curl + gpg)
        key_cmd = [
            "pkexec", "bash", "-c",
            f"curl -sS {self.SPOTIFY_KEYRING_URL} | "
            f"gpg --dearmor --yes -o {self.SPOTIFY_KEYRING_PATH}"
        ]
        key_result = self._run(key_cmd, timeout=self.TIMEOUT_SHORT, on_line_received=on_line_received)
        if not key_result.success:
            return CommandResult(False, "Falha ao importar a chave GPG do Spotify.", key_result.output)

        # Adiciona o repositório
        repo_cmd = [
            "pkexec", "bash", "-c",
            f'echo "{self.SPOTIFY_REPO_LINE}" > /etc/apt/sources.list.d/spotify.list'
        ]
        repo_result = self._run(repo_cmd, timeout=self.TIMEOUT_SHORT, on_line_received=on_line_received)
        if not repo_result.success:
            return CommandResult(False, "Falha ao registrar o repositório do Spotify.", repo_result.output)

        # Atualiza índices e instala
        update_result = self._run(
            ["pkexec", "apt-get", "update"],
            timeout=self.TIMEOUT_LONG,
            on_line_received=on_line_received,
        )
        if not update_result.success:
            return CommandResult(False, "Falha ao atualizar os índices do APT.", update_result.output)

        install_result = self._run(
            ["pkexec", "apt-get", "install", "-y", "spotify-client"],
            timeout=self.TIMEOUT_LONG,
            on_line_received=on_line_received,
        )
        if not install_result.success:
            return CommandResult(False, "Falha ao instalar spotify-client via APT.", install_result.output)

        return CommandResult(True, "Spotify instalado com sucesso via APT.", install_result.output)

    # ------------------------------------------------------------------ #
    # 3. Ajuste de permissões (chmod a+wr via pkexec)
    # ------------------------------------------------------------------ #
    def fix_permissions(
        self, on_line_received: Optional[Callable[[str], None]] = None
    ) -> CommandResult:
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
        result = self._run(cmd, timeout=self.TIMEOUT_SHORT, on_line_received=on_line_received)
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
    def install_spicetify_cli(
        self, on_line_received: Optional[Callable[[str], None]] = None
    ) -> CommandResult:
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
        result = self._run(cmd, timeout=self.TIMEOUT_LONG, on_line_received=on_line_received)

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
    # Checagem compartilhada — usada por todo método que depende do
    # binário spicetify já instalado (apply, marketplace, restore).
    # ------------------------------------------------------------------ #
    def _require_spicetify_cli(self) -> tuple[Optional[str], Optional[CommandResult]]:
        """
        Devolve (caminho_do_binário, None) se o spicetify CLI existir,
        ou (None, CommandResult de falha) caso contrário — assim quem
        chama só precisa de:
            bin_path, error = self._require_spicetify_cli()
            if error is not None:
                return error
        em vez de reescrever a mesma checagem em cada método.
        """
        spicetify_bin = shutil.which("spicetify")
        if spicetify_bin:
            return spicetify_bin, None

        fallback = Path.home() / ".spicetify" / "spicetify"
        if fallback.exists():
            return str(fallback), None

        return None, CommandResult(
            False,
            "Spicetify CLI não encontrado. Instale-o primeiro "
            "(install_spicetify_cli) antes de continuar.",
            "",
        )

    # ------------------------------------------------------------------ #
    # 5. Aplicação do Spicetify (backup + apply)
    # ------------------------------------------------------------------ #
    def apply_spicetify(
        self, on_line_received: Optional[Callable[[str], None]] = None
    ) -> CommandResult:
        """
        Executa `spicetify backup apply`.
        """
        spicetify_bin, error = self._require_spicetify_cli()
        if error is not None:
            return error

        result = self._run(
            [spicetify_bin, "backup", "apply"],
            timeout=self.TIMEOUT_LONG,
            on_line_received=on_line_received,
        )
        if result.success:
            return CommandResult(True, "Spicetify aplicado com sucesso.", result.output)
        return CommandResult(False, "Falha ao executar 'spicetify backup apply'.", result.output)

    # ------------------------------------------------------------------ #
    # 6. Instalação do Spicetify Marketplace
    # ------------------------------------------------------------------ #
    def install_marketplace(
        self, on_line_received: Optional[Callable[[str], None]] = None
    ) -> CommandResult:
        """
        Instala o Spicetify Marketplace via script oficial (curl | sh).
        Exige o Spicetify CLI já instalado — o Marketplace é uma
        custom app que vive dentro da estrutura de config dele
        (~/.config/spicetify/CustomApps), não é standalone.

        Roda como usuário normal, pelo mesmo motivo do
        install_spicetify_cli: o script escreve em diretório de
        usuário, não em área de root.
        """
        _, error = self._require_spicetify_cli()
        if error is not None:
            return error

        if os.geteuid() == 0:
            return CommandResult(
                False,
                "Este processo está rodando como root. O Marketplace "
                "precisa ser instalado como usuário normal — não chame "
                "este método via pkexec/sudo.",
                "",
            )

        cmd = [
            "bash", "-c",
            f"curl -fsSL {self.SPICETIFY_MARKETPLACE_INSTALL_URL} | sh",
        ]
        result = self._run(cmd, timeout=self.TIMEOUT_LONG, on_line_received=on_line_received)
        if result.success:
            return CommandResult(
                True, "Spicetify Marketplace instalado com sucesso.", result.output
            )
        return CommandResult(False, "Falha ao instalar o Spicetify Marketplace.", result.output)

    # ------------------------------------------------------------------ #
    # 7. Restaurar o Spotify ao estado original
    # ------------------------------------------------------------------ #
    def restore_spicetify(
        self, on_line_received: Optional[Callable[[str], None]] = None
    ) -> CommandResult:
        """
        Executa `spicetify restore` — desfaz temas/extensões e devolve
        o Spotify ao arquivo original que o Spicetify guardou como
        backup. Útil quando um tema quebra a UI, ou antes de
        desinstalar o Spicetify de vez.
        """
        spicetify_bin, error = self._require_spicetify_cli()
        if error is not None:
            return error

        result = self._run(
            [spicetify_bin, "restore"],
            timeout=self.TIMEOUT_LONG,
            on_line_received=on_line_received,
        )
        if result.success:
            return CommandResult(True, "Spotify restaurado ao estado original.", result.output)
        return CommandResult(False, "Falha ao executar 'spicetify restore'.", result.output)

    # ------------------------------------------------------------------ #
    # Execução assíncrona genérica (não trava a UI)
    # ------------------------------------------------------------------ #
    def run_async(
        self,
        target: Callable[..., CommandResult | SpotifyStatus],
        callback: Callable[[CommandResult | SpotifyStatus], None],
        *args,
        on_line_received: Optional[Callable[[str], None]] = None,
        **kwargs,
    ) -> threading.Thread:
        """
        Roda qualquer método da classe (ex: self.install_spicetify_cli)
        em uma thread separada e entrega o resultado ao 'callback' —
        que você deve fazer disparar de volta na thread da UI (ex: via
        QMetaObject.invokeMethod, root.after() no Tkinter, ou um
        Queue consumido por um timer).

        on_line_received é keyword-only de propósito: colocá-lo antes
        de *args faria uma chamada como run_async(alvo, callback, "x")
        cair silenciosamente no slot errado. Ele só funciona com os
        métodos que aceitam esse kwarg (install_spotify_apt,
        fix_permissions, install_spicetify_cli, apply_spicetify,
        migrate_snap_to_apt, install_marketplace, restore_spicetify). Passar em check_spotify_installation ou
        ensure_spotify_prefs — que não têm esse parâmetro — resulta
        num CommandResult de falha entregue ao callback (TypeError
        capturado internamente), não numa thread morta em silêncio.

        Cada linha chega em on_line_received NA THREAD DESTE WORKER,
        igual ao próprio 'callback' final — se o front-end for Qt,
        agende a atualização de widget via QTimer.singleShot(0, ...)
        dentro do seu wrapper, não toque widgets direto daqui.

        Exemplo:
            def on_line(line):
                print("[log]", line)

            def on_done(result):
                print(result.success, result.message)

            mgr.run_async(
                mgr.install_spicetify_cli,
                on_done,
                on_line_received=on_line,
            )
        """
        def _worker():
            try:
                with self._lock:
                    if on_line_received is not None:
                        result = target(*args, on_line_received=on_line_received, **kwargs)
                    else:
                        result = target(*args, **kwargs)
            except Exception as e:
                # Sem este try/except, um TypeError (ex: passar
                # on_line_received para um método que não aceita esse
                # kwarg) mata a thread em silêncio: o callback nunca
                # dispara e a UI fica esperando pra sempre, sem
                # nenhuma pista além de um traceback perdido no
                # stderr. Isso contradiz o princípio do resto da
                # classe — nenhuma exceção "solta" para a UI — então
                # converto em CommandResult de falha e entrego ao
                # callback normalmente, em vez de deixar a thread
                # sumir sem avisar ninguém.
                logger.exception("Exceção não tratada dentro de run_async")
                callback(CommandResult(False, f"Erro interno inesperado: {e}", ""))
                return
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
