"""Linha de comando: conversa sem anel (teclado e microfone do Mac), serviço do anel, saúde e utilitários.

As bibliotecas de Bluetooth só são carregadas pelos comandos que falam com o anel; quem não tem o anel não
precisa instalá-las."""
from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from . import audio, protocol as p
from .config import DATA
from .transcricao import transcrever

if TYPE_CHECKING:
    from .link import Ring


async def _connect() -> Ring:
    try:
        from .link import Ring
    except ImportError:
        raise SystemExit("Faltam as bibliotecas do anel. Instale com: uv sync --extra anel")
    ring = Ring()
    print("Procurando o anel…", flush=True)
    if not await ring.connect():
        raise SystemExit("Não achei o anel. Ele está perto e desconectado do aplicativo do celular?")
    print("Conectado.", flush=True)
    return ring


async def status() -> None:
    ring = await _connect()
    try:
        await asyncio.sleep(0.8)  # o anel despeja informações sozinho ao conectar
        battery = await ring.request(p.Cmd.BATTERY, b"\0\0")
        features = await ring.request(p.Cmd.FEATURES, b"\0\0")
        state = await ring.request(p.Cmd.AUDIO_STATE, b"\0\0")
        count = await ring.request(p.Cmd.OFFLINE_COUNT)
        if battery:
            print(f"Bateria: {battery.payload[0]}%")
        if features and len(features.payload) >= 2:
            print(f"Gravação remota: {'sim' if features.payload[0] else 'não'} | "
                  f"gravação na memória: {'sim' if features.payload[1] else 'não'}")
        if state:
            print(f"Áudio: {p.describe_state(state.payload[0])}")
        if count and len(count.payload) >= 2:
            print(f"Gravações guardadas: {int.from_bytes(count.payload[:2], 'little')}")
    finally:
        await ring.disconnect()


async def baixar() -> Path | None:
    """Baixa a gravação guardada mais antiga. Não apaga nada do anel."""
    ring = await _connect()
    packets: dict[int, bytes] = {}
    progress = {"total": 0, "last": time.time()}

    def on_frame(frame: p.Frame) -> None:
        if frame.cmd == p.Cmd.AUDIO_OFFLINE:
            packets[frame.current] = frame.payload
            progress["total"] = frame.total
            progress["last"] = time.time()

    ring.on_frame(on_frame)
    try:
        await ring.send(p.Cmd.OFFLINE_TRANSFER_STOP)
        await asyncio.sleep(0.4)
        await ring.send(p.Cmd.AUDIO_OFFLINE)
        while ring.connected and time.time() - progress["last"] < 10:
            await asyncio.sleep(0.5)
            if progress["total"] and len(packets) >= progress["total"]:
                break
    finally:
        await ring.disconnect()
    if not packets:
        print("O anel não enviou nenhuma gravação.")
        return None
    if len(packets) < progress["total"]:
        print(f"Transferência incompleta: {len(packets)} de {progress['total']} pacotes.")
        return None
    frames = [f for i in sorted(packets) for f in p.opus_frames(packets[i])]
    path = DATA / "gravacoes" / f"{datetime.now():%Y%m%d-%H%M%S}.wav"
    seconds = audio.write_wav(frames, path)
    print(f"Gravação de {seconds:.0f}s salva em {path}")
    return path


