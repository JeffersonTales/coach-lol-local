"""Leitura da Live Client Data API — a API oficial que o próprio LoL abre no seu PC durante a partida."""
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # certificado autoassinado da Riot

URL = "https://127.0.0.1:2999/liveclientdata/allgamedata"

_sessao = requests.Session()
_sessao.verify = False


def ler_partida():
    """Devolve o JSON da partida, ou None se não houver partida carregada."""
    try:
        r = _sessao.get(URL, timeout=1.5)
        if r.status_code != 200:
            return None
        dados = r.json()
        if "activePlayer" not in dados or "gameData" not in dados:
            return None
        return dados
    except (requests.RequestException, ValueError):
        return None


def meu_jogador(dados):
    """Acha o activePlayer dentro de allPlayers (é onde ficam KDA, CS, itens e rota)."""
    ativo = dados["activePlayer"]
    ids = {ativo.get("riotId"), ativo.get("summonerName"), ativo.get("riotIdGameName")} - {None, ""}
    for p in dados.get("allPlayers", []):
        if {p.get("riotId"), p.get("summonerName"), p.get("riotIdGameName")} & ids:
            return p
    return None


def rota(p):
    """Rota do jogador. No Treino/Personalizada a API não informa: quem leva Golpear (Smite) é o caçador."""
    posicao = (p.get("position") or "").upper()
    if posicao and posicao != "NONE":       # no Treino a API manda o texto "NONE"
        return posicao
    feiticos = p.get("summonerSpells", {})
    if any("SummonerSmite" in (f or {}).get("rawDisplayName", "") for f in feiticos.values()):
        return "JUNGLE"
    return ""


def nomes(p):
    return {p.get("riotId"), p.get("summonerName"), p.get("riotIdGameName"), p.get("championName")} - {None, ""}
