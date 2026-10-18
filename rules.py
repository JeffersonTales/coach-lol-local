"""
Regras de macro — custo praticamente zero, rodam a cada leitura da API.
Só usam informação que o jogador vê na própria tela (placar, avisos globais de objetivo, seu ouro).
Cada regra devolve Dica(texto, prioridade); a prioridade maior fura a fila.
"""
from dataclasses import dataclass, field

from live_client import meu_jogador, nomes, rota

RESPOSTA, URGENTE, NORMAL, BAIXA = 4, 3, 2, 1
ROTA_PT = {"TOP": "topo", "JUNGLE": "caçador/jungle", "MIDDLE": "meio", "BOTTOM": "atirador/bot",
           "UTILITY": "suporte"}


@dataclass
class Dica:
    texto: str
    prioridade: int = NORMAL
    pedido_llm: str | None = None   # se a IA estiver ligada, ela reescreve/explica com esse contexto
    chave: str | None = None        # tipo da dica; a mais nova do mesmo tipo substitui a antiga na fila


@dataclass
class Estado:
    ultimo_evento: int = -1
    ultima: dict = field(default_factory=dict)      # nome da regra -> tempo de jogo da última fala
    proximo_dragao: float | None = None
    proximo_barao: float | None = None
    arauto: float | None = None
    avisados: set = field(default_factory=set)
    minhas_mortes: int = 0


class Regras:
    def __init__(self, cfg):
        self.cfg = cfg["regras"]
        self.jog = cfg["jogador"]
        self.e = Estado()

    def nova_partida(self):
        self.e = Estado(
            proximo_dragao=self.cfg["dragao_primeiro"],
            proximo_barao=self.cfg["barao_primeiro"],
            arauto=self.cfg["arauto_primeiro"],
        )

    def _pode(self, regra, t, intervalo):
        if t - self.e.ultima.get(regra, -1e9) < intervalo:
            return False
        self.e.ultima[regra] = t
        return True

    def avaliar(self, dados):
        t = dados["gameData"]["gameTime"]
        eu = meu_jogador(dados)
        dicas = []
        dicas += self._eventos(dados, eu, t)
        dicas += self._objetivos(t)
        if eu:
            dicas += self._ouro(dados, eu, t)
            dicas += self._farm(eu, t)
            dicas += self._nivel(dados, eu, t)
        return dicas

    # ---------------- eventos globais (os mesmos avisos que aparecem no chat do jogo) ----------------
    def _eventos(self, dados, eu, t):
        dicas = []
        eventos = dados.get("events", {}).get("Events", [])
        meus_nomes = nomes(eu) if eu else set()
        for ev in eventos:
            if ev["EventID"] <= self.e.ultimo_evento:
                continue
            self.e.ultimo_evento = ev["EventID"]
            nome, quando = ev["EventName"], ev["EventTime"]
            if t - quando > 10:          # evento antigo (abriu o coach no meio da partida)
                self._so_registrar(nome, quando)
                continue
            if nome == "DragonKill":
                self.e.proximo_dragao = quando + self.cfg["dragao_renasce"]
                self._limpar_aviso("dragao")
            elif nome == "BaronKill":
                self.e.proximo_barao = quando + self.cfg["barao_renasce"]
                self._limpar_aviso("barao")
                dicas.append(Dica("Barão caiu. Com o buff, agrupem e empurrem uma rota.", URGENTE))
            elif nome == "HeraldKill":
                self.e.arauto = None
            elif nome == "Ace":
                dicas.append(Dica("Ace! Hora de pegar objetivo ou derrubar torre.", URGENTE))
            elif nome == "ChampionKill" and ev.get("VictimName") in meus_nomes:
                self.e.minhas_mortes += 1
                matador = ev.get("KillerName", "?")
                dicas.append(Dica(
                    "Você morreu. Pensa: tinha visão e sabia onde estava o caçador?",
                    NORMAL,
                    pedido_llm=f"O jogador acabou de morrer para {matador} aos {int(t // 60)} minutos "
                               f"(morte número {self.e.minhas_mortes}). Dê uma dica curta e construtiva.",
                ))
            elif nome == "FirstBrick":
                dicas.append(Dica("Primeira torre caiu. Olhem o mapa e rotacionem.", BAIXA))
        return dicas

    def _so_registrar(self, nome, quando):
        if nome == "DragonKill":
            self.e.proximo_dragao = quando + self.cfg["dragao_renasce"]
        elif nome == "BaronKill":
            self.e.proximo_barao = quando + self.cfg["barao_renasce"]
        elif nome == "HeraldKill":
            self.e.arauto = None

    def _limpar_aviso(self, chave):
        self.e.avisados = {a for a in self.e.avisados if not a.startswith(chave)}

    # ---------------- objetivos nascendo ----------------
    def _objetivos(self, t):
        dicas = []
        antes = self.cfg["aviso_objetivo"]
        for chave, quando, frase in (
            ("dragao", self.e.proximo_dragao, "Dragão nasce em um minuto. Preparem visão e empurrem a rota."),
            ("barao", self.e.proximo_barao, "Barão nasce em um minuto. Controlem a visão do rio de cima."),
            ("arauto", self.e.arauto, "Arauto nasce em um minuto."),
        ):
            if quando is None:
                continue
            marca = f"{chave}:{int(quando)}"
            if quando - antes <= t < quando and marca not in self.e.avisados:
                self.e.avisados.add(marca)
                dicas.append(Dica(frase, URGENTE))
        return dicas

    # ---------------- seu ouro, farm e nível ----------------
    def _ouro(self, dados, eu, t):
        ouro = dados["activePlayer"].get("currentGold", 0)
        if eu.get("isDead") or ouro < self.cfg["ouro_para_base"]:
            return []
        if not self._pode("ouro", t, self.cfg["intervalo_ouro"]):
            return []
        return [Dica(f"Você tem {int(ouro)} de ouro. Na próxima brecha, volta pra base.", NORMAL, chave="ouro")]

    def _rota(self, eu):
        return (self.jog.get("rota") or rota(eu)).upper()

    def _farm(self, eu, t):
        if t < 240 or self._rota(eu) in ("JUNGLE", "UTILITY"):
            return []
        cs_min = eu.get("scores", {}).get("creepScore", 0) / (t / 60)
        if cs_min >= self.jog["meta_cs_min"]:
            return []
        if not self._pode("farm", t, self.cfg["intervalo_cs"]):
            return []
        return [Dica(f"Seu farm está em {cs_min:.1f} por minuto. Capricha nas tropas.", BAIXA, chave="farm")]

    def _nivel(self, dados, eu, t):
        minha, rota_de = self._rota(eu), rota
        if not minha:
            return []
        rival = next((p for p in dados.get("allPlayers", [])
                      if p.get("team") != eu.get("team") and rota_de(p) == minha), None)
        if not rival:
            return []
        dif = rival.get("level", 0) - eu.get("level", 0)
        if dif < self.cfg["diferenca_nivel"] or not self._pode("nivel", t, 150):
            return []
        return [Dica(f"{rival['championName']} está {dif} níveis na sua frente. Joga mais seguro.", NORMAL,
                     chave="nivel")]