async def puxar(ring: Ring, apagar: bool, andamento: Callable[[int, int], None] | None = None,
                cortar: tuple[int, int] | None = None, cortadas: dict[Path, float] | None = None) -> list[Path]:
    """Baixa as gravações guardadas usando uma conexão já aberta. Com apagar=True confirma cada uma
    ao anel (que então a apaga), mas só depois de o WAV estar salvo no Mac.

    O anel entrega a mais antiga primeiro, sempre do começo, e só a apaga depois de mandá-la inteira (medido em
    15/11/2026: o pedido de apagar antes disso é ignorado, de qualquer jeito que se peça). Uma gravação de 30
    minutos, o máximo que ele faz, leva uns 10 minutos para chegar; andamento(recebidos, total) é chamado ao longo
    de cada transferência para quem quiser avisar disso.

    cortar=(limite, guarda): de uma gravação com mais de `limite` pacotes só os primeiros `guarda` são guardados;
    o resto precisa chegar para o anel apagar, mas é jogado fora na chegada. O WAV dessas ganha o sufixo -comeco e
    entra em `cortadas` com a duração, em segundos, que a gravação tinha no anel."""
    saved: list[Path] = []
    packets: dict[int, bytes] = {}
    chegaram: set[int] = set()      # números dos pacotes recebidos, guardados ou não
    st = {"total": 0, "last": time.time(), "remaining": None, "empty": False}

    def on_frame(frame: p.Frame) -> None:
        if frame.cmd == p.Cmd.AUDIO_OFFLINE:
            if not (cortar and frame.total > cortar[0] and frame.current > cortar[1]):
                packets[frame.current] = frame.payload
            chegaram.add(frame.current)
            st["total"] = frame.total
            st["last"] = time.time()
        elif frame.cmd == p.Cmd.AUDIO_OFFLINE_CLEAR and len(frame.payload) >= 2:
            st["remaining"] = int.from_bytes(frame.payload[:2], "little")
        elif frame.cmd == p.Cmd.AUDIO_OFFLINE_EMPTY:
            st["empty"] = True

    ring.on_frame(on_frame)
    try:
        for _ in range(12):
            count = await ring.request(p.Cmd.OFFLINE_COUNT)
            n = int.from_bytes(count.payload[:2], "little") if count and len(count.payload) >= 2 else 0
            if n == 0:
                break
            packets.clear()
            chegaram.clear()
            st.update(total=0, last=time.time(), remaining=None, empty=False)
            await ring.send(p.Cmd.OFFLINE_TRANSFER_STOP)
            await asyncio.sleep(0.4)
            await ring.send(p.Cmd.AUDIO_OFFLINE)
            while ring.connected and not st["empty"] and time.time() - st["last"] < 10:
                await asyncio.sleep(0.2)
                if st["total"] and len(chegaram) >= st["total"] and st["remaining"] is not None:
                    break
                if andamento and st["total"]:
                    andamento(len(chegaram), st["total"])
            if st["empty"] or not packets:
                break
            if len(chegaram) < st["total"]:
                print(f"Transferência incompleta ({len(chegaram)} de {st['total']} pacotes, "
                      f"{len(chegaram) / st['total']:.0%}); nada foi apagado.", flush=True)
                break
            frames = [f for i in sorted(packets) for f in p.opus_frames(packets[i])]
            inicio = p.timestamp(packets[min(packets)])
            cortada = len(packets) < st["total"]
            path = DATA / "gravacoes" / f"{inicio:%Y%m%d-%H%M%S}{'-comeco' if cortada else ''}.wav"
            seconds = audio.write_wav(frames, path)
            saved.append(path)
            if cortada:
                if cortadas is not None:
                    cortadas[path] = st["total"] * p.PACKET_SECONDS
                print(f"Salva: {path.name} ({seconds:.0f}s, gravada em {inicio:%d/%m %H:%M}; a gravação tinha "
                      f"{st['total'] * p.PACKET_SECONDS / 60:.0f} min e o resto foi descartado na chegada)", flush=True)
            else:
                print(f"Salva: {path.name} ({seconds:.0f}s, gravada em {inicio:%d/%m %H:%M})", flush=True)
            if not apagar:
                break  # sem confirmar, o anel devolveria sempre a mesma gravação
            if st["remaining"] is None:
                print("O anel não sinalizou o fim do arquivo; não apaguei.", flush=True)
                break
            await ring.send(p.Cmd.AUDIO_OFFLINE_CLEAR, st["remaining"].to_bytes(2, "little"))
            await asyncio.sleep(1.2)
    finally:
        ring.off_frame(on_frame)
    return saved


