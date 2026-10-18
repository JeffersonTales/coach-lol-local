"""Cliente do Ollama — modelo pequeno, resposta curta, e se demorar a resposta é jogada fora."""
import re

import requests

PERSONALIDADE = (
    "Você é o Coach, parceiro de duo do streamer {canal} no League of Legends, falando ao vivo na live dele, "
    "em português do Brasil. Jeito: amigo animado, encoraja, comemora as boas jogadas e zoa de leve quando ele "
    "erra, sem ofender. Fale como gente, sem emojis, sem markdown, sem listas.\n"
    "REGRAS IMPORTANTES:\n"
    "- Você NÃO vê o mapa, nem o minimapa, nem a posição de ninguém. Só sabe o que está na 'Situação da partida'.\n"
    "- Nunca invente onde um inimigo está, nem vida, mana, cooldown ou dados que não recebeu.\n"
    "- Se perguntarem onde alguém está, diga que não enxerga o mapa e dê uma dica útil (olhar o minimapa, "
    "ward, jogar pelo lado seguro).\n"
    "- Se ele te der uma informação (ex.: 'o caçador deles tá no meio'), tire uma conclusão prática dela "
    "(ex.: 'então o outro lado do mapa tá livre, aproveita').\n"
    "- Não termine as frases com 'bora'."
)
DICA = (" Agora: se houver algo CONCRETO na situação (objetivo nascendo, ouro para item, alguém muito na frente, "
        "muitas mortes), dê UMA dica de no máximo 15 palavras sobre isso. Se não houver nada concreto e útil, "
        "responda exatamente NADA.")
CONVERSA = (" Agora o streamer falou com você. Responda à fala dele em no máximo 25 palavras, direto ao ponto. "
            "Se ele perguntar o que um item ou habilidade faz, pode usar até 40 palavras e cite os números dos "
            "dados oficiais. Para sugerir item: prefira o próximo da build que ele costuma fazer (fale \"sua build\", "
            "nunca \"do streamer\"); cite o meta do patch como segunda "
            "opinião (a % de vitória por vaga de item é só referência); diga o porquê em poucas palavras. "
            "Se for sobre a partida, use a situação. Se não souber, diga que não sabe.")
DADOS = "\n\nDados oficiais da Riot (use só estes atributos, não invente números):\n"


def limpar(texto):
    texto = re.sub(r"<think>.*?</think>", "", texto, flags=re.S)
    texto = re.sub(r"[*_#`]+", "", texto).strip().strip('"')     # markdown a voz não deve ler
    texto = re.sub(r"[,.!\s]*\bbora\b[^.!?]{0,25}[.!?]*\s*$", ".", texto, flags=re.I)   # o "bora!" do fim
    return " ".join(texto.split()).strip()[:300]


class LLM:
    def __init__(self, cfg):
        c = cfg["llm"]
        self.url = c["url"].rstrip("/") + "/api/chat"
        self.modelo = c["modelo"]
        self.contexto = c["contexto"]
        self.timeout = c["timeout"]
        self.personalidade = PERSONALIDADE.format(canal=cfg["jogador"].get("canal") or "do canal")

    def _chat(self, sistema, mensagens, limite):
        corpo = {
            "model": self.modelo,
            "stream": False,
            "think": False,
            "keep_alive": "2h",   # fica carregado: sem pico de carregamento no meio da partida
            "options": {"num_ctx": self.contexto, "num_predict": limite, "temperature": 0.6},
            "messages": [{"role": "system", "content": sistema}] + mensagens,
        }
        try:
            r = requests.post(self.url, json=corpo, timeout=self.timeout)
            r.raise_for_status()
            return limpar(r.json()["message"]["content"]) or None
        except (requests.RequestException, KeyError, ValueError):
            return None

    def perguntar(self, pedido, resumo, evitar=()):
        """Dica automática. Devolve None quando a IA não tem nada concreto a dizer (responde NADA)."""
        repetir = ("\n\nVocê já disse isto há pouco, NÃO repita a mesma ideia:\n- "
                   + "\n- ".join(evitar)) if evitar else ""
        resposta = self._chat(self.personalidade + DICA,
                              [{"role": "user", "content": f"Situação da partida:\n{resumo}{repetir}\n\nPedido: {pedido}"}], 60)
        if not resposta or resposta.strip(" .!").upper().startswith("NADA"):
            return None
        return resposta

    def conversar(self, fala, resumo, historico, dados_oficiais=""):
        """Resposta quando o streamer chama o coach. historico = [(fala, resposta), ...] das últimas trocas."""
        mensagens = []
        for f, r in historico:
            mensagens += [{"role": "user", "content": f}, {"role": "assistant", "content": r}]
        extra = DADOS + dados_oficiais if dados_oficiais else ""
        mensagens.append({"role": "user", "content": f"Situação da partida:\n{resumo}{extra}\n\nStreamer: {fala}"})
        return self._chat(self.personalidade + CONVERSA, mensagens, 130)

    def aquecer(self):
        """Carrega o modelo antes da partida começar."""
        return self._chat("Responda só: pronto.", [{"role": "user", "content": "pronto?"}], 5)
