"""Acompanhamento da saúde: resumo diário e alertas, a partir do que o serviço grava em dados/saude.jsonl.

Este módulo só decide o que dizer e quando; quem fala é a sessão de voz do Claude, que recebe o texto montado aqui
como mensagem automática (ver servico.py). Tudo fica neste Mac:
  dados/saude_estado.json   dia do último resumo e hora do último alerta de cada tipo
  dados/saude_avisos.jsonl  cada resumo e alerta emitido (ou calado pelo horário de silêncio), uma linha por aviso

Os limites abaixo são fáceis de ajustar. O anel não é um aparelho médico: as regras são conservadoras, para não
incomodar à toa, e um alerta só sai com leitura confirmada por várias leituras seguidas.
"""
import json
import statistics
from dataclasses import dataclass
from datetime import datetime, time as hora_do_dia, timedelta
from pathlib import Path

from . import saude

ESTADO = saude.ARQUIVO.with_name("saude_estado.json")
AVISOS = saude.ARQUIVO.with_name("saude_avisos.jsonl")

# ---- resumo diário ----
RESUMO_PADRAO = "09:30"                 # horário local; mudar com --resumo-saude ou MAJOR_RESUMO_SAUDE ("nao" desliga)
RESUMO_ATE = hora_do_dia(12, 0)         # se o horário passou (serviço parado, Mac dormindo), ainda fala até aqui
RESUMO_FOLGA_MIN = 150                  # …ou até este tanto depois do horário, se ele for marcado para mais tarde
RESUMO_DADOS_FRESCOS_MIN = 45           # o resumo espera uma coleta completa mais nova que isto (a noite sincronizada)…
RESUMO_ULTIMA_CHANCE_MIN = 10           # …mas a tantos minutos do fim do prazo sai com o que houver
RESUMO_TENTATIVAS = 3                   # se o Claude não responder, tenta de novo algumas vezes e desiste por hoje
DIAS_DE_COMPARACAO = 7

# ---- alertas: limites amplos, valem sempre, mesmo sem histórico e mesmo no horário de silêncio ----
BATIMENTOS_MAX = 120                    # em repouso (ver PASSOS_DE_ATIVIDADE)
BATIMENTOS_MIN = 45
BATIMENTOS_MIN_DE_NOITE = 38            # dormindo o coração bate mais devagar: de noite só abaixo disto acorda alguém
OXIGENACAO_MIN = 92
# ---- alertas pelo padrão do próprio usuário, quando já houver dados suficientes ----
DIAS_PARA_PADRAO = 3                    # dias diferentes com leituras antes de comparar com o padrão dele
LEITURAS_PARA_PADRAO = 12
BATIMENTOS_DESVIOS = 2.5                # fora do padrão: a tantos desvios da média dele…
BATIMENTOS_FOLGA = 20                   # …e a pelo menos tantos bpm dela
OXIGENACAO_QUEDA = 4                    # pontos abaixo da média dele (e abaixo de OXIGENACAO_ATENCAO)
OXIGENACAO_ATENCAO = 95
TEMPERATURA_SUBIDA = 1.5                # °C acima da média dele (temperatura da pele do dedo, não do corpo)
BATERIA_MIN = 15                        # % da bateria do anel
# ---- quando alertar ----
CONFIRMACAO_JANELA_MIN = 50             # um alerta olha todas as leituras deste tanto de minutos para trás…
CONFIRMACAO_ESPALHADAS_MIN = 15         # …que precisam ser pelo menos duas, separadas por pelo menos isto, e todas fora do limite
PASSOS_DE_ATIVIDADE = 300               # passos entre duas coletas acima disto: batimento alto é exercício, não alerta
ALERTA_INTERVALO_H = {"bateria": 8, "temperatura": 12}     # o mesmo tipo de alerta, no máximo uma vez a cada tantas horas
ALERTA_INTERVALO_PADRAO_H = 4
SILENCIO_DAS, SILENCIO_ATE = hora_do_dia(22, 0), hora_do_dia(8, 0)     # só alertas graves (limites amplos) falam aqui

MARCA = "[MENSAGEM AUTOMÁTICA DO SERVIÇO DO ANEL. Não é fala do usuário: foi gerada pelo próprio serviço.]"


