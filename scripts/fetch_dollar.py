#!/usr/bin/env python3
"""
Busca a cotacao do dolar (serie 10813 do Banco Central - dolar comercial venda)
e grava numa lista do SharePoint usando a Microsoft Graph API (autenticacao
app-only por client secret).

Variaveis de ambiente obrigatorias:
  TENANT_ID, CLIENT_ID, CLIENT_SECRET   -> autenticacao no Graph
  SHAREPOINT_SITE_URL                   -> ex.: https://contoso.sharepoint.com/sites/Financeiro
  SHAREPOINT_LIST_NAME                  -> nome (displayName) da lista

Modos de execucao:
  * Diario (padrao): janela dos ultimos dias; grava o que ainda nao existir.
  * Backfill: definindo BACKFILL_START (YYYY-MM-DD) e, opcionalmente,
    BACKFILL_END (YYYY-MM-DD; vazio = hoje), percorre todo o periodo.

Em ambos os modos ha deduplicacao: datas ja presentes na lista sao puladas,
entao rodar de novo nunca cria duplicados.
"""
import os
import sys
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import requests

BCB_SERIE = "10813"  # Dolar comercial (venda) - diario
FONTE = "Banco Central - API"
GRAPH = "https://graph.microsoft.com/v1.0"


# --------------------------------------------------------------------------- #
# Banco Central
# --------------------------------------------------------------------------- #
def buscar_periodo(inicio: date, fim: date):
    """Retorna lista de {'data': 'dd/mm/aaaa', 'valor': float} do BCB."""
    url = (
        f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{BCB_SERIE}/dados"
        f"?formato=json"
        f"&dataInicial={inicio.strftime('%d/%m/%Y')}"
        f"&dataFinal={fim.strftime('%d/%m/%Y')}"
    )
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return [
        {"data": item["data"], "valor": float(str(item["valor"]).replace(",", "."))}
        for item in resp.json()
    ]


