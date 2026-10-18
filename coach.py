"""
Coach de LoL (IA local)
=======================
Lê a API oficial do cliente do LoL (127.0.0.1:2999), aplica regras de macro e fala as dicas em voz alta.
Com o LLM ligado (config.yaml → llm.ativado), um modelo pequeno do Ollama comenta a partida de vez em quando.
O guarda-costas pausa a IA se o jogo ou o OBS começarem a sofrer.

Rodar:  iniciar-coach.bat   (ou: python coach.py)
Testes: python coach.py --teste-voz | --teste-llm | --simular
Parar:  feche a janela ou Ctrl+C
"""
import argparse
import json
import logging
import random
import sys
import threading
import time
import warnings
from collections import deque
from pathlib import Path

import yaml

import live_client
from conhecimento import Conhecimento
from guard import Guarda, baixar_prioridade
from llm import LLM
from visao import Visao
import minimapa
from minimapa import Olheiro
from ouvido import OI, Ouvido, normalizar
from rules import BAIXA, NORMAL, RESPOSTA, Regras, resumo_partida
from voice import Voz

PASTA = Path(__file__).resolve().parent
INTERVALO_API = 2      # segundos entre leituras da partida
ARQ_ESTADO = "estado_coach.json"

log = logging.getLogger("coach")
# ditas assim que ele entende a pergunta, para você saber que ele ouviu enquanto a IA pensa
COMENTARIO = ("Comente o momento como um parceiro de duo: UMA dica prática de no máximo 18 palavras, apoiada em um "
              "fato da linha 'Agora' (onde os inimigos aparecem no minimapa, objetivo, inimigos mortos, seu ouro). "
              "Diga o fato e o que fazer por causa dele.")
PENSANDO = ("Hum, deixa eu ver.", "Peraí, pensando aqui.", "Boa pergunta, deixa eu ver.")


def configurar_log():
    warnings.filterwarnings("ignore", category=UserWarning)
    warnings.filterwarnings("ignore", category=FutureWarning)
    logging.getLogger("obsws_python").setLevel(logging.CRITICAL)  # OBS fechado não é erro
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
    log.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(PASTA / "coach.log", encoding="utf-8")):
        h.setFormatter(fmt)
        log.addHandler(h)


def palavras(texto):
    return {p for p in normalizar(texto) if len(p) > 3}


def repetida(texto, anteriores, limite=0.5):
    """O modelo pequeno ignora o "não repita": aqui a dica com metade das palavras iguais a uma recente é cortada."""
    novas = palavras(texto)
    for velha in anteriores:
        iguais = len(novas & palavras(velha)) / max(1, min(len(novas), len(palavras(velha))))
        if iguais >= limite:
            return True
    return False


def foto_partida(dados):
    """Placar, itens, níveis e objetivos. Se nada disso mudou, não há dica nova de macro para dar."""
    ps = dados.get("allPlayers", [])
    return (tuple((p.get("level"), tuple(sorted(p.get("scores", {}).items())), len(p.get("items", []))) for p in ps),
            len(dados.get("events", {}).get("Events", [])))


