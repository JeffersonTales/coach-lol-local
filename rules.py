"""
Regras de macro — custo praticamente zero, rodam a cada leitura da API.
Só usam informação que o jogador vê na própria tela (placar, avisos globais de objetivo, seu ouro).
Cada regra devolve Dica(texto, prioridade); a prioridade maior fura a fila.
"""
import random
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
    mortes_por: dict = field(default_factory=dict)   # campeão -> quantas vezes ele te matou
    meu_nivel: int = 0
    mortos_avisados: set = field(default_factory=set)   # inimigos mortos que já viraram aviso de janela
    meus_itens: set | None = None                    # ids dos itens grandes que você já tinha
    alertas_fortes: dict = field(default_factory=dict)   # campeão inimigo -> abates do último alerta


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
            dicas += self._janela(dados, eu, t)
            dicas += self._pico(eu)
            dicas += self._inimigo_forte(dados, eu, t)
            dicas += self._visao(eu, t)
            dicas += self._caranguejo(eu, t)
        return dicas

    # ---------------- dicas para quando você está vivo (tudo com dado real da API, sem IA) ----------------
    def _objetivo_livre(self, t):
        """O objetivo que está vivo no mapa agora (pelos eventos de abate que todo mundo vê)."""
        if t >= 1200 and self.e.proximo_barao is not None and self.e.proximo_barao <= t:
            return "o Barão"
        if self.e.proximo_dragao is not None and self.e.proximo_dragao <= t:
            return "o dragão"
        if self.e.arauto is not None and self.e.arauto <= t < 1185:
            return "o Arauto"
        return None

    def _janela(self, dados, eu, t):
        """Inimigos mortos = janela de tempo. É a dica mais valiosa que a API permite dar."""
        if eu.get("isDead"):
            return []
        mortos = [p for p in dados.get("allPlayers", [])
                  if p.get("team") != eu.get("team") and p.get("isDead") and p.get("respawnTimer", 0) >= 12]
        nomes_agora = {p["championName"] for p in mortos}
        self.e.mortos_avisados &= nomes_agora          # quem renasceu pode ser avisado de novo na próxima morte
        if not nomes_agora - self.e.mortos_avisados:   # nenhum morto novo desde o último aviso
            return []
        segundos = int(min(p.get("respawnTimer", 0) for p in mortos))
        nomes_mortos = " e ".join(p["championName"] for p in mortos[:2]) + (f" e mais {len(mortos) - 2}" if len(mortos) > 2 else "")
        alvo = self._objetivo_livre(t)
        cacador = next((p for p in mortos if rota(p) == "JUNGLE"), None)
        if len(mortos) >= 2:
            acao = f"Corre pro {alvo[2:]}!" if alvo else "Derruba uma torre!"
            self.e.mortos_avisados |= nomes_agora
            return [Dica(f"{nomes_mortos} mortos por {segundos} segundos. {acao}", URGENTE, chave="janela")]
        if cacador and self._rota(eu) == "JUNGLE":
            acao = f"Faz {alvo} ou invade a selva deles." if alvo else "Invade a selva deles sem medo."
            self.e.mortos_avisados |= nomes_agora
            return [Dica(f"Caçador deles morto por {segundos} segundos. {acao}", URGENTE, chave="janela")]
        if alvo and segundos >= 20:
            self.e.mortos_avisados |= nomes_agora
            return [Dica(f"{nomes_mortos} morto por {segundos} segundos e {alvo} está vivo. Dá pra pegar!",
                         NORMAL, chave="janela")]
        return []

    def _pico(self, eu):
        """Nível 6/11/16 e item grande fechado: momentos de ficar mais forte e procurar briga."""
        dicas = []
        nivel = eu.get("level", 0)
        if nivel != self.e.meu_nivel:
            if self.e.meu_nivel and nivel in (6, 11, 16):
                frase = {6: "Nível 6, ultimate liberada! Hora de procurar abate.",
                         11: "Nível 11, ultimate mais forte. Aproveita pra forçar luta.",
                         16: "Nível 16, ultimate no máximo. Você está no seu auge."}[nivel]
                dicas.append(Dica(frase, NORMAL, chave="pico"))
            self.e.meu_nivel = nivel
        grandes = {i.get("itemID"): i.get("displayName", "") for i in eu.get("items", []) if i.get("price", 0) >= 900}
        if self.e.meus_itens is not None:
            novos = [nome for iid, nome in grandes.items() if iid not in self.e.meus_itens]
            if novos:
                dicas.append(Dica(f"Fechou {novos[0]}! Você ficou bem mais forte, aproveita.", NORMAL, chave="pico"))
        self.e.meus_itens = set(grandes)
        return dicas

    def _inimigo_forte(self, dados, eu, t):
        """Avisa ANTES de você brigar com alguém que está muito forte (5 e 8 abates)."""
        for p in dados.get("allPlayers", []):
            if p.get("team") == eu.get("team"):
                continue
            abates = p.get("scores", {}).get("kills", 0)
            marco = 8 if abates >= 8 else 5 if abates >= 5 else 0
            if marco and self.e.alertas_fortes.get(p["championName"], 0) < marco:
                self.e.alertas_fortes[p["championName"]] = marco
                return [Dica(f"Cuidado: {p['championName']} já tem {abates} abates. Não enfrenta sozinho, "
                             f"briga só com o time junto.", NORMAL, chave="forte")]
        return []

    def _visao(self, eu, t):
        if t < 600 or not self._pode("visao", t, 360):
            return []
        placar = eu.get("scores", {}).get("wardScore", 0)
        if placar / (t / 60) >= 0.6:
            return []
        return [Dica(f"Seu placar de visão está em {int(placar)}. Na próxima volta pra base, compra uma "
                     f"sentinela de controle.", BAIXA, chave="visao")]

    def _caranguejo(self, eu, t):
        """Caranguejo do rio nasce aos 3:30: para o caçador, é a primeira disputa do jogo."""
        if self._rota(eu) != "JUNGLE" or not 190 <= t < 210 or "caranguejo" in self.e.avisados:
            return []
        self.e.avisados.add("caranguejo")
        return [Dica("Caranguejo do rio nasce em 20 segundos. Vê qual lado o caçador deles foi.", NORMAL)]

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
                dicas.append(Dica(self._minha_morte(dados, eu, ev), URGENTE, chave="morte"))
            elif nome == "FirstBrick":
                dicas.append(Dica("Primeira torre caiu. Olhem o mapa e rotacionem.", BAIXA))
        return dicas

    def _minha_morte(self, dados, eu, ev):
        """Frase montada com o que a API conta da morte (quem matou, quantos ajudaram, nível, abates).
        Sem IA: o modelo pequeno respondia sempre a mesma dica genérica."""
        jogadores = dados.get("allPlayers", [])

        def achar(nome):
            return next((p for p in jogadores if nome in nomes(p)), None)

        nome = ev.get("KillerName", "")
        matador = achar(nome)
        ajudantes = [p for p in (achar(n) for n in ev.get("Assisters", [])) if p]
        if not matador:
            if "Turret" in nome:
                return random.choice(("A torre te pegou. Só mergulha com tropa na frente e vida cheia.",
                                      "Morreu pra torre! Mergulho só com a onda tankando o tiro."))
            if "Minion" in nome:
                return "Morreu pras tropas! Cuidado, elas batem forte no começo do jogo."
            return random.choice(("Morreu. Respira e olha o mapa antes de voltar pra rota.",
                                  "Caiu essa. Aproveita o tempo morto pra olhar onde está cada inimigo."))
        campeao = matador["championName"]
        vezes = self.e.mortes_por[campeao] = self.e.mortes_por.get(campeao, 0) + 1
        envolvidos = [matador] + ajudantes
        cacador = next((p for p in envolvidos if rota(p) == "JUNGLE"), None)
        dif_nivel = matador.get("level", 0) - eu.get("level", 0)
        abates = matador.get("scores", {}).get("kills", 0)

        if len(envolvidos) >= 3:
            frase = random.choice((f"Te pegaram em {len(envolvidos)}. Sozinho contra vários não dá, cola no time.",
                                   f"{len(envolvidos)} inimigos em você! Se sumiram do mapa, joga mais recuado."))
        elif cacador and self._rota(eu) != "JUNGLE":
            frase = random.choice((f"Gank! {cacador['championName']} veio pela rota. Quando o caçador sumir do mapa, recua um pouco.",
                                   f"{cacador['championName']} veio te pegar. Ward no rio te salva dessa."))
        elif vezes >= 2:
            frase = random.choice((f"{campeao} te pegou de novo, {vezes}ª vez. Evita a troca direta por enquanto.",
                                   f"De novo: {campeao}, já são {vezes}. Farma seguro e espera o time."))
        elif dif_nivel >= 2:
            frase = f"{campeao} está {dif_nivel} níveis na sua frente, por isso pesou. Farma seguro até alcançar."
        elif abates >= 5:
            frase = f"{campeao} já tem {abates} abates, tá forte. Não briga sozinho."
        elif matador.get("level", 0) >= 6:
            frase = random.choice((f"{campeao} ganhou a troca. Olha a vida e se tem ultimate antes da próxima.",
                                   f"Perdeu o um contra um pra {campeao}. Na próxima, troca só com vantagem de vida."))
        else:   # antes do nível 6 ninguém tem ultimate: falar disso soa errado
            frase = random.choice((f"{campeao} ganhou a troca. Entra só com mais vida do que o rival.",
                                   f"Perdeu o um contra um pra {campeao}. Espera a habilidade principal sair antes de entrar."))
        if self.e.minhas_mortes >= 5 and self.e.minhas_mortes % 2 == 1:
            frase += f" Já são {self.e.minhas_mortes} mortes, segura um pouco."
        return frase

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

    def agora(self, dados):
        """Fatos concretos do momento, para a IA responder "o que eu faço agora?" com algo útil."""
        t = dados["gameData"]["gameTime"]
        fatos = []
        for nome, quando, desde in (("Dragão", self.e.proximo_dragao, 0), ("Arauto", self.e.arauto, 0),
                                    ("Barão", self.e.proximo_barao, 0)):
            if quando is None:
                continue
            falta = quando - t
            fatos.append(f"{nome} está vivo no mapa agora" if falta <= 0 else
                         f"{nome} nasce em {int(falta // 60)}:{int(falta % 60):02d}" if falta < 180 else "")
        eu = meu_jogador(dados)
        if eu and eu.get("isDead"):
            fatos.append("Ele está morto esperando renascer")
        ouro = int(dados["activePlayer"].get("currentGold", 0))
        if ouro >= self.cfg["ouro_para_base"]:
            fatos.append(f"Tem {ouro} de ouro: dá para voltar à base e comprar")
        return [f for f in fatos if f]

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
        linhas.append(f"Streamer (fale com ele de você): {eu['championName']} ({ROTA_PT.get(rota(eu), 'rota ?')}), nível {eu.get('level')}, "
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
