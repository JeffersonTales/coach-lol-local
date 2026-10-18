"""
Ouvido do coach — responde quando você chama "Coach, ...".
Tudo no processador, em duas etapas, para não pesar:
  1. detector de voz (webrtcvad, quase gratuito) separa cada frase que você fala
  2. Whisper "base" transcreve a frase só para procurar o nome do coach
  3. achou o nome → Whisper "small" transcreve de novo, com mais qualidade, e a pergunta vai para a IA
Enquanto o coach está falando, o microfone é ignorado (para ele não ouvir a própria voz).
"""
import logging
import queue
import re
import threading
import time
import unicodedata
from difflib import SequenceMatcher

import numpy as np

log = logging.getLogger("coach")

TAXA = 16000
QUADRO = 480            # 30 ms
FIM_SILENCIO = 30       # quadros de silêncio (~0,9 s) que encerram uma frase
ESPERA_PERGUNTA = 7     # depois de ouvir só "Coach", a próxima frase nesse prazo é a pergunta
# dica de vocabulário para o Whisper (sem isso, "item" vira "tinha", "build" vira "bilde"...)
VOCABULARIO = ("Coach, que item eu faço agora? Build, runa, dragão, barão, arauto, torre, inibidor, "
               "jungle, caçador, top, mid, bot, suporte, farm, flash, ult, ward, visão, gank, base.")
OI = "Oi!"
MAX_QUADROS = 400       # frase de no máximo ~12 s
MIN_QUADROS = 12        # menos que ~0,36 s é ruído


def normalizar(texto):
    texto = unicodedata.normalize("NFD", texto.lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9 ]+", " ", texto).split()


