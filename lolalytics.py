"""
Meta do patch pelo lolalytics.com (página de build do campeão, em português).
Não é API oficial: por educação, baixa UMA página por campeão/rota por patch e guarda em dados\\<patch>\\.
Se o site mudar o layout e a leitura falhar, devolve None e o coach segue sem o meta.
"""
import json
import logging
import re

import requests

log = logging.getLogger("coach")

URL = "https://lolalytics.com/pt_br/lol/{slug}/build/"
SLUG_ESPECIAL = {"MonkeyKing": "wukong"}
ROTAS = {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "middle", "BOTTOM": "bottom", "UTILITY": "support"}
SECOES = ("Core Build", "Item 4", "Item 5", "Item 6")

RE_ITEM = re.compile(r'item64/(\d+)\.webp"[^>]*alt="([^"]+)"')
RE_NUMEROS = re.compile(r'>([\d.]+)%</span><br><span[^>]*>([\d,]+)</span>')


def _ler_secao(html):
    """[(id, nome, vitória %, partidas)] — cada item seguido dos seus números, quando existem."""
    itens = list(RE_ITEM.finditer(html))
    saida = []
    for i, m in enumerate(itens):
        fim = itens[i + 1].start() if i + 1 < len(itens) else len(html)
        n = RE_NUMEROS.search(html, m.end(), fim)
        saida.append((m.group(1), m.group(2), float(n.group(1)) if n else None,
                      int(n.group(2).replace(",", "")) if n else None))
    return saida


def analisar(html):
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)   # o site embrulha os textos em comentários (Qwik)
    patch = re.search(r"Patch\s*([\d.]+)", html)
    total = re.search(r"([\d,]+) Games</span>", html)
    posicoes = [(s, m.start()) for s in SECOES for m in [re.search(rf">{s}</div>", html)] if m]
    if not posicoes or posicoes[0][0] != "Core Build":
        return None
    posicoes.sort(key=lambda x: x[1])
    secoes = {}
    for i, (nome, ini) in enumerate(posicoes):
        fim = posicoes[i + 1][1] if i + 1 < len(posicoes) else ini + 20000
        secoes[nome] = _ler_secao(html[ini:fim])
    core = secoes["Core Build"]
    vit = next((v for _, _, v, _ in core if v), None)
    jogos = next((j for _, _, _, j in core if j), None)
    return {
        "patch": patch.group(1) if patch else "?",
        "partidas": int(total.group(1).replace(",", "")) if total else None,
        "core": {"itens": [(i, n) for i, n, _, _ in core][:3], "vitoria": vit, "partidas": jogos},
        "opcoes": {s: [{"id": i, "nome": n, "vitoria": v, "partidas": j} for i, n, v, j in secoes.get(s, [])[:4]]
                   for s in SECOES[1:] if s in secoes},
    }


def build_meta(id_campeao, rota, pasta_patch):
    """Build do meta para o campeão (id do Data Dragon, ex.: MasterYi) na rota (TOP/JUNGLE/...)."""
    slug = SLUG_ESPECIAL.get(id_campeao, id_campeao.lower())
    lane = ROTAS.get((rota or "").upper())
    cache = pasta_patch / f"lolalytics_{slug}_{lane or 'padrao'}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8")) or None
    try:
        r = requests.get(URL.format(slug=slug), params={"lane": lane} if lane else None, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        r.raise_for_status()
        dados = analisar(r.text)
    except Exception as erro:
        log.warning("lolalytics indisponível para %s (%s)", slug, erro)
        return None
    pasta_patch.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")   # null = página sem build
    if not dados:
        log.warning("lolalytics: não entendi a página de %s (o site mudou o layout?)", slug)
    return dados
