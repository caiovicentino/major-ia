"""Voz das respostas, gerada no próprio Mac: Pocket TTS (voz Rafael, a padrão) ou, como opcional, Kokoro (vozes Alex
e Dora); a voz do macOS fica de reserva."""
import math
import os
import queue
import re
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import numpy
from num2words import num2words
from scipy.signal import resample_poly

from . import config, portaudio  # noqa: F401 -- config lê o arquivo .env antes de MAJOR_VOZ ser consultada

VOZES = {                     # nome: (motor, voz dentro do motor)
    "rafael": ("pocket", "rafael"),     # masculina; a padrão
    "alex": ("kokoro", "pm_alex"),      # masculina; precisa do extra kokoro (ver README)
    "dora": ("kokoro", "pf_dora"),      # feminina; idem
}
# Para trocar de voz, defina MAJOR_VOZ=nome no ambiente ou no arquivo .env e reinicie.
VOZ = os.environ.get("MAJOR_VOZ", "").strip().lower() or "rafael"
VOZ_RESERVA = "Luciana"       # voz do macOS, usada se a voz escolhida não puder ser carregada ou falhar

_lock = threading.Lock()      # os modelos não são reentrantes: carregam e falam uma coisa por vez
_motor = None                 # o motor da voz escolhida depois de carregado; False se o carregamento falhou


MESES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
         "novembro", "dezembro")


def _n(numero: str | int, feminino: bool = False) -> str:
    palavras = num2words(int(numero), lang="pt_BR")
    if feminino:    # horas são femininas: "uma e meia", "vinte e duas horas"
        palavras = re.sub(r"um$", "uma", re.sub(r"dois$", "duas", palavras))
    return palavras


def _hora(m: re.Match) -> str:
    hora, minuto = int(m[1]), int(m[2] or 0)
    if minuto == 0:
        return f"{_n(hora, True)} {'hora' if hora == 1 else 'horas'}"
    return f"{_n(hora, True)} e {_n(minuto)}"


def _data(m: re.Match) -> str:
    dia, mes = int(m[1]), int(m[2])
    if not 1 <= mes <= 12:
        return m[0]
    texto = f"{'primeiro' if dia == 1 else _n(dia)} de {MESES[mes - 1]}"
    return texto + (f" de {_n(m[3])}" if m[3] else "")


def _reais(m: re.Match) -> str:
    reais, centavos = int(m[1].replace(".", "")), int(m[2] or 0)
    texto = f"{_n(reais)} {'real' if reais == 1 else 'reais'}"
    return texto + (f" e {_n(centavos)} {'centavo' if centavos == 1 else 'centavos'}" if centavos else "")


def _ordinal(m: re.Match) -> str:
    palavras = num2words(int(m[1]), lang="pt_BR", to="ordinal")
    return re.sub(r"o\b", "a", palavras) if m[2] == "ª" else palavras


