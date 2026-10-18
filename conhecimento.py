"""
Conhecimento oficial do LoL — Data Dragon da Riot (itens e campeões em português, sempre do patch atual).
O modelo pequeno não sabe o que os itens fazem; aqui a gente entrega os dados reais junto com a pergunta.
Os arquivos ficam em cache na pasta "dados" e só são baixados de novo quando sai um patch.
"""
import json
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path

import requests
import yaml

from live_client import meu_jogador, rota
from lolalytics import build_meta
from ouvido import normalizar

log = logging.getLogger("coach")

DDRAGON = "https://ddragon.leagueoflegends.com"
PASTA = Path(__file__).resolve().parent / "dados"
ARQ_BUILDS = Path(__file__).resolve().parent / "builds.yaml"   # suas builds (python builds.py meus)

# tipo do campeão (Data Dragon) → tags de item que combinam com ele
TAGS_POR_CLASSE = {
    "Marksman": {"CriticalStrike", "AttackSpeed", "Damage", "LifeSteal"},
    "Fighter": {"Damage", "Health", "AbilityHaste", "LifeSteal"},
    "Assassin": {"Damage", "ArmorPenetration", "AbilityHaste", "OnHit"},
    "Mage": {"SpellDamage", "MagicPenetration", "Mana", "AbilityHaste"},
    "Tank": {"Health", "Armor", "SpellBlock", "AbilityHaste"},
    "Support": {"Aura", "ManaRegen", "AbilityHaste", "Health"},
}


def limpar_html(texto):
    texto = re.sub(r"<br\s*/?>", "; ", texto or "")
    texto = re.sub(r"<[^>]+>", "", texto)
    texto = re.sub(r"(;\s*)+", "; ", texto)
    return " ".join(texto.split()).strip("; ")


