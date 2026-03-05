import requests
import json
from datetime import datetime
import os

def get_dollar_rate():
    """Busca a cotação do dólar na API do Banco Central"""
    url = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.10813/dados/ultimos/1?formato=json"
    
    response = requests.get(url)
    data = response.json()
    
    return {
        "data": data[0]["data"],
        "valor": float(data[0]["valor"]),
        "data_coleta": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fonte": "Banco Central - API"
    }

def post_to_sharepoint(cotacao):
    """Envia os dados para o SharePoint"""
    comando = f'''m365 spo listitem add \\
        --webUrl "{os.environ['SHAREPOINT_SITE_URL']}" \\
        --listTitle "{os.environ['SHAREPOINT_LIST_NAME']}" \\
        --Title "Cotação {cotacao['data']}" \\
        --DataCotacao "{cotacao['data']}" \\
        --Valor "{cotacao['valor']}" \\
        --Fonte "{cotacao['fonte']}" \\
        --DataColeta "{cotacao['data_coleta']}"'''
    
    print("📤 Enviando para SharePoint...")
    os.system(comando)
    print("✅ Dados enviados ao SharePoint!")

def main():
    print("🔄 Buscando cotação do dólar...")
    cotacao = get_dollar_rate()
    
    print(f"📅 Data: {cotacao['data']}")
    print(f"💰 Valor: R$ {cotacao['valor']}")
    
    post_to_sharepoint(cotacao)
    
    print("✅ Processo concluído!")

if __name__ == "__main__":
    main()
