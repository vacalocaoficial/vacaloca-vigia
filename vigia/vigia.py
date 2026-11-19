#!/usr/bin/env python3
"""VIGIA independente (SOMENTE LEITURA). Só usa a biblioteca padrão do Python; não importa nada do projeto da nuvem,
não usa segredos, não escreve em lugar nenhum e não altera nenhum feed. Serve para rodar num repositório separado.

O que faz: lê o feed.csv PÚBLICO e, depois da virada (padrão 28/11/2026 00:05 de Brasília), procura grupos que
ainda mostram sale_price depois do fim da promoção.

TRÊS NÍVEIS DE TEMPO (todos em horário com fuso):
  antecipada  (antes do corte, 00:05)      só informa e avisa; nunca falha.
  aviso       (corte até o limite, 01:55)  encontra o problema, mas só AVISA (código 0): dá tempo à nuvem/Mac/GitHub atrasados.
  emergência  (a partir do limite, 01:55)  problema comprovado = FALHA (e-mail/push do GitHub).
  --estrito trata qualquer horário como emergência (para ensaios).
  Passou do --fim (padrão 03/12/2026 00:00) a vigilância termina: sai com 0 sem baixar nada.

COMO DECIDE SE UM sale_price É VENCIDO (cada grupo, nesta ordem):
  1. Exceção manual válida (arquivo do Beto, com início, fim e nota)           -> permitido.
  2. Registro de vencimentos utilizável (cópia pública, só códigos e datas, atualizada há <= 26 h):
       fim já passou (+1 min)                    -> VENCIDO COMPROVADO
       campanha em vigor no registro             -> permitido (campanha nova/prorrogada legítima)
       início no futuro ou duração > 45 dias     -> suspeito = VENCIDO PRESUMIDO
  3. Sem data (grupo fora do registro ou registro ausente/velho/ilegível):
       grupo na fotografia de grupos vigiados    -> VENCIDO PRESUMIDO
       sem registro e sem fotografia             -> VENCIDO PRESUMIDO (modo conservador)
       registro bom e fotografia não conhece     -> NOVO (só aviso; em massa vira falha)
  O registro e a fotografia são OPCIONAIS: se faltarem, o vigia continua funcionando em modo conservador (nunca fica cego).
Integridade do feed: mínimo de linhas, queda anormal contra a fotografia, 21 colunas, e idade do feed (Last-Modified): acima de 36 h gera só um AVISO, nunca falha
(o feed só é republicado quando algo muda; feed velho SEM promoção vencida não é perigoso).
Download com 3 tentativas. Códigos: 0 ok/aviso; 1 PROMOÇÃO VENCIDA (ou preço inválido, ou muitos grupos sem data);
2 feed ilegível, suspeito ou falha de rede, ou exceções ilegíveis. (O código 3, "feed parado", foi aposentado: agora é só aviso.)
Nunca imprime URLs com chave nem conteúdo do feed além de números de grupo.
"""
import argparse
import csv
import io
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime

CORTE_PADRAO = "2026-11-28T00:05:00-03:00"
LIMITE_PADRAO = "2026-11-28T01:55:00-03:00"
FIM_PADRAO = "2026-12-03T00:00:00-03:00"
COLUNAS = 21
MIN_LINHAS = 3000
QUEDA_MAX = 0.15                 # queda acima de 15% contra a fotografia = suspeito
IDADE_FEED_MAX_H = 36            # acima disso só AVISA (Last-Modified só anda quando o conteúdo muda)
REGISTRO_IDADE_MAX_H = 26
REGISTRO_MIN_GRUPOS = 300
SNAPSHOT_IDADE_MAX_DIAS = 10
NOVOS_MAX_ABS = 20               # grupos novos sem data: acima disso (e de 5% do registro) vira falha
NOVOS_MAX_FRACAO = 0.05
TENTATIVAS = 3
ESPERA = 5.0
MAX_BYTES = 80_000_000
FOLGA = timedelta(minutes=1)


class Ilegivel(Exception):
    pass


def hora(texto):
    try:
        d = datetime.fromisoformat(str(texto).strip())
    except (ValueError, TypeError):
        return None
    return d if d.utcoffset() is not None else None


def nivel_de(agora, corte, limite, estrito=False):
    if estrito:
        return "emergencia"
    return "antecipada" if agora < corte else "aviso" if agora < limite else "emergencia"


