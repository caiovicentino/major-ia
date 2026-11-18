"""Registro de saúde: coleta periódica pela conexão que o serviço já mantém, gravada em dados/saude.jsonl.

Cada coleta é uma linha, sempre acrescentada ao fim do arquivo. Os dados ficam só neste Mac.
Campos de uma linha:
  hora                      quando a coleta terminou (ISO, com fuso)
  bateria                   % da bateria do anel
  passos, calorias, distancia   contadores do dia, como o anel informa
  medicao                   "ok", "parcial", "sem leitura" (anel fora do dedo?), "interrompida" (o botão foi
                            apertado: a voz tem prioridade) ou "falha"
  batimentos, oxigenacao    mediana das leituras ao vivo (bpm e %), ou null
  batimentos_leituras, oxigenacao_leituras   as leituras ao vivo, na ordem
  historico                 registros que o anel mediu sozinho e ainda não estavam no arquivo
  sono                      registros de sono ainda não gravados
  falhas                    o que deu errado nesta coleta, em texto
"""
from __future__ import annotations

import asyncio
import calendar
import json
import os
import statistics
import time
from datetime import datetime, timedelta
from pathlib import Path

from typing import TYPE_CHECKING

from . import config, protocol as p

if TYPE_CHECKING:     # o módulo também serve para consultar o histórico sem as bibliotecas de Bluetooth
    from .link import Ring

ARQUIVO = config.DATA / "saude.jsonl"
INTERVALO_PADRAO_MIN = 30
SEGUNDOS_POR_MEDIDA = 30        # com 22 s por medida o anel às vezes não entrega nenhuma leitura
LEITURAS = {"batimentos": 3, "oxigenacao": 1}   # o anel manda os batimentos em série e a oxigenação uma vez só
ADIAR_SEGUNDOS = 180            # nova tentativa depois de uma coleta interrompida pelo botão
DIAS_NO_ANEL = 7                # até quantos dias para trás pedir o que o anel guardou sozinho
AJUSTES = ARQUIVO.with_name("anel_ajustes.json")            # o que queremos ligado no anel (ver medicao_automatica)
AJUSTES_APLICADOS = ARQUIVO.with_name("anel_ajustes.jsonl")  # cada vez que um ajuste diferente foi mandado ao anel
# Medição automática do próprio anel (batimentos, oxigenação e temperatura, gravados na memória dele mesmo longe do
# Mac). O formato é o do app oficial: das 00:00 às 23:59, ligada, intervalo. O app só manda intervalo 5 e não diz a
# unidade; pelos registros do anel são minutos. O usuário pediu 10, para poupar bateria. Muda com
# `major medicao-automatica ligar --intervalo N` ou, sem ajuste gravado, com a variável MAJOR_MEDICAO_INTERVALO.
# O anel não tem comando para ler este ajuste de volta.
AUTO_INICIO, AUTO_FIM, AUTO_INTERVALO = (0, 0), (23, 59), 10


def _intervalo_padrao() -> int:
    try:
        return int(os.environ.get("MAJOR_MEDICAO_INTERVALO", AUTO_INTERVALO))
    except ValueError:
        return AUTO_INTERVALO
RELOGIO_TOLERANCIA = 120        # segundos de diferença entre o relógio do anel e o do Mac a partir dos quais se corrige
TENTATIVAS_APOS_QUEDA = 3       # a ligação caiu no meio da coleta: tenta de novo logo depois de reconectar
ESPERA_APOS_QUEDA = 15
ESPERA_APOS_FALHAS = 300        # nem assim: nova rodada de tentativas daqui a pouco, em vez de esperar o intervalo todo
ESPERA_INICIAL = 60             # o serviço acabou de subir: deixa a ligação assentar antes da primeira coleta


def _iso(hora: datetime) -> str:
    """O anel guarda a hora local sem fuso; aqui ela ganha o fuso deste Mac."""
    return hora.astimezone().isoformat(timespec="seconds")