@dataclass
class Alerta:
    tipo: str           # batimentos_altos, batimentos_baixos, oxigenacao, temperatura, bateria
    descricao: str      # o que foi visto, com os números
    grave: bool         # passou de um limite amplo: fala mesmo no horário de silêncio


# ---- estado e registro dos avisos ----

def ler_estado(arquivo: Path | None = None) -> dict:
    arquivo = arquivo or ESTADO
    try:
        estado = json.loads(arquivo.read_text())
    except (OSError, json.JSONDecodeError):
        estado = {}
    estado.setdefault("resumo_dia", None)
    estado.setdefault("resumo_tentativas", {})
    estado.setdefault("alertas", {})
    return estado


def gravar_estado(estado: dict, arquivo: Path | None = None) -> None:
    arquivo = arquivo or ESTADO
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    temporario = arquivo.with_suffix(".tmp")
    temporario.write_text(json.dumps(estado, ensure_ascii=False))
    temporario.replace(arquivo)


def registrar_aviso(aviso: dict, arquivo: Path | None = None) -> None:
    arquivo = arquivo or AVISOS
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    with arquivo.open("a") as out:
        out.write(json.dumps(aviso, ensure_ascii=False) + "\n")


# ---- leituras ----

def _quando(texto: str) -> datetime:
    return datetime.fromisoformat(texto).replace(tzinfo=None)   # tudo é hora local deste Mac


def leituras(coletas: list[dict], metrica: str) -> list[tuple[datetime, float]]:
    """Todas as leituras de 'batimentos', 'oxigenacao' ou 'temperatura', em ordem: as medições ao vivo (uma por
    coleta) e o que o anel mediu sozinho."""
    achadas = []
    for c in coletas:
        if c.get("hora") and c.get(metrica) is not None:
            achadas.append((_quando(c["hora"]), c[metrica]))
        for r in c.get("historico") or []:
            if r.get(metrica):
                achadas.append((_quando(r["hora"]), r[metrica]))
    return sorted(achadas)


def _padrao(lidas: list[tuple[datetime, float]], antes_de: datetime) -> tuple[float, float] | None:
    """Média e desvio do usuário, só com dias anteriores e só quando já há dados suficientes."""
    antigas = [(q, v) for q, v in lidas if q.date() < antes_de.date()]
    if len({q.date() for q, _ in antigas}) < DIAS_PARA_PADRAO or len(antigas) < LEITURAS_PARA_PADRAO:
        return None
    valores = [v for _, v in antigas]
    return statistics.mean(valores), statistics.pstdev(valores)


def _duas_ultimas(lidas: list[tuple[datetime, float]], agora: datetime) -> list[tuple[datetime, float]] | None:
    """As leituras recentes que podem confirmar um alerta: todas as da janela, se forem pelo menos duas e não
    estiverem coladas uma na outra (uma leitura ruim isolada, ou duas seguidas, não bastam)."""
    recentes = [(q, v) for q, v in lidas if timedelta(0) <= agora - q <= timedelta(minutes=CONFIRMACAO_JANELA_MIN)]
    if len(recentes) < 2 or recentes[-1][0] - recentes[0][0] < timedelta(minutes=CONFIRMACAO_ESPALHADAS_MIN):
        return None
    return recentes


def em_silencio(agora: datetime) -> bool:
    h = agora.time()
    return h >= SILENCIO_DAS or h < SILENCIO_ATE


# ---- alertas ----

