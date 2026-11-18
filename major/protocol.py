"""Protocolo "JY RING" do anel SR116.

Quadro (little-endian): u16 0xFCFE | u16 comando | u16 total de pacotes | u16 pacote atual | u16 tamanho | conteúdo.
Escrita na característica 33F3, respostas por notificação na 33F4 (serviço 56FF). Sem criptografia.
"""
import struct
from dataclasses import dataclass
from datetime import datetime, timezone

SERVICE = "000056ff-0000-1000-8000-00805f9b34fb"
WRITE = "000033f3-0000-1000-8000-00805f9b34fb"
NOTIFY = "000033f4-0000-1000-8000-00805f9b34fb"
MANUFACTURER_ID = 0x594A  # "JY" nos dados de anúncio
MAGIC = 0xFCFE
OPUS_FRAME = 40           # bytes por quadro Opus (20 ms, 16 kHz mono)
PACKET_SECONDS = 0.24     # áudio em cada pacote de uma gravação guardada: 12 quadros
TRANSFER_RATE = 13        # pacotes por segundo que o anel entrega ao transferir uma gravação (medido: de 9 a 18)


class Cmd:
    SET_TIME = 1
    BATTERY = 6
    HEART_RATE_ON = 7
    HEART_RATE_OFF = 8
    HEART_RATE_DATA = 11    # anel -> nós: timestamp + bpm
    AUX_SETTINGS = 13
    BP_HRV = 15
    TEMPERATURE = 20
    TEMPERATURE_DATA = 21
    SPO2 = 23
    SPO2_DATA = 24          # anel -> nós: timestamp + %
    SPO2_END = 25
    STEP_INFO = 29
    AUTO_MEASURE = 30       # medição automática: hora e minuto de início e de fim, ligada (0/1), intervalo
    STEP_DETAIL = 33
    SLEEP = 34
    HEALTH_RECORD = 36
    DEVICE_INFO = 37
    DEVICE_OPERATION = 39   # 1 byte: 0 reinicia, 1 desliga, 2 restaura de fábrica (o app oficial avisa que apaga todos
                            # os dados do anel). Nunca enviado por este projeto; só com pedido explícito do usuário.
    STEP_EMPTY = 42
    BUTTON = 40             # anel -> nós: 1 = pressionou, 2 = soltou
    SLEEP_EMPTY = 43
    HEALTH_EMPTY = 45
    AUDIO_STATE = 47
    AUDIO_MODE = 48         # 0 = parar, 1 = gravação online, 2 = diálogo com IA
    AUDIO_PAUSE = 49
    AUDIO_ONLINE = 50
    AUDIO_DIALOG = 51
    AUDIO_OFFLINE = 52      # baixa a gravação guardada mais antiga
    AUDIO_OFFLINE_EMPTY = 53  # anel -> nós: não há gravações
    AUDIO_OFFLINE_CLEAR = 54  # confirma o recebimento; o anel apaga o arquivo
    FEATURES = 55
    OFFLINE_COUNT = 61
    HEARTBEAT = 62          # o app oficial manda a cada 2 s. Testado em 15/11/2026: o anel não responde, a ligação
                            # cai do mesmo jeito e a transferência de gravações fica umas 6 vezes mais lenta. Não usar.
    OFFLINE_REC_STOP = 63
    OFFLINE_TRANSFER_STOP = 64
    NAME = 68

AUDIO_CMDS = (Cmd.AUDIO_ONLINE, Cmd.AUDIO_DIALOG, Cmd.AUDIO_OFFLINE)

# bits do estado de áudio (comando 47)
STATE_ONLINE = 1
STATE_DIALOG = 2
STATE_OFFLINE_RECORDING = 16
STATE_OFFLINE_PENDING = 64


@dataclass
class Frame:
    cmd: int
    total: int
    current: int
    payload: bytes


def build(cmd: int, payload: bytes = b"", total: int = 1, current: int = 1) -> bytes:
    return struct.pack("<HHHHH", MAGIC, cmd, total, current, len(payload)) + payload


def parse(raw: bytes) -> Frame | None:
    """Devolve None para notificações fora do formato (o anel manda uma ao conectar)."""
    if len(raw) < 10:
        return None
    magic, cmd, total, current, size = struct.unpack("<HHHHH", raw[:10])
    if magic != MAGIC or len(raw) != size + 10:
        return None
    return Frame(cmd, total, current, raw[10:])


def opus_frames(payload: bytes) -> list[bytes]:
    """Conteúdo de um pacote de áudio: 6 bytes de timestamp + N quadros de 40 bytes."""
    body = payload[6:]
    return [body[i:i + OPUS_FRAME] for i in range(0, len(body) - OPUS_FRAME + 1, OPUS_FRAME)]


def describe_state(state: int) -> str:
    names = [(STATE_ONLINE, "gravação online"), (STATE_DIALOG, "diálogo com IA"),
             (STATE_OFFLINE_RECORDING, "gravando na memória"), (STATE_OFFLINE_PENDING, "gravações pendentes")]
    on = [n for bit, n in names if state & bit]
    return ", ".join(on) if on else ("ocioso" if state == 0 else f"desconhecido ({state})")


def timestamp(raw: bytes) -> datetime:
    """6 bytes little-endian em segundos. O relógio do anel guarda a hora local como se fosse UTC."""
    return datetime.fromtimestamp(int.from_bytes(raw[:6], "little"), timezone.utc).replace(tzinfo=None)


def _records(payload: bytes, size: int) -> list[bytes]:
    chunks = [payload[i:i + size] for i in range(0, len(payload) - size + 1, size)]
    return [c for c in chunks if c != b"\xff" * size]


def steps_now(payload: bytes) -> dict:
    steps, calories, distance = struct.unpack("<IHH", payload[6:14])
    return {"hora": timestamp(payload), "passos": steps, "calorias": calories, "distancia": distance}


def health_records(payload: bytes) -> list[dict]:
    """Registros de 14 bytes: timestamp, bpm, SpO2, temperatura em décimos de grau, 4 bytes desconhecidos."""
    out = []
    for r in _records(payload, 14):
        out.append({"hora": timestamp(r), "bpm": r[6], "spo2": r[7],
                    "temperatura": struct.unpack("<h", r[8:10])[0] / 10})
    return out


SLEEP_STAGES = {0: "acordado", 1: "acordado", 2: "REM", 3: "leve", 4: "profundo"}


def sleep_records(payload: bytes) -> list[dict]:
    return [{"hora": timestamp(r), "fase": SLEEP_STAGES.get(r[6], f"? ({r[6]})"), "movimento": r[7]}
            for r in _records(payload, 8)]