def ler(arquivo: Path = ARQUIVO) -> list[dict]:
    try:
        linhas = arquivo.read_text().splitlines()
    except OSError:
        return []
    coletas = []
    for linha in linhas:
        try:
            coletas.append(json.loads(linha))
        except json.JSONDecodeError:
            continue    # uma linha cortada (queda de energia) não invalida o resto
    return coletas


class Registro:
    """O arquivo de coletas: sabe o que já foi gravado, para não repetir histórico nem sono."""

    def __init__(self, arquivo: Path = ARQUIVO) -> None:
        self.arquivo = arquivo
        coletas = ler(arquivo)
        self._historico = {_chave(r) for c in coletas for r in c.get("historico") or []}
        self._sono = {_chave(r) for c in coletas for r in c.get("sono") or []}
        self.ultima: float | None = None      # hora da última coleta gravada
        self.completa: float | None = None    # hora da última que recolheu o histórico e o sono até o fim
        for c in coletas:
            try:
                self.ultima = datetime.fromisoformat(c["hora"]).timestamp()
            except (KeyError, ValueError):
                continue
            if c.get("historico_completo", not c.get("falhas") and c.get("medicao") in ("ok", "parcial", "sem leitura", "não feita")):
                self.completa = self.ultima

    def gravar(self, coleta: dict) -> dict:
        """Acrescenta a coleta ao arquivo, deixando em 'historico' e 'sono' só o que é novo. Devolve o que gravou."""
        coleta = dict(coleta)
        coleta["historico"] = self._novos(coleta.get("historico"), self._historico)
        coleta["sono"] = self._novos(coleta.get("sono"), self._sono)
        self.arquivo.parent.mkdir(parents=True, exist_ok=True)
        with self.arquivo.open("a") as out:
            out.write(json.dumps(coleta, ensure_ascii=False) + "\n")
        self._historico.update(_chave(r) for r in coleta["historico"])
        self._sono.update(_chave(r) for r in coleta["sono"])
        self.ultima = time.time()
        if coleta.get("historico_completo"):
            self.completa = self.ultima
        return coleta

    @staticmethod
    def _novos(registros: list[dict] | None, vistos: set[str]) -> list[dict]:
        novos, chaves = [], set()
        for r in registros or []:
            chave = _chave(r)
            if chave not in vistos and chave not in chaves:     # o mesmo registro pode vir em dois dias pedidos
                chaves.add(chave)
                novos.append(r)
        return novos

    def tem_novidade(self, coleta: dict) -> bool:
        """A coleta traz algum registro do anel que ainda não está no arquivo?"""
        return bool(self._novos(coleta.get("historico"), self._historico) or self._novos(coleta.get("sono"), self._sono))


def _chave(registro: dict) -> str:
    """Identidade de um registro do anel: os bytes como ele mandou. (Linhas antigas, sem 'bruto', usam a hora.)"""
    return registro.get("bruto") or registro["hora"]


def hora_certa(hora_do_anel: datetime, agora: datetime, adiantado: float) -> datetime:
    """Hora de um registro com o relógio do anel corrigido. Só mexe no que é impossível de outro jeito: com o
    relógio do anel adiantado, um registro "no futuro" foi carimbado com o relógio errado."""
    if adiantado > RELOGIO_TOLERANCIA and hora_do_anel > agora + timedelta(seconds=RELOGIO_TOLERANCIA):
        segundos = round(adiantado)
        if abs(segundos - round(segundos / 60) * 60) <= 5:     # relógio errado é fuso errado: minutos inteiros
            segundos = round(segundos / 60) * 60
        return hora_do_anel - timedelta(seconds=segundos)
    return hora_do_anel


async def relogio(ring: Ring) -> float | None:
    """Quantos segundos o relógio do anel está adiantado em relação ao deste Mac (negativo: atrasado).
    O anel deve guardar a hora local; a hora dele vem junto com a contagem de passos."""
    r = await ring.request(p.Cmd.STEP_INFO)
    if not r or len(r.payload) < 6:
        return None
    return (p.timestamp(r.payload) - datetime.now()).total_seconds()


