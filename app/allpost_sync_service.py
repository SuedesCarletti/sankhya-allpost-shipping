import os
import time
import threading
import logging
import sys
from datetime import datetime
from typing import Dict, List, Set, Tuple
from collections import defaultdict
import requests
from flask import request, jsonify, Blueprint
from dotenv import load_dotenv

# Importar funções de autenticação do routes.py
# Para uma arquitetura melhor, isso seria movido para um módulo compartilhado (ex: sankhya_client.py)
from routes import get_new_bearer_token, logout_sankhya, extract_value

load_dotenv()

# ==================== BLUEPRINT ====================
sync_service_bp = Blueprint('sync_service_bp', __name__)

# ==================== CONFIGURAÇÕES ====================
ALLPOST_API_URL = os.getenv('ALLPOST_API_URL', 'https://www.allpost.com.br/api/v1/produto')
# CORREÇÃO: Adicionar a variável para o Token de API
ALLPOST_API_TOKEN = os.getenv('ALLPOST_API_TOKEN', '')
ALLPOST_APP_TOKEN = os.getenv('ALLPOST_APP_TOKEN', '')
SANKHYA_API_URL = os.getenv('SANKHYA_API_URL', 'https://api.sankhya.com.br')
WEBHOOK_SECRET_KEY = os.getenv('WEBHOOK_SECRET_KEY', '')  # obrigatório: sem ele os endpoints recusam tudo


def _secret_ok(candidate):
    import hmac
    return bool(WEBHOOK_SECRET_KEY) and hmac.compare_digest(str(candidate or ''), WEBHOOK_SECRET_KEY)

# Configurações do Buffer
BUFFER_INTERVAL_SECONDS = 3  # Processa buffer a cada 3 segundos
MAX_PRODUTOS_POR_BATCH = 50  # Máximo de produtos por requisição AllPost
RATE_LIMIT_PER_MINUTE = 300  # Limite da API AllPost

# ==================== LOGGING ====================
# CORREÇÃO: Direcionar explicitamente o stream para sys.stdout para evitar que logs INFO sejam interpretados como ERRORS no Railway/Gunicorn.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# ==================== BUFFER GLOBAL ====================
class BufferProdutos:
    """Buffer thread-safe para acumular produtos antes de enviar"""
    
    def __init__(self):
        self.buffer: Set[int] = set()  # Set de CODPRODs únicos
        self.lock = threading.Lock()
        self.stats = {
            'produtos_recebidos': 0,
            'produtos_enviados': 0,
            'lotes_enviados': 0,
            'erros': 0,
            'ultimo_envio': None
        }
    
    def adicionar(self, codprod: int):
        """Adiciona produto ao buffer"""
        with self.lock:
            self.buffer.add(codprod)
            self.stats['produtos_recebidos'] += 1
            logger.info(f"Produto {codprod} adicionado ao buffer. Total no buffer: {len(self.buffer)}")
    
    def obter_e_limpar(self) -> List[int]:
        """Obtém produtos do buffer e limpa"""
        with self.lock:
            if not self.buffer:
                return []
            
            produtos = list(self.buffer)
            self.buffer.clear()
            logger.info(f"Obtendo {len(produtos)} produtos do buffer para processar")
            return produtos
    
    def atualizar_stats(self, enviados: int, lotes: int, erros: int):
        """Atualiza estatísticas"""
        with self.lock:
            self.stats['produtos_enviados'] += enviados
            self.stats['lotes_enviados'] += lotes
            self.stats['erros'] += erros
            self.stats['ultimo_envio'] = datetime.now().isoformat()
    
    def obter_stats(self) -> dict:
        """Retorna estatísticas"""
        with self.lock:
            return {
                'buffer_atual': len(self.buffer),
                **self.stats
            }

# Instncia global do buffer
buffer_global = BufferProdutos()

