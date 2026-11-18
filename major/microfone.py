"""Gravação pela entrada de áudio padrão do Mac, usada tanto pelo botão do anel quanto pelo modo sem anel."""
import wave
from datetime import datetime
from pathlib import Path

import numpy
from scipy.signal import resample_poly

from . import audio, config, portaudio

MIC_MIN_RMS = 80        # medido no canal do microfone: ruído de fundo perto de 40, fala acima de 300


class Microfone:
    """Grava a entrada de áudio padrão do Mac entre start() e stop().

    A captura é feita dentro do próprio processo. Antes era um ffmpeg por gravação, que falhava ao abrir o microfone
    uma vez a cada três ou quatro ("audio format is not supported"), e o comando inteiro se perdia."""

    def __init__(self) -> None:
        self._fluxo = None
        self._blocos: list[bytes] = []
        self._taxa = self._canais = 0
        self._inicio = datetime.now()
        self.error = ""

    @property
    def gravando(self) -> bool:
        return self._fluxo is not None

    def start(self) -> None:
        """Abre a entrada de áudio. Uma interface USB parada leva cerca de meio segundo para começar a entregar som."""
        if self._fluxo:
            return
        self._blocos, self._inicio, self.error = [], datetime.now(), ""

        def criar(sd):
            entrada = sd.query_devices(kind="input")
            self._taxa, self._canais = int(entrada["default_samplerate"]), min(2, entrada["max_input_channels"])
            return sd.RawInputStream(samplerate=self._taxa, channels=self._canais, dtype="int16",
                                     callback=lambda dados, *_: self._blocos.append(bytes(dados)))

        try:
            self._fluxo = portaudio.abrir(criar)
        except Exception as erro:       # sem microfone, sem permissão, interface desligada…
            self.error = repr(erro)

    def stop(self) -> Path | None:
        """Encerra a gravação e devolve o WAV (16 kHz, mono), ou None se nada foi gravado."""
        fluxo, self._fluxo = self._fluxo, None
        if not fluxo:
            return None
        portaudio.fechar(fluxo)
        if not self._blocos:
            self.error = "o microfone abriu, mas não entregou áudio"
            return None
        return self._salvar()

    def _salvar(self) -> Path:
        som = numpy.frombuffer(b"".join(self._blocos), dtype=numpy.int16).reshape(-1, self._canais).astype(numpy.float32)
        # Numa interface de dois canais o microfone costuma estar ligado em um só: fica o canal mais forte.
        canal = som[:, int(numpy.argmax((som ** 2).mean(axis=0)))]
        if self._taxa != audio.RATE:
            canal = resample_poly(canal, audio.RATE, self._taxa)
        path = config.DATA / "voz" / f"{self._inicio:%Y%m%d-%H%M%S}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(audio.RATE)
            out.writeframes(numpy.clip(canal, -32768, 32767).astype(numpy.int16).tobytes())
        return path
