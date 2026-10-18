"""
Olhos do coach — print só do minimapa, tirado da própria "Captura de jogo" do OBS.
Pegar a imagem pelo OBS evita capturar a tela de novo (o OBS já faz isso para a live) e funciona com o jogo
em tela cheia. O recorte é o canto inferior direito; ajuste visao.tamanho_px no config.yaml se o seu
minimapa for maior ou menor (ele depende da "Escala do minimapa" nas configurações do LoL).
"""
import base64
import io
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("coach")


class Visao:
    def __init__(self, cfg):
        self.cfg = cfg["visao"]
        self.obs_cfg = cfg["guarda"]["obs"]
        self._obs = None

    def _conectar(self):
        if self._obs:
            return self._obs
        senha = self.obs_cfg["senha"]
        if not senha:
            try:
                arq = Path(os.environ["APPDATA"]) / "obs-studio/plugin_config/obs-websocket/config.json"
                senha = json.loads(arq.read_text(encoding="utf-8")).get("server_password", "")
            except (OSError, ValueError, KeyError):
                pass
        import obsws_python as obs
        self._obs = obs.ReqClient(host=self.obs_cfg["host"], port=self.obs_cfg["porta"], password=senha, timeout=3)
        return self._obs

    def recorte(self):
        """Minimapa em tamanho real (imagem PIL), ou None se o OBS não deu a imagem ou o jogo está minimizado."""
        from PIL import Image, ImageStat
        try:
            r = self._conectar().get_source_screenshot(self.cfg["fonte_obs"], "png", 1920, 1080, -1)
        except Exception as erro:
            self._obs = None
            log.info("sem imagem do jogo pelo OBS (%s)", erro)
            return None
        bruto = base64.b64decode(r.image_data.split(",", 1)[1])
        tela = Image.open(io.BytesIO(bruto)).convert("RGB")
        lado = int(self.cfg["tamanho_px"] * tela.height / 1080)
        recorte = tela.crop((tela.width - lado, tela.height - lado, tela.width, tela.height))
        if max(ImageStat.Stat(recorte).mean) < 10:   # tudo preto: jogo minimizado (alt+tab)
            return None
        return recorte

    def coletar(self, pasta, tempo_jogo):
        """Guarda o minimapa em PNG para calibrar a leitura por cor (não usa IA nem placa de vídeo)."""
        recorte = self.recorte()
        if recorte:
            pasta.mkdir(parents=True, exist_ok=True)
            recorte.save(pasta / f"{int(tempo_jogo // 60):02d}m{int(tempo_jogo % 60):02d}s.png")

    def minimapa(self, salvar=None):
        """JPEG do minimapa em base64 (pronto para o Ollama), ou None se não houver imagem."""
        recorte = self.recorte()
        if not recorte:
            return None
        recorte = recorte.resize((self.cfg["lado_ia"], self.cfg["lado_ia"]))
        if salvar:
            recorte.save(salvar)
        saida = io.BytesIO()
        recorte.save(saida, "JPEG", quality=85)
        return base64.b64encode(saida.getvalue()).decode()