# ==================== CONSULTA SANKHYA (CORRIGIDO COM VIEW) ====================
def consultar_produtos_sankhya(codprods: List[int], bearer_token: str) -> Tuple[bool, List[Dict]]:
    """
    Consulta dados completos dos produtos no Sankhya usando uma View customizada (VW_PRODUTOS_ESTOQUE_ALLPOST).
    Retorna uma tupla (sucesso, dados_dos_produtos)
    """
    if not bearer_token:
        logger.error("Token de sessão não fornecido para consulta Sankhya.")
        return False, []

    try:
        codprods_str = ','.join(str(c) for c in codprods)
        
        payload = {
            "serviceName": "CRUDServiceProvider.loadView",
            "requestBody": {
                "query": {
                    "viewName": "VW_PRODUTOS_ESTOQUE_ALLPOST",
                    "where": {
                        "$": f"CODPROD IN ({codprods_str})"
                    }
                }
            }
        }
        
        headers = {
            'Authorization': f'Bearer {bearer_token}', # USA O TOKEN DE SESSÃO
            'Content-Type': 'application/json'
        }
        
        url = f"{SANKHYA_API_URL}/gateway/v1/mge/service.sbr?serviceName=CRUDServiceProvider.loadView&outputType=json"
        
        logger.info(f"Consultando {len(codprods)} produtos no Sankhya via View...")
        response = requests.post(url, json=payload, headers=headers, timeout=30)

        if response.status_code in [401, 403]:
            logger.error(f"Erro de Autenticação/Autorização ({response.status_code}) ao consultar Sankhya. O token de sessão pode ter expirado.")
            return False, []

        response.raise_for_status()
        
        dados = response.json()
        
        # CORREÇÃO: Extração robusta dos dados, tratando diferentes formatos de resposta da API Sankhya.
        records = (
            dados.get('responseBody', {}).get('entities', {}).get('entity', []) or
            dados.get('responseBody', {}).get('records', {}).get('record', []) or
            []
        )
        if not isinstance(records, list):
            records = [records]

        produtos = []
        for record in records:
            # Usamos a função 'extract_value' para lidar com o formato {'$': 'valor'}
            produto = {
                'codprod': int(extract_value(record.get('CODPROD'))),
                'titulo': extract_value(record.get('TITULO')) or f"Produto {extract_value(record.get('CODPROD'))}",
                'peso': float(extract_value(record.get('PESO_KG'))),
                'altura': float(extract_value(record.get('ALTURA_CM'))),
                'largura': float(extract_value(record.get('LARGURA_CM'))),
                'profundidade': float(extract_value(record.get('PROFUNDIDADE_CM'))),
                'preco': float(extract_value(record.get('PRECO'))),
                'estoque_201': int(extract_value(record.get('ESTOQUE_LOCAL_201'))),
                'estoque_401': int(extract_value(record.get('ESTOQUE_LOCAL_401'))),
                'ativo': extract_value(record.get('STATUS_PRODUTO')) == 'S'
            }
            produtos.append(produto)
        
        logger.info(f"{len(produtos)} produtos consultados com sucesso")
        return True, produtos
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Erro de conexão ao consultar produtos no Sankhya: {e}", exc_info=True)
        return False, []
    except Exception as e:
        logger.error(f"Erro inesperado ao consultar produtos no Sankhya: {e}", exc_info=True)
        return False, []
        
# ==================== ENVIO PARA ALLPOST ====================

