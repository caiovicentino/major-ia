"""Testes do que funciona sem o anel. Não usam Bluetooth, microfone, alto-falante nem a conta do Claude: o comando
claude é trocado por um programa falso que responde no mesmo formato."""
import json
import os
import stat
import sys
import tempfile
import unittest
import wave
from pathlib import Path

PASTA = Path(tempfile.mkdtemp(prefix="major-teste-"))
os.environ.update(MAJOR_DADOS=str(PASTA / "dados"), MAJOR_PROJETO=str(PASTA / "trabalho"),
                  MAJOR_INSTRUCOES=str(PASTA / "instrucoes.md"), MAJOR_PERMITIR="")

CLAUDE_FALSO = '''#!/usr/bin/env python3
import json, sys
open(sys.argv[0] + ".args", "w").write(json.dumps(sys.argv[1:]))
for linha in sys.stdin:
    pedido = json.loads(linha)
    if pedido.get("type") != "user":
        continue
    texto = pedido["message"]["content"][0]["text"].split("\\n", 1)[1]
    print(json.dumps({"type": "user", "isReplay": True}), flush=True)
    for pedaco in ("Você disse: ", texto, ". Custa R$ 5,50 ", "às 16h30."):
        print(json.dumps({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": pedaco}}}), flush=True)
    print(json.dumps({"type": "stream_event", "event": {"type": "content_block_stop"}}), flush=True)
    print(json.dumps({"type": "result", "session_id": "sessao-de-teste", "total_cost_usd": 0.0}), flush=True)
'''
BIN = PASTA / "bin"
BIN.mkdir()
(BIN / "claude").write_text(CLAUDE_FALSO)
(BIN / "claude").chmod(stat.S_IRWXU)
os.environ["PATH"] = f"{BIN}{os.pathsep}{os.environ['PATH']}"

from major import audio, config, local, microfone, ponte, transcricao  # noqa: E402


class SemBluetooth(unittest.TestCase):
    def test_modo_sem_anel_nao_carrega_bluetooth(self):
        import major.cli  # noqa: F401
        self.assertNotIn("bleak", sys.modules)
        self.assertNotIn("hid", sys.modules)

    def test_dados_ficam_na_pasta_configurada(self):
        self.assertEqual(config.DATA, PASTA / "dados")
        self.assertEqual(ponte.SESSAO.parent, PASTA / "dados")


class Instrucoes(unittest.TestCase):
    def test_instrucoes_de_quem_usa_sao_somadas(self):
        self.assertNotIn("Instruções de quem usa", ponte.instrucoes(True))
        config.INSTRUCOES.write_text("Me chame de Fulano.")
        try:
            self.assertTrue(ponte.instrucoes(True).endswith("Me chame de Fulano."))
        finally:
            config.INSTRUCOES.unlink()

    def test_modo_restrito_nao_fala_em_acesso_total(self):
        self.assertNotIn("acesso total", ponte.instrucoes(True))
        self.assertIn("sim explícito", ponte.instrucoes(False))
        self.assertNotIn("{modelo}", ponte.instrucoes(False))


class Conversa(unittest.TestCase):
    def conversar(self, linhas, **opcoes):
        fila, saida = list(linhas), []

        def entrada(_prompt):
            if not fila:
                raise EOFError
            return fila.pop(0)

        local.conversar(falar=False, entrada=entrada, mostrar=saida.append, **opcoes)
        return saida

    def test_texto_digitado_vai_ao_claude_e_a_resposta_volta_em_frases(self):
        saida = self.conversar(["bom dia", "sair"], so_texto=True)
        self.assertIn("  Você disse: bom dia.", saida)
        self.assertIn("  Custa R$ 5,50 às 16h30.", saida)

    def test_padrao_e_o_modo_restrito(self):
        self.conversar(["oi"], so_texto=True)
        args = json.loads((BIN / "claude.args").read_text())
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertIn("--allowedTools", args)

    def test_acesso_total_so_quando_pedido(self):
        self.conversar(["oi"], so_texto=True, restrita=False)
        self.assertIn("--dangerously-skip-permissions", json.loads((BIN / "claude.args").read_text()))

    def test_enter_grava_e_enter_envia(self):
        gravacao = PASTA / "fala.wav"
        _wav(gravacao, segundos=2, amplitude=8000)

        class MicrofoneFalso:
            gravando, error = False, ""

            def start(self):
                self.gravando = True

            def stop(self):
                self.gravando = False
                return gravacao

        original = local.transcricao.transcrever
        local.transcricao.transcrever = lambda path: "que horas são"
        try:
            saida = self.conversar(["", "", "sair"], microfone=MicrofoneFalso())
        finally:
            local.transcricao.transcrever = original
        self.assertTrue(any(linha.startswith("Você disse") and "que horas são" in linha for linha in saida))
        self.assertIn("  Você disse: que horas são.", saida)
        self.assertFalse(gravacao.exists(), "a gravação deve ser apagada depois de transcrita")

    def test_silencio_nao_vira_pergunta(self):
        silencio = PASTA / "silencio.wav"
        _wav(silencio, segundos=2, amplitude=0)
        self.assertIsNone(local.ouvir(silencio))


class Gravacao(unittest.TestCase):
    def test_microfone_converte_para_16k_mono_pelo_canal_mais_forte(self):
        import numpy
        mic = microfone.Microfone()
        mic._taxa, mic._canais = 48000, 2
        t = numpy.arange(48000) / 48000
        estereo = numpy.zeros((48000, 2), dtype=numpy.int16)
        estereo[:, 1] = (numpy.sin(2 * numpy.pi * 440 * t) * 10000).astype(numpy.int16)
        mic._blocos = [estereo.tobytes()]
        path = mic._salvar()
        with wave.open(str(path)) as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getnframes()), (16000, 1, 16000))
        self.assertTrue(audio.has_speech(path, min_rms=microfone.MIC_MIN_RMS))


class Voz(unittest.TestCase):
    def test_numeros_por_extenso(self):
        from major import voz
        self.assertEqual(voz.normalizar("Custa R$ 5,50 às 16h30."), "Custa cinco reais e cinquenta centavos às dezesseis e trinta.")
        self.assertEqual(voz.normalizar("95% no dia 1/3"), "noventa e cinco por cento no dia primeiro de março")


class Saude(unittest.TestCase):
    def test_alerta_so_com_leituras_confirmadas(self):
        from datetime import datetime, timedelta
        from major import acompanhamento
        agora = datetime(2026, 1, 10, 15, 0)

        def coleta(minutos_atras, bpm):
            return {"hora": (agora - timedelta(minutes=minutos_atras)).isoformat(), "batimentos": bpm, "passos": 100}

        self.assertEqual(acompanhamento.avaliar([coleta(5, 150)], agora), [])
        alertas = acompanhamento.avaliar([coleta(35, 140), coleta(5, 150)], agora)
        self.assertEqual([a.tipo for a in alertas], ["batimentos_altos"])


def _wav(path, segundos, amplitude):
    import numpy
    t = numpy.arange(16000 * segundos) / 16000
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes((numpy.sin(2 * numpy.pi * 300 * t) * amplitude).astype(numpy.int16).tobytes())


if __name__ == "__main__":
    unittest.main()