async def acertar_relogio(ring: Ring) -> str | None:
    """Põe o relógio do anel na hora local deste Mac, se ele estiver errado, como o app oficial faz a cada
    conexão. Devolve um texto para o registro se acertou (ou tentou). Quem chama deve antes recolher o
    histórico (ver coletar com medir=False), porque os registros já gravados no anel ficam com o carimbo errado."""
    adiantado = await relogio(ring)
    if adiantado is None or abs(adiantado) <= RELOGIO_TOLERANCIA:
        return None
    agora = datetime.now()
    carimbo = calendar.timegm(agora.timetuple())        # a hora local, contada como se fosse UTC
    resposta = await ring.request(p.Cmd.SET_TIME, carimbo.to_bytes(6, "little") + b"\0", timeout=3)
    depois = await relogio(ring)
    linha = {"hora": _iso(agora), "ajuste": "relogio", "comando": p.Cmd.SET_TIME,
             "valor_anterior": f"relógio do anel {adiantado:+.0f} s em relação ao Mac",
             "enviado": (carimbo.to_bytes(6, "little") + b"\0").hex(), "significado": f"hora local {agora:%d/%m/%Y %H:%M:%S}",
             "resposta_do_anel": resposta.payload.hex() if resposta else None,
             "depois": None if depois is None else f"{depois:+.0f} s"}
    AJUSTES_APLICADOS.parent.mkdir(parents=True, exist_ok=True)
    with AJUSTES_APLICADOS.open("a") as out:
        out.write(json.dumps(linha, ensure_ascii=False) + "\n")
    return (f"relógio do anel estava {adiantado:+.0f} s em relação ao Mac; acertado para a hora local "
            f"(agora {'sem resposta' if depois is None else f'{depois:+.0f} s'})")


async def _historico(ring: Ring, cmd: int, vazio: int, dia: int) -> list[bytes]:
    """Pede um histórico de vários pacotes e devolve os conteúdos em ordem."""
    pacotes: dict[int, bytes] = {}
    fim = asyncio.Event()
    ultimo = {"t": time.time()}

    def on_frame(frame: p.Frame) -> None:
        if frame.cmd == vazio:
            fim.set()
        elif frame.cmd == cmd:
            pacotes[frame.current] = frame.payload
            ultimo["t"] = time.time()
            if frame.current >= frame.total:
                fim.set()

    ring.on_frame(on_frame)
    try:
        await ring.send(cmd, bytes([dia]))
        while ring.connected and not fim.is_set() and time.time() - ultimo["t"] < 5:
            await asyncio.sleep(0.2)
    finally:
        ring.off_frame(on_frame)
    return [pacotes[i] for i in sorted(pacotes)]


def _registros_de_saude(conteudo: bytes, agora: datetime, adiantado: float) -> list[dict]:
    """Como protocol.health_records, mas guardando também os 4 bytes ainda não decifrados de cada registro e o
    registro bruto, que serve para não gravar duas vezes o mesmo."""
    registros = []
    for bruto in p._records(conteudo, 14):
        r = p.health_records(bruto)[0]
        registros.append({"hora": _iso(hora_certa(r["hora"], agora, adiantado)), "batimentos": r["bpm"] or None,
                          "oxigenacao": r["spo2"] or None, "temperatura": r["temperatura"] or None,
                          "extra": bruto[10:14].hex(), "bruto": bruto.hex()})
    return registros


def _registros_de_sono(conteudo: bytes, agora: datetime, adiantado: float) -> list[dict]:
    registros = []
    for bruto in p._records(conteudo, 8):
        r = p.sleep_records(bruto)[0]
        registros.append({"hora": _iso(hora_certa(r["hora"], agora, adiantado)), "fase": r["fase"],
                          "movimento": r["movimento"], "bruto": bruto.hex()})
    return registros