def resumo_partida(dados):
    """Texto compacto para a IA (poucos tokens = resposta rápida)."""
    t = dados["gameData"]["gameTime"]
    eu = meu_jogador(dados)
    linhas = [f"Tempo de jogo: {int(t // 60)}:{int(t % 60):02d}"]
    if eu:
        s = eu.get("scores", {})
        itens = ", ".join(i.get("displayName", "") for i in eu.get("items", [])) or "nenhum"
        linhas.append(f"Eu: {eu['championName']} ({ROTA_PT.get(rota(eu), 'rota ?')}), nível {eu.get('level')}, "
                      f"KDA {s.get('kills')}/{s.get('deaths')}/{s.get('assists')}, CS {s.get('creepScore')}, "
                      f"ouro {int(dados['activePlayer'].get('currentGold', 0))}, itens: {itens}")
        for aliado, rotulo in ((True, "Aliados"), (False, "Inimigos")):
            ps = [p for p in dados["allPlayers"] if (p.get("team") == eu.get("team")) == aliado and p is not eu]
            linhas.append(rotulo + ": " + "; ".join(
                f"{p['championName']} ({ROTA_PT.get(rota(p), '?')}) nv{p.get('level')} "
                f"{p['scores'].get('kills')}/{p['scores'].get('deaths')}"
                f"/{p['scores'].get('assists')}"
                # itens dos inimigos aparecem no placar (TAB), então é informação que o jogador já tem
                + ("" if aliado else " [" + ", ".join(i.get("displayName", "") for i in p.get("items", [])) + "]")
                for p in ps))
    return "\n".join(linhas)
