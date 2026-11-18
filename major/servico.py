"""Serviço de voz: segure o botão do anel, fale, e o comando chega ao Claude.

O anel não transmite o próprio microfone ao vivo; conectado, ele só avisa "botão pressionado" e "botão solto".
Então o serviço
1. fica com o dispositivo de entrada (HID) do anel em modo exclusivo, para o toque não mexer o mouse do Mac;
2. grava o microfone do Mac enquanto o botão está pressionado, transcreve e entrega o texto;
3. fala o que o Claude responder, inclusive os avisos de tarefas que terminam em segundo plano;
4. nos intervalos, traz o que o anel tiver gravado sozinho na memória.
"""
import asyncio
import fcntl
import json
import os
import re
import shlex
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import hid

from . import acompanhamento, audio, protocol as p, saude, voz
from .cli import puxar
from .config import DATA
from .microfone import MIC_MIN_RMS, Microfone
from .transcricao import transcrever
from .link import Ring
from .ponte import Conversa

HID_VENDOR, HID_PRODUCT = 0x05AC, 0x0220   # identidade que o anel declara ao macOS
IDLE_POLL = 45                             # intervalo para buscar gravações guardadas na memória do anel
RING_MIN_RMS = 250                         # microfone do anel: silêncio perto de 60, fala acima de 700
ANEL_GRAVANDO_MAX = 180                    # segundos de voz calada à espera do fim de uma gravação do anel
GRAVACAO_LONGA = 60                        # acima disso a gravação do anel pode ter começado sem querer
GRAVACAO_SEM_QUERER = 300                  # segundos: acima disso só o primeiro minuto é guardado; o resto é descartado
GRAVACAO_DEMORADA = 800                    # pacotes (3 min de áudio, 1 min para baixar): acima disso o serviço avisa
SINAL_FRACO = -70                          # dBm: daí para baixo o aviso de gravação longa diz que o sinal está fraco
PEDIDO_ATRASADO = 600                      # segundos entre a gravação e a chegada: acima disso o Claude confirma antes de agir
SOM_OUVI = "/System/Library/Sounds/Tink.aiff"      # entendi o comando, estou pensando
SOM_AVISO = "/System/Library/Sounds/Glass.aiff"    # vem aí um aviso que ninguém pediu: uma tarefa terminou
SOM_ERRO = "/System/Library/Sounds/Basso.aiff"     # o microfone não abriu
GRAVACAO_LONGA_OBS = (" [Esta gravação do microfone do anel passou de um minuto e pode ter começado sem querer: o anel "
                      "grava com quatro toques. Se não for claramente um pedido para você, diga só que recebeu uma "
                      "gravação longa do anel sem nenhum pedido.]")
GRAVACAO_SEM_QUERER_OBS = (" [O anel gravou {minutos} minutos seguidos, quase certamente sem querer: ele grava com "
                           "quatro toques. Este texto é só o primeiro minuto; o resto foi descartado sem ser guardado. "
                           "Se não for claramente um pedido para você, diga só que o anel gravou {minutos} minutos sem "
                           "querer e que a gravação foi descartada.]")
PEDIDO_ATRASADO_OBS = (" [Este pedido foi gravado há {minutos} minutos e só chegou agora, porque o anel estava longe ou "
                       "ocupado. Antes de executar qualquer coisa, diga que ele chegou atrasado e pergunte se ainda vale.]")
_SONS = re.compile(r"[\[(][^\])]*[\])]")      # o transcritor descreve música e ruído assim: [MÚSICA DE FUNDO], (risos)
LIGACAO_CAIU = (" [A ligação com o anel caiu durante esta gravação: o fim da frase pode ter sido cortado. "
                "Se o pedido parecer incompleto, peça para repetir.]")