async def _medir(ring: Ring, parar: asyncio.Event, segundos: float) -> tuple[dict[str, list[int]], bool]:
    """Batimentos e depois oxigenação, um de cada vez (pedidos juntos, só a oxigenação responde).
    Devolve as leituras e se a medição foi interrompida."""
    leituras: dict[str, list[int]] = {"batimentos": [], "oxigenacao": []}

    def on_frame(frame: p.Frame) -> None:
        if frame.cmd == p.Cmd.HEART_RATE_DATA and len(frame.payload) >= 7 and frame.payload[6]:
            leituras["batimentos"].append(frame.payload[6])
        elif frame.cmd == p.Cmd.SPO2_DATA and len(frame.payload) >= 7 and frame.payload[6]:
            leituras["oxigenacao"].append(frame.payload[6])

    ring.on_frame(on_frame)
    try:
        for nome, ligar, desligar in (("batimentos", (p.Cmd.HEART_RATE_ON, b""), (p.Cmd.HEART_RATE_OFF, b"")),
                                      ("oxigenacao", (p.Cmd.SPO2, b"\x01"), (p.Cmd.SPO2, b"\x00"))):
            if parar.is_set() or not ring.connected:
                break
            await ring.send(*ligar)
            try:
                inicio = time.time()
                while (ring.connected and not parar.is_set() and time.time() - inicio < segundos
                       and len(leituras[nome]) < LEITURAS[nome]):
                    await asyncio.sleep(0.25)
            finally:
                if ring.connected:
                    await ring.send(*desligar)     # o sensor não pode ficar ligado gastando bateria
            if not parar.is_set():
                await asyncio.sleep(1)
    finally:
        ring.off_frame(on_frame)
    return leituras, parar.is_set()


def dias_a_buscar(ultima: float | None, agora: datetime | None = None) -> list[int]:
    """Quais dias pedir ao anel (0 = hoje), do mais antigo ao mais novo: tudo desde a última coleta gravada, para
    não perder o que ele mediu sozinho longe do Mac; na primeira vez, tudo o que ele guardar."""
    agora = agora or datetime.now()
    atras = DIAS_NO_ANEL - 1 if ultima is None else (agora.date() - datetime.fromtimestamp(ultima).date()).days
    return list(range(max(0, min(atras, DIAS_NO_ANEL - 1)), -1, -1))


def medicao_automatica() -> dict:
    """O que deve estar ajustado no anel: {'ligada': bool, 'intervalo': int}. Ligada por padrão; muda com
    `major medicao-automatica ligar|desligar`, que grava dados/anel_ajustes.json. O serviço aplica a cada conexão."""
    try:
        ajuste = json.loads(AJUSTES.read_text())["medicao_automatica"]
        return {"ligada": bool(ajuste["ligada"]), "intervalo": int(ajuste.get("intervalo") or _intervalo_padrao())}
    except (OSError, ValueError, KeyError, TypeError):
        return {"ligada": True, "intervalo": _intervalo_padrao()}


def definir_medicao_automatica(ligada: bool, intervalo: int | None = None) -> None:
    intervalo = intervalo or medicao_automatica()["intervalo"]
    AJUSTES.parent.mkdir(parents=True, exist_ok=True)
    AJUSTES.write_text(json.dumps({"medicao_automatica": {"ligada": ligada, "intervalo": intervalo,
                                                          "definida_em": _iso(datetime.now())}}, ensure_ascii=False))