class Coach:
    def __init__(self, cfg, fonte=live_client.ler_partida):
        self.cfg = cfg
        self.fonte = fonte
        self.regras = Regras(cfg)
        self.voz = Voz(cfg, PASTA)
        self.guarda = Guarda(cfg)
        self.llm = LLM(cfg) if cfg["llm"]["ativado"] else None
        self._llm_ocupado = threading.Lock()
        self._ultima_macro = None
        self.em_partida = False
        self.ultimos_dados = None
        self.historico = deque(maxlen=4)   # últimas conversas, para ele entender "e depois?"
        self._ultima_conversa = 0.0
        self.silencio_ate = 0.0            # "Coach, silêncio" pausa as dicas automáticas
        self.ultimas_dicas = deque(maxlen=6)   # a IA vê as últimas dicas para não repetir
        self._foto_macro = None                # como a partida estava na última análise geral
        self.saber = Conhecimento()            # itens e campeões oficiais (Data Dragon)
        self.visao = Visao(cfg) if self.llm and cfg.get("visao", {}).get("ativado") else None
        self._ultimo_mapa = 0.0
        v = cfg.get("visao", {})
        # leitura do minimapa por cor (sem IA): onde os inimigos aparecem
        self._olhos = Visao(cfg) if v.get("ler_mapa") or v.get("coletar") else None
        self.olheiro = Olheiro()
        self._olhando = threading.Lock()
        self._ultima_olhada = -1e9
        self._ultima_coleta = -1e9
        self._pasta_coleta = None
        ouvir = cfg["ouvido"]["ativado"] and self.llm
        self.ouvido = Ouvido(cfg, self.voz, self.responder,
                             lambda: self.saber.vocabulario(self.ultimos_dados)) if ouvir else None

    def preparar(self):
        log.info("carregando a voz (processador)...")
        self.voz.carregar()
        self.voz.preparar_frases(PENSANDO + (OI,))
        if self.llm:
            log.info("carregando a IA %s no Ollama...", self.cfg["llm"]["modelo"])
            log.info("IA respondeu: %s", self.llm.aquecer() or "(sem resposta — confira se o Ollama está aberto)")
        try:
            self.saber.carregar()
        except Exception as erro:
            log.warning("não carregou os dados de itens/campeões: %s", erro)
        if self.ouvido:
            log.info("carregando o ouvido (Whisper no processador)...")
            self.ouvido.carregar()
            self.ouvido.iniciar()
        baixar_prioridade()

    # ---------------- conversa ("Coach, ...") ----------------
    def responder(self, fala):
        normal = " ".join(normalizar(fala))
        if any(p in normal for p in ("silencio", "quieto", "cala a boca", "para de falar")):
            self.silencio_ate = time.monotonic() + 300
            self.voz.calar_dicas()
            self.voz.dizer("Beleza, fico quieto por cinco minutos. Me chama se precisar!", RESPOSTA, "resposta")
            return
        if any(p in normal for p in ("pode falar", "volta a falar", "pode voltar")):
            self.silencio_ate = 0.0
            self.voz.dizer("Voltei! Bora pra cima.", RESPOSTA, "resposta")
            return
        if not fala:
            self.voz.dizer("Fala, tô aqui!", RESPOSTA, "resposta")
            return
        self.voz.dizer(random.choice(PENSANDO), RESPOSTA, "pensando")
        self.voz.escrever_overlay("🤔 Pensando…")
        resumo = resumo_partida(self.ultimos_dados) if self.ultimos_dados else "Fora de partida (no lobby ou na seleção)."
        if self.ultimos_dados:
            resumo = self._resumo_completo(self.ultimos_dados)
        # conversa antiga não entra: o modelo pequeno copia as frases dela em vez de responder a pergunta nova
        if time.monotonic() - self._ultima_conversa > 60:
            self.historico.clear()
        # a pergunta espera a IA terminar uma dica automática (no máximo 3 s), em vez de ser descartada
        if not self._llm_ocupado.acquire(timeout=3):
            self.voz.dizer("Peraí, já te respondo. Pergunta de novo!", RESPOSTA, "resposta")
            return
        ultima = self.historico[-1][1] if self.historico else ""
        oficiais = self.saber.contexto_para(fala, self.ultimos_dados, ultima)
        if oficiais:
            log.info("dados oficiais enviados: %s", oficiais.split("\n")[0])
        try:
            inicio = time.monotonic()
            self.guarda.ia_trabalhando(True)
            resposta = self.llm.conversar(fala, resumo, list(self.historico), oficiais)
            log.info("IA respondeu em %.1fs", time.monotonic() - inicio)
        finally:
            self.guarda.ia_trabalhando(False)
            self._llm_ocupado.release()
        resposta = resposta or "Deu um branco aqui, pergunta de novo?"
        self.historico.append((fala, resposta))
        self._ultima_conversa = time.monotonic()
        self.voz.dizer(resposta, RESPOSTA, "resposta")

    def _resumo_completo(self, dados):
        """Placar + fatos do momento + o que o minimapa mostra: o que a IA sabe da partida."""
        fatos = self.regras.agora(dados)
        eu = live_client.meu_jogador(dados)
        mapa = self.olheiro.resumo(eu.get("team")) if eu else ""
        fatos += [mapa] if mapa else []
        return resumo_partida(dados) + "\nAgora: " + ("; ".join(fatos) if fatos else "nenhum objetivo perto de nascer")

    def _dicas_ligadas(self):
        return time.monotonic() >= self.silencio_ate

    def _pedir_llm(self, pedido, resumo, reserva, prioridade, mapa=False):
        """Uma pergunta por vez. Ocupado, pausado, sem nada concreto ou repetido → usa a frase pronta (reserva)."""
        def dizer(texto):
            if texto and not repetida(texto, self.ultimas_dicas):
                self.ultimas_dicas.append(texto)
                self.voz.dizer(texto, prioridade, chave="ia")

        if not self.llm or not self.guarda.livre or not self._llm_ocupado.acquire(blocking=False):
            dizer(reserva)
            return

        def tarefa():
            try:
                inicio = time.monotonic()
                self.guarda.ia_trabalhando(True)
                if mapa:
                    imagem = self.visao.minimapa()
                    resposta = self.llm.ver_mapa(imagem, resumo, list(self.ultimas_dicas)) if imagem else None
                else:
                    resposta = self.llm.perguntar(pedido, resumo, list(self.ultimas_dicas))
                self.guarda.ia_trabalhando(False)
                log.info("IA %sem %.1fs", "(minimapa) " if mapa else "", time.monotonic() - inicio)
                if resposta and repetida(resposta, self.ultimas_dicas):
                    log.info("descartada (repetida): %s", resposta)
                    resposta = None
                dizer(resposta or reserva)
            finally:
                self.guarda.ia_trabalhando(False)
                self._llm_ocupado.release()

        threading.Thread(target=tarefa, daemon=True).start()

    def _olhar(self, dados, t):
        """Lê o minimapa (uns milissegundos no processador) e fala o que for importante."""
        with self._olhando:
            v = self.cfg["visao"]
            imagem = self._olhos.recorte()
            if imagem is None:
                return
            if v.get("coletar") and t - self._ultima_coleta >= v["coletar_a_cada"]:
                self._ultima_coleta = t
                self._pasta_coleta.mkdir(parents=True, exist_ok=True)
                imagem.save(self._pasta_coleta / f"{int(t // 60):02d}m{int(t % 60):02d}s.png")
            if not v.get("ler_mapa"):
                return
            eu = live_client.meu_jogador(dados)
            if not eu or eu.get("isDead"):
                return
            dicas = self.olheiro.avaliar(minimapa.ler(imagem), eu.get("team"), self.regras._rota(eu) == "JUNGLE",
                                         t, self.regras._objetivo_livre(t))
            if self._dicas_ligadas():
                for texto, prioridade in dicas:
                    log.info("mapa: %s", texto)
                    self.voz.dizer(texto, prioridade, chave="mapa")

    def passo(self):
        dados = self.fonte()
        self.ultimos_dados = dados
        if not dados:
            if self.em_partida:
                log.info("partida terminou")
                self.em_partida = False
            return
        if not self.em_partida:
            self.em_partida = True
            self.regras.nova_partida()
            self._pasta_coleta = PASTA / "dados" / "minimapas" / time.strftime("%Y-%m-%d_%H%M")
            self._ultima_macro = dados["gameData"]["gameTime"]
            log.info("partida encontrada (%s)", dados["gameData"].get("gameMode"))
            self.voz.dizer("Coach na área. Bom jogo!", NORMAL)
            # baixa o meta do seu campeão agora (1 página por patch), para a 1ª pergunta não esperar a internet
            threading.Thread(target=self.saber.meta_para, args=(dados,), daemon=True).start()

        dicas = self.regras.avaliar(dados)   # roda sempre, para os timers de objetivo não se perderem
        if not self._dicas_ligadas():
            return
        for dica in dicas:
            if dica.pedido_llm:
                self._pedir_llm(dica.pedido_llm, resumo_partida(dados), dica.texto, dica.prioridade)
            else:
                self.voz.dizer(dica.texto, dica.prioridade, dica.chave)

        t = dados["gameData"]["gameTime"]
        if self._olhos and t - self._ultima_olhada >= self.cfg["visao"]["ler_a_cada"] and not self._olhando.locked():
            self._ultima_olhada = t
            threading.Thread(target=self._olhar, args=(dados, t), daemon=True).start()
        if self.visao and t - self._ultimo_mapa >= self.cfg["visao"]["intervalo"] and not self.voz.ocupada():
            # olhando o minimapa sempre há assunto novo; a análise geral sem imagem fica para depois
            self._ultimo_mapa = self._ultima_macro = t
            self._pedir_llm(None, resumo_partida(dados), None, BAIXA, mapa=True)
            return
        # ficou quieto por um tempo e você está vivo: comenta o momento com o que está acontecendo de verdade
        quieto = self.cfg["llm"].get("comentar_apos", 30)
        eu = live_client.meu_jogador(dados)
        if (quieto and eu and not eu.get("isDead") and not self.voz.ocupada()
                and self.voz.falou_ha() >= quieto and t - self._ultima_macro >= quieto):
            self._ultima_macro = t
            narrado = self.olheiro.narrar(eu.get("team"))
            if narrado and not repetida(narrado, self.ultimas_dicas):
                # o que o mapa mostra, dito direto: a IA confundia os lados ao reescrever
                self.ultimas_dicas.append(narrado)
                self.voz.dizer(narrado, BAIXA, chave="ia")
            elif self.llm:
                self._pedir_llm(COMENTARIO, self._resumo_completo(dados), None, BAIXA)

    def estado(self):
        """O que o coach está fazendo agora, para o Stream Deck mostrar (lido pelo controle.py)."""
        if not self.em_partida:
            return "⏳ Esperando a partida"
        if self.voz.falando:
            return "🗣️ Falando"
        if self.ouvido and self.ouvido.entendendo:
            return "🎤 Entendendo você"
        if self._llm_ocupado.locked():
            return "🤔 Pensando"
        if self.ouvido and time.monotonic() < self.ouvido._esperando_ate:
            return "👂 Esperando sua pergunta"
        if not self._dicas_ligadas():
            return f"🤫 Em silêncio por mais {int(self.silencio_ate - time.monotonic())} s"
        if self.llm and not self.guarda.livre:
            return f"⏸️ IA pausada ({self.guarda.motivo}), só dicas fixas"
        return "👂 Ouvindo (chame \"Coach\")"

    def _publicar_estado(self):
        arq = PASTA / ARQ_ESTADO
        while True:
            try:
                arq.write_text(json.dumps({"estado": self.estado()}, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            time.sleep(1)

    def rodar(self, intervalo=INTERVALO_API):
        threading.Thread(target=self._publicar_estado, daemon=True).start()
        log.info("esperando uma partida... (abra o LoL e entre em jogo)")
        while True:
            try:
                self.passo()
            except Exception:
                log.exception("erro no passo — seguindo")
            time.sleep(intervalo)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teste-voz", action="store_true", help="fala uma frase e sai")
    ap.add_argument("--teste-llm", action="store_true", help="faz uma pergunta à IA com uma partida fictícia")
    ap.add_argument("--simular", action="store_true", help="roda o coach com uma partida fictícia acelerada")
    ap.add_argument("--teste-ouvido", action="store_true", help="testa o \"Coach, ...\" com frases sintéticas")
    args = ap.parse_args()

    configurar_log()
    cfg = yaml.safe_load((PASTA / "config.yaml").read_text(encoding="utf-8"))

    if args.teste_llm:
        import simulador
        cfg["llm"]["ativado"] = True
        dados = simulador.Partida().ler()
        inicio = time.monotonic()
        resposta = LLM(cfg).perguntar("Qual a melhor decisão de macro para os próximos 60 segundos?",
                                      resumo_partida(dados))
        log.info("IA (%.1fs): %s", time.monotonic() - inicio, resposta)
        return

    if args.teste_ouvido:
        import numpy as np
        import simulador
        from kokoro import KPipeline
        voz_teste = KPipeline(lang_code="p", repo_id="hexgrad/Kokoro-82M", device="cpu")
        ouvido = Ouvido(cfg, None, None)
        ouvido.carregar()
        llm = LLM(cfg)
        dados = simulador.Partida().ler()
        # "Côuti" = como um brasileiro pronuncia "coach" (a voz sintética lê "Coach" letra por letra)
        for frase in ("Côuti, que item eu faço agora?", "Ô côuti, vale a pena pegar o dragão?",
                      "Bora pessoal, chegou mais gente no chat!", "Côuti, silêncio.",
                      "Essa coisa tá difícil, mano.", "Côtch, eu tô jogando bem?",
                      "Esse jungler inimigo tá muito forte, mano."):
            audio = np.concatenate([r.audio.numpy() for r in voz_teste(frase, voice="pm_alex")])
            audio16 = np.interp(np.arange(0, len(audio), 1.5), np.arange(len(audio)), audio).astype(np.float32)
            inicio = time.monotonic()
            pergunta = ouvido.entender(audio16)
            log.info("FALOU: %-45s -> ENTENDEU: %s (%.1fs)", frase,
                     "(ignorou)" if pergunta is None else repr(pergunta), time.monotonic() - inicio)
            if pergunta:
                inicio = time.monotonic()
                resposta = llm.conversar(pergunta, resumo_partida(dados), [])
                log.info("      COACH (%.1fs): %s", time.monotonic() - inicio, resposta)
        return

    if args.simular:
        import simulador
        partida = simulador.Partida()
        coach = Coach(cfg, fonte=partida.ler)
        coach.preparar()
        coach.rodar(intervalo=0.5)
        return

    coach = Coach(cfg)
    coach.preparar()
    if args.teste_voz:
        coach.voz.dizer("Fala, galera! Dragão nasce em um minuto, preparem a visão.", NORMAL)
        time.sleep(1)
        while coach.voz.ocupada():
            time.sleep(0.2)
        return
    coach.rodar()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