def _segurar_hid(stop: threading.Event) -> None:
    """Mantém o HID do anel aberto em modo exclusivo; sem isso, tocar no anel mexe o cursor do Mac."""
    while not stop.is_set():
        device = None
        try:
            paths = [d["path"] for d in hid.enumerate(HID_VENDOR, HID_PRODUCT) if "SR116" in (d.get("product_string") or "")]
            if not paths:
                stop.wait(2)
                continue
            device = hid.device()
            device.open_path(paths[0])
            while not stop.is_set():
                device.read(64, 500)
        except OSError:
            stop.wait(2)     # o anel sai e volta da lista de dispositivos quando a ligação cai
        finally:
            if device:
                device.close()


def _intervalo_de_saude(minutos: float | None) -> float:
    """Segundos entre coletas de saúde: a opção da linha de comando, senão MAJOR_SAUDE_MINUTOS, senão o padrão.
    Zero desliga."""
    if minutos is None:
        try:
            minutos = float(os.environ.get("MAJOR_SAUDE_MINUTOS", saude.INTERVALO_PADRAO_MIN))
        except ValueError:
            minutos = saude.INTERVALO_PADRAO_MIN
    return max(0.0, minutos) * 60


def _horario_do_resumo(texto: str | None):
    """Horário do resumo diário de saúde: a opção, senão MAJOR_RESUMO_SAUDE, senão o padrão. None = desligado."""
    if texto is None:
        texto = os.environ.get("MAJOR_RESUMO_SAUDE", acompanhamento.RESUMO_PADRAO)
    try:
        return acompanhamento.horario_do_resumo(texto)
    except ValueError:
        print(f"Horário de resumo inválido ({texto!r}); usando {acompanhamento.RESUMO_PADRAO}.", flush=True)
        return acompanhamento.horario_do_resumo(acompanhamento.RESUMO_PADRAO)