async def baixar_tudo(apagar: bool) -> list[Path]:
    ring = await _connect()
    try:
        await asyncio.sleep(0.8)
        return await puxar(ring, apagar)
    finally:
        await ring.disconnect()


async def escutar(segundos: int, modo: int, com_transcricao: bool) -> None:
    """Fica conectado. Ao apertar o botão pede áudio ao vivo; ao soltar, salva e transcreve."""
    ring = await _connect()
    loop = asyncio.get_running_loop()
    t0 = time.time()
    frames: list[bytes] = []
    clips: list[Path] = []

    def stamp() -> str:
        return f"[{time.time() - t0:5.1f}s]"

    def finish_clip() -> None:
        if not frames:
            print(f"{stamp()} soltou o botão; nenhum áudio chegou.", flush=True)
            return
        path = DATA / "voz" / f"{datetime.now():%Y%m%d-%H%M%S}.wav"
        seconds = audio.write_wav(list(frames), path)
        frames.clear()
        clips.append(path)
        print(f"{stamp()} clipe de {seconds:.1f}s salvo em {path}", flush=True)

    def on_frame(frame: p.Frame) -> None:
        if frame.cmd == p.Cmd.BUTTON and frame.payload:
            if frame.payload[0] == 1:
                print(f"{stamp()} botão pressionado → pedindo áudio (modo {modo})", flush=True)
                loop.create_task(ring.send(p.Cmd.AUDIO_MODE, bytes([modo, 0])))
            elif frame.payload[0] == 2:
                loop.create_task(ring.send(p.Cmd.AUDIO_MODE, b"\0\0"))
                loop.call_later(0.8, finish_clip)
        elif frame.cmd in (p.Cmd.AUDIO_ONLINE, p.Cmd.AUDIO_DIALOG):
            if not frames:
                print(f"{stamp()} áudio ao vivo chegando (comando {frame.cmd})", flush=True)
            frames.extend(p.opus_frames(frame.payload))
        elif frame.cmd in (p.Cmd.AUDIO_MODE, p.Cmd.AUDIO_PAUSE) and len(frame.payload) >= 2:
            print(f"{stamp()} anel respondeu ao modo {frame.payload[0]}: estado final {frame.payload[1]}", flush=True)
        elif frame.cmd == p.Cmd.AUDIO_STATE and frame.payload:
            print(f"{stamp()} estado de áudio: {p.describe_state(frame.payload[0])}", flush=True)
        elif frame.cmd not in (p.Cmd.HEARTBEAT, p.Cmd.DEVICE_INFO, p.Cmd.NAME, p.Cmd.FEATURES, p.Cmd.STEP_INFO):
            print(f"{stamp()} comando {frame.cmd}: {frame.payload[:24].hex()}", flush=True)

    ring.on_frame(on_frame)
    print(f"Escutando por {segundos}s. Segure o botão do anel e fale.", flush=True)
    try:
        while ring.connected and time.time() - t0 < segundos:
            await asyncio.sleep(0.5)
        if not ring.connected:
            print(f"{stamp()} a conexão caiu.", flush=True)
    finally:
        await ring.disconnect()
    if com_transcricao:
        for clip in clips:
            print(f"{clip.name}: {transcrever(clip) or '(sem texto)'}", flush=True)


async def _coletar(ring: Ring, cmd: int, vazio: int, dia: int) -> list[bytes]:
    """Pede um histórico de vários pacotes e devolve os conteúdos em ordem."""
    packets: dict[int, bytes] = {}
    done = asyncio.Event()
    last = {"t": time.time()}

    def on_frame(frame: p.Frame) -> None:
        if frame.cmd == vazio:
            done.set()
        elif frame.cmd == cmd:
            packets[frame.current] = frame.payload
            last["t"] = time.time()
            if frame.current >= frame.total:
                done.set()

    ring.on_frame(on_frame)
    await ring.send(cmd, bytes([dia]))
    while not done.is_set() and time.time() - last["t"] < 5:
        await asyncio.sleep(0.2)
    ring.off_frame(on_frame)
    return [packets[i] for i in sorted(packets)]