def enviar_para_allpost(produtos_dados: List[Dict]) -> Dict:
    """Envia produtos para AllPost em formato batch"""
    try:
        if not produtos_dados:
            return {'sucesso': False, 'erro': 'Nenhum produto para enviar'}
        
        produtos_allpost = []
        for prod in produtos_dados:
            filiais = []
            if prod['estoque_201'] >= 0:
                filiais.append({"idFilial": "1", "quantidade": prod['estoque_201'], "prazoAdicional": 0})
            if prod['estoque_401'] >= 0:
                filiais.append({"idFilial": "6", "quantidade": prod['estoque_401'], "prazoAdicional": 0})
            
            produto_allpost = {
                "sku": str(prod['codprod']),
                "skuERP": str(prod['codprod']),
                "titulo": prod['titulo'][:255],
                "peso": round(prod['peso'], 3),
                "largura": round(prod['largura'], 3),
                "altura": round(prod['altura'], 3),
                "profundidade": round(prod['profundidade'], 3),
                "preco": round(prod['preco'], 2),
                "status": "true" if prod['ativo'] else "false",
                "filial": filiais
            }
            produtos_allpost.append(produto_allpost)
        
        payload = {"produto": produtos_allpost}
        # CORREÇÃO: Utilizar ALLPOST_API_TOKEN em vez de ALLPOST_APP_TOKEN
        headers = {'Authorization': f'Bearer {ALLPOST_API_TOKEN}', 'Content-Type': 'application/json'}
        
        logger.info(f"Enviando {len(produtos_allpost)} produtos para AllPost...")
        response = requests.post(ALLPOST_API_URL, json=payload, headers=headers, timeout=30)
        
        if response.status_code == 200:
            logger.info(f"{len(produtos_allpost)} produtos enviados com sucesso para AllPost")
            return {'sucesso': True, 'produtos_enviados': len(produtos_allpost), 'resposta_allpost': response.json()}
        else:
            logger.error(f"Erro AllPost: {response.status_code} - {response.text}")
            return {'sucesso': False, 'erro': f"Status {response.status_code}", 'detalhes': response.text}
            
    except Exception as e:
        logger.error(f"Erro ao enviar para AllPost: {e}", exc_info=True)
        return {'sucesso': False, 'erro': str(e)}

# ==================== WORKER DO BUFFER ====================
def worker_processar_buffer():
    """Worker que roda em background processando o buffer periodicamente"""
    logger.info("Worker do buffer de sincronização iniciado")
    while True:
        sankhya_token = None
        try:
            time.sleep(BUFFER_INTERVAL_SECONDS)
            codprods = buffer_global.obter_e_limpar()
            if not codprods:
                continue
            
            logger.info(f"Processando {len(codprods)} produtos do buffer...")
            total_enviados, total_erros, lotes = 0, 0, 0

            # 1. FAZER LOGIN PARA OBTER TOKEN DE SESSÃO
            sankhya_token = get_new_bearer_token()
            if not sankhya_token:
                logger.error("Falha ao obter token de sessão do Sankhya. Abortando ciclo.")
                buffer_global.atualizar_stats(0, 0, len(codprods))
                continue
            
            for i in range(0, len(codprods), MAX_PRODUTOS_POR_BATCH):
                lote = codprods[i:i + MAX_PRODUTOS_POR_BATCH]
                # 2. USAR O TOKEN DE SESSÃO NA CONSULTA
                sucesso_sankhya, produtos_dados = consultar_produtos_sankhya(lote, sankhya_token)
                
                if not sucesso_sankhya:
                    logger.error(f"Falha na comunicação com Sankhya para o lote {lote}. O lote será ignorado.")
                    total_erros += len(lote)
                    continue

                if not produtos_dados:
                    logger.warning(f"Nenhum dado retornado para o lote de produtos: {lote}")
                    # Não necessariamente um erro, o produto pode não existir mais.
                    continue
                
                resultado = enviar_para_allpost(produtos_dados)
                if resultado['sucesso']:
                    total_enviados += len(produtos_dados)
                    lotes += 1
                else:
                    total_erros += len(lote)
                
                if i + MAX_PRODUTOS_POR_BATCH < len(codprods):
                    time.sleep(0.5)
            
            buffer_global.atualizar_stats(total_enviados, lotes, total_erros)
            logger.info(f"Ciclo concluído: {total_enviados} enviados, {total_erros} erros, {lotes} lotes")
            
        except Exception as e:
            logger.error(f"Erro grave no worker: {e}", exc_info=True)
            time.sleep(5)
        finally:
            # 3. FAZER LOGOUT AO FINAL DO CICLO
            if sankhya_token:
                logout_sankhya(sankhya_token)