async def aplicar_medicao_automatica(ring: Ring) -> str | None:
    """Manda ao anel o ajuste de medição automática desejado. Devolve um texto para o registro quando o ajuste
    mandado é diferente do último mandado (a primeira vez, ou depois de ligar/desligar), senão None.
    Não mexe em nenhum outro ajuste do anel."""
    ajuste = medicao_automatica()
    if not 1 <= ajuste["intervalo"] <= 255:
        return f"medição automática: intervalo inválido ({ajuste['intervalo']}); nada foi mandado ao anel"
    conteudo = bytes([*AUTO_INICIO, *AUTO_FIM, 1 if ajuste["ligada"] else 0, ajuste["intervalo"]])
    resposta = await ring.request(p.Cmd.AUTO_MEASURE, conteudo, timeout=3)
    try:
        anteriores = [json.loads(l) for l in AJUSTES_APLICADOS.read_text().splitlines()]
        ultimo = [a for a in anteriores if a.get("ajuste") == "medicao_automatica"][-1]
    except (OSError, ValueError, IndexError):
        ultimo = None
    if ultimo and ultimo.get("enviado") == conteudo.hex():
        return None
    linha = {"hora": _iso(datetime.now()), "ajuste": "medicao_automatica", "comando": p.Cmd.AUTO_MEASURE,
             "enviado": conteudo.hex(), "significado": f"das {AUTO_INICIO[0]:02d}:{AUTO_INICIO[1]:02d} às "
             f"{AUTO_FIM[0]:02d}:{AUTO_FIM[1]:02d}, {'ligada' if ajuste['ligada'] else 'desligada'}, intervalo {ajuste['intervalo']}",
             "resposta_do_anel": resposta.payload.hex() if resposta else None,
             "valor_anterior": ultimo["enviado"] if ultimo else "desconhecido: o anel não tem comando para ler este ajuste",
             "como_desfazer": "major medicao-automatica desligar (manda 00 00 17 3b 00 05, o que o app oficial manda para desligar)"}
    AJUSTES_APLICADOS.parent.mkdir(parents=True, exist_ok=True)
    with AJUSTES_APLICADOS.open("a") as out:
        out.write(json.dumps(linha, ensure_ascii=False) + "\n")
    return (f"medição automática do anel {'ligada' if ajuste['ligada'] else 'desligada'} (intervalo {ajuste['intervalo']}); "
            f"resposta do anel: {linha['resposta_do_anel'] or 'nenhuma'}")