# ------------------------------------------------------------------ feed
def ler_feed(bruto, min_linhas=MIN_LINHAS, ref_linhas=None):
    try:
        linhas = list(csv.reader(io.StringIO(bruto.decode("utf-8"), newline="")))
    except (UnicodeDecodeError, csv.Error):
        raise Ilegivel("feed não é um CSV legível") from None
    if not linhas or "sale_price" not in linhas[0] or "group_id" not in linhas[0] or len(linhas[0]) != COLUNAS:
        raise Ilegivel("cabeçalho do feed inesperado")
    corpo = linhas[1:]
    if len(corpo) < min_linhas or any(len(l) != COLUNAS for l in corpo):
        raise Ilegivel(f"feed pequeno demais ou com linhas quebradas ({len(corpo)} linhas; mínimo {min_linhas})")
    if ref_linhas and len(corpo) < ref_linhas * (1 - QUEDA_MAX):
        raise Ilegivel(f"queda anormal de produtos: {len(corpo)} linhas contra {ref_linhas} na fotografia (limite {int(QUEDA_MAX * 100)}%)")
    return linhas[0], corpo


def _dinheiro(txt):
    try:
        return Decimal(str(txt).split()[0].replace(",", "."))
    except (InvalidOperation, IndexError):
        return None


# ------------------------------------------------------------------ arquivos de apoio (todos opcionais, exceto a leitura das exceções)
def ler_excecoes(bruto, max_dias=45):
    if bruto is None:
        return []
    try:
        out = []
        for c in json.loads(bruto)["campanhas"]:
            ini, fim = hora(c["inicio"]), hora(c["fim"])
            g = str(c["grupo"]).strip()
            if ini is None or fim is None or ini > fim or not g.isdigit() or not str(c.get("nota", "")).strip():
                raise ValueError
            if fim - ini > timedelta(days=max_dias):
                raise Ilegivel(f"exceção do grupo {g} dura mais de {max_dias} dias (suspeita: confirme e ajuste --max-dias-excecao)")
            out.append((g, ini, fim))
        return out
    except (ValueError, KeyError, TypeError, AttributeError):
        raise Ilegivel("arquivo de exceções ilegível (grupo numérico, datas com fuso e nota são obrigatórios)") from None


def ler_registro(bruto, agora, idade_max_h=REGISTRO_IDADE_MAX_H, min_grupos=REGISTRO_MIN_GRUPOS):
    """Cópia pública do registro de fins: {"fins": {grupo: fim}, "inicios": {grupo: inicio}, "atualizado_em": ...}.
    Devolve {grupo: (inicio|None, fim)}. Levanta Ilegivel se não dá para confiar (o vigia então segue sem ele)."""
    try:
        doc = json.loads(bruto)
        fins, inicios = doc["fins"], doc.get("inicios") or {}
        atual = hora(doc["atualizado_em"])
        if not isinstance(fins, dict) or not isinstance(inicios, dict) or atual is None:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise Ilegivel("registro de vencimentos ilegível") from None
    idade = agora - atual
    if idade < -timedelta(minutes=10):
        raise Ilegivel("registro de vencimentos datado do futuro")
    if idade > timedelta(hours=idade_max_h):
        raise Ilegivel(f"registro de vencimentos DESATUALIZADO ({idade.total_seconds() / 3600:.1f} h > {idade_max_h} h)")
    reg, ruins = {}, 0
    for g, f in fins.items():
        fim, ini = hora(f), hora(inicios.get(g)) if g in inicios else None
        if not str(g).isdigit() or fim is None or (g in inicios and ini is None) or (ini and ini > fim):
            ruins += 1
            continue
        reg[str(g)] = (ini, fim)
    if ruins > 0.2 * max(len(fins), 1):
        raise Ilegivel(f"registro de vencimentos com {ruins} entradas inválidas")
    if len(reg) < min_grupos:
        raise Ilegivel(f"registro de vencimentos pequeno demais ({len(reg)} grupos < {min_grupos})")
    return reg