def avaliar(coletas: list[dict], agora: datetime) -> list[Alerta]:
    """O que as coletas mais recentes mostram de fora do comum. Não olha frequência nem horário: isso é com filtrar()."""
    alertas: list[Alerta] = []
    com_hora = [c for c in coletas if c.get("hora")]
    if not com_hora:
        return alertas

    # Batimentos: duas leituras seguidas, e sem exercício entre elas.
    bpm = leituras(coletas, "batimentos")
    par = _duas_ultimas(bpm, agora)
    if par:
        valores = [v for _, v in par]
        texto = f"entre {min(valores):.0f} e {max(valores):.0f} bpm nas {len(valores)} leituras de {par[0][0]:%H:%M} a {par[-1][0]:%H:%M}"
        passos = [c["passos"] for c in com_hora[-3:] if c.get("passos") is not None]
        em_atividade = len(passos) >= 2 and max(passos) - min(passos) > PASSOS_DE_ATIVIDADE
        padrao = _padrao(bpm, agora)
        minimo_amplo = BATIMENTOS_MIN_DE_NOITE if em_silencio(agora) else BATIMENTOS_MIN
        if min(valores) > BATIMENTOS_MAX and not em_atividade:
            alertas.append(Alerta("batimentos_altos", f"batimentos acima de {BATIMENTOS_MAX} em repouso: {texto}", True))
        elif max(valores) < minimo_amplo:
            alertas.append(Alerta("batimentos_baixos", f"batimentos abaixo de {minimo_amplo}: {texto}", True))
        elif padrao:
            media, desvio = padrao
            folga = max(BATIMENTOS_DESVIOS * desvio, BATIMENTOS_FOLGA)
            if min(valores) > media + folga and not em_atividade:
                alertas.append(Alerta("batimentos_altos", f"batimentos bem acima do padrão dele (média {media:.0f} bpm): {texto}", False))
            elif max(valores) < media - folga and not em_silencio(agora):
                alertas.append(Alerta("batimentos_baixos", f"batimentos bem abaixo do padrão dele (média {media:.0f} bpm): {texto}", False))

    spo2 = leituras(coletas, "oxigenacao")
    par = _duas_ultimas(spo2, agora)
    if par:
        valores = [v for _, v in par]
        texto = f"entre {min(valores):.0f}% e {max(valores):.0f}% nas {len(valores)} leituras de {par[0][0]:%H:%M} a {par[-1][0]:%H:%M}"
        padrao = _padrao(spo2, agora)
        if max(valores) < OXIGENACAO_MIN:
            alertas.append(Alerta("oxigenacao", f"oxigenação abaixo de {OXIGENACAO_MIN}%: {texto}", True))
        elif padrao and max(valores) < min(padrao[0] - OXIGENACAO_QUEDA, OXIGENACAO_ATENCAO):
            alertas.append(Alerta("oxigenacao", f"oxigenação abaixo do padrão dele (média {padrao[0]:.0f}%): {texto}", False))

    temperatura = leituras(coletas, "temperatura")
    par = _duas_ultimas(temperatura, agora)
    padrao = _padrao(temperatura, agora)
    if par and padrao and min(v for _, v in par) > padrao[0] + TEMPERATURA_SUBIDA:
        alertas.append(Alerta("temperatura", f"temperatura da pele do dedo entre {min(v for _, v in par):.1f} e {max(v for _, v in par):.1f} °C nas "
                                             f"{len(par)} leituras de {par[0][0]:%H:%M} a {par[-1][0]:%H:%M}, mais de {TEMPERATURA_SUBIDA} °C acima da média dele ({padrao[0]:.1f} °C)", False))

    ultima = com_hora[-1]
    if ultima.get("bateria") is not None and ultima["bateria"] < BATERIA_MIN:
        alertas.append(Alerta("bateria", f"bateria do anel em {ultima['bateria']}%", False))
    return alertas


def filtrar(alertas: list[Alerta], estado: dict, agora: datetime) -> tuple[list[Alerta], list[Alerta]]:
    """Separa o que deve ser falado do que fica calado pelo horário de silêncio. O que já foi avisado há pouco
    some. Marca no estado (sem gravar) a hora de cada tipo que passou, falado ou calado."""
    falar, calados = [], []
    for alerta in alertas:
        ultimo = estado["alertas"].get(alerta.tipo)
        intervalo = timedelta(hours=ALERTA_INTERVALO_H.get(alerta.tipo, ALERTA_INTERVALO_PADRAO_H))
        if ultimo and agora - _quando(ultimo) < intervalo:
            continue
        if em_silencio(agora) and not alerta.grave:
            calados.append(alerta)      # não marca a hora: se continuar de manhã, aí sim é falado
            continue
        estado["alertas"][alerta.tipo] = agora.isoformat(timespec="seconds")
        falar.append(alerta)
    return falar, calados


def mensagem_de_alerta(alertas: list[Alerta]) -> str:
    return (f"{MARCA} Alerta de saúde do anel. O serviço acabou de observar: "
            + "; ".join(a.descricao for a in alertas) + ". "
            "Avise o usuário agora, você mesmo, sem usar ferramentas nem subagentes, em uma ou duas frases curtas e "
            "calmas: diga o que foi visto, o que pode ser (inclusive leitura ruim do anel) e o que vale fazer, e lembre "
            "que o anel não é um aparelho médico.")