def _instancia_unica():
    """Garante um só serviço no ar: dois disputariam o anel, o microfone e a mesma conversa do Claude. Isso pode
    acontecer porque mais de uma coisa religa o serviço (a sessão de voz, ao aplicar uma mudança, e quem o vigia).

    Devolve o arquivo da trava, que precisa ficar aberto enquanto o serviço viver; o sistema solta a trava sozinho
    quando o processo acaba, seja como for."""
    DATA.mkdir(parents=True, exist_ok=True)
    trava = (DATA / "servico.lock").open("a+")
    try:
        fcntl.flock(trava, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        trava.seek(0)
        raise SystemExit(f"Já existe um serviço de voz no ar (processo {trava.read().strip() or 'desconhecido'}); "
                         "este não vai subir.")
    trava.seek(0)
    trava.truncate()
    trava.write(str(os.getpid()))
    trava.flush()
    return trava


async def servir(executar: str | None, claude: bool = False, modelo: str | None = None, restrito: bool = True,
                 saude_minutos: float | None = None, resumo_saude: str | None = None,
                 alertas_saude: bool | None = None) -> None:
    trava = _instancia_unica()      # fica aberta até o serviço acabar
    stop = threading.Event()
    threading.Thread(target=_segurar_hid, args=(stop,), daemon=True).start()
    mic = Microfone()
    historico = DATA / "comandos.jsonl"
    tarefas: set[asyncio.Task] = set()
    locutor: voz.Locutor | None = None
    conversa: Conversa | None = None

    def log(message: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)

    if claude:
        locutor = voz.Locutor()

        def falar(frase: str, espontanea: bool, primeira: bool) -> None:
            # O aviso de uma tarefa concluída chega sem ninguém ter perguntado: vem depois de um sinal sonoro e
            # não se perde se o botão for apertado antes de ele começar a tocar.
            locutor.dizer(frase, guardar=espontanea, sinal=SOM_AVISO if espontanea and primeira else None)

        def avisar(r: dict) -> None:
            if r["resposta"]:
                log(f"CLAUDE (por conta própria, em {r['segundos']}s" + (", calado" if r["interrompida"] else "")
                    + f"): {r['resposta']}")

        conversa = Conversa(modelo, restrito, ao_falar=falar, ao_avisar=avisar, ao_registrar=log)
        # deixa a voz e o Claude carregados antes do primeiro comando
        threading.Thread(target=voz.preparar, daemon=True).start()
        threading.Thread(target=conversa.iniciar, daemon=True).start()

    async def atender(path, origem: str, min_rms: float, observacao: str = "") -> None:
        solto = time.time()
        text = await asyncio.to_thread(transcrever, path) if audio.has_speech(path, min_rms=min_rms) else None
        if not text or not re.search(r"[A-Za-zÀ-ÿ]{2,}", _SONS.sub(" ", text)):
            log(f"{origem}: sem fala reconhecida ({path.name}" + (f": {text}" if text else "") + ")")
            return
        log(f"VOCÊ DISSE ({origem}, transcrito em {time.time() - solto:.1f}s): {text}")
        with historico.open("a") as out:
            out.write(json.dumps({"hora": datetime.now().isoformat(timespec="seconds"), "origem": origem,
                                  "audio": path.name, "texto": text}, ensure_ascii=False) + "\n")
        if executar:
            await asyncio.to_thread(subprocess.run, [*shlex.split(executar), text], check=False)
        if conversa:
            subprocess.Popen(["afplay", SOM_OUVI])
            r = await asyncio.to_thread(conversa.perguntar, text + observacao)
            log(f"CLAUDE (1ª frase em {r['primeira_frase_s']}s, completa em {r['segundos']}s"
                + (", calada" if r["interrompida"] else "") + f"): {r['resposta']}"
                + (f" [negadas: {r['negadas']}]" if r["negadas"] else ""))

    def em_segundo_plano(coro) -> None:
        """Atende sem travar o laço do botão: assim um novo aperto é visto mesmo durante uma resposta."""
        tarefa = asyncio.create_task(coro)
        tarefas.add(tarefa)

        def fim(t: asyncio.Task) -> None:
            tarefas.discard(t)
            if not t.cancelled() and t.exception():
                log(f"falha ao atender um comando: {t.exception()!r}")

        tarefa.add_done_callback(fim)

    # ---- coleta periódica de saúde, pela mesma ligação; a voz sempre tem prioridade ----
    intervalo_saude = _intervalo_de_saude(saude_minutos)
    registro_saude = saude.Registro() if intervalo_saude else None
    parar_coleta = asyncio.Event()
    coleta: asyncio.Task | None = None
    # A primeira coleta espera um minuto depois de o serviço subir; as seguintes, o intervalo desde a última gravada.
    proxima_coleta = (max(time.time() + saude.ESPERA_INICIAL, (registro_saude.ultima or 0) + intervalo_saude)
                      if registro_saude else 0.0)

    # ---- acompanhamento: resumo diário e alertas, falados pela mesma conversa de voz ----
    horario_resumo = _horario_do_resumo(resumo_saude) if registro_saude else None
    if alertas_saude is None:
        alertas_saude = os.environ.get("MAJOR_ALERTAS_SAUDE", "1").strip().lower() not in ("0", "nao", "não", "off")
    alertas_pendentes: list[acompanhamento.Alerta] = []
    livre_desde = time.time()
    proximo_resumo = 0.0

    presa_desde = 0.0

    def conversa_livre() -> bool:
        """Ninguém gravando, nenhum comando esperando resposta, nenhum turno em curso e nada na fila de fala.
        Um comando que nunca recebe resposta não segura os avisos para sempre: depois de cinco minutos assim, com o
        microfone e a voz parados, a conversa conta como livre."""
        nonlocal presa_desde
        if mic.gravando or anel_gravando_desde or (locutor and not locutor._fila.empty()):
            presa_desde = 0.0
            return False
        if tarefas or (conversa and conversa.em_turno):
            presa_desde = presa_desde or time.time()
            return time.time() - presa_desde > 300
        presa_desde = 0.0
        return True

    async def avisar_saude(tipo: str, mensagem: str, detalhe) -> bool:
        """Entrega uma mensagem automática à conversa de voz e registra o que foi dito. Devolve se foi respondida."""
        if conversa:
            try:
                r = await asyncio.wait_for(asyncio.to_thread(conversa.perguntar, mensagem, True), 240)
                resposta, ok = r["resposta"], r["segundos"] is not None
            except asyncio.TimeoutError:    # a mensagem foi entregue e a fala sai sozinha; só a confirmação não voltou
                resposta, ok = "(entregue ao Claude; a confirmação da resposta não voltou em quatro minutos)", True
        else:       # sem o Claude não há quem interprete: fica só no registro
            resposta, ok = None, True
        log(f"SAÚDE ({tipo}): {detalhe if isinstance(detalhe, str) else '; '.join(detalhe)} -> {resposta or '(só registrado)'}")
        acompanhamento.registrar_aviso({"hora": datetime.now().astimezone().isoformat(timespec="seconds"), "tipo": tipo,
                                        "detalhe": detalhe, "mensagem": mensagem, "falado": bool(conversa and ok),
                                        "resposta": resposta})
        return ok

    async def acompanhar() -> None:
        """Roda sempre, com ou sem o anel por perto: fala o resumo na hora marcada e os alertas pendentes, sempre
        esperando a conversa ficar livre por alguns segundos."""
        nonlocal livre_desde, proximo_resumo
        avisou_espera = False
        while True:
            await asyncio.sleep(2)
            try:
                if not conversa_livre():
                    livre_desde = time.time()
                    continue
                if time.time() - livre_desde < 6:
                    continue
                agora = datetime.now()
                if alertas_pendentes:
                    estado = acompanhamento.ler_estado()
                    falar, calados = acompanhamento.filtrar(list(alertas_pendentes), estado, agora)
                    alertas_pendentes.clear()
                    for a in calados:
                        log(f"saúde: alerta calado pelo horário de silêncio: {a.descricao}")
                        acompanhamento.registrar_aviso({"hora": agora.astimezone().isoformat(timespec="seconds"), "tipo": "alerta",
                                                        "detalhe": [a.descricao], "falado": False, "motivo": "horário de silêncio"})
                    if falar:
                        acompanhamento.gravar_estado(estado)    # antes de falar: um alerta nunca se repete por falha no meio
                        await avisar_saude("alerta", acompanhamento.mensagem_de_alerta(falar), [a.descricao for a in falar])
                        livre_desde = time.time()
                    continue
                estado = acompanhamento.ler_estado()
                if time.time() >= proximo_resumo and acompanhamento.resumo_devido(agora, estado, horario_resumo):
                    if acompanhamento.resumo_deve_esperar(agora, registro_saude.completa, horario_resumo):
                        if not avisou_espera:
                            avisou_espera = True
                            log("saúde: resumo diário à espera da primeira coleta completa da manhã (o anel ainda não sincronizou)")
                        continue
                    avisou_espera = False
                    hoje = agora.date().isoformat()
                    estado["resumo_tentativas"] = {hoje: estado["resumo_tentativas"].get(hoje, 0) + 1}
                    acompanhamento.gravar_estado(estado)
                    mensagem = acompanhamento.mensagem_de_resumo(saude.ler(registro_saude.arquivo), agora, intervalo_saude / 60)
                    if await avisar_saude("resumo diário", mensagem, acompanhamento.dados_do_resumo(
                            saude.ler(registro_saude.arquivo), agora, intervalo_saude / 60).replace("\n", " ")):
                        estado["resumo_dia"] = hoje
                        acompanhamento.gravar_estado(estado)
                    else:
                        proximo_resumo = time.time() + 300      # o Claude não respondeu: tenta de novo daqui a pouco
                    livre_desde = time.time()
            except Exception as erro:       # o acompanhamento não pode derrubar o serviço de voz
                log(f"saúde: falha no acompanhamento ({erro!r})")
                await asyncio.sleep(60)

    if registro_saude:
        acompanhante = asyncio.create_task(acompanhar())    # fora de `tarefas`: não é um comando em andamento
        log(f"saúde: coleta a cada {intervalo_saude / 60:g} min; resumo diário "
            + (f"às {horario_resumo:%H:%M}" if horario_resumo else "desligado")
            + f"; alertas {'ligados' if alertas_saude else 'desligados'}")

    async def ajustar_anel(ring: Ring) -> None:
        """A cada conexão: confere o relógio do anel e reaplica a medição automática."""
        nonlocal ajuste_conferido, conectado_em, ajuste_tentativas
        try:
            adiantado = await saude.relogio(ring)
            if adiantado is not None and abs(adiantado) > saude.RELOGIO_TOLERANCIA:
                # Antes de acertar o relógio, recolhe tudo o que o anel gravou com a hora errada: depois não dá
                # mais para saber quais registros precisam de correção.
                recolhida = await saude.coletar(ring, parar_coleta, medir=False, todos_os_dias=True)
                if recolhida["falhas"] or parar_coleta.is_set() or not ring.connected:
                    log(f"saúde: relógio do anel {adiantado:+.0f} s fora, mas não consegui recolher o histórico antes "
                        f"de acertar ({recolhida['falhas'] or 'botão apertado ou ligação caiu'}); fica para a próxima conexão")
                    if registro_saude.tem_novidade(recolhida):
                        registro_saude.gravar(recolhida)
                    return
                gravada = registro_saude.gravar(recolhida)
                log(f"saúde: recolhido antes de acertar o relógio: {saude.resumo_da_coleta(gravada)}")
                aviso = await saude.acertar_relogio(ring)
                if aviso:
                    log(f"saúde: {aviso}")
            aviso = await saude.aplicar_medicao_automatica(ring)
            if aviso:
                log(f"saúde: {aviso}")
        except Exception as erro:
            # Logo depois de reconectar o macOS às vezes ainda não listou os serviços do anel: tenta de novo em instantes.
            ajuste_tentativas += 1
            if ajuste_tentativas < 4:
                log(f"saúde: não consegui ajustar o anel ({erro!r}); tento de novo em dez segundos")
                ajuste_conferido, conectado_em = False, time.time() + 7
            else:
                log(f"saúde: não consegui ajustar o anel ({erro!r}); fica para a próxima conexão")

    quedas_seguidas = 0

    async def coletar_saude(ring: Ring) -> None:
        nonlocal proxima_coleta, quedas_seguidas
        try:
            colhida = await saude.coletar(ring, parar_coleta, ultima=registro_saude.completa)
            if saude.caiu(colhida) and quedas_seguidas < saude.TENTATIVAS_APOS_QUEDA:
                # A ligação cai com frequência: em vez de esperar o próximo intervalo, tenta de novo assim que
                # reconectar. Só vai para o arquivo o que o anel entregou de novo.
                quedas_seguidas += 1
                proxima_coleta = time.time() + saude.ESPERA_APOS_QUEDA
                if registro_saude.tem_novidade(colhida):
                    colhida = registro_saude.gravar(colhida)
                log(f"saúde: coleta incompleta ({'; '.join(colhida['falhas'])}); nova tentativa ao reconectar "
                    f"({quedas_seguidas} de {saude.TENTATIVAS_APOS_QUEDA})")
                return
            esgotou = saude.caiu(colhida)       # nem com as tentativas: nova rodada daqui a pouco
            quedas_seguidas = 0
            gravada = registro_saude.gravar(colhida)
            log(f"saúde: {saude.resumo_da_coleta(gravada)}")
            if esgotou:
                proxima_coleta = time.time() + saude.ESPERA_APOS_FALHAS
                return
            if ring.connected and not parar_coleta.is_set():
                await saude.aplicar_medicao_automatica(ring)    # desligar o sensor ao fim da medição não pode desligar a automática
            if alertas_saude:
                alertas_pendentes.extend(acompanhamento.avaliar(saude.ler(registro_saude.arquivo), datetime.now()))
            proxima_coleta = time.time() + (saude.ADIAR_SEGUNDOS if gravada["medicao"] == "interrompida" else intervalo_saude)
        except Exception as erro:       # nada na coleta pode derrubar o serviço de voz
            log(f"saúde: a coleta falhou ({erro!r})")
            proxima_coleta = time.time() + intervalo_saude

    # ---- microfone do próprio anel: quatro toques começam a gravar, mais quatro terminam ----
    # O gesto é do firmware do anel e não dá para trocar daqui. Medido em 14/11/2026 com o anel de verdade:
    # - segurar o botão nunca grava no anel, com o serviço conectado ou não; ele só avisa "apertou" e "soltou";
    # - quatro toques gravam mesmo com o serviço conectado, e o anel avisa o começo (estado de áudio com o bit 16);
    # - o protocolo do app oficial não tem comando para começar a gravação, só para parar (63) e para baixar (52);
    # - durante a gravação o anel não responde a comandos e a ligação costuma cair; a gravação não se perde;
    # - o anel só entrega o áudio depois que a gravação termina: não existe transmissão ao vivo neste modelo.
    anel_gravando_desde = 0.0       # quando o anel avisou que começou a gravar; 0 = não está gravando

    def anel_comecou() -> None:
        """O anel avisou que está gravando. Daqui até ele terminar não se manda nenhum comando (ele para de responder
        e a ligação cai) e a voz fica calada, para não entrar na gravação."""
        nonlocal anel_gravando_desde
        if anel_gravando_desde:
            return
        anel_gravando_desde = time.time()
        log("o anel começou a gravar pelo próprio microfone")
        parar_coleta.set()
        if locutor:
            locutor.calar()
            locutor.segurar()

    def anel_terminou() -> None:
        nonlocal anel_gravando_desde
        if anel_gravando_desde:
            anel_gravando_desde = 0.0
            if locutor and not mic.gravando:
                locutor.soltar()

    demorada = {"falou_em": 0.0, "anotou_em": 0.0, "tarefa": None}

    async def avisar_demora(ring: Ring, recebidos: int, total: int, falar: bool) -> None:
        falta = max(1, round((total - recebidos) / p.TRANSFER_RATE / 60))
        sinal = await ring.rssi()
        log(f"o anel está transmitindo uma gravação longa: {recebidos} de {total} pacotes ({recebidos / total:.0%}); "
            f"{'falta 1 min' if falta == 1 else f'faltam uns {falta} min'}" + (f"; sinal de {sinal} dBm" if sinal is not None else ""))
        if falar:
            locutor.dizer(f"O anel guardou uma gravação longa, de uns {round(total * p.PACKET_SECONDS / 60)} minutos. "
                          f"Ele precisa de mais ou menos {falta} {'minuto' if falta == 1 else 'minutos'} para transmitir e "
                          "apagar, e até lá não atende comandos. Não toque no anel e deixe ele bem perto do Mac."
                          + (" O sinal dele está fraco." if sinal is not None and sinal <= SINAL_FRACO else ""),
                          guardar=True, sinal=SOM_AVISO)

    def andamento_do_anel(ring: Ring, recebidos: int, total: int) -> None:
        """O anel entrega primeiro a gravação mais antiga e só a apaga depois de transmiti-la inteira; até lá não
        atende mais nada. 30 minutos gravados sem querer são uns 10 minutos de anel mudo, ou muito mais se a ligação
        cair e tudo recomeçar. Quem está usando precisa saber disso: o serviço avisa por voz (de novo a cada cinco
        minutos) e anota o andamento e a força do sinal no registro uma vez por minuto."""
        if total < GRAVACAO_DEMORADA:
            return
        agora = time.time()
        falar = bool(locutor) and agora - demorada["falou_em"] >= 300 and not mic.gravando
        if falar or agora - demorada["anotou_em"] >= 60:
            demorada["anotou_em"] = agora
            if falar:
                demorada["falou_em"] = agora
            demorada["tarefa"] = asyncio.create_task(avisar_demora(ring, recebidos, total, falar))

    async def recolher_do_anel(ring: Ring) -> None:
        """Baixa o que o anel gravou com o próprio microfone e trata cada gravação como um comando de voz."""
        # Ninguém dita um pedido de mais de cinco minutos: uma gravação assim começou sem querer e, se houver pedido,
        # está no começo. Dela só o primeiro minuto é guardado; o resto chega (o anel só apaga depois de transmitir
        # tudo) e é descartado sem ir para o disco nem para a conversa.
        cortadas: dict[Path, float] = {}
        cortar = (round(GRAVACAO_SEM_QUERER / p.PACKET_SECONDS), round(60 / p.PACKET_SECONDS))
        for path in await puxar(ring, apagar=True, cortar=cortar, cortadas=cortadas,
                                andamento=lambda recebidos, total: andamento_do_anel(ring, recebidos, total)):
            segundos = cortadas.get(path) or audio.duracao(path)    # quanto a gravação tinha no anel
            if demorada["falou_em"] and segundos >= GRAVACAO_DEMORADA * p.PACKET_SECONDS:
                demorada["falou_em"] = 0.0
                locutor.dizer("A gravação longa saiu do anel. Ele voltou a atender.", guardar=True, sinal=SOM_AVISO)
            if path in cortadas:
                observacao = GRAVACAO_SEM_QUERER_OBS.format(minutos=round(segundos / 60))
                em_segundo_plano(atender(path, "microfone do anel", RING_MIN_RMS, observacao))
                continue
            observacao = GRAVACAO_LONGA_OBS if segundos > GRAVACAO_LONGA else ""
            try:    # o nome do arquivo é a hora em que o anel começou a gravar
                atraso = (datetime.now() - datetime.strptime(path.stem, "%Y%m%d-%H%M%S")).total_seconds() - segundos
            except ValueError:
                atraso = 0.0
            if atraso > PEDIDO_ATRASADO:    # ficou preso atrás de outra gravação ou o anel estava longe: não é para agir às cegas
                observacao += PEDIDO_ATRASADO_OBS.format(minutos=round(atraso / 60))
            em_segundo_plano(atender(path, "microfone do anel", RING_MIN_RMS, observacao))

    def apertou() -> None:
        parar_coleta.set()      # se houver uma medição em curso, ela para e fica para daqui a pouco
        if locutor:             # o usuário vai falar: silêncio já, e nada novo toca até ele soltar o botão
            locutor.calar()
            locutor.segurar()
        if conversa:
            conversa.interromper()
        mic.start()
        if mic.gravando:
            log("botão pressionado: gravando o microfone do Mac")
        else:
            log(f"botão pressionado, mas o microfone do Mac não abriu: {mic.error}")
            subprocess.Popen(["afplay", SOM_ERRO])

    def soltou(origem: str = "microfone do Mac", observacao: str = "") -> None:
        estava_gravando = mic.gravando
        path = mic.stop()
        if locutor and not anel_gravando_desde:
            locutor.soltar()
        if path:
            em_segundo_plano(atender(path, origem, MIC_MIN_RMS, observacao=observacao))
        elif estava_gravando:
            log(f"o microfone do Mac não gravou nada: {mic.error}")

    avisou_em = 0.0
    try:
        while True:
            ring = Ring()
            if not await ring.connect(tries=2, scan_timeout=8):
                if time.time() - avisou_em > 30:     # sem isso o serviço parece travado quando o anel está longe
                    avisou_em = time.time()
                    log("Não consegui conectar no anel (longe, sinal fraco ou em uso por outro aparelho); tentando de novo.")
                await asyncio.sleep(2)
                continue
            log("Conectado ao anel. Segure o botão para falar pelo microfone do Mac, ou dê quatro toques, fale e dê mais "
                "quatro toques para falar pelo microfone do anel.")
            ajuste_conferido = not registro_saude       # a medição automática do anel é reaplicada a cada conexão
            ajuste_tentativas = 0
            conectado_em = time.time()
            events: asyncio.Queue = asyncio.Queue()
            ring.on_frame(lambda f: f.payload and f.cmd in (p.Cmd.BUTTON, p.Cmd.AUDIO_STATE)
                          and events.put_nowait((f.cmd, f.payload[0])))
            last_sync = 0.0
            estado_visto = False        # o anel informa o estado de áudio ao conectar, se houver algo a informar
            try:
                while ring.connected:
                    try:
                        event = await asyncio.wait_for(events.get(), 1)
                    except asyncio.TimeoutError:
                        if anel_gravando_desde:
                            if time.time() - anel_gravando_desde > ANEL_GRAVANDO_MAX:
                                log("o anel não avisou o fim da gravação; a voz volta ao normal")
                                anel_terminou()
                            elif not estado_visto and time.time() - conectado_em > 2:
                                estado_visto = True     # reconectou no meio de uma gravação e o anel nada informou: pergunta
                                await ring.send(p.Cmd.AUDIO_STATE, b"\x00\x00")
                            continue        # nenhum comando enquanto o anel grava
                        if coleta and not coleta.done():
                            continue        # uma coleta de saúde está usando a ligação
                        if not ajuste_conferido and time.time() - conectado_em > 3 and not mic.gravando:
                            ajuste_conferido = True
                            parar_coleta.clear()
                            coleta = asyncio.create_task(ajustar_anel(ring))    # sem travar o laço do botão
                            continue
                        if (registro_saude and time.time() >= proxima_coleta and not mic.gravando
                                and (not tarefas or time.time() - proxima_coleta > 300)):
                            # só com a voz parada: sem gravação e sem comando esperando resposta. Mas um comando que
                            # nunca recebe resposta não pode segurar a coleta para sempre: depois de cinco minutos ela sai.
                            parar_coleta.clear()
                            proxima_coleta = time.time() + intervalo_saude
                            coleta = asyncio.create_task(coletar_saude(ring))
                            continue
                        if not mic.gravando and time.time() - last_sync > IDLE_POLL:
                            last_sync = time.time()
                            await recolher_do_anel(ring)    # o que tiver ficado no anel sem aviso
                        continue
                    comando, valor = event
                    if comando == p.Cmd.AUDIO_STATE:
                        estado_visto = True
                        if valor & p.STATE_OFFLINE_RECORDING:
                            anel_comecou()
                            continue
                        anel_terminou()
                        if valor & p.STATE_OFFLINE_PENDING and not mic.gravando:
                            if coleta and not coleta.done():    # a ligação é uma só: a coleta de saúde cede a vez
                                parar_coleta.set()
                                await asyncio.wait({coleta}, timeout=10)
                            last_sync = time.time()
                            await recolher_do_anel(ring)
                    elif valor == 1:
                        apertou()
                    elif valor == 2:
                        log("botão solto")
                        soltou()
            except Exception as error:
                log(f"ligação interrompida: {error!r}")
            finally:
                # Se a ligação caiu com o botão apertado, o aviso de "soltou" não chega mais: aproveita o que foi
                # gravado até aqui em vez de perder o comando.
                soltou("microfone do Mac, com a ligação caindo durante a gravação", LIGACAO_CAIU)
                if coleta and not coleta.done():    # a coleta em curso grava o que conseguiu antes de a ligação fechar
                    parar_coleta.set()
                    await asyncio.wait({coleta}, timeout=10)
                await ring.disconnect()
            log("Anel desconectou; reconectando.")
    finally:
        stop.set()
        if conversa:
            conversa.fechar()