async def saude(dia: int) -> None:
    """Passos de agora, registros de saúde e sono do dia pedido (0 = hoje)."""
    ring = await _connect()
    try:
        await asyncio.sleep(0.8)
        passos = await ring.request(p.Cmd.STEP_INFO)
        if passos and len(passos.payload) >= 14:
            s = p.steps_now(passos.payload)
            print(f"Passos: {s['passos']} | calorias: {s['calorias']} | distância: {s['distancia']}")
        registros = [r for pl in await _coletar(ring, p.Cmd.HEALTH_RECORD, p.Cmd.HEALTH_EMPTY, dia) for r in p.health_records(pl)]
        print(f"Registros de saúde: {len(registros)}")
        for r in registros[-12:]:
            print(f"  {r['hora']:%d/%m %H:%M}  {r['bpm']} bpm  SpO2 {r['spo2']}%  {r['temperatura']:.1f} °C")
        sono = [r for pl in await _coletar(ring, p.Cmd.SLEEP, p.Cmd.SLEEP_EMPTY, dia) for r in p.sleep_records(pl)]
        print(f"Registros de sono: {len(sono)}")
        if sono:
            fases: dict[str, int] = {}
            for r in sono:
                fases[r["fase"]] = fases.get(r["fase"], 0) + 1
            print(f"  de {sono[0]['hora']:%d/%m %H:%M} a {sono[-1]['hora']:%d/%m %H:%M}: {fases}")
    finally:
        await ring.disconnect()


async def medir(segundos: int) -> None:
    """Mede batimentos e SpO2 agora. O anel precisa estar no dedo."""
    ring = await _connect()
    leituras = {"bpm": [], "spo2": []}

    def on_frame(frame: p.Frame) -> None:
        if frame.cmd == p.Cmd.HEART_RATE_DATA and len(frame.payload) >= 7:
            leituras["bpm"].append(frame.payload[6])
            print(f"  batimentos: {frame.payload[6]} bpm", flush=True)
        elif frame.cmd == p.Cmd.SPO2_DATA and len(frame.payload) >= 7:
            leituras["spo2"].append(frame.payload[6])
            print(f"  SpO2: {frame.payload[6]}%", flush=True)

    ring.on_frame(on_frame)
    try:
        # um de cada vez: medidos juntos, só o SpO2 responde
        for nome, ligar, desligar in (("bpm", (p.Cmd.HEART_RATE_ON, b""), (p.Cmd.HEART_RATE_OFF, b"")),
                                      ("spo2", (p.Cmd.SPO2, b"\x01"), (p.Cmd.SPO2, b"\x00"))):
            await ring.send(*ligar)
            t0 = time.time()
            while ring.connected and time.time() - t0 < segundos / 2 and len(leituras[nome]) < 3:
                await asyncio.sleep(0.5)
            if not ring.connected:
                break
            await ring.send(*desligar)
            await asyncio.sleep(1)
    finally:
        await ring.disconnect()
    if not leituras["bpm"] and not leituras["spo2"]:
        print("Nenhuma leitura chegou. O anel está no dedo?")