# ---- resumo diário ----

def horario_do_resumo(texto: str | None) -> hora_do_dia | None:
    """'09:30' -> hora; vazio, 'nao', 'off', '0' -> None (desligado). Formato errado também desliga, com ValueError."""
    texto = (texto or "").strip().lower()
    if texto in ("", "nao", "não", "off", "0", "desligado"):
        return None
    h, _, m = texto.replace("h", ":").partition(":")
    return hora_do_dia(int(h), int(m or 0))


def resumo_devido(agora: datetime, estado: dict, horario: hora_do_dia | None) -> bool:
    """Chegou a hora do resumo de hoje e ele ainda não foi falado? Vale do horário marcado até o meio-dia (ou até
    um tempo depois do horário, se ele for mais tarde), para cobrir serviço parado ou Mac dormindo."""
    if horario is None:
        return False
    hoje = agora.date().isoformat()
    if estado.get("resumo_dia") == hoje or estado["resumo_tentativas"].get(hoje, 0) >= RESUMO_TENTATIVAS:
        return False
    inicio = datetime.combine(agora.date(), horario)
    fim = max(datetime.combine(agora.date(), RESUMO_ATE), inicio + timedelta(minutes=RESUMO_FOLGA_MIN))
    return inicio <= agora <= fim


def resumo_deve_esperar(agora: datetime, ultima_coleta: float | None, horario: hora_do_dia) -> bool:
    """O resumo espera a sincronização da manhã: se a última coleta é antiga (o anel passou a noite longe do Mac),
    ele só sai depois que uma coleta nova trouxer a noite. Perto do fim do prazo sai de qualquer jeito, dizendo
    que faltam dados."""
    inicio = datetime.combine(agora.date(), horario)
    fim = max(datetime.combine(agora.date(), RESUMO_ATE), inicio + timedelta(minutes=RESUMO_FOLGA_MIN))
    if agora >= fim - timedelta(minutes=RESUMO_ULTIMA_CHANCE_MIN):
        return False
    return ultima_coleta is None or agora.timestamp() - ultima_coleta > RESUMO_DADOS_FRESCOS_MIN * 60


def _faixa(lidas: list[tuple[datetime, float]], casas: int = 0) -> str:
    if not lidas:
        return "sem leituras"
    valores = [v for _, v in lidas]
    return (f"mínimo {min(valores):.{casas}f}, média {statistics.mean(valores):.{casas}f}, máximo {max(valores):.{casas}f} "
            f"({len(valores)} leituras)")


def _sono(coletas: list[dict], inicio: datetime, fim: datetime) -> dict[str, int]:
    """Minutos por fase entre inicio e fim. Cada registro do anel vale o intervalo até o seguinte (em geral 5 min)."""
    registros = sorted({(_quando(r["hora"]), r["fase"]) for c in coletas for r in c.get("sono") or []})
    passos = [(b[0] - a[0]).total_seconds() / 60 for a, b in zip(registros, registros[1:])]
    passo = min(max(statistics.median(passos), 1), 30) if passos else 5
    minutos: dict[str, int] = {}
    for quando, fase in registros:
        if inicio <= quando < fim:
            minutos[fase] = minutos.get(fase, 0) + round(passo)
    return minutos


def _texto_do_sono(minutos: dict[str, int]) -> str:
    if not minutos:
        return "sem registros"
    dormido = sum(m for fase, m in minutos.items() if fase != "acordado")
    partes = ", ".join(f"{fase} {m} min" for fase, m in minutos.items())
    return f"{dormido // 60}h{dormido % 60:02d} dormindo ({partes})"