def ler_snapshot(bruto, agora, max_dias=SNAPSHOT_IDADE_MAX_DIAS):
    """Fotografia dos grupos promovidos tirada ANTES da virada: {"gerado_em", "linhas", "grupos": [...]}."""
    try:
        doc = json.loads(bruto)
        gerado = hora(doc["gerado_em"])
        grupos = {str(g) for g in doc["grupos"] if str(g).isdigit()}
        linhas = int(doc["linhas"]) if doc.get("linhas") else None
        if gerado is None or not grupos:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise Ilegivel("fotografia de grupos vigiados ilegível") from None
    if gerado > agora + timedelta(minutes=10):
        raise Ilegivel("fotografia de grupos vigiados datada do futuro")
    if agora - gerado > timedelta(days=max_dias):
        raise Ilegivel(f"fotografia de grupos vigiados velha demais (> {max_dias} dias)")
    return grupos, linhas


# ------------------------------------------------------------------ rede
def baixar(url, validar=None, tentativas=TENTATIVAS, espera=ESPERA, abrir=urllib.request.urlopen, dormir=time.sleep):
    """GET com até N tentativas (também quando o conteúdo baixado não passa em validar). Devolve (bytes, Last-Modified|None)."""
    if not str(url).startswith("https://"):
        raise Ilegivel("só endereço https")
    ultimo = "erro desconhecido"
    for n in range(1, tentativas + 1):
        try:
            req = urllib.request.Request(f"{url}{'&' if '?' in url else '?'}c={int(time.time())}{n}",
                                         headers={"Cache-Control": "no-cache"})
            with abrir(req, timeout=30) as r:
                bruto = r.read(MAX_BYTES + 1)
                lm = r.headers.get("Last-Modified") if getattr(r, "headers", None) else None
            if len(bruto) > MAX_BYTES:
                raise Ilegivel("arquivo grande demais")
            if validar:
                validar(bruto)
            try:
                lm = parsedate_to_datetime(lm) if lm else None
            except (TypeError, ValueError):
                lm = None
            return bruto, lm
        except Ilegivel as e:
            ultimo = str(e)
        except Exception as e:
            ultimo = f"falha de rede ({type(e).__name__})"
        if n < tentativas:
            dormir(espera * n)
    raise Ilegivel(f"não consegui baixar/validar após {tentativas} tentativas: {ultimo}")


# ------------------------------------------------------------------ decisão
def classificar(com_preco, agora, excecoes, registro=None, snapshot=None):
    """Separa os grupos com sale_price em: permitidos_excecao, permitidos_registro, vencidos_comprovados,
    vencidos_presumidos, novos (sem data, só aviso)."""
    ok = {gr for gr, ini, fim in excecoes if ini <= agora < fim + FOLGA}
    out = {"permitidos_excecao": [], "permitidos_registro": [], "vencidos_comprovados": [], "vencidos_presumidos": [], "novos": []}
    for g in com_preco:
        if g in ok:
            out["permitidos_excecao"].append(g)
        elif registro is not None and g in registro:
            ini, fim = registro[g]
            if agora >= fim + FOLGA:
                out["vencidos_comprovados"].append(g)
            elif (ini and ini > agora) or (ini and fim - ini > timedelta(days=45)):
                out["vencidos_presumidos"].append(g)
            else:
                out["permitidos_registro"].append(g)
        elif registro is not None:
            (out["vencidos_presumidos"] if snapshot is not None and g in snapshot else out["novos"]).append(g)
        elif snapshot is not None:
            (out["vencidos_presumidos"] if g in snapshot else out["novos"]).append(g)
        else:
            out["vencidos_presumidos"].append(g)
    return out


def avaliar(bruto_feed, agora, corte, excecoes, min_linhas=MIN_LINHAS, registro=None, snapshot=None, ref_linhas=None):
    """Devolve a classificação + invalidos + total de linhas. Levanta Ilegivel se o feed é suspeito."""
    cab, corpo = ler_feed(bruto_feed, min_linhas, ref_linhas)
    i, g, pr = cab.index("sale_price"), cab.index("group_id"), cab.index("price")
    com_linhas = [l for l in corpo if l[i].strip()]
    com_preco = sorted({l[g] for l in com_linhas})
    invalidos = sorted({l[g] for l in com_linhas if (_dinheiro(l[i]) is None or _dinheiro(l[pr]) is None
                                                      or not (0 < _dinheiro(l[i]) < _dinheiro(l[pr])))})
    r = {"antes_do_corte": agora < corte, "com_preco": com_preco, "invalidos": invalidos, "linhas": len(corpo)}
    if agora < corte:
        r.update({"permitidos_excecao": [], "permitidos_registro": [], "vencidos_comprovados": [], "vencidos_presumidos": [], "novos": []})
    else:
        r.update(classificar(com_preco, agora, excecoes, registro, snapshot))
    r["vencidos"] = sorted(set(r["vencidos_comprovados"]) | set(r["vencidos_presumidos"]))
    return r


