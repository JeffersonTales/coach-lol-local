"""
Leitura do minimapa por cor, sem IA: acha os ícones de campeão (anel vermelho = inimigo, azul = aliado) e o
quadrado branco da câmera (onde você está olhando, quase sempre o seu campeão).
Rápido (uns milissegundos no processador) e não inventa: se o ícone não está no mapa, ele não vê.
Coordenadas: 0..1 nos dois eixos, (0,0) no canto de cima à esquerda. A base azul (ORDER) fica embaixo à
esquerda e a vermelha (CHAOS) em cima à direita, para os dois times.
"""
import math
from dataclasses import dataclass, field

import numpy as np

ANGULOS = np.linspace(0, 2 * np.pi, 36, endpoint=False)
BORDA = 0.04    # moldura em volta do mapa, fração do lado do recorte
DRAGAO = (0.67, 0.70)
BARAO = (0.32, 0.29)


@dataclass
class Leitura:
    inimigos: list = field(default_factory=list)   # [(x, y)]
    aliados: list = field(default_factory=list)
    camera: tuple | None = None


def _aneis(mascara, raios, minimo=0.55):
    altura, largura = mascara.shape
    melhor = np.zeros((altura, largura))
    for raio in raios:
        fracao = np.zeros((altura, largura))
        for t in ANGULOS:
            dy, dx = int(round(raio * math.sin(t))), int(round(raio * math.cos(t)))
            fracao += np.roll(np.roll(mascara, -dy, 0), -dx, 1)
        melhor = np.maximum(melhor, fracao / len(ANGULOS))
    # o miolo do ícone é o rosto do campeão, não a cor do anel (descarta manchas cheias, como torres)
    miolo = np.zeros((altura, largura))
    for t in ANGULOS[::3]:
        for raio in (raios[0] // 4, raios[0] // 2):
            dy, dx = int(round(raio * math.sin(t))), int(round(raio * math.cos(t)))
            miolo += np.roll(np.roll(mascara, -dy, 0), -dx, 1)
    miolo /= len(ANGULOS[::3]) * 2
    ok = (melhor >= minimo) & (miolo < 0.35)
    m = raios[-1] + 1
    ok[:m], ok[-m:], ok[:, :m], ok[:, -m:] = False, False, False, False
    pontos = []
    distancia = (raios[-1] * 1.4) ** 2
    for y, x in sorted(zip(*np.where(ok)), key=lambda p: -melhor[p]):
        if all((y - py) ** 2 + (x - px) ** 2 > distancia for py, px in pontos):
            pontos.append((y, x))
    return pontos


def _camera(r, g, b):
    """Centro do retângulo branco da câmera, pelas linhas brancas longas."""
    branco = (r > 225) & (g > 225) & (b > 225)
    linhas = np.where(branco.sum(1) >= branco.shape[1] * 0.12)[0]
    colunas = np.where(branco.sum(0) >= branco.shape[0] * 0.06)[0]
    if len(linhas) < 2 or len(colunas) < 2:
        return None
    return (colunas.min() + colunas.max()) / 2, (linhas.min() + linhas.max()) / 2


def ler(imagem):
    """imagem: recorte do minimapa (PIL). Calibrado num minimapa de 280 px; outros tamanhos são escalados."""
    a = np.asarray(imagem.convert("RGB")).astype(int)
    lado = a.shape[0]
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    escala = lado / 280
    raios = tuple(max(4, round(x * escala)) for x in (11, 12, 13, 14))
    vermelho = (r > 150) & (r - g > 90) & (r - b > 80)
    azul = (b > 150) & (b - r > 60) & (b - g > 10)

    def normal(x, y):
        return (round((x / lado - BORDA) / (1 - 2 * BORDA), 3), round((y / lado - BORDA) / (1 - 2 * BORDA), 3))

    cam = _camera(r, g, b)
    return Leitura(inimigos=[normal(x, y) for y, x in _aneis(vermelho, raios)],
                   aliados=[normal(x, y) for y, x in _aneis(azul, raios)],
                   camera=normal(*cam) if cam else None)


# ---------------- onde fica cada ponto do mapa ----------------
def distancia(p, q):
    return math.hypot(p[0] - q[0], p[1] - q[1])


def regiao(p, meu_time):
    """Nome da região do ponto, do ponto de vista do seu time ("ORDER" = azul, "CHAOS" = vermelho)."""
    x, y = p
    azul_baixo = meu_time != "CHAOS"
    if (x < 0.22 and y > 0.78) or (x > 0.78 and y < 0.22):
        minha = (x < 0.5) == azul_baixo
        return "na sua base" if minha else "na base deles"
    if distancia(p, DRAGAO) < 0.1:
        return "no dragão"
    if distancia(p, BARAO) < 0.1:
        return "no Barão"
    if x < 0.15 or y < 0.15:
        return "no topo"
    if x > 0.85 or y > 0.85:
        return "no bot"
    if abs(x + y - 1) < 0.12:
        return "no meio"
    if abs(x - y) < 0.07:
        return "no rio de cima" if x + y < 1 else "no rio de baixo"
    lado = "de cima" if x + y < 1 else "de baixo"
    selva_azul = y > x
    return f"na sua selva {lado}" if selva_azul == azul_baixo else f"na selva deles {lado}"


# ---------------- dicas a partir das leituras ----------------
NUMERO = {2: "Dois", 3: "Três", 4: "Quatro", 5: "Cinco"}


class Olheiro:
    """Transforma as leituras do minimapa em dicas, com intervalo próprio para cada tipo (não repete)."""

    def __init__(self):
        self.ultima = {}       # tipo de dica -> tempo de jogo da última
        self.vazias = 0        # leituras seguidas sem nenhum inimigo visível
        self.leitura = None

    def _pode(self, tipo, t, intervalo):
        if t - self.ultima.get(tipo, -1e9) < intervalo:
            return False
        self.ultima[tipo] = t
        return True

    def resumo(self, meu_time):
        """Uma linha para a IA usar quando você pergunta algo."""
        if not self.leitura:
            return ""
        l = self.leitura
        onde = ", ".join(regiao(p, meu_time) for p in l.inimigos) or "nenhum visível"
        voce = f"; você está {regiao(l.camera, meu_time)}" if l.camera else ""
        return f"Minimapa agora: inimigos {onde}{voce}"

    def avaliar(self, leitura, meu_time, sou_cacador, t, objetivo_vivo=None):
        """Lista de (texto, prioridade 1-3). objetivo_vivo: 'o dragão' / 'o Barão' / None."""
        self.leitura = leitura
        dicas = []
        inimigos, cam = leitura.inimigos, leitura.camera
        self.vazias = 0 if inimigos else self.vazias + 1
        na_base = cam is not None and regiao(cam, meu_time) == "na sua base"

        # 1. inimigos chegando perto de você
        if cam and not na_base:
            perto = [p for p in inimigos if distancia(p, cam) < 0.13]
            if len(perto) >= 2 and self._pode("perto", t, 25):
                dicas.append((f"{NUMERO.get(len(perto), 'Vários')} inimigos perto de você, cuidado!", 3))
                return dicas

        # 2. inimigos agrupados num lugar
        for centro in inimigos:
            grupo = [p for p in inimigos if distancia(p, centro) < 0.2]
            if len(grupo) >= 3:
                meio = (sum(p[0] for p in grupo) / len(grupo), sum(p[1] for p in grupo) / len(grupo))
                onde = regiao(meio, meu_time)
                if cam and distancia(meio, cam) < 0.2:
                    break   # já coberto pelo aviso de perigo
                if self._pode("grupo", t, 45):
                    if onde in ("no dragão", "no Barão"):
                        texto = f"{NUMERO[min(len(grupo), 5)]} inimigos {onde}! Contesta com o time ou pega algo do outro lado."
                    else:
                        texto = f"{NUMERO[min(len(grupo), 5)]} inimigos juntos {onde}. O outro lado do mapa tá livre."
                        if objetivo_vivo and onde not in ("no bot",) and objetivo_vivo == "o dragão":
                            texto += " Dá pra tentar o dragão."
                    dicas.append((texto, 3))
                break

        # 3. inimigo na sua selva (importa mais para quem joga de caçador)
        if sou_cacador:
            invasores = [p for p in inimigos if regiao(p, meu_time).startswith("na sua selva")
                         and not (cam and distancia(p, cam) < 0.13)]
            if invasores and self._pode("invasao", t, 60):
                dicas.append((f"Inimigo {regiao(invasores[0], meu_time)}. Cuidado com seus monstros.", 2))

        # 4. todos sumiram do mapa (3 leituras seguidas), com você fora da base
        if self.vazias >= 3 and t > 600 and not na_base and self._pode("sumiram", t, 120):
            dicas.append(("Nenhum inimigo aparece no mapa. Cuidado ao avançar, podem estar te esperando.", 2))
        return dicas

    def narrar(self, meu_time):
        """Frase pronta com o que o mapa mostra (usada quando a IA não tem nada melhor). None se não há o que dizer."""
        if not self.leitura or not self.leitura.inimigos:
            return None
        rotas = {"no topo": 0, "no meio": 0, "no bot": 0}
        outros = []
        for p in self.leitura.inimigos:
            onde = regiao(p, meu_time)
            if onde in rotas:
                rotas[onde] += 1
            else:
                outros.append(onde)
        livres = [onde for onde, n in rotas.items() if not n]
        palavra = {1: "um", 2: "dois", 3: "três", 4: "quatro", 5: "cinco"}
        vistos = [f"{palavra.get(n, n)} {onde}" for onde, n in rotas.items() if n] + [f"um {o}" for o in outros[:2]]
        frase = "Inimigos aparecendo: " + (", ".join(vistos[:-1]) + " e " + vistos[-1] if len(vistos) > 1 else vistos[0]) + "."
        # só sugere um lado livre se dá para ver boa parte deles (com um inimigo visível, o resto pode estar ali)
        if len(self.leitura.inimigos) >= 2 and 0 < len(livres) < 3:
            frase += f" Ninguém deles aparece {livres[0]}, bom lado pra você."
        return frase