async def coletar(ring: Ring, parar: asyncio.Event, segundos: float = SEGUNDOS_POR_MEDIDA,
                  ultima: float | None = None, medir: bool = True, todos_os_dias: bool = False) -> dict:
    """Uma coleta completa. ultima: hora da última coleta gravada, para pedir ao anel o histórico e o sono de
    todos os dias desde então (na primeira vez, ou com todos_os_dias, tudo o que ele guardar). medir=False só
    recolhe o que o anel já tem, sem ligar os sensores. Nunca levanta erro por causa do anel: o que falhar vai
    para 'falhas'. As horas dos registros são corrigidas se o relógio do anel estiver adiantado.
    parar é ligado quando o botão é apertado; a medição ao vivo para na hora e o resto da coleta é gravado."""
    coleta: dict = {"hora": None, "bateria": None, "passos": None, "calorias": None, "distancia": None,
                    "medicao": "falha", "batimentos": None, "oxigenacao": None, "batimentos_leituras": [],
                    "oxigenacao_leituras": [], "historico": [], "sono": [], "falhas": []}
    relogio_do_anel = {"adiantado": 0.0, "dias": [1, 0]}

    async def etapa(nome: str, corrotina) -> None:
        try:
            await corrotina
        except Exception as erro:       # ligação caiu, escrita travou, resposta fora do formato…
            coleta["falhas"].append(f"{nome}: {erro!r}")

    async def bateria() -> None:
        r = await ring.request(p.Cmd.BATTERY, b"\0\0")
        if r and r.payload:
            coleta["bateria"] = r.payload[0]
        else:
            coleta["falhas"].append("bateria: sem resposta")

    async def passos() -> None:
        r = await ring.request(p.Cmd.STEP_INFO)
        if r and len(r.payload) >= 14:
            s = p.steps_now(r.payload)
            coleta.update(passos=s["passos"], calorias=s["calorias"], distancia=s["distancia"])
            # A hora do anel vem junto: serve para corrigir os carimbos e para saber que dias pedir (os "dias"
            # do anel seguem o relógio dele).
            adiantado = (s["hora"] - datetime.now()).total_seconds()
            relogio_do_anel["adiantado"] = adiantado
            if abs(adiantado) > RELOGIO_TOLERANCIA:
                coleta["relogio_adiantado_s"] = round(adiantado)
            relogio_do_anel["dias"] = dias_a_buscar(None if todos_os_dias or ultima is None else ultima + adiantado, s["hora"])
        else:
            coleta["falhas"].append("passos: sem resposta")
            if todos_os_dias or ultima is None:
                relogio_do_anel["dias"] = dias_a_buscar(None)

    async def historico() -> None:
        for dia in sorted(relogio_do_anel["dias"]):     # do mais novo ao mais antigo, até achar um dia vazio
            if parar.is_set():
                break
            pacotes = await _historico(ring, p.Cmd.HEALTH_RECORD, p.Cmd.HEALTH_EMPTY, dia)
            for conteudo in pacotes:
                coleta["historico"] += _registros_de_saude(conteudo, datetime.now(), relogio_do_anel["adiantado"])
            if not pacotes and dia >= 2:
                break
        coleta["historico"].sort(key=lambda r: r["hora"])

    async def sono() -> None:
        for dia in sorted(relogio_do_anel["dias"]):
            if parar.is_set():
                break
            pacotes = await _historico(ring, p.Cmd.SLEEP, p.Cmd.SLEEP_EMPTY, dia)
            for conteudo in pacotes:
                coleta["sono"] += _registros_de_sono(conteudo, datetime.now(), relogio_do_anel["adiantado"])
            if not pacotes and dia >= 2:
                break
        coleta["sono"].sort(key=lambda r: r["hora"])

    async def medicao() -> None:
        leituras, interrompida = await _medir(ring, parar, segundos)
        for nome, valores in leituras.items():
            coleta[f"{nome}_leituras"] = valores
            coleta[nome] = round(statistics.median(valores)) if valores else None
        com_leitura = [nome for nome, valores in leituras.items() if valores]
        if not ring.connected:
            coleta["medicao"] = "parcial" if com_leitura else "falha"
            coleta["falhas"].append("medição: a ligação com o anel caiu")
        elif interrompida:
            coleta["medicao"] = "interrompida"
        elif len(com_leitura) == 2:
            coleta["medicao"] = "ok"
        elif com_leitura:
            coleta["medicao"] = "parcial"
        else:
            coleta["medicao"] = "sem leitura"

    for nome, corrotina in (("bateria", bateria()), ("passos", passos()), ("histórico", historico()), ("sono", sono())):
        if parar.is_set() or not ring.connected:
            corrotina.close()
            continue
        await etapa(nome, corrotina)
    # O histórico só conta como recolhido até o fim se nada falhou nem foi interrompido antes da medição ao vivo:
    # é a partir da última coleta assim que a próxima pede os dias ao anel, para não pular o que faltou.
    coleta["historico_completo"] = bool(ring.connected and not parar.is_set() and not coleta["falhas"])
    if not medir:
        coleta["medicao"] = "não feita"
    elif parar.is_set():
        coleta["medicao"] = "interrompida"
    elif not ring.connected:
        coleta["falhas"].append("medição: a ligação com o anel caiu")
    else:
        await etapa("medição", medicao())
    coleta["hora"] = _iso(datetime.now())
    return coleta


def caiu(coleta: dict) -> bool:
    """A coleta ficou incompleta porque a ligação com o anel caiu (ou algum pedido falhou)?"""
    return bool(coleta.get("falhas")) and coleta.get("medicao") in ("falha", "parcial")


def resumo_da_coleta(c: dict) -> str:
    """Uma linha para o registro do serviço."""
    partes = [f"medição {c['medicao']}"]
    if c.get("batimentos"):
        partes.append(f"{c['batimentos']} bpm")
    if c.get("oxigenacao"):
        partes.append(f"oxigenação {c['oxigenacao']}%")
    if c.get("passos") is not None:
        partes.append(f"{c['passos']} passos")
    if c.get("bateria") is not None:
        partes.append(f"bateria {c['bateria']}%")
    partes.append(f"{len(c.get('historico') or [])} registros novos de histórico e {len(c.get('sono') or [])} de sono")
    if c.get("falhas"):
        partes.append("falhas: " + "; ".join(c["falhas"]))
    return ", ".join(partes)