class Conhecimento:
    def __init__(self):
        self.versao = None
        self.itens = {}         # id -> dict
        self.campeoes = {}      # nome exibido -> dict (resumo do champion.json)
        self._detalhes = {}     # id do campeão -> texto das habilidades

    # ---------------- carregar / baixar ----------------
    def _baixar(self, caminho, arquivo):
        destino = PASTA / self.versao / arquivo
        if not destino.exists():
            destino.parent.mkdir(parents=True, exist_ok=True)
            r = requests.get(f"{DDRAGON}/cdn/{self.versao}/data/pt_BR/{caminho}", timeout=20)
            r.raise_for_status()
            destino.write_bytes(r.content)
        return json.loads(destino.read_text(encoding="utf-8"))

    def carregar(self):
        try:
            self.versao = requests.get(f"{DDRAGON}/api/versions.json", timeout=10).json()[0]
        except Exception:   # sem internet: usa o último patch que já está no cache
            baixados = sorted((p.name for p in PASTA.glob("*") if p.is_dir()), key=lambda v: [int(x) for x in v.split(".")])
            if not baixados:
                log.warning("sem internet e sem dados em cache: coach sem conhecimento de itens")
                return
            self.versao = baixados[-1]
        itens = self._baixar("item.json", "item.json")["data"]
        for id_, it in itens.items():
            if not it.get("maps", {}).get("11") or not it.get("gold", {}).get("purchasable") or it.get("inStore") is False:
                continue   # só itens compráveis no Summoner's Rift (tira as versões de Arena e afins)
            self.itens[id_] = {
                "id": id_, "nome": it["name"], "ouro": it["gold"]["total"], "tags": set(it.get("tags", [])),
                "texto": limpar_html(it.get("description")), "resumo": it.get("plaintext", ""),
                "lendario": it["gold"]["total"] >= 2200 and not it.get("into")
                            and not {"Consumable", "Trinket", "Boots"} & set(it.get("tags", [])),
                "chave": normalizar(it["name"]),
                "componentes": set(it.get("from", [])),   # ids dos itens que montam este
            }
        for c in self._baixar("champion.json", "champion.json")["data"].values():
            self.campeoes[c["name"]] = {"id": c["id"], "key": c["key"], "nome": c["name"], "titulo": c["title"],
                                        "tags": c["tags"],
                                        "info": c["info"], "chave": normalizar(c["name"])}
        log.info("conhecimento do patch %s: %d itens, %d campeões", self.versao, len(self.itens), len(self.campeoes))

    # ---------------- achar nomes na fala ----------------
    @staticmethod
    def _aparece(chave, palavras, limite=0.85):
        n = len(chave)
        alvo = " ".join(chave)
        for i in range(max(1, len(palavras) - n + 1)):
            janela = " ".join(palavras[i:i + n])
            if janela == alvo or (len(alvo) > 4 and SequenceMatcher(None, janela, alvo).ratio() >= limite):
                return True
        return False

    def achar_itens(self, texto):
        palavras = normalizar(texto)
        achados = {}
        for it in self.itens.values():
            if self._aparece(it["chave"], palavras):
                achados.setdefault(it["nome"], it)   # nomes repetidos: fica o primeiro (são o mesmo item)
        # "Dança da Morte" contém "Morte": se achou um nome maior, tira os menores contidos nele
        nomes = list(achados)
        return [achados[n] for n in nomes if not any(n != m and n.lower() in m.lower() for m in nomes)]

    def achar_campeoes(self, texto):
        palavras = normalizar(texto)
        # "eu vi" não é a campeã Vi e "dança" não é a Diana: nomes de até 6 letras só valem exatos
        achados = []
        for c in self.campeoes.values():
            nome = " ".join(c["chave"])
            if len(nome) <= 3:
                continue
            if self._aparece(c["chave"], palavras, 1.01 if len(nome) <= 6 else 0.85):
                achados.append(c)
        return achados

    # ---------------- textos para a IA ----------------
    def descrever_item(self, it):
        return f"{it['nome']} ({it['ouro']} de ouro): {it['texto']}"

    def descrever_campeao(self, c):
        if c["id"] not in self._detalhes:
            try:
                d = self._baixar(f"champion/{c['id']}.json", f"champion_{c['id']}.json")["data"][c["id"]]
                partes = [f"Passiva {d['passive']['name']}: {limpar_html(d['passive']['description'])[:220]}"]
                for tecla, s in zip("QWER", d["spells"]):
                    partes.append(f"{tecla} {s['name']}: {limpar_html(s['description'])[:220]}")
                self._detalhes[c["id"]] = f"{c['nome']}, {c['titulo']} ({'/'.join(c['tags'])}). " + " | ".join(partes)
            except Exception:
                self._detalhes[c["id"]] = f"{c['nome']}, {c['titulo']} ({'/'.join(c['tags'])})."
        return self._detalhes[c["id"]]

    # ---------------- builds: a sua (builds.yaml) e o meta (lolalytics) ----------------
    def _minha_rota(self, eu):
        return rota(eu) or None

    def meta_para(self, dados):
        """Build do meta para o seu campeão nesta partida (baixa 1x por patch e guarda; None se não tiver)."""
        eu = meu_jogador(dados) if dados else None
        if not eu or eu["championName"] not in self.campeoes or not self.versao:
            return None
        return build_meta(self.campeoes[eu["championName"]]["id"], self._minha_rota(eu), PASTA / self.versao)

    def builds(self, dados):
        """Texto com a sua build e a do meta, a partir do que você já tem. Vazio se não houver nenhuma."""
        eu = meu_jogador(dados)
        if not eu:
            return ""
        tenho = {i.get("displayName") for i in eu.get("items", [])}
        lendarios = sum(1 for it in self.itens.values() if it["nome"] in tenho and it["lendario"])
        linhas = []
        try:
            minhas = (yaml.safe_load(ARQ_BUILDS.read_text(encoding="utf-8")) or {}) if ARQ_BUILDS.exists() else {}
        except (OSError, yaml.YAMLError):
            minhas = {}
        faltam = [n for n in minhas.get(eu["championName"]) or [] if n not in tenho]
        if faltam:
            linhas.append(f"A build que você costuma fazer — próximo: {faltam[0]}"
                          + (f"; depois: {', '.join(faltam[1:3])}" if len(faltam) > 1 else ""))
        meta = self.meta_para(dados)
        if meta:
            core = [n for _, n in meta["core"]["itens"] if n not in tenho]
            fonte = f"Meta do patch {meta['patch']} (lolalytics)"
            if core and lendarios < 3:
                linhas.append(f"{fonte} — build principal, faltam: {', '.join(core)} "
                              f"({meta['core']['vitoria']}% de vitória em {meta['core']['partidas']} partidas)")
            else:
                vaga = f"Item {min(lendarios + 1, 6)}"
                opcoes = [o for o in meta["opcoes"].get(vaga, []) if o["nome"] not in tenho]
                if opcoes:
                    linhas.append(f"{fonte} — opções para o {vaga.lower()}: " + "; ".join(
                        f"{o['nome']} ({o['vitoria']}% vit., {o['partidas']} partidas)" for o in opcoes))
        nomes = set(faltam[:1])
        if meta:
            nomes |= {n for _, n in meta["core"]["itens"]}
        detalhes = [self.descrever_item(it)[:220] for it in self.itens.values() if it["nome"] in nomes]
        if linhas and detalhes:
            linhas.append("O que esses itens fazem:\n" + "\n".join(f"- {d}" for d in detalhes[:4]))
        return "\n".join(linhas)

    def defesa(self, dados):
        eu = meu_jogador(dados)
        fisico = magico = 0
        for p in dados["allPlayers"]:
            c = self.campeoes.get(p["championName"])
            if eu and p.get("team") != eu.get("team") and c:
                fisico += c["info"]["attack"]
                magico += c["info"]["magic"]
        if magico > fisico * 1.3:
            return "O time inimigo causa mais dano mágico: se estiver morrendo muito, vale um item com Resistência Mágica."
        if fisico > magico * 1.3:
            return "O time inimigo causa mais dano físico: se estiver morrendo muito, vale um item com Armadura."
        return ""

    def candidatos(self, dados, quantos=7):
        """Itens lendários que combinam com o seu campeão + defesa contra o tipo de dano do time inimigo.
        Só é usado quando não há build sua nem do meta para o campeão."""
        eu = meu_jogador(dados)
        if not eu or eu["championName"] not in self.campeoes:
            return ""
        meu = self.campeoes[eu["championName"]]
        desejo = set().union(*(TAGS_POR_CLASSE.get(t, set()) for t in meu["tags"]))
        fisico = magico = 0
        for p in dados["allPlayers"]:
            c = self.campeoes.get(p["championName"])
            if p.get("team") != eu.get("team") and c:
                fisico += c["info"]["attack"]
                magico += c["info"]["magic"]
        defesa = "SpellBlock" if magico > fisico * 1.3 else "Armor" if fisico > magico * 1.3 else None
        tenho = {i.get("displayName") for i in eu.get("items", [])}
        # os itens grandes que você já comprou dizem qual é a sua build (ex.: Yi de ataque rápido/on-hit);
        # cada tag deles pesa o dobro da classe do campeão
        peso = dict.fromkeys(desejo, 1)
        for it in self.itens.values():
            if it["nome"] in tenho and it["ouro"] >= 2200:
                for tag in it["tags"] - {"Boots", "Consumable", "Trinket"}:
                    peso[tag] = peso.get(tag, 0) + 2
        livres = [it for it in self.itens.values() if it["lendario"] and it["nome"] not in tenho]
        # campeão de dano físico não leva item de Poder de Habilidade (e o mago não leva item de Dano de Ataque)
        mago = "Mage" in meu["tags"] or meu["info"]["magic"] > meu["info"]["attack"]
        livres = [it for it in livres if ("Damage" not in it["tags"] or "SpellDamage" in it["tags"]) == mago
                  or not ({"Damage", "SpellDamage"} & it["tags"])]
        # componentes na mochila (ex.: Cetro Vampírico + Adaga) apontam para o item que você está montando
        meus_ids = {str(i.get("itemID")) for i in eu.get("items", [])}

        def nota(it):
            return sum(peso.get(t, 0) for t in it["tags"]) + 4 * len(it["componentes"] & meus_ids)

        ofensivos = sorted(livres, key=lambda it: -nota(it))[:quantos]
        linhas = [f"Itens que combinam com {meu['nome']} ({'/'.join(meu['tags'])}):"]
        linhas += [f"- {self.descrever_item(it)[:260]}" for it in ofensivos]
        if defesa:
            tipo = "mágico" if defesa == "SpellBlock" else "físico"
            defensivos = [it for it in livres if defesa in it["tags"] and it["tags"] & desejo][:2] or \
                         [it for it in livres if defesa in it["tags"]][:2]
            linhas.append(f"O time inimigo causa mais dano {tipo}. Opções defensivas:")
            linhas += [f"- {self.descrever_item(it)[:260]}" for it in defensivos]
        return "\n".join(linhas)

    def contexto_para(self, fala, dados, ultima_resposta=""):
        """Bloco de dados oficiais relevante para a fala do streamer (vazio se nada se aplica)."""
        if not self.itens:
            return ""
        blocos = []
        itens = self.achar_itens(fala)
        normal = " ".join(normalizar(fala))
        retomando = any(p in normal for p in ("por que", "porque", "esse item", "esse ai", "isso ai", "ele faz"))
        if retomando and ultima_resposta:
            itens += [it for it in self.achar_itens(ultima_resposta) if it not in itens]
        if itens:
            blocos.append("Itens citados:\n" + "\n".join(f"- {self.descrever_item(it)}" for it in itens[:3]))
        campeoes = self.achar_campeoes(fala)
        if campeoes:
            blocos.append("Campeões citados:\n" + "\n".join(f"- {self.descrever_campeao(c)}" for c in campeoes[:2]))
        if dados and any(p in normal for p in ("item", "build", "compr", "faco agora", "fazer agora")):
            builds = self.builds(dados)
            blocos.append("\n".join(b for b in (builds, self.defesa(dados)) if b) if builds
                          else self.candidatos(dados))
        return "\n\n".join(b for b in blocos if b)

    def vocabulario(self, dados):
        """Nomes que o ouvido deve esperar ouvir: seus itens e os campeões da partida."""
        if not dados:
            return ""
        eu = meu_jogador(dados) or {}
        nomes = [i.get("displayName", "") for i in eu.get("items", [])]
        nomes += [p["championName"] for p in dados.get("allPlayers", [])]
        return ", ".join(n for n in nomes if n)
