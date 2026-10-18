"""Voz do coach — Kokoro em português, rodando só no processador (a placa de vídeo fica para o jogo e o OBS)."""
import heapq
import itertools
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("coach")


class Voz:
    def __init__(self, cfg, pasta):
        c = cfg["voz"]
        self.cfg = c
        self.overlay = pasta / cfg["overlay"]["arquivo"]
        self.limpar_apos = cfg["overlay"]["limpar_apos"]
        self._fila = []
        self._seq = itertools.count()
        self._cond = threading.Condition()
        self._ultima_fala = 0.0
        self._ultimo_texto = 0.0
        self.falando = False
        self._pipeline = None
        self._prontas = {}   # frases curtas já em áudio: tocam na hora, sem esperar a voz ser gerada
        self.escrever_overlay("")
        threading.Thread(target=self._loop, daemon=True).start()

    def carregar(self):
        if not self.cfg["ativada"]:
            return
        import torch
        torch.set_num_threads(self.cfg["threads_cpu"])
        from kokoro import KPipeline
        self._pipeline = KPipeline(lang_code="p", repo_id="hexgrad/Kokoro-82M", device="cpu")
        list(self._pipeline("Coach pronto.", voice=self.cfg["voz"]))  # aquece

    def preparar_frases(self, frases):
        if not self._pipeline:
            return
        import numpy as np
        for frase in frases:
            partes = [r.audio.numpy() for r in self._pipeline(frase, voice=self.cfg["voz"], speed=self.cfg["velocidade"])]
            if partes:
                self._prontas[frase] = np.concatenate(partes)

    def dizer(self, texto, prioridade, chave=None):
        """Dica com a mesma chave substitui a que ainda está esperando na fila (não repete)."""
        chave = chave or texto
        with self._cond:
            self._fila = [item for item in self._fila if item[4] != chave]
            heapq.heapify(self._fila)
            heapq.heappush(self._fila, (-prioridade, next(self._seq), time.monotonic(), texto, chave))
            self._cond.notify()

    def ocupada(self):
        return self.falando or bool(self._fila)

    def falou_ha(self):
        """Segundos desde o fim da última fala (o ouvido usa para não escutar o próprio coach)."""
        return time.monotonic() - self._ultima_fala

    def calar_dicas(self):
        """Tira da fila tudo que não é resposta a uma pergunta."""
        with self._cond:
            self._fila = [item for item in self._fila if -item[0] >= 4]
            heapq.heapify(self._fila)

    def escrever_overlay(self, texto):
        try:
            self.overlay.write_text(texto, encoding="utf-8")
            self._ultimo_texto = time.monotonic()
        except OSError:
            pass

    def _loop(self):
        while True:
            with self._cond:
                while not self._fila:
                    if self._ultimo_texto and time.monotonic() - self._ultimo_texto > self.limpar_apos:
                        self.escrever_overlay("")
                        self._ultimo_texto = 0.0
                    self._cond.wait(timeout=1)
                neg_prio, _, chegou, texto, _ = self._fila[0]
                # resposta a pergunta (4) fala na hora; urgente (3) espera 8 s; o resto, o intervalo mínimo
                espera = 0 if -neg_prio >= 4 else 8 if -neg_prio == 3 else self.cfg["intervalo_min"]
                falta = espera - (time.monotonic() - self._ultima_fala)
                if falta > 0:
                    self._cond.wait(timeout=min(falta, 1))
                    continue
                heapq.heappop(self._fila)
            if time.monotonic() - chegou > self.cfg["validade"]:
                log.info("descartada (velha): %s", texto)
                continue
            self._falar(texto)

    def _falar(self, texto):
        log.info("COACH: %s", texto)
        self.escrever_overlay(texto)
        self._ultima_fala = time.monotonic()
        if not self._pipeline:
            return
        self.falando = True
        try:
            import numpy as np
            import sounddevice as sd
            inicio = time.monotonic()
            if texto in self._prontas:
                partes = [self._prontas[texto]]
            else:
                partes = [r.audio.numpy() for r in self._pipeline(texto, voice=self.cfg["voz"], speed=self.cfg["velocidade"])]
                log.info("voz gerada em %.1fs", time.monotonic() - inicio)
            if partes:
                sd.play(np.concatenate(partes) * self.cfg["volume"], 24000)
                sd.wait()
        except Exception as erro:  # voz nunca pode derrubar o coach
            log.warning("falha na voz: %s", erro)
        finally:
            self.falando = False
            self._ultima_fala = time.monotonic()
