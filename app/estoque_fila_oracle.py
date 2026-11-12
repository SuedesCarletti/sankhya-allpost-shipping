import os
import time
import threading
import logging
import requests

from allpost_sync_service import (
    consultar_produtos_sankhya,
    enviar_para_allpost,
    MAX_PRODUTOS_POR_BATCH,
    SANKHYA_API_URL,
)
from routes import get_isolated_bearer_token, logout_sankhya, extract_value, rate_limit_control

logger = logging.getLogger(__name__)

# View registrada no Sankhya que expõe CODPRODs com movimentação recente nos
# locais 201 e 401. Usa TGFITE + TGFCAB para detectar movimentos recentes
# e TGFEST para filtrar locais — CODLOCAL não existe em TGFITE nem TGFCAB.
#
#   CREATE OR REPLACE VIEW VW_FILA_SYNC_ESTOQUE AS
#   SELECT DISTINCT i.CODPROD
#   FROM EMPRESAX.TGFITE i
#   JOIN EMPRESAX.TGFCAB c ON c.NUNOTA = i.NUNOTA
#   WHERE c.DTFATUR >= SYSDATE - (5/1440)        -- últimos 5 minutos
#     AND i.CODPROD IN (
#         SELECT CODPROD FROM EMPRESAX.TGFEST
#         WHERE CODLOCAL IN (201, 401) AND ATIVO = 'S'
#     );
#   -- CODLOCAL não existe em TGFITE nem TGFCAB; filtramos via TGFEST que tem a coluna.
#
# Janela de 5 min cobre 5 ciclos de polling (60 s cada) com folga.
# Reenviar o mesmo saldo ao AllPost é idempotente — não há risco em sobrepor ciclos.
VW_FILA_ESTOQUE = os.getenv('SANKHYA_VW_FILA_ESTOQUE', 'VW_FILA_SYNC_ESTOQUE')

_URL = f"{SANKHYA_API_URL}/gateway/v1/mge/service.sbr"

# ==================== CONSULTA DA FILA VIA API ====================

def consultar_fila_pendente(bearer_token: str) -> list[int]:
    """
    Consulta VW_FILA_SYNC_ESTOQUE via CRUDServiceProvider.loadView.
    Retorna lista de CODPRODs únicos com saldo alterado recentemente.
    """
    payload = {
        "serviceName": "CRUDServiceProvider.loadView",
        "requestBody": {
            "query": {
                "viewName": VW_FILA_ESTOQUE,
            }
        },
    }
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
    }

    try:
        rate_limit_control()
        response = requests.post(
            _URL,
            params={"serviceName": "CRUDServiceProvider.loadView", "outputType": "json"},
            json=payload,
            headers=headers,
            timeout=30,
        )

        if response.status_code in (401, 403):
            logger.error(f"Token Sankhya inválido ao consultar {VW_FILA_ESTOQUE} ({response.status_code})")
            return []

        response.raise_for_status()
        dados = response.json()

        records = (
            dados.get("responseBody", {}).get("entities", {}).get("entity", [])
            or dados.get("responseBody", {}).get("records", {}).get("record", [])
            or []
        )
        if not isinstance(records, list):
            records = [records]

        codprods = []
        for rec in records:
            val = extract_value(rec.get("CODPROD"))
            if val:
                codprods.append(int(val))

        return codprods

    except Exception as e:
        logger.error(f"Erro ao consultar {VW_FILA_ESTOQUE}: {e}", exc_info=True)
        return []


# ==================== PROCESSAMENTO PRINCIPAL ====================

def processar_fila_estoque():
    """
    Polling da fila de estoque via Sankhya API:
      1. Consulta VW_FILA_SYNC_ESTOQUE — produtos com saldo alterado (janela de 5 min)
      2. Busca dados completos via consultar_produtos_sankhya()
      3. Envia para AllPost via enviar_para_allpost()

    Sem marcação de "processado": a view usa janela de tempo (DTALTER),
    e o reenvio do mesmo saldo ao AllPost é idempotente.

    NOTA: Usa get_isolated_bearer_token() — NÃO atualiza o token global em cache.
    Isso evita que o logout desta thread invalide o token usado pelo fluxo de
    cotações (consultar_pedidos_para_cotacao → get_cached_token), o que causaria
    falha silenciosa na atualização do TGFCAB.
    """
    bearer_token = None
    try:
        bearer_token = get_isolated_bearer_token()
        if not bearer_token:
            logger.error("Falha ao obter token Sankhya. Fila de estoque não processada.")
            return

        codprods = consultar_fila_pendente(bearer_token)
        if not codprods:
            return

        # Deduplica — a view já retorna DISTINCT, mas garantimos aqui também
        codprods = list(set(codprods))
        logger.info(f"Fila de estoque: {len(codprods)} produto(s) com saldo alterado")

        total_enviados = 0
        total_erros = 0

        for i in range(0, len(codprods), MAX_PRODUTOS_POR_BATCH):
            lote = codprods[i : i + MAX_PRODUTOS_POR_BATCH]
            try:
                sucesso, produtos_dados = consultar_produtos_sankhya(lote, bearer_token)
                if not sucesso or not produtos_dados:
                    logger.error(f"Falha ao consultar Sankhya para lote {lote}")
                    total_erros += len(lote)
                    continue

                resultado = enviar_para_allpost(produtos_dados)
                if resultado["sucesso"]:
                    total_enviados += len(produtos_dados)
                    logger.info(f"Lote sincronizado: {len(produtos_dados)} produto(s)")
                else:
                    total_erros += len(lote)
                    logger.error(f"Erro AllPost para lote {lote}: {resultado.get('erro')}")

            except Exception as e:
                total_erros += len(lote)
                logger.error(f"Erro ao processar lote {lote}: {e}")

        logger.info(f"Fila processada: {total_enviados} enviados, {total_erros} erros")

    except Exception as e:
        logger.error(f"Erro grave ao processar fila de estoque: {e}", exc_info=True)
    finally:
        if bearer_token:
            logout_sankhya(bearer_token)


# ==================== WORKER ====================

_fila_thread = None
_fila_lock = threading.Lock()
_fila_initialized = threading.Event()


def iniciar_polling_fila_estoque():
    """
    Inicia thread daemon de polling da fila de estoque (a cada 60 segundos).
    Mesmo padrão do worker_processar_buffer do allpost_sync_service.
    """
    global _fila_thread

    with _fila_lock:
        if _fila_initialized.is_set():
            logger.warning("Polling de fila de estoque já inicializado. Ignorando.")
            return _fila_thread

        def _loop():
            logger.info("Thread de polling da fila de estoque iniciada.")
            processar_fila_estoque()
            while True:
                time.sleep(60)
                processar_fila_estoque()

        _fila_thread = threading.Thread(target=_loop, daemon=True)
        _fila_thread.start()
        _fila_initialized.set()
        logger.info("Polling da fila de estoque iniciado em background")
        return _fila_thread
