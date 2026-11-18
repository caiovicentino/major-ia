"""Ponte de voz: mantém uma sessão do Claude Code aberta, entrega cada comando falado e devolve o que ela responde
frase por frase, para a voz começar antes de o texto terminar.

A sessão é a voz da conversa: responde na hora e entrega o trabalho demorado a subagentes em segundo plano. Quando
um deles termina, o Claude Code abre um turno sozinho para contar o resultado, sem ninguém ter perguntado; por isso
a saída do processo é lida o tempo todo, e não só depois de um comando.

Por padrão a sessão roda no modo restrito: só lê arquivos, pesquisa e abre apps, arquivos e links. O modo de acesso
total (restrita=False) roda sem bloqueio de permissões e com todas as ferramentas, como uma sessão do Claude Code
aberta no terminal com --dangerously-skip-permissions. Leia os avisos de segurança do README antes de usar.
"""
import collections
import hashlib
import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from . import config, voz

SESSAO = config.DATA / "sessao_voz.json"
SESSAO_MAX_OCIOSA = 6 * 3600            # depois disso começa conversa nova, para o contexto não crescer sem fim
PERMITIDAS_NO_MODO_RESTRITO = ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "Bash(open *)", "Bash(date)", "Bash(date *)"]
FERRAMENTAS_DE_DELEGAR = {"Agent", "Task"}
INSTRUCOES = (
    "Você está recebendo um comando de voz do usuário, transcrito automaticamente; pode haver erros de transcrição. "
    "Sua resposta será lida em voz alta: responda em português do Brasil, em até duas frases curtas, sem markdown, "
    "listas, caminhos longos ou código, e escreva números, horários e siglas por extenso. Se o pedido estiver ambíguo "
    "ou parecer erro de transcrição, peça para repetir em vez de adivinhar. "
    "Cada mensagem começa com a data e a hora atuais entre colchetes; use-as em vez de consultar o relógio. "
)
INSTRUCOES_ACESSO_TOTAL = (
    "Você tem acesso total a este Mac, como numa sessão normal do Claude Code no terminal: o que for pedido deve ser "
    "executado, não só explicado. Mas você é a voz da conversa e precisa estar sempre livre para ouvir e responder, "
    "então quem executa o trabalho são subagentes. "
    "O que você mesmo resolve na hora: conversa, perguntas que sabe responder e ações instantâneas de um passo só, "
    "como abrir um app, um arquivo ou um site. "
    "Todo o resto (criar ou alterar arquivos e código, pesquisar na internet, instalar, investigar, qualquer coisa com "
    "mais de um passo ou que possa passar de uns cinco segundos) você entrega a um subagente: primeiro escreva uma "
    "frase curta dizendo o que vai fazer, que é falada na hora; depois chame a ferramenta Agent com{modelo} "
    "run_in_background true; e encerre o turno ali com poucas palavras, como: Já te aviso. Não repita o que acabou "
    "de dizer, não espere o subagente, não acompanhe o trabalho dele com outras ferramentas e não faça a mesma "
    "tarefa em paralelo. "
    "O subagente não ouviu a conversa: ponha no prompt dele tudo o que ele precisa saber (o pedido completo, "
    "caminhos, o que já foi combinado e como o resultado deve ficar) e peça um relato final curto do que foi feito "
    "e de onde ficou. "
    "Quando chegar o aviso de que um subagente terminou, conte o resultado ao usuário em uma ou duas frases; se ele "
    "falhou ou ficou em dúvida, diga isso com clareza. Se o aviso mostrar que ele ainda não acabou, diga apenas que "
    "o trabalho continua em andamento. "
    "Enquanto os subagentes trabalham, siga conversando normalmente, sem comentar o andamento deles a não ser que "
    "o usuário pergunte. Pedidos independentes podem ter um subagente cada um, ao mesmo tempo. Para mudar ou "
    "continuar uma tarefa que um subagente está fazendo ou acabou de fazer, mande a mensagem a esse mesmo subagente "
    "com SendMessage, em vez de criar outro mexendo nos mesmos arquivos. Se o usuário mandar parar ou cancelar, "
    "pare o subagente com TaskStop. "
    "Como os comandos chegam por reconhecimento de voz, antes de apagar ou sobrescrever arquivos que já existiam, "
    "enviar mensagens, publicar algo ou gastar dinheiro, repita em uma frase o que entendeu e só entregue a tarefa "
    "depois de um sim explícito do usuário. Passe a mesma regra ao subagente: se o trabalho esbarrar numa ação "
    "dessas que não foi confirmada, ele deve parar e relatar em vez de executar."
)
INSTRUCOES_MODO_RESTRITO = (
    "Se uma ação for negada por falta de permissão, diga em uma frase o que você faria e que isso precisa ser "
    "liberado no computador."
)
def instrucoes(restrita: bool) -> str:
    """As instruções do projeto para o modo pedido, mais as de quem usa (arquivo instrucoes.md), se houver."""
    if restrita:
        texto = INSTRUCOES + INSTRUCOES_MODO_RESTRITO
    else:
        modelo = f" model {config.MODELO_SUBAGENTE} e" if config.MODELO_SUBAGENTE else ""
        texto = INSTRUCOES + INSTRUCOES_ACESSO_TOTAL.replace("{modelo}", modelo)
    try:
        suas = config.INSTRUCOES.read_text(encoding="utf-8").strip()
    except OSError:
        suas = ""
    return texto + (f"\n\nInstruções de quem usa este computador:\n{suas}" if suas else "")


