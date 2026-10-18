"""
Controle do coach para o Stream Deck do OBS
===========================================
Serviço pequeno e escondido (inicia com o Windows, igual ao banner do Spotify) que liga e desliga o coach.
http://127.0.0.1:8767/status    -> {"rodando": bool, "dica": "..."}
POST /ligar  |  POST /desligar  (desligar também tira a IA da placa de vídeo)

Só aceita pedidos do próprio deck (página file:// com o cabeçalho X-Deck); outros sites são recusados.
Rodar: .venv\Scripts\pythonw.exe controle.py
"""
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
import yaml

PASTA = Path(__file__).resolve().parent
PORTA = 8767
PYTHON = PASTA / ".venv" / "Scripts" / "python.exe"
SEM_JANELA = 0x08000000   # CREATE_NO_WINDOW

coach = None


def rodando():
    return coach is not None and coach.poll() is None


def ligar():
    global coach
    if not rodando():
        (PASTA / "dica_atual.txt").write_text("", encoding="utf-8")
        coach = subprocess.Popen([str(PYTHON), "coach.py"], cwd=PASTA, creationflags=SEM_JANELA,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def desligar():
    global coach
    if rodando():
        coach.terminate()
        try:
            coach.wait(timeout=5)
        except subprocess.TimeoutExpired:
            coach.kill()
    coach = None
    (PASTA / "dica_atual.txt").write_text("", encoding="utf-8")
    try:  # libera a VRAM na hora, em vez de esperar o keep_alive
        cfg = yaml.safe_load((PASTA / "config.yaml").read_text(encoding="utf-8"))["llm"]
        requests.post(cfg["url"].rstrip("/") + "/api/generate", json={"model": cfg["modelo"], "keep_alive": 0}, timeout=5)
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    def _autorizado(self):
        # o deck é aberto como file:///, cuja origem é "null"; qualquer site da internet tem outra origem
        return self.headers.get("Origin") in (None, "null")

    def _responder(self, codigo, corpo=None):
        dados = json.dumps(corpo or {}, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        if self.headers.get("Origin") == "null":
            self.send_header("Access-Control-Allow-Origin", "null")
            self.send_header("Access-Control-Allow-Headers", "X-Deck")
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def _status(self):
        try:
            dica = (PASTA / "dica_atual.txt").read_text(encoding="utf-8").strip()
        except OSError:
            dica = ""
        return {"rodando": rodando(), "dica": dica if rodando() else ""}

    def do_OPTIONS(self):
        self._responder(204 if self._autorizado() else 403)

    def do_GET(self):
        if self.path != "/status" or not self._autorizado():
            return self._responder(404)
        self._responder(200, self._status())

    def do_POST(self):
        if not self._autorizado() or self.headers.get("X-Deck") != "1":
            return self._responder(403)
        if self.path == "/ligar":
            ligar()
        elif self.path == "/desligar":
            desligar()
        else:
            return self._responder(404)
        self._responder(200, self._status())

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    try:
        servidor = ThreadingHTTPServer(("127.0.0.1", PORTA), Handler)
    except OSError:
        sys.exit(0)   # já tem um controle rodando
    try:
        servidor.serve_forever()
    finally:
        desligar()
