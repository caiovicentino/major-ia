"""Fluxos de áudio do serviço: a voz (saída) e o microfone (entrada) usam o mesmo PortAudio.

O PortAudio só lê a lista de dispositivos ao ser iniciado. Para a fala e a gravação seguirem a saída e a entrada
que estiverem escolhidas no macOS naquele momento, ele é reiniciado (leva uns 4 ms) sempre que um fluxo vai ser
aberto e não há outro em uso. Reiniciar com um fluxo aberto o derrubaria, daí a contagem.
"""
import threading
from typing import Callable

import sounddevice

_trava = threading.Lock()
_abertos = 0


def abrir(criar: Callable):
    """Abre e inicia um fluxo. criar recebe o módulo sounddevice (já com os dispositivos atuais) e devolve o fluxo."""
    global _abertos
    with _trava:
        if _abertos == 0:
            sounddevice._terminate()
            sounddevice._initialize()
        fluxo = criar(sounddevice)
        _abertos += 1
    try:
        fluxo.start()       # fora da trava: uma interface USB parada leva meio segundo para começar
    except Exception:
        fechar(fluxo, cortar=True)
        raise
    return fluxo


def fechar(fluxo, cortar: bool = False) -> None:
    """Fecha o fluxo. Com cortar=True descarta o que ainda não tocou; sem ele, espera tocar até o fim."""
    global _abertos
    try:
        if cortar:
            fluxo.abort()
        else:
            fluxo.stop()
    except sounddevice.PortAudioError:
        pass
    with _trava:        # abrir e fechar mexem na mesma lista interna do PortAudio: um de cada vez
        try:
            fluxo.close()
        except sounddevice.PortAudioError:
            pass
        _abertos -= 1