DIAS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")
_FIM_DE_FRASE = re.compile(r"""(?<=[.!?…])["”')\]]*\s+""")
_SEM_RESPOSTA = {"resposta": "Não consegui falar com o Claude.", "negadas": [], "primeira_frase_s": None,
                 "segundos": None, "custo_usd": 0.0, "interrompida": False}


@dataclass
class _Pedido:
    """Um comando enviado, à espera do turno que o responde."""
    texto: str
    enviado_em: float = field(default_factory=time.time)
    pronto: threading.Event = field(default_factory=threading.Event)
    resultado: dict | None = None       # continua None se o processo morrer antes de responder
    automatico: bool = False            # mensagem do próprio serviço (resumo, alerta): a resposta é falada como um aviso


@dataclass
class _Turno:
    inicio: float = field(default_factory=time.time)
    pedido: _Pedido | None = None       # None: turno que o Claude Code abriu sozinho (aviso de tarefa concluída)
    frases: list[str] = field(default_factory=list)
    pendente: str = ""                  # texto recebido que ainda não fechou uma frase
    primeira: float | None = None
    calado: bool = False                # o usuário apertou o botão: o resto deste turno não é mais falado
    delegando: bool = False             # já começou a chamar um subagente
    seguinte: _Pedido | None = None     # comando que entrou na conversa antes de este turno fechar: é o dono do próximo