# --------------------------------------------------------------------------- #
# Microsoft Graph
# --------------------------------------------------------------------------- #
def obter_token(tenant: str, client_id: str, client_secret: str) -> str:
    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    dados = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    resp = requests.post(url, data=dados, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Falha ao obter token do Graph: {resp.status_code} {resp.text}")
    return resp.json()["access_token"]


def resolver_site(sess: requests.Session, site_url: str) -> str:
    """Resolve o siteId a partir da URL do site."""
    p = urlparse(site_url)
    host = p.netloc
    caminho = p.path.rstrip("/")
    resp = sess.get(f"{GRAPH}/sites/{host}:{caminho}", timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Falha ao resolver o site '{site_url}': {resp.status_code} {resp.text}")
    return resp.json()["id"]


def resolver_lista(sess: requests.Session, site_id: str, nome: str) -> str:
    resp = sess.get(
        f"{GRAPH}/sites/{site_id}/lists",
        params={"$filter": f"displayName eq '{nome}'", "$select": "id,displayName"},
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Falha ao listar listas do site: {resp.status_code} {resp.text}")
    itens = resp.json().get("value", [])
    if not itens:
        raise RuntimeError(f"Lista '{nome}' nao encontrada no site.")
    return itens[0]["id"]


def datas_existentes(sess: requests.Session, site_id: str, list_id: str) -> set:
    """Datas (dd/mm/aaaa) ja gravadas na lista, para deduplicar."""
    datas = set()
    url = f"{GRAPH}/sites/{site_id}/lists/{list_id}/items"
    params = {"$expand": "fields($select=DataCotacao)", "$top": "5000"}
    while url:
        resp = sess.get(url, params=params, timeout=120)
        if resp.status_code != 200:
            print(f"AVISO: falha ao listar itens existentes: {resp.status_code} {resp.text}")
            return datas
        corpo = resp.json()
        for item in corpo.get("value", []):
            valor = (item.get("fields") or {}).get("DataCotacao")
            if valor:
                datas.add(_normalizar_data(valor))
        url = corpo.get("@odata.nextLink")
        params = None  # o nextLink ja traz os parametros
    return datas


def _normalizar_data(valor: str) -> str:
    """Converte qualquer formato devolvido pelo Graph para dd/mm/aaaa."""
    valor = str(valor).strip()
    for bruto in (valor[:10], valor):
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(bruto, fmt).strftime("%d/%m/%Y")
            except ValueError:
                continue
    return valor


def _para_iso(data_ddmmyyyy: str) -> str:
    """dd/mm/aaaa -> ISO 8601 (meio-dia UTC, para a data nao escorregar de dia)."""
    d = datetime.strptime(data_ddmmyyyy, "%d/%m/%Y")
    return d.strftime("%Y-%m-%dT12:00:00Z")


def purgar_insercoes_do_dia(sess: requests.Session, site_id: str, list_id: str, dia: date) -> int:
    """Remove itens cujo DataColeta e do dia informado (limpa tentativas parciais)."""
    alvo = dia.strftime("%Y-%m-%d")
    url = f"{GRAPH}/sites/{site_id}/lists/{list_id}/items"
    params = {"$expand": "fields($select=DataColeta)", "$top": "5000"}
    ids = []
    while url:
        resp = sess.get(url, params=params, timeout=120)
        if resp.status_code != 200:
            print(f"AVISO: falha ao listar para limpeza: {resp.status_code} {resp.text}")
            return 0
        corpo = resp.json()
        for item in corpo.get("value", []):
            dc = (item.get("fields") or {}).get("DataColeta")
            if dc and str(dc)[:10] == alvo:
                ids.append(item["id"])
        url = corpo.get("@odata.nextLink")
        params = None
    for item_id in ids:
        r = sess.delete(f"{GRAPH}/sites/{site_id}/lists/{list_id}/items/{item_id}", timeout=60)
        if r.status_code not in (200, 204):
            print(f"AVISO: falha ao remover item {item_id}: {r.status_code} {r.text}")
    return len(ids)


def gravar_item(sess: requests.Session, site_id: str, list_id: str, cotacao: dict) -> None:
    data_coleta = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "fields": {
            "Title": f"Cotacao {cotacao['data']}",
            "DataCotacao": _para_iso(cotacao["data"]),
            "Valor": cotacao["valor"],
            "Fonte": FONTE,
            "DataColeta": data_coleta,
        }
    }
    resp = sess.post(
        f"{GRAPH}/sites/{site_id}/lists/{list_id}/items", json=payload, timeout=60
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Erro ao gravar {cotacao['data']}: {resp.status_code} {resp.text}"
        )


# --------------------------------------------------------------------------- #
def _data_env(nome: str):
    valor = (os.environ.get(nome) or "").strip()
    if not valor:
        return None
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except ValueError:
        print(f"ERRO: {nome}='{valor}' invalido. Use o formato YYYY-MM-DD.")
        sys.exit(1)


def _data_env_opcional(nome: str):
    """Como _data_env, mas nao sai do processo em caso de erro (uso em passos auxiliares)."""
    valor = (os.environ.get(nome) or "").strip()
    if not valor:
        return None
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except ValueError:
        print(f"AVISO: {nome}='{valor}' invalido (esperado YYYY-MM-DD); ignorando.")
        return None


def main() -> None:
    tenant = os.environ.get("TENANT_ID")
    client_id = os.environ.get("CLIENT_ID")
    client_secret = os.environ.get("CLIENT_SECRET")
    site_url = os.environ.get("SHAREPOINT_SITE_URL")
    lista = os.environ.get("SHAREPOINT_LIST_NAME")
    faltando = [
        n
        for n, v in (
            ("TENANT_ID", tenant),
            ("CLIENT_ID", client_id),
            ("CLIENT_SECRET", client_secret),
            ("SHAREPOINT_SITE_URL", site_url),
            ("SHAREPOINT_LIST_NAME", lista),
        )
        if not v
    ]
    if faltando:
        print(f"ERRO: variaveis obrigatorias ausentes: {', '.join(faltando)}.")
        sys.exit(1)

    hoje = date.today()
    inicio = _data_env("BACKFILL_START")
    if inicio:
        fim = _data_env("BACKFILL_END") or hoje
        print(f"Modo BACKFILL: de {inicio} ate {fim}")
    else:
        inicio = hoje - timedelta(days=7)
        fim = hoje
        print(f"Modo DIARIO: janela de {inicio} ate {fim}")

    cotacoes = buscar_periodo(inicio, fim)
    if not cotacoes:
        print("Nenhuma cotacao retornada pelo BCB no periodo (fim de semana/feriado?).")
        return

    token = obter_token(tenant, client_id, client_secret)
    sess = requests.Session()
    sess.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})

    site_id = resolver_site(sess, site_url)
    list_id = resolver_lista(sess, site_id, lista)

    purge_dia = _data_env_opcional("PURGE_DATACOLETA_DAY")
    if purge_dia:
        removidos = purgar_insercoes_do_dia(sess, site_id, list_id, purge_dia)
        print(f"Limpeza: {removidos} item(ns) removido(s) com DataColeta de {purge_dia}.")

    ja_existem = datas_existentes(sess, site_id, list_id)
    print(f"{len(ja_existem)} data(s) ja na lista; {len(cotacoes)} no periodo do BCB.")

    novos = 0
    for cotacao in cotacoes:
        if cotacao["data"] in ja_existem:
            print(f"  = {cotacao['data']} ja existe, pulando.")
            continue
        gravar_item(sess, site_id, list_id, cotacao)
        print(f"  + {cotacao['data']} -> R$ {cotacao['valor']}")
        novos += 1

    print(f"Concluido. {novos} cotacao(oes) nova(s) gravada(s).")


if __name__ == "__main__":
    main()