class Ouvido:
    def __init__(self, cfg, voz, ao_ouvir, extra_vocabulario=lambda: ""):
        self.cfg = cfg["ouvido"]
        self.voz = voz
        self.ao_ouvir = ao_ouvir
        self.extra_vocabulario = extra_vocabulario   # nomes da partida atual (seus itens, campeões)
        self.nomes = [normalizar(n)[0] for n in self.cfg["nomes"]]
        self._frases = queue.Queue(maxsize=3)
        self._rapido = self._bom = None
        self._esperando_ate = 0.0   # >agora: ele foi chamado e está esperando a pergunta
        self.entendendo = False      # transcrevendo uma frase em que ouviu o nome (para o status do deck)

    def carregar(self):
        from faster_whisper import WhisperModel
        threads = self.cfg["threads_cpu"]
        self._rapido = WhisperModel(self.cfg["modelo_rapido"], device="cpu", compute_type="int8", cpu_threads=threads)
        self._bom = WhisperModel(self.cfg["modelo_bom"], device="cpu", compute_type="int8", cpu_threads=threads)

    def iniciar(self):
        threading.Thread(target=self._escutar, daemon=True).start()
        threading.Thread(target=self._processar, daemon=True).start()

    # ---------------- captura + detector de voz ----------------
    def _dispositivo(self):
        import sounddevice as sd
        alvo = self.cfg["microfone"].lower()
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and alvo in d["name"].lower():
                return i
        log.warning("microfone '%s' não encontrado, usando o padrão do Windows", self.cfg["microfone"])
        return None

    def _escutar(self):
        import sounddevice as sd
        import webrtcvad
        vad = webrtcvad.Vad(self.cfg["sensibilidade"])
        falando, silencio, quadros = False, 0, []
        while True:
            try:
                with sd.InputStream(samplerate=TAXA, channels=1, dtype="int16", blocksize=QUADRO,
                                    device=self._dispositivo()) as mic:
                    log.info("ouvindo o microfone — chame \"%s, ...\"", self.cfg["nomes"][0])
                    while True:
                        bloco, _ = mic.read(QUADRO)
                        if self.voz.falando or self.voz.falou_ha() < 0.6:
                            falando, silencio, quadros = False, 0, []
                            continue
                        voz = vad.is_speech(bloco.tobytes(), TAXA)
                        if voz:
                            falando, silencio = True, 0
                        elif falando:
                            silencio += 1
                        if falando:
                            quadros.append(bloco.copy())
                        if falando and (silencio >= FIM_SILENCIO or len(quadros) >= MAX_QUADROS):
                            if len(quadros) - silencio >= MIN_QUADROS:
                                audio = np.concatenate(quadros).flatten().astype(np.float32) / 32768
                                try:
                                    self._frases.put_nowait(audio)
                                except queue.Full:
                                    pass  # muita fala seguida: descarta, a prioridade é não acumular
                            falando, silencio, quadros = False, 0, []
            except Exception as erro:
                log.warning("microfone falhou (%s), tentando de novo em 5 s", erro)
                time.sleep(5)

    # ---------------- transcrição em duas etapas ----------------
    def _transcrever(self, modelo, audio, beam, dica="Coach,"):
        partes, _ = modelo.transcribe(audio, language="pt", beam_size=beam, without_timestamps=True,
                                      condition_on_previous_text=False, initial_prompt=dica)
        return " ".join(p.text for p in partes).strip()

    def _parece(self, palavra, limite):
        return palavra in self.nomes or max(SequenceMatcher(None, palavra, n).ratio() for n in self.nomes) >= limite

    def _dica(self):
        try:
            extra = self.extra_vocabulario()
        except Exception:
            extra = ""
        return VOCABULARIO + (" " + extra[:400] if extra else "")   # o Whisper só aproveita ~220 tokens

    def chamou(self, texto, limite):
        """Posição da palavra depois do nome, se o nome aparecer nas primeiras palavras; senão None.
        Também junta pares ("co aixe" → "coaixe"), porque o Whisper às vezes quebra o nome em dois."""
        palavras = normalizar(texto)[:4]
        for i, p in enumerate(palavras):
            if self._parece(p, limite):
                return i + 1
            if i + 1 < len(palavras) and self._parece(p + palavras[i + 1], limite):
                return i + 2
        return None

    def entender(self, audio):
        """Devolve a pergunta (pode ser "") se o coach foi chamado, ou None. Usado também no teste.
        Etapa rápida é tolerante (não perder chamadas); a etapa boa confirma com rigor (não disparar à toa)."""
        rapido = self._transcrever(self._rapido, audio, 1)
        if self.chamou(rapido, 0.6) is None:
            return None
        if self.voz:
            self.voz.escrever_overlay("🎤 Entendendo…")
        self.entendendo = True
        try:
            texto = self._transcrever(self._bom, audio, 1, self._dica())   # beam 3 não ficou melhor nem mais rápido
        finally:
            self.entendendo = False
        pos = self.chamou(texto, 0.75)
        if pos is None:
            return None
        resto = texto.split()
        # tira o nome e a pontuação logo depois dele ("Coach, ..." / "Ô coach! ...")
        return " ".join(resto[pos:]).strip(" ,.!?") if len(resto) >= pos else ""

    def _processar(self):
        while True:
            audio = self._frases.get()
            try:
                inicio = time.monotonic()
                pergunta = self.entender(audio)
                if pergunta is None and time.monotonic() < self._esperando_ate:
                    # já tinha sido chamado: esta frase inteira é a pergunta
                    pergunta = self._transcrever(self._bom, audio, 1, self._dica()).strip(" ,.!?") or None
                if pergunta == "":
                    # só "Coach" e uma pausa: responde "Oi!" (curtinho, já gravado) e espera a pergunta
                    self.voz.dizer(OI, 4, "oi")   # 4 = prioridade de resposta, fala na hora
                    self._esperando_ate = time.monotonic() + ESPERA_PERGUNTA + 1
                    self.voz.escrever_overlay("🎤 Ouvindo…")
                    log.info("VOCÊ: Coach… (esperando a pergunta)")
                    continue
                if pergunta is not None:
                    self._esperando_ate = 0.0
                    log.info("VOCÊ: %s  (entendido em %.1fs)", pergunta, time.monotonic() - inicio)
                    self.ao_ouvir(pergunta)
            except Exception as erro:
                log.warning("falha ao transcrever: %s", erro)