class Conversa:
    """Um processo do Claude Code que fica no ar entre os comandos. Subir o processo a cada comando custava mais
    de um segundo; assim cada comando é só uma linha no stdin, e a resposta chega em pedaços no stdout.

    ao_falar(frase, espontanea, primeira): cada frase completa do agente principal, na ordem. espontanea indica um
        turno aberto pelo próprio Claude Code (aviso de tarefa concluída); primeira, a primeira frase do turno.
    ao_avisar(resultado): fim de um turno espontâneo, com o mesmo dicionário que perguntar() devolve.
    ao_registrar(texto): o que acontece com as tarefas em segundo plano, para o registro do serviço.
    As três são chamadas na thread que lê a saída do processo."""

    def __init__(self, modelo: str | None = None, restrita: bool = True,
                 ao_falar: Callable[[str, bool, bool], None] | None = None,
                 ao_avisar: Callable[[dict], None] | None = None,
                 ao_registrar: Callable[[str], None] | None = None) -> None:
        self.modelo = modelo
        self.restrita = restrita
        self.ao_falar = ao_falar
        self.ao_avisar = ao_avisar
        self.ao_registrar = ao_registrar
        self.tarefas: list[str] = []         # o que está rodando em segundo plano agora
        self._instrucoes = instrucoes(restrita)
        self._proc: subprocess.Popen | None = None
        self._vida = threading.Lock()        # troca de processo e entrada de pedidos na fila, uma coisa por vez
        self._escrita = threading.Lock()     # perguntar() e interromper() escrevem no mesmo stdin
        self._fila: collections.deque[_Pedido] = collections.deque()    # enviados, na ordem, ainda sem turno
        self._turno: _Turno | None = None
        self._custo = 0.0                    # o Claude Code informa o custo acumulado; guardamos para achar o de cada turno

    # ---- processo ----

    def _marca(self) -> str:
        """Identifica o modelo e as instruções. O Claude Code grava as instruções na primeira mensagem de uma
        conversa e as reaproveita ao retomar, mesmo que o comando traga outras; então uma conversa só é retomada
        se foi criada com estas mesmas instruções."""
        return hashlib.sha256(f"{self.modelo}\n{self._instrucoes}".encode()).hexdigest()[:16]

    def _sessao_salva(self) -> tuple[str | None, float]:
        """Sessão a retomar e o custo que ela já acumulou."""
        try:
            salva = json.loads(SESSAO.read_text())
        except (OSError, json.JSONDecodeError):
            return None, 0.0
        if time.time() - salva.get("usada_em", 0) >= SESSAO_MAX_OCIOSA or salva.get("marca") != self._marca():
            return None, 0.0
        return salva["session_id"], salva.get("custo_acumulado", 0.0)

    def iniciar(self) -> None:
        """Sobe o processo se ainda não estiver no ar, para o primeiro comando não pagar essa espera."""
        with self._vida:
            self._garantir_processo(retomar=True)

    def _garantir_processo(self, retomar: bool) -> None:
        """Chamar com self._vida tomada."""
        if self._proc and self._proc.poll() is None:
            return
        self._abandonar()
        sessao, self._custo = self._sessao_salva() if retomar else (None, 0.0)
        cmd = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "--replay-user-messages", "--append-system-prompt", self._instrucoes]
        if self.restrita:
            permitidas = PERMITIDAS_NO_MODO_RESTRITO + config.PERMITIR
            ferramentas = sorted({regra.split("(")[0] for regra in permitidas})
            cmd += ["--effort", "low", "--strict-mcp-config", "--disable-slash-commands", "--tools", ",".join(ferramentas),
                    "--allowedTools", *permitidas]
        else:   # acesso total: nenhuma ação pede confirmação ao sistema de permissões
            cmd += ["--dangerously-skip-permissions"]
        if sessao:
            cmd += ["--resume", sessao]
        if self.modelo:
            cmd += ["--model", self.modelo]
        config.PROJETO.mkdir(parents=True, exist_ok=True)
        self._proc = subprocess.Popen(cmd, cwd=config.PROJETO, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, bufsize=1)
        threading.Thread(target=self._ler, args=(self._proc,), daemon=True).start()

    def _abandonar(self) -> None:
        """O processo acabou: quem esperava resposta dele é liberado sem resultado. Chamar com self._vida tomada."""
        orfaos = list(self._fila)
        self._fila.clear()
        turno, self._turno = self._turno, None
        if turno and turno.pedido:
            orfaos.append(turno.pedido)
        self.tarefas = []
        for pedido in orfaos:
            pedido.pronto.set()

    def _enviar(self, mensagem: dict) -> None:
        with self._escrita:
            self._proc.stdin.write(json.dumps(mensagem, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()

    def fechar(self) -> None:
        """Encerra o processo. Subagentes que ainda estiverem trabalhando morrem junto."""
        with self._vida:
            proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            proc.stdin.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ---- comandos ----

    @property
    def em_turno(self) -> bool:
        """Há um turno em curso ou um comando na fila (tarefas em segundo plano não contam)."""
        return bool(self._turno or self._fila)

    @property
    def ocupada(self) -> bool:
        """Há um turno em curso, um comando na fila ou uma tarefa em segundo plano."""
        return bool(self._turno or self._fila or self.tarefas)

    def perguntar(self, texto: str, automatico: bool = False) -> dict:
        """Envia o comando e espera o turno que o responde; as frases saem por ao_falar enquanto isso. Um comando
        enviado durante outro turno entra na fila do Claude Code e é atendido em seguida.
        Devolve {'resposta', 'negadas', 'primeira_frase_s', 'segundos', 'custo_usd', 'interrompida'}."""
        agora = datetime.now()
        contexto = (f"[Agora é {DIAS[agora.weekday()]}, {agora.day} de {voz.MESES[agora.month - 1]} de {agora.year}, "
                    f"{agora:%H:%M}.]")
        mensagem = {"type": "user", "message": {"role": "user",
                                                "content": [{"type": "text", "text": f"{contexto}\n{texto}"}]}}
        # Se o processo morrer no meio, tenta de novo retomando a conversa; se nem assim (a sessão guardada pode
        # não existir mais), recomeça do zero.
        for retomar in (True, True, False):
            pedido = _Pedido(texto, automatico=automatico)
            with self._vida:
                try:
                    self._garantir_processo(retomar)
                    self._fila.append(pedido)
                    self._enviar(mensagem)
                except (BrokenPipeError, OSError):
                    self._proc = None
                    self._abandonar()
                    continue
            pedido.pronto.wait()
            if pedido.resultado:
                return pedido.resultado
        return dict(_SEM_RESPOSTA)

    def interromper(self) -> None:
        """O usuário apertou o botão para falar de novo: o resto do turno em curso deixa de ser falado.

        O Claude Code só é mandado parar quando isso não custa nada. A interrupção dele mata também os subagentes
        em segundo plano, e o agente principal continua achando que eles estão trabalhando (medido na versão
        2.1.325); com alguma tarefa em andamento, ou sendo entregue neste turno, o turno segue até o fim, calado."""
        turno = self._turno
        if turno is None:
            return
        turno.calado = True
        if self.tarefas or turno.delegando:
            return
        try:
            self._enviar({"type": "control_request", "request_id": f"int-{time.time_ns()}",
                          "request": {"subtype": "interrupt"}})
        except (BrokenPipeError, OSError, AttributeError):
            pass    # o processo já morreu; perguntar() percebe e sobe outro

    # ---- leitura da saída do processo ----

    def _ler(self, proc: subprocess.Popen) -> None:
        for linha in proc.stdout:
            try:
                self._tratar(json.loads(linha))
            except json.JSONDecodeError:
                continue
            except Exception as erro:       # um evento fora do esperado não pode parar a leitura
                self._registrar(f"evento do Claude Code ignorado ({erro!r})")
        with self._vida:                    # o stdout fechou: o processo morreu
            if self._proc is proc:
                self._proc = None
                self._abandonar()

    def _registrar(self, texto: str) -> None:
        if self.ao_registrar:
            self.ao_registrar(texto)

    def _turno_atual(self) -> _Turno:
        if self._turno is None:
            self._turno = _Turno()
        return self._turno

    def _tratar(self, evento: dict) -> None:
        if evento.get("parent_tool_use_id"):
            return                          # o que os subagentes escrevem não é fala
        tipo = evento.get("type")
        if tipo == "system":
            self._tratar_tarefas(evento)
        elif tipo == "user" and evento.get("isReplay"):
            # O Claude Code devolve cada mensagem nossa no instante em que ela entra na conversa: o turno que
            # começa aqui é a resposta ao comando mais antigo da fila.
            turno = self._turno_atual()
            if turno.pedido is None and self._fila:
                turno.pedido = self._fila.popleft()
            elif turno.pedido is not None and turno.seguinte is None and self._fila:
                # O usuário apertou o botão no meio de uma resposta e já falou de novo: o novo comando entra na
                # conversa antes de chegar o fim do turno interrompido. Sem guardá-lo aqui ele ficava na fila para
                # sempre, e dali em diante cada resposta era atribuída ao comando anterior.
                turno.seguinte = self._fila.popleft()
        elif tipo == "stream_event":
            self._tratar_texto(evento["event"])
        elif tipo == "result":
            self._fechar_turno(evento)

    def _tratar_tarefas(self, evento: dict) -> None:
        subtipo = evento.get("subtype")
        if subtipo == "background_tasks_changed":
            self.tarefas = [t.get("description") or t.get("task_type") or "?" for t in evento.get("tasks") or []]
        elif subtipo == "task_started" and not evento.get("parent_task_id"):
            self._registrar(f"em segundo plano, começou: {evento.get('description')}")
        elif subtipo == "task_progress":
            self._registrar(f"em segundo plano: {(evento.get('description') or '')[:160]}")
        elif subtipo == "task_notification" and not evento.get("parent_task_id"):
            resumo = " ".join((evento.get("summary") or "").split())
            self._registrar(f"em segundo plano, terminou ({evento.get('status')}): {resumo[:400]}")

    def _tratar_texto(self, interno: dict) -> None:
        turno = self._turno_atual()
        delta = interno.get("delta") or {}
        if delta.get("type") == "text_delta":
            *completas, turno.pendente = _FIM_DE_FRASE.split(turno.pendente + delta["text"])
        elif interno.get("type") in ("content_block_stop", "message_stop"):
            # O bloco de texto acabou: fala o que sobrou já, sem esperar o modelo terminar de escrever a chamada de
            # ferramenta que vem depois (o pedido a um subagente leva segundos para ser escrito).
            completas, turno.pendente = [turno.pendente], ""
        else:
            bloco = interno.get("content_block") or {}
            if bloco.get("type") == "tool_use" and bloco.get("name") in FERRAMENTAS_DE_DELEGAR:
                turno.delegando = True
            return
        for frase in filter(None, map(str.strip, completas)):
            primeira = not turno.frases
            turno.frases.append(frase)
            if primeira:
                turno.primeira = round(time.time() - (turno.pedido.enviado_em if turno.pedido else turno.inicio), 2)
            if self.ao_falar and not turno.calado:
                self.ao_falar(frase, turno.pedido is None or turno.pedido.automatico, primeira)

    def _fechar_turno(self, evento: dict) -> None:
        turno, self._turno = self._turno or _Turno(), None
        if turno.seguinte:
            self._turno = _Turno(pedido=turno.seguinte)
        total = evento.get("total_cost_usd") or self._custo
        custo, self._custo = round(total - self._custo, 4), total
        if evento.get("session_id"):
            SESSAO.parent.mkdir(parents=True, exist_ok=True)
            SESSAO.write_text(json.dumps({"session_id": evento["session_id"], "usada_em": time.time(),
                                          "modelo": self.modelo, "marca": self._marca(), "custo_acumulado": total}))
        inicio = turno.pedido.enviado_em if turno.pedido else turno.inicio
        resultado = {"resposta": " ".join(turno.frases) or (evento.get("result") or "").strip(),
                     "negadas": [d.get("tool_name", "?") for d in evento.get("permission_denials") or []],
                     "primeira_frase_s": turno.primeira, "segundos": round(time.time() - inicio, 2),
                     "custo_usd": custo, "interrompida": turno.calado}
        if turno.pedido:
            turno.pedido.resultado = resultado
            turno.pedido.pronto.set()
        elif self.ao_avisar:
            self.ao_avisar(resultado)


def responder(texto: str, falar: bool = True, modelo: str | None = None, restrita: bool = True,
              esperar_tarefas: float = 900, registrar: Callable[[str], None] | None = None) -> dict:
    """Um comando avulso: sobe a conversa, pergunta e fala a resposta (se falar=True). Se o comando deixou alguma
    tarefa em segundo plano, espera o aviso de que ela terminou (por até esperar_tarefas segundos) antes de encerrar,
    porque fechar a conversa mata os subagentes. Os avisos vêm na chave 'avisos' do resultado.
    registrar recebe, na hora em que acontecem, a resposta, o andamento das tarefas e os avisos."""
    locutor = voz.Locutor() if falar else None
    avisos: list[dict] = []
    registrar = registrar or (lambda linha: None)

    def avisar(aviso: dict) -> None:
        avisos.append(aviso)
        registrar(f"AVISO: {aviso['resposta']}")

    conversa = Conversa(modelo, restrita, ao_avisar=avisar, ao_registrar=registrar,
                        ao_falar=(lambda frase, espontanea, primeira: locutor.dizer(frase)) if locutor else None)
    try:
        resultado = conversa.perguntar(texto)
        registrar(f"RESPOSTA: {resultado['resposta']}")
        limite = time.time() + esperar_tarefas
        ocupada_em = time.time() if conversa.tarefas else None
        while ocupada_em and time.time() < limite:
            # A lista de tarefas esvazia um instante antes de o turno do aviso começar: só vale como fim se a
            # conversa ficar parada por alguns segundos.
            if conversa.ocupada:
                ocupada_em = time.time()
            elif time.time() - ocupada_em > 3:
                break
            time.sleep(0.2)
        if locutor:
            locutor.esperar()
        return {**resultado, "avisos": avisos}
    finally:
        conversa.fechar()
        if locutor:
            locutor.fechar()
