"""
Guarda-costas de desempenho: se a placa de vídeo apertar ou o OBS começar a pular frames,
a IA pausa (o coach continua só com as regras, que não pesam nada).
"""
import ctypes
import json
import logging
import os
import threading
import time
from pathlib import Path

import psutil

log = logging.getLogger("coach")

ABAIXO_DO_NORMAL = 0x00004000


def baixar_prioridade():
    """Coach e Ollama em prioridade 'abaixo do normal': o Windows sempre atende o LoL e o OBS primeiro."""
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), ABAIXO_DO_NORMAL)
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower().startswith("ollama"):
            try:
                p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            except psutil.Error:
                pass


class Guarda:
    def __init__(self, cfg):
        self.cfg = cfg["guarda"]
        self.livre = True
        self.motivo = ""
        self._calmo_desde = time.monotonic()
        self._gpu = None
        self._obs = None
        self._pulados = None
        try:
            import pynvml
            pynvml.nvmlInit()
            self._nv = pynvml
            self._gpu = pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception as erro:
            log.warning("sem leitura da GPU (%s) — guarda só pelo OBS", erro)
        self._obs_tentativa = 0.0
        self._ia_ate = 0.0   # enquanto a própria IA do coach roda (e logo depois), o pico da GPU é dela
        threading.Thread(target=self._loop, daemon=True).start()

    def _conectar_obs(self):
        """Tenta a cada 15 s (o OBS pode ser aberto depois do coach). Sem senha no config → lê a do próprio OBS."""
        if self._obs or not self.cfg["obs"]["ativado"] or time.monotonic() - self._obs_tentativa < 15:
            return
        self._obs_tentativa = time.monotonic()
        o = self.cfg["obs"]
        senha = o["senha"]
        if not senha:
            try:
                arq = Path(os.environ["APPDATA"]) / "obs-studio/plugin_config/obs-websocket/config.json"
                senha = json.loads(arq.read_text(encoding="utf-8")).get("server_password", "")
            except (OSError, ValueError, KeyError):
                pass
        try:
            import obsws_python as obs
            self._obs = obs.ReqClient(host=o["host"], port=o["porta"], password=senha, timeout=2)
            self._pulados = None
            log.info("conectado no OBS")
        except Exception:
            self._obs = None

    def ia_trabalhando(self, ativa):
        """O coach avisa quando chama a IA: esse pico de ~1 s não é o jogo sofrendo e não deve pausar nada."""
        self._ia_ate = float("inf") if ativa else time.monotonic() + 4

    def _medir(self):
        if self._gpu:
            uso = self._nv.nvmlDeviceGetUtilizationRates(self._gpu).gpu
            mem = self._nv.nvmlDeviceGetMemoryInfo(self._gpu)
            livre_mb = mem.free / 2**20
            if uso > self.cfg["gpu_max"] and time.monotonic() >= self._ia_ate:
                return f"GPU em {uso}%"
            if livre_mb < self.cfg["vram_livre_min"]:
                return f"VRAM livre {livre_mb:.0f} MB"
        if self._obs:
            try:
                s = self._obs.get_stats()
                pulados = s.output_skipped_frames + s.render_skipped_frames
                novos = 0 if self._pulados is None else pulados - self._pulados
                self._pulados = pulados
                if novos > 0:
                    return f"OBS pulou {novos} frames"
            except Exception:
                self._obs = None   # OBS fechou; reconecta depois
        return ""

    def _loop(self):
        while True:
            self._conectar_obs()
            motivo = self._medir()
            agora = time.monotonic()
            if motivo:
                if self.livre:
                    log.warning("IA pausada: %s", motivo)
                self.livre, self.motivo, self._calmo_desde = False, motivo, agora
            elif not self.livre and agora - self._calmo_desde > self.cfg["calmo_por"]:
                self.livre, self.motivo = True, ""
                log.info("IA liberada de novo")
            time.sleep(2)