# ==================== ROTAS FLASK ====================
@sync_service_bp.route('/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat(), 'buffer_stats': buffer_global.obter_stats()}), 200

@sync_service_bp.route('/webhooks/sankhya/venda-realizada-http', methods=['POST'])
def webhook_venda_oracle():
    """Recebe webhook de venda do Sankhya (via procedure Oracle existente)"""
    try:
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer ') or not _secret_ok(auth_header[7:]):
            logger.warning("Tentativa de acesso não autorizada ao webhook.")
            return jsonify({'error': 'Não autorizado'}), 401
        
        data = request.get_json()
        if not data or 'produto' not in data or 'codprod' not in data['produto']:
            return jsonify({'error': 'JSON inválido ou CODPROD ausente'}), 400
        
        codprod = data['produto']['codprod']
        logger.info(f"Webhook recebido - CODPROD: {codprod}")
        buffer_global.adicionar(int(codprod))
        
        return jsonify({'status': 'sucesso', 'codprod': codprod, 'mensagem': 'Produto adicionado ao buffer'}), 200
        
    except Exception as e:
        logger.error(f"Erro no webhook: {e}", exc_info=True)
        return jsonify({'status': 'erro', 'erro': str(e)}), 500

@sync_service_bp.route('/sync/produtos', methods=['POST'])
def sync_produtos_manual():
    """Rota para sincronização manual de produtos por lista de CODPRODs"""
    sankhya_token = None
    try:
        data = request.get_json()
        if not data or not _secret_ok(data.get('api_key')):
            return jsonify({'error': 'API Key inválida'}), 401
        
        produtos_str = data.get('produtos', '')
        if not produtos_str:
            return jsonify({'error': 'Lista de produtos obrigatória'}), 400
        
        try:
            codprods = [int(c.strip()) for c in produtos_str.split(',') if c.strip()]
        except ValueError:
            return jsonify({'error': 'Formato inválido. Use: \"1001,1002,1003\"'}), 400
        
        logger.info(f"Sincronização manual solicitada para {len(codprods)} produtos")
        
        sankhya_token = get_new_bearer_token()
        if not sankhya_token:
            return jsonify({'status': 'erro', 'error': 'Falha na autenticação com o ERP Sankhya'}), 503

        total_enviados, total_erros, resultados = 0, 0, []
        for i in range(0, len(codprods), MAX_PRODUTOS_POR_BATCH):
            lote = codprods[i:i + MAX_PRODUTOS_POR_BATCH]
            sucesso_sankhya, produtos_dados = consultar_produtos_sankhya(lote, sankhya_token)

            if not sucesso_sankhya:
                return jsonify({
                    'status': 'erro',
                    'error': 'Falha na comunicação com o ERP Sankhya',
                    'details': f'Não foi possível consultar os produtos do lote: {lote}'
                }), 503

            if not produtos_dados:
                total_erros += len(lote)
                continue
            resultado = enviar_para_allpost(produtos_dados)
            resultados.append(resultado)
            if resultado['sucesso']:
                total_enviados += len(produtos_dados)
            else:
                total_erros += len(lote)

        return jsonify({'status': 'concluido', 'enviados': total_enviados, 'erros': total_erros, 'detalhes': resultados}), 200

    except Exception as e:
        logger.error(f"Erro na sincronização manual: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if sankhya_token:
            logout_sankhya(sankhya_token)

@sync_service_bp.route('/stats', methods=['GET'])
def estatisticas():
    return jsonify(buffer_global.obter_stats()), 200

# ==================== INICIALIZAÇÃO ====================
def iniciar_worker():
    worker_thread = threading.Thread(target=worker_processar_buffer, daemon=True)
    worker_thread.start()
    logger.info("Worker do buffer de sincronização iniciado em background")