def _ler_opcional(origem, tipo, leitor, notas, retry):
    """Lê registro/fotografia (arquivo ou https). Qualquer problema vira NOTA, nunca derruba o vigia."""
    if not origem:
        return None
    try:
        if origem.startswith("https://"):
            bruto, _ = baixar(origem, leitor, **retry)
        else:
            with open(origem, "rb") as f:
                bruto = f.read()
        return leitor(bruto)
    except (Ilegivel, OSError) as e:
        notas.append(f"{tipo} indisponível: {e}; seguindo sem ele")
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    fonte = ap.add_mutually_exclusive_group(required=True)
    fonte.add_argument("--feed-url")
    fonte.add_argument("--feed-arquivo")
    ap.add_argument("--excecoes", help="arquivo JSON de campanhas autorizadas")
    ap.add_argument("--registro", help="cópia pública do registro de vencimentos (arquivo ou https; opcional)")
    ap.add_argument("--snapshot", help="fotografia dos grupos promovidos antes da virada (arquivo ou https; opcional)")
    ap.add_argument("--agora", help="relógio simulado com fuso (ensaio)")
    ap.add_argument("--corte", default=CORTE_PADRAO)
    ap.add_argument("--limite-falha", default=LIMITE_PADRAO, help="a partir daqui problema comprovado FALHA (antes: só aviso)")
    ap.add_argument("--fim", default=FIM_PADRAO, help="depois daqui a vigilância termina (código 0)")
    ap.add_argument("--estrito", action="store_true", help="ensaio: trata qualquer horário como emergência")
    ap.add_argument("--min-linhas", type=int, default=MIN_LINHAS)
    ap.add_argument("--max-dias-excecao", type=int, default=45)
    ap.add_argument("--idade-feed-max-h", type=float, default=IDADE_FEED_MAX_H)
    ap.add_argument("--feed-modificado", help="ensaio: Last-Modified simulado do feed (com fuso)")
    ap.add_argument("--tentativas", type=int, default=TENTATIVAS)
    ap.add_argument("--espera", type=float, default=ESPERA, help="segundos entre tentativas (multiplica pela tentativa)")
    ap.add_argument("--imprimir-snapshot", action="store_true", help="imprime a fotografia (JSON) dos grupos com sale_price e sai")
    a = ap.parse_args(argv)
    retry = {"tentativas": max(1, a.tentativas), "espera": a.espera}
    notas, avisos, erros = [], [], []
    try:
        agora = hora(a.agora) if a.agora else datetime.now(timezone.utc)
        corte, limite, fim = hora(a.corte), hora(a.limite_falha), hora(a.fim)
        if None in (agora, corte, limite, fim) or limite < corte:
            raise Ilegivel("--agora/--corte/--limite-falha/--fim inválidos (precisam de fuso; limite não pode ser antes do corte)")
    except Ilegivel as e:
        print(f"FALHA (entrada suspeita): {e}", file=sys.stderr)
        return 2
    if agora >= fim and not a.imprimir_snapshot:
        print(f"ok: vigilância encerrada em {fim.isoformat()}")
        return 0
    nivel = nivel_de(agora, corte, limite, a.estrito)

    def encerrar(codigo, msg):
        """Antes do limite, o problema vira AVISO (código 0); na emergência, falha."""
        if nivel != "emergencia":
            print(f"::warning title=Vigia (nivel {nivel})::{msg}")
            print(f"AVISO (nível {nivel}; ainda sem alarme): {msg}")
            return 0
        print(f"::error title=Vigia FALHOU::{msg}")
        print(f"FALHA (nível emergência): {msg}", file=sys.stderr)
        return codigo

    try:
        exc = ler_excecoes(open(a.excecoes, "rb").read() if a.excecoes else None, a.max_dias_excecao)
    except (Ilegivel, OSError) as e:
        return encerrar(2, f"exceções ilegíveis: {e}")
    registro = _ler_opcional(a.registro, "registro de vencimentos", lambda b: ler_registro(b, agora), notas, retry)
    snap = _ler_opcional(a.snapshot, "fotografia de grupos vigiados", lambda b: ler_snapshot(b, agora), notas, retry)
    snapshot, ref_linhas = (snap if snap else (None, None))

    try:
        def validar(b):
            ler_feed(b, a.min_linhas, ref_linhas)
        lm = None
        if a.feed_url:
            bruto, lm = baixar(a.feed_url, validar, **retry)
        else:
            with open(a.feed_arquivo, "rb") as f:
                bruto = f.read()
            validar(bruto)
        if a.feed_modificado:
            lm = hora(a.feed_modificado)
        r = avaliar(bruto, agora, corte, exc, a.min_linhas, registro, snapshot, ref_linhas)
    except (Ilegivel, OSError) as e:
        for n in notas:
            print(f"nota: {n}")
        return encerrar(2, f"feed suspeito ou inacessível: {e}")

    if a.imprimir_snapshot:
        print(json.dumps({"gerado_em": datetime.now(timezone.utc).isoformat(), "linhas": r["linhas"], "grupos": r["com_preco"]}))
        return 0
    for n in notas:
        print(f"nota: {n}")
    idade_h = (agora - lm).total_seconds() / 3600 if lm else None
    parado = idade_h is not None and idade_h > a.idade_feed_max_h
    print(f"nível {nivel}; relógio {agora.isoformat()}; linhas {r['linhas']}; com sale_price {len(r['com_preco'])}; "
          f"registro {'sim' if registro is not None else 'não'}; fotografia {'sim' if snapshot is not None else 'não'}; "
          f"idade do feed {'?' if idade_h is None else f'{idade_h:.1f} h'}")
    if r["antes_do_corte"]:
        if r["invalidos"]:
            print(f"AVISO: {len(r['invalidos'])} grupo(s) com preço promocional inválido")
        print(f"ok: antes do corte ({corte.isoformat()}); {len(r['com_preco'])} grupo(s) com sale_price (normal até a virada)")
        if parado:
            print(f"::warning title=Vigia::feed público sem republicar há {idade_h:.1f} h (só aviso)")
        return 0
    print(f"autorizados por exceção: {len(r['permitidos_excecao'])}; por registro: {len(r['permitidos_registro'])}; "
          f"novos sem data: {len(r['novos'])}")
    base = max(len(registro) if registro else 0, len(snapshot) if snapshot else 0)
    novos_demais = len(r["novos"]) > max(NOVOS_MAX_ABS, NOVOS_MAX_FRACAO * base)
    causa = f" (feed sem republicar há {idade_h:.1f} h)" if parado else ""
    if r["vencidos_comprovados"] or r["vencidos_presumidos"]:
        partes = []
        if r["vencidos_comprovados"]:
            partes.append(f"{len(r['vencidos_comprovados'])} comprovado(s) pelo registro")
        if r["vencidos_presumidos"]:
            partes.append(f"{len(r['vencidos_presumidos'])} presumido(s) sem data confirmada")
        return encerrar(1, f"PROMOÇÃO VENCIDA NO FEED: {len(r['vencidos'])} grupo(s) com sale_price ({'; '.join(partes)}){causa}. "
                           f"Amostra: {', '.join(r['vencidos'][:20])}")
    if novos_demais:
        return encerrar(1, f"PROMOÇÃO SEM DATA CONFIRMADA em massa: {len(r['novos'])} grupo(s) novos com sale_price fora do registro{causa}. "
                           f"Amostra: {', '.join(r['novos'][:20])}")
    if r["invalidos"]:
        return encerrar(1, f"PREÇO PROMOCIONAL INVÁLIDO (ilegível ou não menor que o preço normal): {len(r['invalidos'])} grupo(s). "
                           f"Amostra: {', '.join(r['invalidos'][:20])}")
    if parado:
        print(f"::warning title=Vigia::feed público sem republicar há {idade_h:.1f} h (limite de aviso {a.idade_feed_max_h:g} h); "
              f"não é falha: o feed só é republicado quando algo muda")
    if r["novos"]:
        print(f"::warning title=Vigia::{len(r['novos'])} grupo(s) novos com sale_price sem data no registro (campanha nova?): "
              f"{', '.join(r['novos'][:20])}")
    print("ok: nenhum sale_price vencido")
    return 0


if __name__ == "__main__":
    sys.exit(main())