# ---- consulta ----

def _faixa(valores: list[float], casas: int = 0) -> str:
    if not valores:
        return "sem leitura"
    media = statistics.mean(valores)
    return (f"mín {min(valores):.{casas}f}, média {media:.{casas}f}, máx {max(valores):.{casas}f} "
            f"({len(valores)} leitura{'s' if len(valores) != 1 else ''})")


def historico(ultimas: int = 10, dias: int = 14, arquivo: Path = ARQUIVO) -> str:
    """Texto com as últimas coletas e o resumo por dia."""
    coletas = [c for c in ler(arquivo) if c.get("hora")]
    if not coletas:
        return f"Ainda não há nenhuma coleta gravada em {arquivo}."
    linhas = [f"Arquivo: {arquivo}",
              f"{len(coletas)} coletas, de {datetime.fromisoformat(coletas[0]['hora']):%d/%m/%Y %H:%M} "
              f"a {datetime.fromisoformat(coletas[-1]['hora']):%d/%m/%Y %H:%M}", "", f"Últimas {min(ultimas, len(coletas))} coletas:"]

    def campo(valor, sufixo: str) -> str:
        return f"{valor}{sufixo}" if valor is not None else "-"

    for c in coletas[-ultimas:]:
        linhas.append(f"  {datetime.fromisoformat(c['hora']):%d/%m %H:%M}  batimentos {campo(c.get('batimentos'), ' bpm')}  "
                      f"oxigenação {campo(c.get('oxigenacao'), '%')}  passos {campo(c.get('passos'), '')}  "
                      f"bateria {campo(c.get('bateria'), '%')}  medição: {c.get('medicao', '?')}")

    # Por dia: as medições ao vivo (uma por coleta) mais o que o anel mediu sozinho.
    por_dia: dict[str, dict[str, list]] = {}

    def dia_de(hora: str) -> dict[str, list]:
        return por_dia.setdefault(hora[:10], {"batimentos": [], "oxigenacao": [], "temperatura": [], "passos": [],
                                              "sono": [], "falhas": []})

    for c in coletas:
        d = dia_de(c["hora"])
        for nome in ("batimentos", "oxigenacao", "passos"):
            if c.get(nome) is not None:
                d[nome].append(c[nome])
        if c.get("medicao") not in ("ok", "parcial", "não feita"):
            d["falhas"].append(c.get("medicao"))
        for r in c.get("historico") or []:
            for nome in ("batimentos", "oxigenacao", "temperatura"):
                if r.get(nome):
                    dia_de(r["hora"])[nome].append(r[nome])
        for r in c.get("sono") or []:
            # a noite pertence ao dia em que se acorda: o que foi registrado depois das 18h conta para o dia seguinte
            hora = datetime.fromisoformat(r["hora"])
            dia = (hora + timedelta(days=1)) if hora.hour >= 18 else hora
            dia_de(dia.isoformat())["sono"].append(r["fase"])

    linhas += ["", "Resumo por dia:"]
    for dia in sorted(por_dia)[-dias:]:
        d = por_dia[dia]
        linhas.append(f"  {datetime.fromisoformat(dia):%d/%m/%Y}")
        linhas.append(f"    batimentos (bpm): {_faixa(d['batimentos'])}")
        linhas.append(f"    oxigenação (%):   {_faixa(d['oxigenacao'])}")
        if d["temperatura"]:
            linhas.append(f"    temperatura (°C): {_faixa(d['temperatura'], 1)}")
        if d["passos"]:
            linhas.append(f"    passos: {max(d['passos'])}")
        if d["sono"]:
            fases = {fase: d["sono"].count(fase) for fase in dict.fromkeys(d["sono"])}
            linhas.append("    sono (registros por fase): " + ", ".join(f"{fase} {n}" for fase, n in fases.items()))
        if d["falhas"]:
            linhas.append(f"    coletas sem medição ao vivo: {len(d['falhas'])}")
    return "\n".join(linhas)