def dados_do_resumo(coletas: list[dict], agora: datetime, intervalo_min: float = saude.INTERVALO_PADRAO_MIN) -> str:
    """Os números das últimas 24 horas e da noite, com os dias anteriores para comparar. Texto para o Claude."""
    coletas = [c for c in coletas if c.get("hora")]
    inicio = agora - timedelta(hours=24)
    antes = inicio - timedelta(days=DIAS_DE_COMPARACAO)
    linhas = []

    def no_periodo(lidas, de, ate):
        return [(q, v) for q, v in lidas if de <= q < ate]

    for nome, metrica, casas in (("Batimentos (bpm)", "batimentos", 0), ("Oxigenação (%)", "oxigenacao", 0),
                                 ("Temperatura da pele do dedo (°C)", "temperatura", 1)):
        lidas = leituras(coletas, metrica)
        linhas.append(f"{nome}: últimas 24 horas: {_faixa(no_periodo(lidas, inicio, agora), casas)}; "
                      f"dias anteriores: {_faixa(no_periodo(lidas, antes, inicio), casas)}.")

    # Passos: o contador zera à meia-noite; vale o maior valor visto em cada dia.
    por_dia: dict = {}
    for c in coletas:
        if c.get("passos") is not None:
            dia = _quando(c["hora"]).date()
            por_dia[dia] = max(por_dia.get(dia, 0), c["passos"])
    ontem = agora.date() - timedelta(days=1)
    anteriores = [n for dia, n in por_dia.items() if agora.date() - timedelta(days=DIAS_DE_COMPARACAO + 1) <= dia < ontem]
    linhas.append(f"Passos: ontem {por_dia.get(ontem, 'sem registro')}; hoje até agora {por_dia.get(agora.date(), 'sem registro')}; "
                  + (f"média dos {len(anteriores)} dias anteriores: {statistics.mean(anteriores):.0f}." if anteriores
                     else "sem dias anteriores para comparar."))

    # Sono: a noite que acabou, das 18h de ontem até agora, e as noites anteriores.
    noite = datetime.combine(ontem, hora_do_dia(18, 0))
    linhas.append(f"Sono desta noite: {_texto_do_sono(_sono(coletas, noite, agora))}.")
    noites = []
    for d in range(1, DIAS_DE_COMPARACAO + 1):
        m = _sono(coletas, noite - timedelta(days=d), noite - timedelta(days=d) + timedelta(hours=18))
        dormido = sum(v for fase, v in m.items() if fase != "acordado")
        if dormido:
            noites.append(dormido)
    if noites:
        media = round(statistics.mean(noites))
        linhas.append(f"Noites anteriores ({len(noites)}): média de {media // 60}h{media % 60:02d} dormindo.")
    else:
        linhas.append("Sem noites anteriores para comparar.")

    # Lacunas: quantas coletas houve, quantas sem medição e o maior buraco.
    recentes = [c for c in coletas if inicio <= _quando(c["hora"]) <= agora]
    esperadas = round(24 * 60 / intervalo_min) if intervalo_min else 0
    recentes = [c for c in recentes if c.get("medicao") != "não feita"]     # só recolheu o histórico: não conta
    sem_medicao = sum(c.get("medicao") not in ("ok", "parcial") for c in recentes)
    marcos = [inicio] + [_quando(c["hora"]) for c in recentes] + [agora]
    buraco = max((b - a).total_seconds() / 3600 for a, b in zip(marcos, marcos[1:]))
    linhas.append(f"Coletas nas últimas 24 horas: {len(recentes)} de cerca de {esperadas} esperadas, {sem_medicao} sem "
                  f"medição ao vivo (anel fora do dedo, interrompida ou falha); maior intervalo sem coleta: {buraco:.1f} horas.")
    if recentes and recentes[-1].get("bateria") is not None:
        linhas.append(f"Bateria do anel: {recentes[-1]['bateria']}%.")
    primeira = min((_quando(c["hora"]) for c in coletas), default=None)
    if primeira:
        linhas.append(f"O registro começou em {primeira:%d/%m/%Y às %H:%M}; com poucos dias de dados, as comparações ainda valem pouco.")
    return "\n".join(linhas)


def mensagem_de_resumo(coletas: list[dict], agora: datetime, intervalo_min: float = saude.INTERVALO_PADRAO_MIN) -> str:
    return (f"{MARCA} Resumo diário de saúde combinado com o usuário. Dados do anel:\n"
            f"{dados_do_resumo(coletas, agora, intervalo_min)}\n"
            "Fale agora ao usuário, você mesmo, sem usar ferramentas nem subagentes, um bom-dia com o resumo "
            "interpretado em até quatro frases curtas: como foi a noite, como estão batimentos, oxigenação e "
            "temperatura em relação ao padrão dele, os passos, e só o que merece atenção (inclusive falta de dados). "
            "Não leia todos os números; destaque os que importam. O anel não é um aparelho médico: diga isso só se "
            "houver algo fora do comum.")
