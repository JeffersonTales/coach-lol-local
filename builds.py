"""
Suas builds para o coach
========================
  python builds.py meus   → cria/atualiza builds.yaml com os campeões que você mais joga e os itens que você
                            mais fecha com eles (lido do cliente do LoL aberto; só leitura, igual Porofessor/Blitz).
                            Campeões que já estão no arquivo NÃO são alterados: suas edições ficam.
O meta do patch vem do lolalytics (lolalytics.py), baixado sozinho pelo coach quando a partida começa.
"""
import base64
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import requests
import urllib3
import yaml

from conhecimento import ARQ_BUILDS, Conhecimento

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
CABECALHO = """# Suas builds — o coach sugere o PRÓXIMO item desta lista que você ainda não tem.
# Escreva os nomes como aparecem no jogo, na ordem em que você compra. Botas não precisam entrar.
# Gerado de início pelas suas partidas (python builds.py meus); edite à vontade — o script não sobrescreve.
"""


def cliente_lol():
    import psutil
    for p in psutil.process_iter(["name", "cmdline"]):
        if p.info["name"] == "LeagueClientUx.exe":
            linha = " ".join(p.info["cmdline"] or [])
            porta = re.search(r"--app-port=(\d+)", linha)
            token = re.search(r"--remoting-auth-token=([\w-]+)", linha)
            if porta and token:
                auth = base64.b64encode(f"riot:{token.group(1)}".encode()).decode()
                return f"https://127.0.0.1:{porta.group(1)}", {"Authorization": f"Basic {auth}"}
    sys.exit("Abra o cliente do LoL (não precisa estar em partida) e rode de novo.")


def meus(saber, quantos_campeoes=8):
    url, cab = cliente_lol()
    jogos = []
    for inicio in range(0, 200, 50):   # até 200 partidas
        r = requests.get(f"{url}/lol-match-history/v1/products/lol/current-summoner/matches",
                         params={"begIndex": inicio, "endIndex": inicio + 49}, headers=cab, verify=False, timeout=10)
        lote = r.json().get("games", {}).get("games", [])
        jogos += lote
        if len(lote) < 50:
            break
    por_id = {c["key"]: c for c in saber.campeoes.values()}
    partidas, itens = Counter(), defaultdict(Counter)
    for j in jogos:
        if j.get("gameMode") != "CLASSIC":       # só Summoner's Rift (sem ARAM, Arena, Treino)
            continue
        eu = j["participants"][0]
        campeao = por_id.get(str(eu["championId"]))
        if not campeao:
            continue
        partidas[campeao["nome"]] += 1
        for k in range(6):
            it = saber.itens.get(str(eu["stats"].get(f"item{k}", 0)))
            if it and it["lendario"]:
                itens[campeao["nome"]][it["nome"]] += 1
    atuais = yaml.safe_load(ARQ_BUILDS.read_text(encoding="utf-8")) if ARQ_BUILDS.exists() else None
    atuais = atuais or {}
    novos = 0
    for nome, n in partidas.most_common(quantos_campeoes):
        if nome in atuais or not itens[nome]:
            continue
        atuais[nome] = [i for i, _ in itens[nome].most_common(6)]
        novos += 1
        print(f"{nome} ({n} partidas no SR): {', '.join(atuais[nome])}")
    ARQ_BUILDS.write_text(CABECALHO + "\n" + yaml.safe_dump(atuais, allow_unicode=True, sort_keys=False),
                          encoding="utf-8")
    print(f"\n{novos} campeão(ões) novo(s) em {ARQ_BUILDS}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "meus":
        saber = Conhecimento()
        saber.carregar()
        meus(saber)
    else:
        print(__doc__)
