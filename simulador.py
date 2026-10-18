"""Partida fictícia acelerada (1 leitura = 20 s de jogo) para testar o coach sem abrir o LoL."""


def _jogador(nome, campeao, time_, posicao, nivel, k, d, a, cs):
    return {"riotId": nome, "summonerName": nome, "championName": campeao, "team": time_,
            "position": posicao, "level": nivel, "isDead": False, "items": [],
            "scores": {"kills": k, "deaths": d, "assists": a, "creepScore": cs}}


class Partida:
    def __init__(self):
        self.t = 200.0
        self.eventos = [{"EventID": 0, "EventName": "GameStart", "EventTime": 0.0}]

    def _evento(self, nome, **extra):
        self.eventos.append({"EventID": len(self.eventos), "EventName": nome, "EventTime": self.t, **extra})

    def ler(self):
        self.t += 20
        minuto = self.t / 60
        if 420 <= self.t < 440:
            self._evento("ChampionKill", KillerName="Inimigo#BR1", VictimName="Eu#BR1")
        if 600 <= self.t < 620:
            self._evento("DragonKill", KillerName="Aliado#BR1", DragonType="Fire", Stolen="False")
        eu = _jogador("Eu#BR1", "Ahri", "ORDER", "MIDDLE", int(minuto // 1.5) + 1, 2, 1, 3, int(minuto * 5.5))
        eu["items"] = [{"displayName": "Anel de Doran"}, {"displayName": "Botas"}]
        rival = _jogador("Inimigo#BR1", "Syndra", "CHAOS", "MIDDLE", int(minuto // 1.5) + 3, 3, 2, 1, int(minuto * 7))
        return {
            "activePlayer": {"riotId": "Eu#BR1", "summonerName": "Eu#BR1", "currentGold": 400 + (self.t % 300) * 4},
            "allPlayers": [eu, rival,
                           _jogador("Aliado#BR1", "Lee Sin", "ORDER", "JUNGLE", 6, 1, 1, 4, 60),
                           _jogador("Inimigo2#BR1", "Vi", "CHAOS", "JUNGLE", 6, 1, 1, 2, 55)],
            "events": {"Events": list(self.eventos)},
            "gameData": {"gameTime": self.t, "gameMode": "SIMULACAO"},
        }