def normalizar(texto: str) -> str:
    """Escreve por extenso o que vier em algarismos: o Pocket TTS lê "16h38" ou "3" como sons sem sentido, e por
    extenso todas as vozes leem horas, datas e valores do mesmo jeito."""
    texto = re.sub(r"R\$\s*(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{2}))?", _reais, texto)
    texto = re.sub(r"\b(\d{1,2})(?:[:h](\d{2})|h)\b", _hora, texto)
    texto = re.sub(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b", _data, texto)
    texto = re.sub(r"(\d+)(º|ª)", _ordinal, texto)
    texto = re.sub(r"\b(\d{1,3}(?:\.\d{3})+)\b", lambda m: m[1].replace(".", ""), texto)      # 1.234 -> 1234
    texto = re.sub(r"\b(\d+),(\d+)\b", lambda m: f"{_n(m[1])} vírgula {' '.join(_n(d) for d in m[2])}", texto)
    texto = re.sub(r"\b(\d+)\.(\d+)\b", lambda m: f"{_n(m[1])} ponto {_n(m[2])}", texto)       # versão 5.2
    texto = re.sub(r"\d{7,}", lambda m: " ".join(_n(d) for d in m[0]), texto)                   # telefones: dígito a dígito
    texto = re.sub(r"\d+", lambda m: _n(m[0]), texto)
    return texto.replace("%", " por cento")


def _trechos(texto: str, limite: int) -> list[str]:
    """Divide um texto comprido em trechos de até `limite` letras, cortando numa pausa: depois de uma pontuação ou,
    na falta, num espaço."""
    trechos = []
    while len(texto) > limite:
        pausas = [m.end() for m in re.finditer(r"[.!?;:,]\s", texto[:limite])]
        corte = pausas[-1] if pausas else texto.rfind(" ", 0, limite) + 1 or limite
        trechos.append(texto[:corte])
        texto = texto[corte:]
    return [trecho.strip() for trecho in trechos + [texto] if trecho.strip()]


class _Pocket:
    """Pocket TTS (Kyutai): o som sai em pedaços curtos enquanto é gerado, uns 0,05 s depois do pedido."""

    def __init__(self, voz: str) -> None:
        from pocket_tts import TTSModel
        self._modelo = TTSModel.load_model(language="portuguese")
        self._estado = self._modelo.get_state_for_audio_prompt(voz)
        self.taxa = self._modelo.sample_rate

    def gerar(self, texto: str) -> Iterator[numpy.ndarray]:
        for pedaco in self._modelo.generate_audio_stream(self._estado, texto):
            yield pedaco.detach().cpu().numpy().reshape(-1)


class _Kokoro:
    """Kokoro, pelo mlx-audio: cada trecho é gerado inteiro antes de tocar, em uns 0,05 s por segundo de fala."""

    REPO = "mlx-community/Kokoro-82M-bf16"
    ARQUIVOS = ["*.json", "*.safetensors"]
    # O Kokoro converte o texto em fonemas com o espeak-ng. O que vem embutido no pacote espeakng-loader procura seus
    # dados num caminho da máquina em que foi compilado e, sem achá-los, encerra o processo inteiro. Por isso só
    # serve o do Homebrew (brew install espeak-ng); sem ele a voz nem tenta carregar.
    ESPEAK = Path("/opt/homebrew/lib/libespeak-ng.dylib"), Path("/opt/homebrew/share/espeak-ng-data")
    LIMITE = 250        # letras por trecho: perto de 430 o texto passa do limite de fonemas e o fim é cortado sem aviso

    def __init__(self, voz: str) -> None:
        biblioteca, dados = self.ESPEAK
        if not (biblioteca.exists() and dados.exists()):
            raise RuntimeError("falta o espeak-ng do Homebrew (brew install espeak-ng)")
        from huggingface_hub import snapshot_download
        try:                    # com o modelo já no disco, iniciar não depende da rede
            pasta = snapshot_download(self.REPO, allow_patterns=self.ARQUIVOS, local_files_only=True)
        except Exception:
            pasta = snapshot_download(self.REPO, allow_patterns=self.ARQUIVOS)
        import misaki.espeak    # noqa: F401 -- ao ser importado, aponta o fonetizador para o espeak embutido
        from phonemizer.backend.espeak.wrapper import EspeakWrapper
        EspeakWrapper.set_library(str(biblioteca))
        EspeakWrapper.set_data_path(str(dados))
        from mlx_audio.tts.utils import load_model
        self._modelo = load_model(Path(pasta), model_type="kokoro")
        self._voz = voz
        self.taxa = self._modelo.sample_rate
        for _ in self.gerar("Pronto, pode falar."):     # a primeira geração é bem mais lenta que as seguintes
            pass

    def gerar(self, texto: str) -> Iterator[numpy.ndarray]:
        for trecho in _trechos(texto, self.LIMITE):
            for parte in self._modelo.generate(trecho, voice=self._voz, lang_code="p"):
                yield numpy.array(parte.audio, dtype=numpy.float32).reshape(-1)


def preparar() -> bool:
    """Carrega a voz escolhida uma vez (leva alguns segundos). Devolve False se só a voz de reserva estiver
    disponível."""
    global _motor
    with _lock:
        if _motor is None:
            try:
                motor, voz = VOZES[VOZ]
                _motor = (_Kokoro if motor == "kokoro" else _Pocket)(voz)
            except Exception as erro:   # sem o modelo (ou sem internet no primeiro uso) a resposta ainda precisa ser falada
                print(f"A voz {VOZ} não pôde ser carregada ({erro!r}); usando a voz do macOS.", flush=True)
                _motor = False
        return bool(_motor)


class _Reamostrador:
    """Converte o áudio da voz (24 kHz) para a taxa da placa de som, pedaço por pedaço e sem emendas.

    A saída é aberta na taxa em que a placa já está. Pedir 24 kHz ao sistema funciona em algumas placas de
    som, mas em outras a voz caía para metade da velocidade depois de algumas aberturas."""

    def __init__(self, origem: int, destino: int) -> None:
        divisor = math.gcd(origem, destino)
        self.sobe, self.desce = destino // divisor, origem // divisor
        # O filtro do resample_poly alcança umas 10 amostras de entrada para cada lado. Cada pedaço é convertido com
        # essa vizinhança, e os cortes caem em múltiplos de "desce" para a grade de saída continuar alinhada.
        self.borda = self.desce * math.ceil(32 / self.desce)
        self._antes = numpy.zeros(0, dtype=numpy.float32)       # vizinhança à esquerda, já convertida
        self._espera = numpy.zeros(0, dtype=numpy.float32)      # entrada que ainda aguarda a vizinhança à direita

    def converter(self, pedaco: numpy.ndarray, fim: bool = False) -> numpy.ndarray:
        """Devolve o que já pode tocar. Com fim=True, devolve também o que estava em espera."""
        if self.sobe == self.desce:
            return pedaco
        self._espera = numpy.concatenate([self._espera, pedaco])
        if fim:
            sobra = -len(self._espera) % self.desce
            prontas = len(self._espera) + sobra
            entrada = numpy.concatenate([self._antes, self._espera, numpy.zeros(sobra, dtype=numpy.float32)])
        else:
            prontas = (len(self._espera) - self.borda) // self.desce * self.desce
            if prontas <= 0:
                return numpy.zeros(0, dtype=numpy.float32)
            entrada = numpy.concatenate([self._antes, self._espera[:prontas + self.borda]])
        saida = resample_poly(entrada, self.sobe, self.desce)
        inicio = len(self._antes) * self.sobe // self.desce
        convertido = saida[inicio:inicio + prontas * self.sobe // self.desce]
        self._antes = numpy.concatenate([self._antes, self._espera[:prontas]])[-self.borda:]
        self._espera = self._espera[prontas:]
        return convertido


class Locutor:
    """A fila única de fala: as frases tocam uma de cada vez, na ordem em que chegam, seja qual for o turno da
    conversa que as produziu (a resposta a um comando ou o aviso de que uma tarefa em segundo plano terminou).

    A saída de áudio fica aberta enquanto há o que falar: um reprodutor novo por frase engolia o começo de cada uma."""

    SILENCIO_INICIAL = 0.15     # o que a saída de áudio perde ao ser aberta passa a ser silêncio, não fala
    OCIOSO = 1.5                # segundos sem frase nova até soltar a saída de áudio

    def __init__(self) -> None:
        self._fila: queue.Queue = queue.Queue()
        self._geracao = 0                   # calar() avança a geração: o que entrou na fila antes é descartado
        self._livre = threading.Event()     # apagado enquanto o usuário grava um comando: nada começa a tocar
        self._livre.set()
        self._taxa, self._canais = 0, 1     # da saída de áudio aberta no momento
        threading.Thread(target=self._tocar, daemon=True).start()

    def dizer(self, frase: str, guardar: bool = False, sinal: str | None = None) -> None:
        """Põe a frase na fila. Com guardar=True ela sobrevive a um calar() dado antes de começar a tocar: o aviso
        de uma tarefa concluída não pode sumir só porque o botão foi apertado naquele instante.
        sinal é um arquivo de som tocado logo antes da frase."""
        if frase.strip():
            self._fila.put((self._geracao, frase, guardar, sinal))

    def calar(self) -> None:
        """Corta o que está tocando e descarta o que esperava na fila."""
        self._geracao += 1

    def segurar(self) -> None:
        """O usuário começou a gravar: nada novo começa a tocar até soltar()."""
        self._livre.clear()

    def soltar(self) -> None:
        self._livre.set()

    def esperar(self) -> None:
        """Só retorna depois que tudo o que entrou na fila até aqui acabou de tocar."""
        fim = threading.Event()
        self._fila.put((self._geracao, fim, True, None))
        fim.wait()

    def fechar(self) -> None:
        self._fila.put((self._geracao, None, True, None))

    def _tocar(self) -> None:
        saida = None
        while True:
            try:
                geracao, item, guardar, sinal = self._fila.get(timeout=self.OCIOSO if saida else None)
            except queue.Empty:                     # ninguém falou mais nada: devolve a placa de som
                saida = self._soltar_saida(saida)
                continue
            if item is None or isinstance(item, threading.Event):
                saida = self._soltar_saida(saida)   # espera o som já enviado tocar até o fim
                if item is None:
                    return
                item.set()
                continue
            if geracao != self._geracao and not guardar:
                continue
            if not self._livre.is_set():
                saida = self._soltar_saida(saida)
                self._livre.wait()
                if geracao != self._geracao and not guardar:
                    continue
            tocando = self._geracao                 # se mudar no meio da frase, é porque mandaram calar

            def cortada() -> bool:
                return self._geracao != tocando

            if sinal:
                subprocess.Popen(["afplay", sinal])
                time.sleep(0.4)                     # o sinal começa antes da voz, sem atrasá-la até ele acabar
            if preparar():
                try:
                    if saida is None:
                        saida = portaudio.abrir(self._criar_saida)
                        self._escrever(saida, numpy.zeros(int(self._taxa * self.SILENCIO_INICIAL), dtype=numpy.float32))
                    conversor = _Reamostrador(_motor.taxa, self._taxa)
                    bloco = _motor.taxa // 10       # a fala vai à placa de 0,1 em 0,1 s, para o calar() cortar logo
                    with _lock:
                        for som in _motor.gerar(normalizar(item)):
                            for inicio in range(0, len(som), bloco):
                                if cortada():
                                    break
                                self._escrever(saida, conversor.converter(som[inicio:inicio + bloco]))
                            if cortada():
                                break
                    if cortada():
                        saida = self._soltar_saida(saida, cortar=True)
                    else:
                        self._escrever(saida, conversor.converter(numpy.zeros(0, dtype=numpy.float32), fim=True))
                    continue
                except Exception as erro:           # a resposta não pode se perder por causa de uma falha na voz
                    print(f"Não consegui falar esta frase com a voz {VOZ} ({erro!r}); usando a voz do macOS.", flush=True)
                    saida = self._soltar_saida(saida, cortar=True)
            reserva = subprocess.Popen(["say", "-v", VOZ_RESERVA, item])
            while reserva.poll() is None:
                if cortada():
                    reserva.terminate()
                time.sleep(0.05)

    def _criar_saida(self, sd):
        """Abre a saída padrão do momento, na taxa em que ela já está (ver _Reamostrador)."""
        placa = sd.query_devices(kind="output")
        self._taxa, self._canais = int(placa["default_samplerate"]), min(2, placa["max_output_channels"])
        return sd.RawOutputStream(samplerate=self._taxa, channels=self._canais, dtype="int16")

    def _escrever(self, saida, som: numpy.ndarray) -> None:
        """Manda para a placa amostras de -1 a 1, como inteiros de 16 bits repetidos em cada canal da saída."""
        if len(som):
            amostras = (numpy.clip(som, -1, 1) * 32767).astype(numpy.int16)
            saida.write(numpy.repeat(amostras, self._canais).tobytes())

    @staticmethod
    def _soltar_saida(saida, cortar: bool = False) -> None:
        """Fecha a saída de áudio, se estiver aberta. Devolve None, para quem chama já esquecer o fluxo."""
        if saida is not None:
            portaudio.fechar(saida, cortar)
        return None


def falar(texto: str) -> None:
    """Lê o texto inteiro em voz alta e só retorna quando a fala termina."""
    locutor = Locutor()
    locutor.dizer(texto)
    locutor.esperar()
    locutor.fechar()
