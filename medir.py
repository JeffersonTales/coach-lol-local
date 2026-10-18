"""
Medidor para o teste: a cada 2 s anota GPU, VRAM, estado do coach e estatísticas do OBS em medicao.csv.
Rodar: python medir.py [minutos]   (padrão 45)
"""
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import obsws_python as obs
import pynvml
import requests

import live_client

PASTA = Path(__file__).resolve().parent
logging.getLogger("obsws_python").setLevel(logging.CRITICAL)   # OBS fechado não é erro


def conectar_obs():
    try:
        arq = Path(os.environ["APPDATA"]) / "obs-studio/plugin_config/obs-websocket/config.json"
        senha = json.loads(arq.read_text(encoding="utf-8")).get("server_password", "")
        return obs.ReqClient(host="127.0.0.1", port=4455, password=senha, timeout=2)
    except Exception:
        return None


def main():
    minutos = float(sys.argv[1]) if len(sys.argv) > 1 else 45
    pynvml.nvmlInit()
    gpu = pynvml.nvmlDeviceGetHandleByIndex(0)
    cliente = None
    fim = time.time() + minutos * 60
    with open(PASTA / "medicao.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["hora", "gpu_%", "vram_mb", "coach", "tempo_jogo", "obs_fps", "obs_render_ms",
                    "obs_render_pulados", "obs_saida_pulados", "gravando", "ao_vivo"])
        while time.time() < fim:
            uso = pynvml.nvmlDeviceGetUtilizationRates(gpu).gpu
            vram = pynvml.nvmlDeviceGetMemoryInfo(gpu).used // 2**20
            try:
                coach = requests.get("http://127.0.0.1:8767/status", timeout=1).json()["rodando"]
            except Exception:
                coach = None
            partida = live_client.ler_partida()
            t_jogo = round(partida["gameData"]["gameTime"]) if partida else ""
            linha = [""] * 6
            cliente = cliente or conectar_obs()
            if cliente:
                try:
                    s = cliente.get_stats()
                    rec = cliente.get_record_status().output_active
                    live = cliente.get_stream_status().output_active
                    linha = [round(s.active_fps, 1), round(s.average_frame_render_time, 2),
                             s.render_skipped_frames, s.output_skipped_frames, rec, live]
                except Exception:
                    cliente = None
            w.writerow([datetime.now().strftime("%H:%M:%S"), uso, vram, coach, t_jogo, *linha])
            f.flush()
            time.sleep(2)


if __name__ == "__main__":
    main()