def preparar() -> None:
    """Confere o que a cadeia de voz precisa e baixa o modelo de transcrição se faltar."""
    import shutil

    from . import config, transcricao
    print(f"Pasta de dados: {config.DATA}")
    print(f"Pasta de trabalho do Claude: {config.PROJETO}")
    ok = True
    for comando, falta in (("claude", "instale o Claude Code e entre na sua conta (rode: claude)"),
                           ("whisper-cli", "brew install whisper-cpp")):
        achou = shutil.which(comando)
        ok &= bool(achou)
        print(f"{comando}: {achou or 'NÃO ENCONTRADO: ' + falta}")
    if not config.WHISPER_MODELO.exists():
        print(f"Baixando o modelo de transcrição (cerca de 470 MB) para {config.WHISPER_MODELO}…", flush=True)
        try:
            transcricao.baixar_modelo()
        except Exception as erro:
            ok = False
            print(f"Não consegui baixar o modelo ({erro!r}). Baixe à mão de {config.WHISPER_MODELO_URL}")
    if config.WHISPER_MODELO.exists():
        print(f"Modelo de transcrição: {config.WHISPER_MODELO}")
    print(f"Instruções suas para o assistente: {config.INSTRUCOES if config.INSTRUCOES.exists() else 'nenhuma (opcional: copie instrucoes.exemplo.md para instrucoes.md)'}")
    print("Tudo pronto. Experimente: major conversar" if ok else "Ainda falta o que está marcado acima.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="major", description="Assistente de voz com o Claude Code, com ou sem o anel SR116")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def opcoes_do_claude(comando) -> None:
        comando.add_argument("--acesso-total", action="store_true",
                             help="o Claude pode fazer qualquer coisa neste Mac, sem pedir permissão (leia os avisos do "
                                  "README); sem esta opção ele só lê arquivos, pesquisa e abre apps e links")
        comando.add_argument("--modelo", help="modelo do Claude (ex.: haiku, sonnet); padrão: o da sua conta")

    sub.add_parser("preparar", help="confere a instalação e baixa o modelo de transcrição")
    c = sub.add_parser("conversar", help="SEM O ANEL: Enter começa e termina de gravar pelo microfone do Mac; ou digite a pergunta")
    c.add_argument("--texto", action="store_true", help="só digitar: o microfone nunca é aberto")
    c.add_argument("--mudo", action="store_true", help="não fala a resposta, só mostra na tela")
    opcoes_do_claude(c)
    pt = sub.add_parser("ponte", help="manda um texto ao Claude como se fosse um comando de voz e encerra")
    pt.add_argument("texto")
    pt.add_argument("--mudo", action="store_true", help="não lê a resposta em voz alta")
    opcoes_do_claude(pt)
    tr = sub.add_parser("transcrever", help="transcreve um arquivo WAV (16 kHz, mono)")
    tr.add_argument("arquivo", type=Path)

    v = sub.add_parser("servir", help="COM O ANEL: fica rodando; segurou o botão e falou, o texto chega aqui")
    v.add_argument("--executar", metavar="COMANDO", help="programa que recebe cada texto transcrito como último argumento")
    v.add_argument("--claude", action="store_true", help="entrega cada frase ao Claude e fala a resposta")
    opcoes_do_claude(v)
    v.add_argument("--resumo-saude", metavar="HH:MM",
                   help="horário do resumo diário de saúde falado (padrão: 09:30, ou a variável MAJOR_RESUMO_SAUDE; "
                        "'nao' desliga)")
    v.add_argument("--sem-alertas-saude", action="store_true",
                   help="não falar alertas de saúde (também: variável MAJOR_ALERTAS_SAUDE=0)")
    v.add_argument("--saude-minutos", type=float, metavar="MIN",
                   help="intervalo da coleta de saúde gravada em dados/saude.jsonl (padrão: 30, ou a variável "
                        "MAJOR_SAUDE_MINUTOS; 0 desliga)")
    sub.add_parser("status", help="anel: bateria, recursos e gravações guardadas")
    b = sub.add_parser("baixar", help="anel: baixa a gravação guardada mais antiga (não apaga)")
    b.add_argument("--transcrever", action="store_true")
    b.add_argument("--tudo", action="store_true", help="baixa todas as gravações")
    b.add_argument("--apagar", action="store_true", help="apaga do anel cada gravação já salva no Mac")
    e = sub.add_parser("escutar", help="anel: mostra o que ele manda ao apertar o botão (para estudar o protocolo)")
    e.add_argument("--segundos", type=int, default=90)
    e.add_argument("--modo", type=int, default=2, choices=(1, 2), help="1 = gravação online, 2 = diálogo com IA")
    e.add_argument("--transcrever", action="store_true")
    h = sub.add_parser("saude", help="anel: passos, registros de saúde e sono guardados")
    h.add_argument("--dia", type=int, default=0, help="0 = hoje, 1 = ontem…")
    m = sub.add_parser("medir", help="anel: mede batimentos e oxigenação agora")
    m.add_argument("--segundos", type=int, default=45)
    ma = sub.add_parser("medicao-automatica", help="liga ou desliga a medição automática do próprio anel (o serviço "
                                                   "aplica na próxima conexão; não usa o Bluetooth daqui)")
    ma.add_argument("acao", choices=("ligar", "desligar", "estado"))
    ma.add_argument("--intervalo", type=int, metavar="MIN", help="de quantos em quantos minutos o anel mede sozinho "
                                                                 "(padrão: 10; o aplicativo do fabricante usa 5)")
    sh = sub.add_parser("saude-historico", help="últimas coletas gravadas pelo serviço e resumo por dia (não usa o anel)")
    sh.add_argument("--ultimas", type=int, default=10, help="quantas coletas listar")
    sh.add_argument("--dias", type=int, default=14, help="quantos dias no resumo")
    args = parser.parse_args()
    if args.cmd == "preparar":
        preparar()
    elif args.cmd == "conversar":
        from .local import conversar
        conversar(so_texto=args.texto, falar=not args.mudo, modelo=args.modelo, restrita=not args.acesso_total)
    elif args.cmd == "transcrever":
        print(transcrever(args.arquivo) or "(sem texto)")
    elif args.cmd == "status":
        asyncio.run(status())
    elif args.cmd == "baixar":
        if args.tudo or args.apagar:
            paths = asyncio.run(baixar_tudo(args.apagar))
        else:
            path = asyncio.run(baixar())
            paths = [path] if path else []
        if args.transcrever:
            for path in paths:
                print(f"{path.name}: {transcrever(path) or '(sem texto)'}")
    elif args.cmd == "servir":
        try:
            from .servico import servir
        except ImportError:
            raise SystemExit("Faltam as bibliotecas do anel. Instale com: uv sync --extra anel")
        asyncio.run(servir(args.executar, args.claude, args.modelo, not args.acesso_total, args.saude_minutos,
                           args.resumo_saude, False if args.sem_alertas_saude else None))
    elif args.cmd == "ponte":
        from .ponte import responder
        r = responder(args.texto, falar=not args.mudo, modelo=args.modelo, restrita=not args.acesso_total,
                      registrar=lambda linha: print(linha, flush=True))
        print(f"(1ª frase em {r['primeira_frase_s']}s, resposta completa em {r['segundos']}s, "
              f"custo US$ {r['custo_usd'] + sum(a['custo_usd'] for a in r['avisos']):.4f}, "
              f"ações negadas: {r['negadas'] or 'nenhuma'})")
    elif args.cmd == "saude":
        asyncio.run(saude(args.dia))
    elif args.cmd == "medir":
        asyncio.run(medir(args.segundos))
    elif args.cmd == "medicao-automatica":
        from . import saude as registro
        if args.acao != "estado":
            if args.intervalo is not None and not 1 <= args.intervalo <= 255:
                raise SystemExit("O intervalo tem de ficar entre 1 e 255.")
            registro.definir_medicao_automatica(args.acao == "ligar", args.intervalo)
        ajuste = registro.medicao_automatica()
        print(f"Medição automática do anel: {'ligada' if ajuste['ligada'] else 'desligada'}, intervalo {ajuste['intervalo']}. "
              "O serviço (major servir) manda este ajuste ao anel a cada conexão; o que já foi mandado fica em "
              f"{registro.AJUSTES_APLICADOS}.")
    elif args.cmd == "saude-historico":
        from .saude import historico
        print(historico(args.ultimas, args.dias))
    elif args.cmd == "escutar":
        asyncio.run(escutar(args.segundos, args.modo, args.transcrever))
