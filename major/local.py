"""Modo sem anel: a mesma cadeia do serviço (microfone do Mac, transcrição local, Claude Code, voz), comandada
pelo teclado no lugar do botão do anel.

  Enter sozinho         começa a gravar pelo microfone do Mac; outro Enter termina e envia
  texto e Enter         envia o texto digitado como se tivesse sido falado
  sair (ou Ctrl+D)      encerra

Como no anel, começar a gravar ou enviar um texto corta a fala em curso.
"""
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from . import audio, transcricao
from .microfone import MIC_MIN_RMS, Microfone
from .ponte import Conversa

SAIR = {"sair", "exit", "quit", "tchau"}


def ouvir(path: Path, min_rms: float = MIC_MIN_RMS, apagar: bool = True) -> str | None:
    """Texto falado numa gravação, ou None se não havia fala. A gravação é apagada depois de transcrita."""
    try:
        if not audio.has_speech(path, min_rms=min_rms):
            return None
        return transcricao.transcrever(path)
    finally:
        if apagar:
            path.unlink(missing_ok=True)


def conversar(so_texto: bool = False, falar: bool = True, modelo: str | None = None, restrita: bool = True,
              entrada: Callable[[str], str] = input, mostrar: Callable[[str], None] = print,
              microfone: Microfone | None = None) -> None:
    """Laço da conversa pelo teclado. entrada, mostrar e microfone existem para os testes trocarem."""
    if not shutil.which("claude"):
        raise SystemExit("Não achei o comando claude. Instale o Claude Code e entre na sua conta antes (rode: claude).")
    if not so_texto and transcricao.problema():
        mostrar(f"Sem gravação por voz: {transcricao.problema()}. Seguindo só com texto digitado.")
        so_texto = True

    locutor = None
    if falar:
        from . import voz       # só aqui: carregar a voz leva alguns segundos e, no modo mudo, não é preciso
        locutor = voz.Locutor()
        threading.Thread(target=voz.preparar, daemon=True).start()

    def ao_falar(frase: str, espontanea: bool, primeira: bool) -> None:
        mostrar(("\n[aviso] " if espontanea and primeira else "  ") + frase)
        if locutor:
            locutor.dizer(frase, guardar=espontanea)

    conversa = Conversa(modelo, restrita, ao_falar=ao_falar, ao_registrar=lambda linha: mostrar(f"  ({linha})"))
    threading.Thread(target=conversa.iniciar, daemon=True).start()
    mic = microfone or (None if so_texto else Microfone())
    pendentes: list[threading.Thread] = []

    def perguntar(texto: str) -> None:
        r = conversa.perguntar(texto)
        if r["segundos"] is None:
            mostrar(f"  {r['resposta']} O Claude Code está instalado e com a conta conectada?")
        elif r["negadas"]:
            mostrar(f"  (ações negadas pelo modo restrito: {', '.join(r['negadas'])})")

    def enviar(texto: str) -> None:
        tarefa = threading.Thread(target=perguntar, args=(texto,), daemon=True)
        pendentes.append(tarefa)
        tarefa.start()

    def calar() -> None:
        """Como apertar o botão do anel: a fala em curso para e o resto da resposta não é mais falado."""
        if locutor:
            locutor.calar()
        conversa.interromper()

    mostrar("Modo " + ("restrito: o Claude só lê arquivos, pesquisa e abre apps e links."
                       if restrita else "de ACESSO TOTAL: o Claude pode fazer qualquer coisa neste Mac."))
    mostrar("Digite a pergunta e Enter" + ("" if so_texto else ", ou só Enter para gravar pelo microfone (Enter de novo "
                                                               "para terminar)") + ". Para encerrar: sair.")
    try:
        while True:
            try:
                linha = entrada("gravando… Enter para terminar> " if mic and mic.gravando else "> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if mic and mic.gravando:
                path = mic.stop()
                if locutor:
                    locutor.soltar()
                if not path:
                    mostrar(f"  O microfone não gravou nada: {mic.error}")
                    continue
                inicio = time.time()
                texto = ouvir(path)
                if not texto:
                    mostrar("  Não ouvi nenhuma fala. Tente de novo, mais perto do microfone.")
                    continue
                mostrar(f"Você disse (transcrito em {time.time() - inicio:.1f}s): {texto}")
                enviar(texto)
            elif linha.lower() in SAIR:
                break
            elif linha:
                calar()
                enviar(linha)
            elif mic:
                calar()
                if locutor:
                    locutor.segurar()
                mic.start()
                if not mic.gravando:
                    if locutor:
                        locutor.soltar()
                    mostrar(f"  O microfone não abriu: {mic.error}. Confira a permissão de microfone do terminal em "
                            "Ajustes do Sistema, Privacidade e Segurança.")
            else:
                mostrar("  Digite a pergunta e aperte Enter.")
    finally:
        if mic and mic.gravando:
            sobra = mic.stop()
            if sobra:
                sobra.unlink(missing_ok=True)
        for tarefa in pendentes:        # a resposta que estava a caminho ainda aparece (e é falada) antes de sair
            tarefa.join(timeout=120)
        if conversa.tarefas:
            mostrar("  Havia tarefas em segundo plano; ao encerrar, elas são interrompidas.")
        if locutor:
            locutor.soltar()
            locutor.esperar()
            locutor.fechar()
        conversa.fechar()
