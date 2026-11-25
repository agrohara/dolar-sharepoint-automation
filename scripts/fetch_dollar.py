#!/usr/bin/env python3
"""
Busca a cotação do dólar (série 10813 do Banco Central - dólar comercial venda)
e grava numa lista do SharePoint usando a CLI do Microsoft 365 (m365).

Modos de execução:
  * Diário (padrão): busca a janela dos últimos dias e grava o que ainda não
    existir na lista (cobre feriados e dias sem rodar).
  * Backfill: defina a variável de ambiente BACKFILL_START (YYYY-MM-DD) e,
    opcionalmente, BACKFILL_END (YYYY-MM-DD; vazio = hoje). O script percorre
    todo o período e grava cada dia útil que ainda não estiver na lista.

Em ambos os modos há deduplicação: datas já presentes na lista são puladas,
então rodar de novo nunca cria duplicados.
"""
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta

import requests

BCB_SERIE = "10813"  # Dólar comercial (venda) - diário
FONTE = "Banco Central - API"


def _bcb_url(inicio: date, fim: date) -> str:
    return (
        f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{BCB_SERIE}/dados"
        f"?formato=json"
        f"&dataInicial={inicio.strftime('%d/%m/%Y')}"
        f"&dataFinal={fim.strftime('%d/%m/%Y')}"
    )


def buscar_periodo(inicio: date, fim: date):
    """Retorna lista de {'data': 'dd/mm/aaaa', 'valor': float} do BCB."""
    resp = requests.get(_bcb_url(inicio, fim), timeout=60)
    resp.raise_for_status()
    dados = resp.json()
    cotacoes = []
    for item in dados:
        cotacoes.append(
            {"data": item["data"], "valor": float(str(item["valor"]).replace(",", "."))}
        )
    return cotacoes


def _normalizar_data(valor: str) -> str:
    """Converte qualquer formato devolvido pelo SharePoint para dd/mm/aaaa."""
    valor = str(valor).strip()
    # tenta pegar só a parte da data em ISO (ex.: 2026-03-05T00:00:00Z)
    candidatos = (valor[:10], valor)
    for bruto in candidatos:
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(bruto, fmt).strftime("%d/%m/%Y")
            except ValueError:
                continue
    return valor  # devolve como veio se não reconhecer


def datas_existentes(site_url: str, lista: str) -> set:
    """Datas (dd/mm/aaaa) já gravadas na lista, para deduplicar."""
    cmd = [
        "m365", "spo", "listitem", "list",
        "--webUrl", site_url,
        "--listTitle", lista,
        "--fields", "DataCotacao",
        "--output", "json",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception as exc:  # noqa: BLE001
        print(f"AVISO: nao foi possivel consultar itens existentes ({exc}).")
        return set()
    if res.returncode != 0:
        print(f"AVISO: falha ao listar itens existentes: {res.stderr.strip()}")
        return set()
    try:
        itens = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        print("AVISO: resposta da listagem nao e JSON valido.")
        return set()
    return {
        _normalizar_data(it["DataCotacao"])
        for it in itens
        if it.get("DataCotacao")
    }


def gravar_item(site_url: str, lista: str, cotacao: dict) -> None:
    """Adiciona uma cotação na lista. Lança erro se o m365 falhar."""
    data_coleta = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cmd = [
        "m365", "spo", "listitem", "add",
        "--webUrl", site_url,
        "--listTitle", lista,
        "--Title", f"Cotacao {cotacao['data']}",
        "--DataCotacao", cotacao["data"],
        "--Valor", str(cotacao["valor"]),
        "--Fonte", FONTE,
        "--DataColeta", data_coleta,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(
            f"Erro ao gravar {cotacao['data']}: {res.stderr.strip() or res.stdout.strip()}"
        )


def _data_env(nome: str):
    valor = (os.environ.get(nome) or "").strip()
    if not valor:
        return None
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except ValueError:
        print(f"ERRO: {nome}='{valor}' invalido. Use o formato YYYY-MM-DD.")
        sys.exit(1)


def main() -> None:
    site_url = os.environ.get("SHAREPOINT_SITE_URL")
    lista = os.environ.get("SHAREPOINT_LIST_NAME")
    if not site_url or not lista:
        print("ERRO: SHAREPOINT_SITE_URL e SHAREPOINT_LIST_NAME sao obrigatorios.")
        sys.exit(1)

    hoje = date.today()
    inicio = _data_env("BACKFILL_START")
    if inicio:
        fim = _data_env("BACKFILL_END") or hoje
        print(f"Modo BACKFILL: de {inicio} ate {fim}")
    else:
        inicio = hoje - timedelta(days=7)  # janela de folga p/ feriados/dias sem rodar
        fim = hoje
        print(f"Modo DIARIO: janela de {inicio} ate {fim}")

    cotacoes = buscar_periodo(inicio, fim)
    if not cotacoes:
        print("Nenhuma cotacao retornada pelo BCB no periodo (fim de semana/feriado?).")
        return

    ja_existem = datas_existentes(site_url, lista)
    print(f"{len(ja_existem)} data(s) ja na lista; {len(cotacoes)} no periodo do BCB.")

    novos = 0
    for cotacao in cotacoes:
        if cotacao["data"] in ja_existem:
            print(f"  = {cotacao['data']} ja existe, pulando.")
            continue
        gravar_item(site_url, lista, cotacao)
        print(f"  + {cotacao['data']} -> R$ {cotacao['valor']}")
        novos += 1

    print(f"Concluido. {novos} cotacao(oes) nova(s) gravada(s).")


if __name__ == "__main__":
    main()
