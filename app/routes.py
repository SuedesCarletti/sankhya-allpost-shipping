import hmac
import os
import logging
import requests
import threading
import schedule
import time
import json
from flask import Blueprint, jsonify, request
from functools import wraps
from datetime import datetime, timedelta
import pytz
from allpost_integration import AllPostIntegration, integrar_allpost
from filtro_transportadora_codlocal import (
    consultar_codlocal_produtos,
    selecionar_melhor_opcao_com_filtro,
    selecionar_segundo_melhor_frete,
    validar_mapeamento
)
import sys

# Criar blueprint para rotas de AllPost
allpost_routes = Blueprint('allpost_routes', __name__)

# Configurações de logging para console
# CORREÇÃO: Direcionar explicitamente o stream para sys.stdout para evitar que logs INFO sejam interpretados como ERRORS no Railway/Gunicorn.
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    stream=sys.stdout)
logger = logging.getLogger(__name__)

# Cria um blueprint para rotas Sankhya
sankhya_routes = Blueprint('sankhya_routes', __name__)

ULTIMA_DATA_NF_FILE = 'ultima_data_nf.txt'

def salvar_data_nf(data):
    """
    Salva a última data processada (notas fiscais) no arquivo.
    """
    try:
        with open(ULTIMA_DATA_NF_FILE, 'w') as f:
            f.write(data.astimezone(pytz.timezone('America/Sao_Paulo')).isoformat())
        logger.info(f"Data das NFs salva: {data.isoformat()}")
    except Exception as e:
        logger.error(f"Erro ao salvar data de NFs: {e}", exc_info=True)

def carregar_data_nf():
    """
    Carrega a última data processada (notas fiscais) do arquivo.
    Se não existir, retorna a data de 15 minutos atrás.
    """
    try:
        if os.path.exists(ULTIMA_DATA_NF_FILE):
            with open(ULTIMA_DATA_NF_FILE, 'r') as f:
                data_str = f.read().strip()
                return datetime.fromisoformat(data_str).astimezone(pytz.timezone('America/Sao_Paulo'))
    except Exception as e:
        logger.warning(f"Erro ao ler data de NFs: {e}")
    logger.info("Nenhuma data anterior encontrada para NFs. Usando fallback de 15 minutos.")
    return datetime.now(pytz.timezone('America/Sao_Paulo')) - timedelta(minutes=15)

ULTIMA_DATA_PEDIDO_FILE = 'ultima_nunota_pedido.txt'

def salvar_nunota_pedido(nunota):
    """
    Salva o último NUNOTA processado no arquivo.
    """
    try:
        with open(ULTIMA_DATA_PEDIDO_FILE, 'w') as f:
            f.write(str(nunota))
        logger.info(f"Último NUNOTA salvo: {nunota}")
    except Exception as e:
        logger.error(f"Erro ao salvar NUNOTA: {e}", exc_info=True)

def carregar_nunota_pedido():
    """
    Carrega o último NUNOTA processado do arquivo.
    Se não existir, retorna 0.
    """
    try:
        if os.path.exists(ULTIMA_DATA_PEDIDO_FILE):
            with open(ULTIMA_DATA_PEDIDO_FILE, 'r') as f:
                value = f.read().strip()
                return int(value) if value.isdigit() else 0
    except Exception as e:
        logger.warning(f"Erro ao ler NUNOTA: {e}")
    return 0

# Função de autenticação básica (decorator)
def basic_auth_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        ALLPOST_WEBHOOK_USERNAME = os.getenv('ALLPOST_WEBHOOK_USERNAME')
        ALLPOST_WEBHOOK_PASSWORD = os.getenv('ALLPOST_WEBHOOK_PASSWORD')

        auth = request.authorization
        if not auth:
            return jsonify({
                "retorno": "Credenciais de autenticação não fornecidas"
            }), 401
        
        if not ALLPOST_WEBHOOK_USERNAME or not ALLPOST_WEBHOOK_PASSWORD:
            logger.error("Credenciais do webhook não configuradas: acesso negado")
            return jsonify({"retorno": "Credenciais inválidas"}), 401

        if not (hmac.compare_digest(auth.username or '', ALLPOST_WEBHOOK_USERNAME) and
                hmac.compare_digest(auth.password or '', ALLPOST_WEBHOOK_PASSWORD)):
            return jsonify({
                "retorno": "Credenciais inválidas"
            }), 401
        
        return f(*args, **kwargs)
    return decorated_function

# Aplica autenticação básica a todas as rotas de um blueprint
def apply_authentication(blueprint):
    @blueprint.before_request
    @basic_auth_required
    def authenticate_all_routes():
        pass


# Aplica autenticação nos dois blueprints
apply_authentication(sankhya_routes)
apply_authentication(allpost_routes)

# Variáveis globais para gerenciar o token
bearer_token = None
token_expiration_time = None
polling_thread = None
last_api_call_time = datetime.now()
rate_limit_per_minute = 20  # Padrão para sandbox, alterar para 1000 em produção

allpost_api_url = os.getenv('ALLPOST_API_ENDPOINT', 'https://www.allpost.com.br/api/v1/pedido/novo')

@allpost_routes.route('/webhook', methods=['POST'])
@basic_auth_required
def handle_allpost_webhook():

    """
    Endpoint para receber webhooks da AllPost
    """
    try:
        # Parsing do body
        body = request.get_json()
        
        # Validação básica do body
        if not body or 'tipo' not in body:
            logger.warning("Body inválido recebido")
            return jsonify({
                "retorno": "Body inválido"
            }), 400
        
        # Inicializar integração AllPost
        integrador = AllPostIntegration()
        
        # Roteamento baseado no tipo de webhook
        webhook_type = body['tipo']
        webhook_data = body.get('dados', {})
        
        # Mapeamento de tipos de webhook para métodos de processamento
        handlers = {
            'Rastreio': integrador.process_tracking_webhook,
            'RastreioEventos': integrador.process_tracking_events,
            'prazoAdicional': integrador.process_additional_deadlines,
            'Aplicativo': integrador.process_app_uninstall,
            'metodoEnvio': integrador.process_shipping_method
        }
        
        # Processar webhook
        handler = handlers.get(webhook_type)
        if handler:
            resultado = handler(webhook_data)
            
            # Verificar status do processamento
            if resultado.get('status') == 'sucesso':
                return jsonify({
                    "retorno": "sucesso"
                }), 200
            else:
                return jsonify({
                    "retorno": resultado.get('mensagem', 'Erro no processamento')
                }), 400
        else:
            # Tipo de webhook não reconhecido
            logger.info(f"Tipo de webhook não implementado: {webhook_type}")
            return jsonify({
                "retorno": "sucesso",
                "obs": "Tipo de webhook não implementado"
            }), 200
    
    except Exception as e:
        logger.error(f"Erro no processamento de webhook: {e}")
        return jsonify({
            "retorno": str(e)
        }), 500
        

def integrar_allpost(notas, app_token=None, api_token=None):
    """
    Integra notas fiscais com a API AllPost
    
    Args:
        notas (list): Lista de notas fiscais a serem integradas
        app_token (str, optional): Token de aplicação AllPost
        api_token (str, optional): Token de API AllPost
    
    Returns:
        list: Resultados da integração de cada nota
    """
    # Usar tokens de ambiente se não forem fornecidos
    api_token = api_token or os.getenv('ALLPOST_API_TOKEN')
    app_token = app_token or os.getenv('ALLPOST_APP_TOKEN')
    
    # Validar tokens
    if not app_token or not api_token:
        raise ValueError("Tokens de integração AllPost não configurados")
    
    resultados = []
    
    # Processar cada nota
    for nota in notas:
        try:
            # Processar nota fiscal, enviando para API
            body = processar_nota_fiscal(nota, enviar_api=True)
            
            resultados.append({
                'numero_nota': nota.get('NUNOTA'),
                'status': 'Sucesso',
                'body': body
            })
            
        except Exception as e:
            logger.error(f"Erro na integração da nota {nota.get('NUNOTA')}: {e}")
            resultados.append({
                'numero_nota': nota.get('NUNOTA'),
                'status': 'Erro',
                'mensagem': str(e)
            })
    
    return resultados
  
@allpost_routes.route('/status-polling', methods=['GET'])
def status_integracao_allpost():
    """
    Rota para verificar status da integração com AllPost
    """
    try:
        # Implementar lógica de status
        return jsonify({
            'status': 'Ativo',
            'ultima_integracao': datetime.now().isoformat(),
            'total_notas_integradas': 0,  # Implementar contagem
            'tokens_configurados': bool(os.getenv('ALLPOST_APP_TOKEN') and os.getenv('ALLPOST_API_TOKEN'))
        }), 200
    except Exception as e:
        return jsonify({
            'status': 'Erro',
            'mensagem': str(e)
        }), 500

def parse_date(date_input, default_fallback=None):
    """
    Robust date parsing function with multiple format support and timezone handling
    """
    local_tz = pytz.timezone('America/Sao_Paulo')
    
    if isinstance(date_input, datetime):
        return date_input if date_input.tzinfo else local_tz.localize(date_input)

    if date_input is None:
        return default_fallback or local_tz.localize(datetime.now() - timedelta(hours=24))

    date_formats = [
        '%Y-%m-%d %H:%M:%S',
        '%d/%m/%Y %H:%M:%S',
        '%Y-%m-%d',
        '%d/%m/%Y',
        '%d-%m-%Y',
        '%d.%m.%Y',
        '%Y/%m/%d'
    ]

    for fmt in date_formats:
        try:
            parsed_date = datetime.strptime(str(date_input), fmt)
            return local_tz.localize(parsed_date)
        except ValueError:
            continue

    logging.warning(f"Unable to parse date: {date_input}. Using fallback.")
    return default_fallback or local_tz.localize(datetime.now() - timedelta(hours=24))

def format_date_for_query(date_input=None, format='%d/%m/%Y %H:%M:%S'):
    parsed_date = parse_date(date_input)
    return parsed_date.strftime(format)

def get_new_bearer_token():
    """
    Obtém um novo Bearer Token da API Sankhya e ATUALIZA o cache global.
    Use este método apenas no contexto principal de polling (notas/cotações).
    Para threads paralelas (ex: sync de estoque), use get_isolated_bearer_token().
    """
    global bearer_token, token_expiration_time

    try:
        # Configurações de autenticação
        headers = {
            'token': os.getenv('SANKHYA_TOKEN'),
            'appkey': os.getenv('SANKHYA_APPKEY'),
            'username': os.getenv('SANKHYA_USERNAME'),
            'password': os.getenv('SANKHYA_PASSWORD')
        }

        # Faz a requisição de login
        response = requests.post('https://api.sankhya.com.br/login', headers=headers)

        # Verifica se a requisição foi bem-sucedida
        response.raise_for_status()

        # Extrai o Bearer Token
        bearer_token = response.json().get('bearerToken')

        # Log detalhado do token
        logger.info("Novo Bearer Token obtido com sucesso")
        logger.info(f"Token obtido: {'sim' if bearer_token else 'não'}")

        # Define tempo de expiração (4 horas a partir de agora)
        token_expiration_time = datetime.now() + timedelta(hours=4)

        return bearer_token

    except requests.RequestException as e:
        logger.error(f"Erro ao obter token: {e}")
        return None


def get_isolated_bearer_token():
    """
    Obtém um Bearer Token ISOLADO da API Sankhya — NÃO atualiza o cache global.

    IMPORTANTE: Use este método em threads paralelas (ex: sync de estoque em
    estoque_fila_oracle.py) para evitar contaminação do token em cache usado
    pelo fluxo de cotações/notas. Se get_new_bearer_token() fosse chamado por
    threads paralelas, o logout delas invalidaria o token global, causando
    falha silenciosa nas chamadas Sankhya do fluxo de cotação (TGFCAB não
    seria atualizado e o pedido ficaria preso em loop de reprocessamento).
    """
    try:
        headers = {
            'token': os.getenv('SANKHYA_TOKEN'),
            'appkey': os.getenv('SANKHYA_APPKEY'),
            'username': os.getenv('SANKHYA_USERNAME'),
            'password': os.getenv('SANKHYA_PASSWORD')
        }
        response = requests.post('https://api.sankhya.com.br/login', headers=headers)
        response.raise_for_status()
        token = response.json().get('bearerToken')
        logger.info("Token isolado Sankhya obtido (sem alterar cache global)")
        return token
    except requests.RequestException as e:
        logger.error(f"Erro ao obter token isolado: {e}")
        return None

def is_token_valid():
    """
    Verifica se o token atual é válido
    """
    return bearer_token is not None and datetime.now() < token_expiration_time

def get_cached_token():
    global bearer_token, token_expiration_time

    if not is_token_valid():
        logger.info("Token expirado ou ausente. Obtendo novo token...")
        bearer_token = get_new_bearer_token()
    else:
        logger.info("Token em cache ainda válido. Usando token atual.")

    return bearer_token

def rate_limit_control():
    """
    Controle de taxa de requisições para respeitar os limites da API
    """
    global last_api_call_time
    
    # Calcula o tempo decorrido desde a última chamada
    elapsed_time = (datetime.now() - last_api_call_time).total_seconds()
    
    # Calcula o tempo mínimo entre chamadas para respeitar o limite por minuto
    min_time_between_calls = 60 / rate_limit_per_minute
    
    # Se o tempo decorrido for menor que o mínimo necessário, espera
    if elapsed_time < min_time_between_calls:
        sleep_time = min_time_between_calls - elapsed_time
        logger.debug(f"Rate limit: Aguardando {sleep_time:.2f}s antes da próxima chamada")
        time.sleep(sleep_time)
    
    # Atualiza o timestamp da última chamada
    last_api_call_time = datetime.now()
    
def logout_sankhya(bearer_token):
    """
    Encerra a sessão do token atual no Sankhya
    """
    try:
        url = 'https://api.sankhya.com.br/gateway/v1/mge/service.sbr?serviceName=MobileLoginSP.logout&outputType=json'
        headers = {
            'Authorization': f'Bearer {bearer_token}',
            'appkey': os.getenv('SANKHYA_APPKEY'),
            'Content-Type': 'application/json'
        }
        response = requests.post(url, headers=headers)
        response.raise_for_status()
        logger.info("Logout realizado com sucesso")
    except Exception as e:
        logger.warning(f"Falha ao realizar logout: {e}")

    
def extract_value(data):
    """
    Extrai o valor correto de um campo, lidando com dicionários aninhados
    
    Args:
        data: Valor de entrada que pode ser um dicionário ou valor primitivo
    
    Returns:
        Valor extraído ou processado
    """
    if isinstance(data, dict):
        # Tenta extrair valor de '$' ou retorna o próprio dicionário
        return data.get('$') or data
    return data

def corrigir_filial_cotacao(id_cotacao_composto):
    """
    Fallback: corrige o código de filial no idCotacao composto.
    O campo pode ter sido gravado com filialERP ('1', '6') mas o AllPost
    espera o código interno da filial ('695', '694').

    Formato esperado: '{idCotacao}-{filial}-{idMetodoEnvio}'
    Ex: '11222333000181-6-98'  → '11222333000181-694-98'
        '11222333000181-1-98'  → '11222333000181-695-98'
    """
    MAPA_FILIAL = {
        '6': '694',   # Serra ES  (CODLOCAL 401 → Filial 6)
        '1': '695',   # Curitiba  (CODLOCAL 201 → Filial 1)
    }

    if not id_cotacao_composto:
        return id_cotacao_composto

    partes = id_cotacao_composto.split('-')

    # Composto válido tem exatamente 3 partes
    if len(partes) != 3:
        logger.warning(f"[NF] idCotacao fora do formato esperado: {id_cotacao_composto}")
        return id_cotacao_composto

    id_cotacao, filial, id_metodo_envio = partes

    filial_corrigida = MAPA_FILIAL.get(filial)
    if filial_corrigida:
        composto_corrigido = f"{id_cotacao}-{filial_corrigida}-{id_metodo_envio}"
        logger.info(
            f"[NF] ⚙️ Filial corrigida no idCotacao: "
            f"{id_cotacao_composto} → {composto_corrigido}"
        )
        return composto_corrigido

    # Filial já está com código interno correto ('694', '695'), sem necessidade de correção
    return id_cotacao_composto


def processar_nota_fiscal(nota, enviar_api=True):
    try:
        # Recupera token em cache
        token = os.getenv('ALLPOST_API_TOKEN')
        
        # Função para converter valores para float com segurança
        def safe_float_convert(value, default=0):
            try:
                return float(value) if value is not None else default
            except (ValueError, TypeError):
                return default
        
        # Função para converter valores para int com segurança
        def safe_int_convert(value, default=0):
            try:
                return int(float(value)) if value is not None else default
            except (ValueError, TypeError):
                return default
                
        # Função para converter data para formato ISO (YYYY-MM-DD)
        def convert_to_iso_date(date_str, default=""):
            if not date_str:
                return default
            try:
                # Assumindo formato brasileiro DD/MM/YYYY
                parts = date_str.split('/')
                if len(parts) == 3:
                    return f"{parts[2]}-{parts[1]}-{parts[0]}"
                return date_str  # Retorna como está se não conseguir converter
            except Exception:
                return default
        
        # Verifica se 'itens' existe e é lista
        itens = nota.get('itens')
        if not itens or not isinstance(itens, list):
            logger.error(f"Nota {nota.get('NUNOTA')} sem itens ou itens inválidos.")
            return None  # Ou lance exceção, conforme necessidade

        # Agora pode usar 'itens' com segurança
        peso_total = sum(safe_float_convert(item.get('PESO_ITEM', 0)) for item in itens)
        altura_total = sum(safe_float_convert(item.get('ALTURA', 0)) for item in itens)
        largura_total = sum(safe_float_convert(item.get('LARGURA', 0)) for item in itens)
        espessura_total = sum(safe_float_convert(item.get('ESPESSURA', 0)) for item in itens)
        
        # Prepara os produtos para o body
        produtos = []
        for item in itens:
            produtos.append({
                'peso': safe_float_convert(item.get('PESO_ITEM', 0)),
                'altura': safe_float_convert(item.get('ALTURA', 0)),
                'largura': safe_float_convert(item.get('LARGURA', 0)),
                'profundidade': safe_float_convert(item.get('ESPESSURA', 0)),
                'preco': safe_float_convert(item.get('VLRUNIT', 0)) / safe_int_convert(item.get('QTDNEG', 1), 1) if safe_int_convert(item.get('QTDNEG', 0)) > 0 else 0,
                'titulo': str(item.get('DESCRPROD', '')),
                'sku': str(item.get('CODPROD', '')),
                'categoria': str(item.get('DESCRGRUPOPROD', '')),  # Se disponível na view, substituir
                'quantidade': safe_int_convert(item.get('QTDNEG', 0)),
                'quantidadeVolumes': safe_int_convert(item.get('QTDVOL_ITEM', 1), 1)
            })
        
        # Preparar body completo para a API destino seguindo o formato especificado
        body = {
            "numeroPedido": str(nota.get('NUNOTA', '')),
            "tipoPedido": "envio",
            "pedidoAuxiliar": "",
            "numeroPedidoEnvioPai": str(nota.get('AD_NUNOTAPED', '')),
            "plataforma": "",
            "canal": "",
            "intermediadorVenda": str(nota.get('APELIDO_VENDEDOR', '')),
            "idCotacao": corrigir_filial_cotacao(str(nota.get('AD_ID_COTACAO_ALLPOST') or '')),
            "valorFretePedido": safe_float_convert(nota.get('AD_VLRFRETEATACADO', 0)),
            "controleEtiqueta": "",
            "enderecoEntrega": {
                "nome": str(nota.get('NOMEPARC', '')),
                "email": str(nota.get('EMAIL', '')),
                "cpfCnpj": str(nota.get('CGC_CPF', '')),
                "cep": str(nota.get('CEP', '')),
                "uf": str(nota.get('UF_DESTINO', '')),
                "cidade": str(nota.get('NOMECID', '')),
                "endereco": str(nota.get('ENDER', '')),
                "numero": str(nota.get('NUMEND', '')),
                "bairro": str(nota.get('BAIRRO', '')),
                "referencia": str(nota.get('OBSERVACAO', '')),
                "telefones": [str(nota.get('TELEFONE', ''))] if nota.get('TELEFONE') else []
            },
            "envio": [{
                "nome": "",
                "peso": peso_total,
                "altura": altura_total,
                "largura": largura_total,
                "profundidade": espessura_total,
                "quantidade": sum(safe_int_convert(item.get('QTDNEG', 0)) for item in itens),
                "quantidadeVolumes": safe_int_convert(nota.get('QTDVOL', 1)),
                "codigoRastreio": "",
                "plp": None,
                "urlRastreioExterno": "",
                "mapaDeCarga": "",
                "prazoDeEntrega": (parse_date(nota.get('DTFATUR')) + timedelta(days=safe_int_convert(nota.get('AD_PRAZO_ENTREGA_ALLPOST'), 0))).isoformat(),
                "nomeTransportadora": "",
                "nomeMetodoEnvio": "",
                "produtos": produtos,
                "notaFiscal": {
                    "filialERP": str(nota.get('ORIGEM', '')),
                    "pedidoERP": str(nota.get('NUNOTA', '')),
                    "chave": str(nota.get('CHAVENFE', '')).strip(),
                    "data": parse_date(nota.get('DTFATUR')).isoformat(),
                    "valorTotal": safe_float_convert(nota.get('VLRNOTA', 0)),
                    "valorTotalProdutos": sum(safe_float_convert(item.get('VALOR_ITEM', 0)) for item in itens)
                },
                "dadosAdicionaisEnvio": {
                    "chave": "valor"
                }
            }],
            "dadosAdicionais": {
                "chave": "valor"
            }
        }
        
        # Log do body gerado
        id_cotacao_log = body.get('idCotacao') or '(vazio)'
        logger.info(f"Body gerado para NF {nota.get('NUNOTA')}. idCotacao: {id_cotacao_log}")
        logger.debug(f"Detalhes do body: {json.dumps(body, indent=2)}")
        
        if enviar_api:
            # Controle de taxa de requisições
            rate_limit_control()
            
            logger.info(f"Enviando NF {nota.get('NUNOTA')} para API destino")
            next_api_url = os.getenv('ALLPOST_BASE_URL', 'https://www.allpost.com.br') + os.getenv('ALLPOST_API_ENDPOINT', '/api/v1/pedido/novo')
            next_api_headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {token}'
            }
            
            try:
                next_api_response = requests.post(
                    next_api_url,
                    json=body,
                    headers=next_api_headers,
                    timeout=30
                )
                
                request_size = len(str(body))
                logger.info(f"Tamanho da requisição: {request_size/1024:.2f} KB")
                
                logger.info(f"Resposta da API AllPost: Status {next_api_response.status_code}")
                logger.info(f"Conteúdo da resposta: {next_api_response.text}")
                
                try:
                    response_json = next_api_response.json()
                    logger.info("Resposta JSON parseada com sucesso:")
                    
                    mensagem_retorno = response_json.get('mensagem', '')

                    if next_api_response.status_code == 200 and mensagem_retorno == 'sucesso':
                        chave_pedido = response_json.get('chavePedido', 'N/A')
                        logger.info(f"NF {nota.get('NUNOTA')} criada no AllPost. chavePedido: {chave_pedido}")
                        logger.info(f"Número do Pedido AllPost: {response_json.get('numeroPedido', 'N/A')}")
                    elif next_api_response.status_code == 200:
                        logger.error(
                            f"❌ AllPost retornou status 200 mas com erro para NF {nota.get('NUNOTA')}: "
                            f"{mensagem_retorno}"
                        )
                    elif next_api_response.status_code == 400:
                        logger.warning(f"Erro na API AllPost: {mensagem_retorno or 'Sem mensagem de erro'}")
                    else:
                        logger.error(f"Resposta inesperada. Status: {next_api_response.status_code}")
                    
                except json.JSONDecodeError:
                    logger.error("Não foi possível parsear a resposta JSON")
                    logger.error(f"Resposta bruta: {next_api_response.text}")
                
                next_api_response.raise_for_status()
                if mensagem_retorno == 'sucesso':
                    logger.info(f"NF {nota.get('NUNOTA')} processada e enviada com sucesso.")
                else:
                    logger.error(f"NF {nota.get('NUNOTA')} enviada mas AllPost reportou erro: {mensagem_retorno}")
                
            except requests.RequestException as api_error:
                logger.error(f"Erro ao enviar NF {nota.get('NUNOTA')} para AllPost: {api_error}")
                if hasattr(api_error, 'response'):
                    logger.error(f"Detalhes da resposta de erro: {api_error.response.text}")
                raise
        else:
            logger.info(f"Envio para API desabilitado para NF {nota.get('NUNOTA')}. Body gerado apenas para inspeção.")
        
        return body
        
    except requests.RequestException as api_error:
        logger.error(f"Erro ao processar NF {nota.get('NUNOTA')}: {api_error}")
        if hasattr(api_error, 'response') and hasattr(api_error.response, 'text'):
            logger.error(f"Detalhes: {api_error.response.text}")
        raise
    except Exception as e:
        logger.error(f"Erro inesperado ao processar NF {nota.get('NUNOTA')}: {e}")
        logger.error(f"Detalhes da nota: {json.dumps(nota, indent=2)}")
        raise
        
def consultar_notas_fiscais_recentes(ultima_data=None, limite=50, offset=0, max_retries=3):
    try:
        logger.info(f"Iniciando consulta de notas fiscais. " + 
                   f"Última data: {ultima_data}, " + 
                   f"Limite: {limite}, " + 
                   f"Offset: {offset}")
        notas_processadas = 0
        ultima_data_processada = ultima_data

        # Retrieve token using get_cached_token()
        token = get_cached_token()
        
        # By default, look back 24 hours if no date is specified
        if ultima_data is None:
            ultima_data = datetime.now(pytz.timezone('America/Sao_Paulo')) - timedelta(hours=24)
        
        # Ensure the date is timezone-aware
        ultima_data = parse_date(ultima_data)
        
        # Log the parsed date details
        logger.info(f"  ultima_data (parsed): {ultima_data}")
        logger.info(f"  ultima_data (timezone): {ultima_data.tzinfo}")
        
        # Ensure we use São Paulo timezone
        if ultima_data.tzinfo is None:
            local_tz = pytz.timezone('America/Sao_Paulo')
            ultima_data = local_tz.localize(ultima_data)
        
        # Use format_date_for_query for consistent API date format
        ultima_data_formatada = format_date_for_query(ultima_data)
        
        # Debug entire date parsing process
        logger.info(f"  ultima_data_formatada: {ultima_data_formatada}")
        
        # body for querying fiscal notes
        body = {
            "serviceName": "CRUDServiceProvider.loadView",
            "requestBody": {
                "query": {
                    "viewName": "VW_NOTAS_FISCAIS_ITENS",
                    "where": {
                        "$": f"DTFATUR > TO_DATE('{ultima_data_formatada}', 'DD/MM/YYYY HH24:MI:SS')"
                    },
                    "orderBy": {
                        "$": "DTFATUR ASC, NUNOTA ASC"
                    },
                    "offsetPage": offset,
                    "pageSize": limite
                }
            }
        }
        
        # Log full body for inspection
        logger.info("body de consulta detalhado:")
        logger.info(json.dumps(body, indent=2))
        
        # Configurar headers
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
        
        # URL para consulta
        url = 'https://api.sankhya.com.br/gateway/v1/mge/service.sbr?serviceName=CRUDServiceProvider.loadView&outputType=json'
        
        # Controle de taxa de requisições
        rate_limit_control()
        
        # Fazer requisição com retry em caso de falha
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                # Fazer requisição
                response = requests.post(url, headers=headers, json=body, timeout=120)
                logger.info(f"Resposta da API: {response.status_code}")
                
                # Adiciona log do conteúdo da resposta
                response_text = response.text
                logger.info(f"Conteúdo da resposta: {response_text[:500]}...")  # Log primeiros 500 caracteres
                
                response.raise_for_status()
                break  # Sai do loop se a requisição for bem-sucedida
            except requests.RequestException as e:
                retry_count += 1
                logger.warning(f"Tentativa {retry_count}/{max_retries} falhou: {e}")
                if retry_count >= max_retries:
                    raise
                time.sleep(2 * retry_count)  # Espera progressiva
        
        # Processar a resposta da API
        try:
            response_data = response.json()
            logger.info("Resposta JSON processada com sucesso")
        except json.JSONDecodeError as e:
            logger.error(f"Erro ao decodificar JSON: {e}")
            logger.error(f"Resposta recebida: {response.text}")
            raise
        
        # Tenta extrair notas de diferentes estruturas possíveis
        notas_fiscais = (
            response_data.get('responseBody', {}).get('entities', {}).get('entity', []) or
            response_data.get('responseBody', {}).get('records', {}).get('record', []) or
            []
        )
        
        # Log de notas fiscais encontradas
        logger.info(f"Número de notas fiscais encontradas: {len(notas_fiscais)}")
        
        # Log do tamanho da resposta para monitoramento
        response_size = len(response.content)
        logger.info(f"Consulta de NFs:")
        logger.info(f"  Data de corte: {ultima_data_formatada}")
        logger.info(f"  Offset: {offset}")
        logger.info(f"  Limite: {limite}")
        logger.info(f"  Notas encontradas: {len(notas_fiscais)}")
        logger.info(f"  Tamanho da resposta: {response_size/1024:.2f} KB")
        
        # Garante que notas_fiscais seja uma lista
        if not isinstance(notas_fiscais, list):
            notas_fiscais = [notas_fiscais] if notas_fiscais else []
        
        if not notas_fiscais:
            logger.info(f"Nenhuma nota fiscal encontrada desde {ultima_data_formatada} (offset {offset})")
            # Salva a data/hora da execução do job
            data_execucao = datetime.now(pytz.timezone('America/Sao_Paulo'))
            salvar_data_nf(data_execucao)
            logger.info(f"Última data processada salva como data de execução: {data_execucao.isoformat()}")
            return {
              "ultima_data": data_execucao,
              "tem_mais": False,
              "total_processado": 0,
              "offset_proximo": 0
            }

        # Agrupar itens por nota fiscal
        notas_agrupadas = {}
        for item in notas_fiscais:
            # Usa extract_value para obter NUNOTA
            nunota = extract_value(item.get('NUNOTA'))
            
            if nunota not in notas_agrupadas:
                # Copia todos os campos do item para a nota
                nota_base = {k: extract_value(v) 
                             for k, v in item.items() if k != 'SEQUENCIA'}
                nota_base['itens'] = []
                notas_agrupadas[nunota] = nota_base
            
            # Adiciona item à nota, usando extract_value
            item_processado = {
                'CODPROD': extract_value(item.get('CODPROD')),
                'DESCRPROD': extract_value(item.get('DESCRPROD')),
                'SEQUENCIA': extract_value(item.get('SEQUENCIA')),
                'QTDNEG': extract_value(item.get('QTDNEG')),
                'VALOR_ITEM': extract_value(item.get('VALOR_ITEM')),
                'PESO_ITEM': extract_value(item.get('PESO_ITEM')),
                'ALTURA': extract_value(item.get('ALTURA')),
                'LARGURA': extract_value(item.get('LARGURA')),
                'ESPESSURA': extract_value(item.get('ESPESSURA')),
                'VLRUNIT': extract_value(item.get('VLRUNIT')),
                'QTDVOL_ITEM': extract_value(item.get('QTDVOL_ITEM'))
            }
            
            # Adiciona item à nota
            notas_agrupadas[nunota]['itens'].append(item_processado)
        
        # Processar cada nota fiscal agrupada
        notas_processadas = 0
        
        for nunota, nota in notas_agrupadas.items():
            try:
                logger.info(f"Processando nota fiscal {nunota}")
                processar_nota_fiscal(nota, enviar_api=True)
                notas_processadas += 1
            except Exception as e:
                logging.error(f"Erro no processamento da nota {nunota}: {e}")

        # Ao final, salva SEMPRE a data de execução
        data_execucao = datetime.now(pytz.timezone('America/Sao_Paulo'))
        salvar_data_nf(data_execucao)
        logger.info(f"Última data processada salva como data de execução: {data_execucao.isoformat()}")

        # Modificar a lógica de tem_mais
        tem_mais = (
            len(notas_fiscais) >= limite and  # Ainda há registros completos
            len(notas_fiscais) > 0  # Garante que não seja uma lista vazia
        )
        return {
            "ultima_data": data_execucao,
            "tem_mais": tem_mais,
            "total_processado": notas_processadas,
            "offset_proximo": offset + limite if tem_mais else 0
        }
    except Exception as e:
        logger.error(f"Erro na consulta de notas fiscais: {e}")
        # Salva data de erro como data atual, se preferir manter rastreio
        data_execucao = datetime.now(pytz.timezone('America/Sao_Paulo'))
        salvar_data_nf(data_execucao)
        return {
            "ultima_data": data_execucao,
            "tem_mais": False,
            "total_processado": 0,
            "offset_proximo": 0
        }
                
@sankhya_routes.route('/consultar-notas-fiscais', methods=['GET'])
def consultar_notas_fiscais_manual():
    try:
        # Use parse_date for robust date handling
        ultima_data = request.args.get('ultima_data')
        ultima_data = parse_date(ultima_data, default_fallback=datetime.now() - timedelta(hours=24))
        
        # Use format_date_for_query for consistent API date format
        ultima_data_formatada = format_date_for_query(ultima_data)
        
        limite = int(request.args.get('limite', 50))
        offset = int(request.args.get('offset', 0))
        processar = request.args.get('processar', 'true').lower() == 'true'
        enviar_api = request.args.get('enviar_api', 'true').lower() == 'true'  # New parameter
        
        # Recupera token de autenticação
        token = get_cached_token()
        
        # Payload para consulta de NFs usando a view otimizada
        payload = {
            "serviceName": "CRUDServiceProvider.loadView",
            "requestBody": {
                "query": {
                    "viewName": "VW_NOTAS_FISCAIS_ITENS",
                    "where": {
                        "$": f"DTFATUR > TO_DATE('{ultima_data_formatada}', 'DD/MM/YYYY HH24:MI:SS')"
                    },
                    "orderBy": {
                        "$": "DTFATUR ASC, NUNOTA ASC"
                    },
                    "offsetPage": offset,
                    "pageSize": limite
                }
            }
        }
        
        # Configurar headers
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
        
        # URL para consulta
        url = 'https://api.sankhya.com.br/gateway/v1/mge/service.sbr?serviceName=CRUDServiceProvider.loadView&outputType=json'
        
        # Controle de taxa de requisições
        rate_limit_control()
        
        # Fazer requisição
        response = requests.post(url, headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        
        # Extração robusta dos dados
        response_data = response.json()
        
        # Tenta extrair notas de diferentes estruturas possíveis
        notas_fiscais = (
            response_data.get('responseBody', {}).get('entities', {}).get('entity', []) or
            response_data.get('responseBody', {}).get('records', {}).get('record', []) or
            []
        )
        
        # Converte para lista se não for
        if not isinstance(notas_fiscais, list):
            notas_fiscais = [notas_fiscais]
        
        # Agrupar itens por nota fiscal
        notas_agrupadas = {}
        for item in notas_fiscais:
            # Extrai NUNOTA de forma robusta
            nunota = item.get('NUNOTA', {}).get('$') or item.get('NUNOTA')
            
            if nunota not in notas_agrupadas:
                # Copia todos os campos do item para a nota
                nota_base = {k: v.get('$') if isinstance(v, dict) and '$' in v else v 
                             for k, v in item.items() if k != 'SEQUENCIA'}
                nota_base['itens'] = []
                notas_agrupadas[nunota] = nota_base
            
            # Adiciona item à nota, tratando campos com {'$': valor}
            notas_agrupadas[nunota]['itens'].append({
                'CODPROD': item.get('CODPROD', {}).get('$') or item.get('CODPROD'),
                'DESCRPROD': item.get('DESCRPROD', {}).get('$') or item.get('DESCRPROD'),
                'SEQUENCIA': item.get('SEQUENCIA', {}).get('$') or item.get('SEQUENCIA'),
                'QTDNEG': item.get('QTDNEG', {}).get('$') or item.get('QTDNEG'),
                'VALOR_ITEM': item.get('VALOR_ITEM', {}).get('$') or item.get('VALOR_ITEM'),
                'PESO_ITEM': item.get('PESO_ITEM', {}).get('$') or item.get('PESO_ITEM'),
                'ALTURA': item.get('ALTURA', {}).get('$') or item.get('ALTURA'),
                'LARGURA': item.get('LARGURA', {}).get('$') or item.get('LARGURA'),
                'ESPESSURA': item.get('ESPESSURA', {}).get('$') or item.get('ESPESSURA'),
                'VLRUNIT': item.get('VLRUNIT', {}).get('$') or item.get('VLRUNIT'),
                'QTDVOL_ITEM': item.get('QTDVOL_ITEM', {}).get('$') or item.get('QTDVOL_ITEM')
            })
        
        # Processar notas fiscais se solicitado
        resultados = []
        ultima_data_processada = ultima_data

        if processar:
            for nunota, nota in notas_agrupadas.items():
                try:
                    start_time = time.time()
                    # Passa o parmetro enviar_api
                    payload = processar_nota_fiscal(nota, enviar_api=enviar_api)
                    processing_time = time.time() - start_time

                    resultados.append({
                        'numero_nota': nunota,
                        'status': 'Payload gerado com sucesso' + (' e enviado' if enviar_api else ''),
                        'tempo_processamento': f"{processing_time:.2f}s",
                        'qtd_itens': len(nota['itens']),
                        'payload': payload  # Opcional: incluir payload nos resultados
                    })
                except Exception as e:
                    resultados.append({
                        'numero_nota': nunota,
                        'status': f'Erro na geração do payload: {str(e)}',
                        'qtd_itens': len(nota['itens'])
                    })
            
        else:
            # Apenas lista as notas sem processá-las
            for nunota, nota in notas_agrupadas.items():
                resultados.append({
                    'numero_nota': nunota,
                    'data_faturamento': nota.get('DTFATUR'),
                    'chave_nfe': nota.get('CHAVENFE'),
                    'valor_nota': nota.get('VLRNOTA'),
                    'parceiro': nota.get('NOMEPARC'),
                    'qtd_itens': len(nota['itens'])
                })
        
        # Formato de retorno melhorado
        return jsonify({
            'total_notas': len(notas_agrupadas),
            'total_itens': len(notas_fiscais),
            'ultima_data_consultada': ultima_data,
            'ultima_data_processada': ultima_data_processada if processar else None,
            'offset': offset,
            'limite': limite,
            'envio_api_habilitado': enviar_api,
            'tem_mais_registros': len(notas_fiscais) >= limite,
            'offset_proximo': offset + limite if len(notas_fiscais) >= limite else None,
            'resultados': resultados
        }), 200
    
    except Exception as e:
        logging.error(f"Erro na consulta manual de notas fiscais: {e}")
        return jsonify({
            'error': 'Falha na consulta de notas fiscais',
            'detalhes': str(e)
        }), 500
                
import time

def enviar_cotacao(pedido):
    try:
        cep = str(pedido.get("CEP", ""))
        
        # CORREÇÃO: Validar CEP antes de enviar
        if not cep or len(cep) < 8:
            logger.error(f"CEP inválido para NUNOTA {pedido.get('NUNOTA')}: {cep}")
            return None
        
        volumes = []
        for item in pedido["itens"]:
            # CORREÇÃO: Validar campos obrigatórios
            qtd = int(float(item.get("QTDNEG") or 0))
            if qtd <= 0:
                logger.warning(f"Quantidade inválida para produto {item.get('CODPROD')}: {qtd}")
                continue
                
            volume = {
                "sku": str(item.get("CODPROD")),
                "quantidade": qtd,
                "valorUnitario": float(item.get("VLRUNIT") or 0),
                "peso": float(item.get("PESO_ITEM") or 0),
                "altura": float(item.get("ALTURA_CM") or 0),
                "largura": float(item.get("LARGURA_CM") or 0),
                "profundidade": float(item.get("ESPESSURA_CM") or 0)
            }
            volumes.append(volume)
        
        if not volumes:
            logger.error(f"Nenhum volume válido para NUNOTA {pedido.get('NUNOTA')}")
            return None
        
        body = {
            "cepDestino": cep,
            "canal": "ERP",  # CORREÇÃO: Canal mais específico
            "volumes": volumes
        }
        
        # CORREÇÃO: Usar variáveis de ambiente específicas para cotação
        url = os.getenv("ALLPOST_COTACAO_ENDPOINT", "https://www.allpost.com.br/api/v1/cotacao")
        token = os.getenv("ALLPOST_COTACAO_TOKEN")
        
        # CORREÇÃO: Fallback para token da API se não houver token específico
        if not token:
            token = os.getenv("ALLPOST_API_TOKEN")
            logger.warning("Token de cotação não encontrado, usando token da API")
        
        if not token:
            logger.error("Nenhum token disponível para cotação")
            return None
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
        
        logger.info(f"Enviando cotação para NUNOTA {pedido.get('NUNOTA')}")
        logger.info(f"CEP: {cep}, Volumes: {len(volumes)}")
        
        # CORREÇÃO: Controle de rate limiting
        rate_limit_control()
        
        response = requests.post(url, headers=headers, json=body, timeout=30)
        
        # CORREÇÃO: Melhor tratamento de respostas
        if response.status_code == 200:
            cotacao_resp = response.json()
            id_cotacao = cotacao_resp.get("retorno", {}).get("idCotacao")
            
            if not id_cotacao:
                logger.error(f"idCotacao não encontrado na resposta! Resposta: {cotacao_resp}")
                return None
            
            logger.info(f"Cotação enviada com sucesso. idCotacao: {id_cotacao}")
            return id_cotacao
        
        elif response.status_code == 400:
            error_data = response.json() if response.content else {}
            error_msg = error_data.get('mensagem', 'Erro desconhecido')
            logger.error(f"Erro 400 ao enviar cotação para NUNOTA {pedido.get('NUNOTA')}: {error_msg}")
            return None
        
        else:
            response.raise_for_status()
            
    except requests.exceptions.Timeout:
        logger.error(f"Timeout ao enviar cotação para NUNOTA {pedido.get('NUNOTA')}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Erro de requisição ao enviar cotação para NUNOTA {pedido.get('NUNOTA')}: {e}")
        return None
    except Exception as e:
        logger.error(f"Erro inesperado ao enviar cotação para NUNOTA {pedido.get('NUNOTA')}: {e}", exc_info=True)
        return None
      
def consultar_detalhe_cotacao(id_cotacao, delay_segundos=60):
    try:
        logger.info(f"Aguardando {delay_segundos}s antes de consultar detalhes da cotação...")
        time.sleep(delay_segundos)
        
        # CORREÇÃO 1: Usar o token específico de cotação, não o token da API
        token_cotacao = os.getenv("ALLPOST_COTACAO_TOKEN")
        
        # CORREÇÃO 2: Se não houver token de cotação específico, usar o token da API
        if not token_cotacao:
            token_cotacao = os.getenv("ALLPOST_API_TOKEN")
            logger.warning("Token de cotação não encontrado, usando token da API")
        
        get_url = f"https://www.allpost.com.br/api/v1/cotacao/{str(id_cotacao)}"
        get_headers = {
            "Authorization": f"Bearer {token_cotacao}",
            "Content-Type": "application/json",
            "User-Agent": "PostmanRuntime/7.32.3",
            "Accept": "*/*"
        }
        
        logger.info(f"Consultando detalhes da cotação em: {get_url}")
        logger.info(f"Usando token de cotação: {'sim' if token_cotacao else 'não'}")
        
        get_resp = requests.get(get_url, headers=get_headers, timeout=30)
        
        # CORREÇÃO 3: Melhor tratamento de erros
        if get_resp.status_code == 400:
            error_data = get_resp.json() if get_resp.content else {}
            error_msg = error_data.get('mensagem', 'Erro desconhecido')
            
            if "Loja não encontrada" in error_msg:
                logger.error(f"Erro de autenticação na cotação {id_cotacao}: {error_msg}")
                logger.error("Verifique se o ALLPOST_COTACAO_TOKEN está configurado corretamente")
                return None
            else:
                logger.error(f"Erro 400 na cotação {id_cotacao}: {error_msg}")
                return None
        elif get_resp.status_code == 404:
            logger.error(f"Cotação {id_cotacao} não encontrada (pode ter expirado)")
            return None
        
        try:
            get_resp.raise_for_status()
        except Exception as err:
            logger.error(f"Erro HTTP ao consultar cotação {id_cotacao}: Status {get_resp.status_code}")
            logger.error(f"Resposta: {get_resp.text}")
            return None
        
        cotacao_detalhe = get_resp.json()
        logger.info(f"Resposta da cotação detalhada para id {id_cotacao}: {cotacao_detalhe}")
        return cotacao_detalhe
        
    except Exception as e:
        logger.error(f"Erro ao consultar detalhes da cotação para id {id_cotacao}: {e}", exc_info=True)
        return None
def consultar_transportadora_por_idallpost(idallpost):
    """
    Consulta a transportadora no Sankhya baseada no ID AllPost
    CORREÇÃO: Estrutura correta para consulta usando loadRecords com critérios
    """
    try:
        token = get_cached_token()
        
        # Estrutura correta usando loadRecords com critérios
        body = {
            "serviceName": "CRUDServiceProvider.loadRecords",
            "requestBody": {
                "dataSet": {
                    "rootEntity": "Parceiro",
                    "includePresentationFields": "S",
                    "tryJoinedFields": "true",
                    "offsetPage": "0",
                    "criteria": {
                        "expression": {
                            "$": "AD_IDALLPOST = ?"
                        },
                        "parameter": [
                            {
                                "$": str(idallpost),
                                "type": "S"
                            }
                        ]
                    },
                    "entity": {
                        "fieldset": {
                            "list": "CODPARC,NOMEPARC,AD_IDALLPOST"
                        }
                    }
                }
            }
        }
        
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
        
        url = 'https://api.sankhya.com.br/gateway/v1/mge/service.sbr?serviceName=CRUDServiceProvider.loadRecords&outputType=json'
        
        rate_limit_control()
        
        logger.info(f"[TRANSP] Consultando transportadora com ID AllPost: {idallpost}")
        response = requests.post(url, headers=headers, json=body, timeout=30)
        
        if response.status_code != 200:
            logger.error(f"[TRANSP] Erro HTTP {response.status_code}: {response.text}")
            return None
            
        response.raise_for_status()
        response_data = response.json()
        
        # Extrair registros da resposta usando a estrutura correta do loadRecords
        entities = response_data.get('responseBody', {}).get('entities', {})
        records = entities.get('entity', []) if entities else []
        
        if not records:
            logger.warning(f"[TRANSP] Nenhuma transportadora encontrada para ID AllPost: {idallpost}")
            return None
            
        # Garantir que é uma lista
        if not isinstance(records, list):
            records = [records]
            
        record = records[0]
        
        # Com loadRecords, os campos vêm como f0, f1, f2, etc.
        # Baseado na ordem do fieldset: "CODPARC,NOMEPARC,AD_IDALLPOST"
        # f0 = CODPARC, f1 = NOMEPARC, f2 = AD_IDALLPOST
        codparc = extract_value(record.get('f0'))
        nomeparc = extract_value(record.get('f1'))
        id_allpost_retornado = extract_value(record.get('f2'))
        
        if not codparc:
            logger.error(f"[TRANSP] CODPARC não encontrado no registro: {record}")
            return None
        
        transportadora_info = {
            'CODPARC': codparc,
            'NOMEPARC': nomeparc,
            'AD_IDALLPOST': id_allpost_retornado or idallpost
        }
        
        logger.info(f"[TRANSP] Transportadora encontrada: {nomeparc} (CODPARC: {codparc})")
        return transportadora_info
        
    except Exception as e:
        logger.error(f"[TRANSP] Erro ao consultar transportadora ID AllPost {idallpost}: {e}", exc_info=True)
        return None
      
def atualizar_tgfcab(pedido, max_retries=2):
    """
    Atualiza TGFCAB com transportadora, frete, ID da cotação e 2ª transportadora.
    """
    if not ATUALIZAR_TGFCAB:
        logger.info(f"[TGFCAB] Atualização desabilitada para pedido {pedido.get('NUNOTA')}")
        return {'status': 'ignorado', 'motivo': 'ATUALIZAR_TGFCAB desativado'}

    nunota = str(pedido.get('NUNOTA'))
    codparc_transportadora = pedido.get('CODPARCTRANSP')
    valor_frete = float(pedido.get('AD_FRETE_PREVISTO_ALLPOST', 0))
    valor_frete2 = float(pedido.get('AD_FRETE_PREVISTO_ALLPOST2', 0))
    id_cotacao_allpost = str(pedido.get('AD_ID_COTACAO_ALLPOST', ''))
    transp2_allpost = str(pedido.get('AD_TRANSP2_ALLPOST', ''))  # novo
    prazo_entrega_allpost = int(pedido.get('AD_PRAZO_ENTREGA_ALLPOST', 0) or 0)

    if not codparc_transportadora:
        return {'status': 'erro', 'motivo': 'CODPARC da transportadora não fornecido'}

    payload = {
        "serviceName": "DatasetSP.save",
        "requestBody": {
            "entityName": "CabecalhoNota",
            "standAlone": False,
            "fields": [
                "NUNOTA",
                "CODPARCTRANSP",
                "AD_FRETE_PREVISTO_ALLPOST",
                "AD_FRETE_PREVISTO_ALLPOST2",
                "AD_ID_COTACAO_ALLPOST",
                "AD_TRANSP2_ALLPOST",  # novo
                "AD_PRAZO_ENTREGA_ALLPOST"
            ],
            "records": [
                {
                    "pk": {
                        "NUNOTA": nunota
                    },
                    "values": {
                        "1": str(codparc_transportadora),
                        "2": str(valor_frete),
                        "3": str(valor_frete2),
                        "4": id_cotacao_allpost,
                        "5": transp2_allpost,  # novo
                        "6": str(prazo_entrega_allpost)
                    }
                }
            ]
        }
    }

    url = 'https://api.sankhya.com.br/gateway/v1/mge/service.sbr?serviceName=DatasetSP.save&outputType=json'

    logger.info(f"[TGFCAB] Atualizando NUNOTA {nunota}")
    logger.info(f"[TGFCAB] CODPARCTRANSP: {codparc_transportadora}")
    logger.info(f"[TGFCAB] AD_FRETE_PREVISTO_ALLPOST: R$ {valor_frete}")
    logger.info(f"[TGFCAB] AD_FRETE_PREVISTO_ALLPOST2: R$ {valor_frete2}")
    logger.info(f"[TGFCAB] AD_ID_COTACAO_ALLPOST: {id_cotacao_allpost}")
    logger.info(f"[TGFCAB] AD_TRANSP2_ALLPOST: {transp2_allpost}")
    logger.info(f"[TGFCAB] AD_PRAZO_ENTREGA_ALLPOST: {prazo_entrega_allpost} dia(s)")

    for tentativa in range(max_retries + 1):
        try:
            token = get_cached_token()
            headers = {
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json'
            }

            rate_limit_control()

            timeout = 45 + (tentativa * 15)
            logger.info(f"[TGFCAB] Tentativa {tentativa + 1}/{max_retries + 1} - Timeout: {timeout}s")

            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
            response.raise_for_status()

            resp_json = response.json()
            status_resp = resp_json.get('status', '')

            if status_resp == '1':
                logger.info(f"[TGFCAB] NUNOTA {nunota} atualizado com sucesso.")
                return {'status': 'sucesso'}
            else:
                msg_erro = resp_json.get('statusMessage', 'Erro desconhecido')
                logger.error(f"[TGFCAB] Sankhya retornou erro para NUNOTA {nunota}: {msg_erro}")
                return {'status': 'erro', 'motivo': msg_erro}

        except requests.exceptions.Timeout:
            logger.warning(f"[TGFCAB] Timeout na tentativa {tentativa + 1} para NUNOTA {nunota}")
            if tentativa >= max_retries:
                return {'status': 'erro', 'motivo': 'Timeout após todas as tentativas'}

        except Exception as e:
            logger.error(f"[TGFCAB] Erro inesperado NUNOTA {nunota}: {e}", exc_info=True)
            return {'status': 'erro', 'motivo': str(e)}

    return {'status': 'erro', 'motivo': 'Máximo de tentativas atingido'}
  
# Flag para controle da atualização TGFCAB (default False)
ATUALIZAR_TGFCAB = os.getenv('ATUALIZAR_TGFCAB', 'true').lower() == 'true' 
        
from threading import Lock
cotacao_lock = Lock()
      
def consultar_pedidos_para_cotacao(limite=50, offset=0, max_retries=3):
    # Exclusão mútua para evitar execuções simultneas
    if not cotacao_lock.acquire(blocking=False):
        logger.warning("[COT] Consulta de cotações já em andamento! Abortando execução simultnea.")
        return {
            "erro": "Consulta de cotações já em andamento. Aguarde o término antes de iniciar outra."
        }
    try:
        logger.info(f"[COT] Início. Limite:{limite}, Offset:{offset}")
        pedidos_processados = 0
        token = get_cached_token()

        body = {
            "serviceName": "CRUDServiceProvider.loadView",
            "requestBody": {
                "query": {
                    "viewName": "VW_PEDIDOS_PARA_COTACAO",
                    "where": {
                        "$": "AD_ID_COTACAO_ALLPOST IS NULL"
                    },
                    "orderBy": {"$": "NUNOTA ASC"},
                    "offsetPage": offset,
                    "pageSize": limite
                }
            }
        }
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
        url = 'https://api.sankhya.com.br/gateway/v1/mge/service.sbr?serviceName=CRUDServiceProvider.loadView&outputType=json'
        rate_limit_control()
        retry_count = 0
        while retry_count < max_retries:
            try:
                response = requests.post(url, headers=headers, json=body, timeout=120)
                response.raise_for_status()
                break
            except requests.RequestException as e:
                retry_count += 1
                logger.warning(f"[COT][ERRO] Tentativa {retry_count}: {e}")
                if retry_count >= max_retries:
                    logger.error(f"[COT][FALHA] Não foi possível consultar pedidos após {max_retries} tentativas.")
                    raise
                time.sleep(2 * retry_count)
        response_data = response.json()
        registros = (
            response_data.get('responseBody', {}).get('entities', {}).get('entity', []) or
            response_data.get('responseBody', {}).get('records', {}).get('record', []) or
            []
        )
        if not isinstance(registros, list):
            registros = [registros] if registros else []
        if not registros:
            logger.info("[COT] Sem pedidos pendentes de cotação.")
            return {
                "tem_mais": False,
                "total_processado": 0,
                "offset_proximo": 0
            }

        pedidos_agrupados = {}
        for item in registros:
            nunota = int(extract_value(item.get("NUNOTA")))
            if nunota not in pedidos_agrupados:
                pedido_base = {k: extract_value(v) for k, v in item.items() if k != 'SEQUENCIA'}
                pedido_base['itens'] = []
                pedidos_agrupados[nunota] = pedido_base
            pedidos_agrupados[nunota]['itens'].append({
                'CODPROD': extract_value(item.get('CODPROD')),
                'QTDNEG': extract_value(item.get('QTDNEG')),
                'VLRUNIT': extract_value(item.get('VLRUNIT')),
                'PESO_ITEM': extract_value(item.get('PESO_ITEM')),
                'ALTURA_CM': extract_value(item.get('ALTURA_CM')),
                'LARGURA_CM': extract_value(item.get('LARGURA_CM')),
                'ESPESSURA_CM': extract_value(item.get('ESPESSURA_CM')),
            })

        # --- FIX: Create authenticated session object for use by filtro_transportadora_codlocal ---
        sankhya_session = requests.Session()
        sankhya_session.headers.update({
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        })

        for nunota, pedido in pedidos_agrupados.items():
            try:
                logger.info(f"[COT] Enviando cotação pedido {nunota}")
                
                # === Consultar CODLOCAL (CORREÇÃO DE AUTENTICAÇÃO) ==========
                try:
                    # FIX 1: Pass the authenticated session object
                    codlocal_produtos = consultar_codlocal_produtos(pedido, sankhya_session=sankhya_session)
                    logger.info(f"[COT] CODLOCALs identificados: {codlocal_produtos}")
                except Exception as e:
                    logger.error(f"[COT] Erro ao consultar CODLOCAL: {e}")
                    codlocal_produtos = {}  # Fallback: continuar sem filtro
                
                id_cotacao = enviar_cotacao(pedido)
                
                if id_cotacao:
                    cotacao_detalhe = consultar_detalhe_cotacao(id_cotacao, delay_segundos=60)
                    
                    if cotacao_detalhe and cotacao_detalhe.get('opcoesEntrega'):
                        opcoes = cotacao_detalhe.get('opcoesEntrega', [])
                        
                        # ========== MODIFICADO: Usar filtro ==========
                        melhor_opcao = selecionar_melhor_opcao_com_filtro(
                            opcoes_entrega=opcoes,
                            codlocal_produtos=codlocal_produtos,
                            criterio='frete'  # ou 'prazo'
                        )

                        segundo_melhor_frete = selecionar_segundo_melhor_frete(
                            opcoes_entrega=opcoes,
                            codlocal_produtos=codlocal_produtos,
                            melhor_opcao=melhor_opcao
                        )
                        if segundo_melhor_frete:
                            pedido['AD_FRETE_PREVISTO_ALLPOST2'] = segundo_melhor_frete.get('freteCobrar', 0)
                            logger.info(f"[COT] 2º melhor frete: R$ {pedido['AD_FRETE_PREVISTO_ALLPOST2']}")
                        
                            # Buscar CODPARC e NOMEPARC da 2ª melhor transportadora
                            id_transp2_allpost = segundo_melhor_frete.get('dadosFrete', {}).get('idTransportadora')
                            if id_transp2_allpost:
                                transp2_info = consultar_transportadora_por_idallpost(id_transp2_allpost)
                                if transp2_info:
                                    codparc2 = transp2_info.get('CODPARC', '')
                                    nomeparc2 = transp2_info.get('NOMEPARC', '')
                                    pedido['AD_TRANSP2_ALLPOST'] = f"{codparc2} - {nomeparc2}"
                                    logger.info(f"[COT] 2ª transportadora: {pedido['AD_TRANSP2_ALLPOST']}")
                                else:
                                    pedido['AD_TRANSP2_ALLPOST'] = ''
                                    logger.warning(f"[COT] Não encontrou transportadora para ID AllPost: {id_transp2_allpost}")
                            else:
                                pedido['AD_TRANSP2_ALLPOST'] = ''                    
                        
                        if melhor_opcao:
                            id_transportadora_allpost = melhor_opcao.get('dadosFrete', {}).get('idTransportadora')
                            valor_frete = melhor_opcao.get('freteCobrar')

                            # Compõe o idCotacao no formato completo exigido pelo AllPost:
                            # "{idCotacao}-{filial}-{idMetodoEnvio}"  ex: "11081910314-20-1"
                            filial_interna  = melhor_opcao.get('dadosFrete', {}).get('filial', '')
                            id_metodo_envio = melhor_opcao.get('dadosFrete', {}).get('idMetodoEnvio', '')
                            pedido['AD_ID_COTACAO_ALLPOST'] = f"{id_cotacao}-{filial_interna}-{id_metodo_envio}"
                            logger.info(
                                f"[COT] 🔑 idCotacao composto: {pedido['AD_ID_COTACAO_ALLPOST']} "
                                f"(filialERP={filial_interna} | metodo={id_metodo_envio})"
                            )
                            
                            # Consultar o CODPARC correspondente ao ID AllPost
                            # FIX 2: Usar a função corretamente definida
                            transportadora_info = consultar_transportadora_por_idallpost(id_transportadora_allpost)
                            
                            if transportadora_info:
                                pedido['CODPARCTRANSP'] = transportadora_info['CODPARC']
                                pedido['AD_FRETE_PREVISTO_ALLPOST'] = valor_frete
                                pedido['AD_PRAZO_ENTREGA_ALLPOST'] = melhor_opcao.get('prazoEntregaTotal', 0)

                                # FIX 3: Usar a função corretamente definida
                                atualizado = atualizar_tgfcab(pedido)
                                
                                # Check the status of the update function
                                if atualizado and atualizado.get('status') == 'sucesso':
                                    logger.info(
                                        f"[COT] TGFCAB atualizado {nunota} "
                                        f"(CODPARC:{transportadora_info['CODPARC']} | "
                                        f"ID AllPost:{id_transportadora_allpost} | "
                                        f"Frete:{valor_frete})"
                                    )
                                else:
                                    logger.warning(f"[COT][ERRO] Falha atualização TGFCAB {nunota}")
                            else:
                                logger.error(f"[COT][ERRO] CODPARC não encontrado para ID AllPost: {id_transportadora_allpost}")
                        else:
                            logger.warning(f"[COT][ERRO] Nenhuma opção entrega válida pedido {nunota}")
                    else:
                        logger.warning(f"[COT][ERRO] Resposta cotação vazia {nunota}")
                else:
                    logger.warning(f"[COT][ERRO] idCotacao não retornado para o pedido {nunota}")
                    
                pedidos_processados += 1
            
            except Exception as e:
                logger.error(f"[COT][ERRO] Pedido {nunota}: {e}")
        
        # LINHAS 1533-1534 REMOVIDAS POIS ERAM UM 'except' DUPLICADO E INCORRETO

        tem_mais = len(registros) >= limite
        return {
            "tem_mais": tem_mais,
            "total_processado": pedidos_processados,
            "offset_proximo": offset + limite if tem_mais else 0,
            "pedidos_agrupados": pedidos_agrupados,
        }
    except Exception as e:
        logger.error(f"[COT][ERRO] Consulta geral: {e}")
        return {
            "tem_mais": False,
            "total_processado": 0,
            "offset_proximo": 0,
            "pedidos_agrupados": {},
        }
    finally:
        cotacao_lock.release()
        
@allpost_routes.route('/listar-transportadoras', methods=['GET'])
def listar_transportadoras_allpost():
    """
    Rota para consultar lista de transportadoras na API AllPost (externa)
    """
    try:
        url = "https://www.allpost.com.br/api/v1/transportadora/lista"
        token = os.getenv("ALLPOST_API_TOKEN") 

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }

        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return jsonify(response.json()), 200

    except Exception as e:
        logger.error(f"Erro ao listar transportadoras AllPost: {e}", exc_info=True)
        return jsonify({"error": "Falha ao consultar lista de transportadoras", "detalhes": str(e)}), 500
        
@sankhya_routes.route('/parse-date', methods=['GET'])
def test_date_parsing():
    """
    Utility route to test date parsing functionality
    """
    date_input = request.args.get('date')
    
    try:
        parsed_date = parse_date(date_input)
        return jsonify({
            'original_input': date_input,
            'parsed_date': parsed_date.isoformat(),
            'formatted_query_date': format_date_for_query(parsed_date),
            'timezone': str(parsed_date.tzinfo)
        }), 200
    except Exception as e:
        return jsonify({
            'error': 'Falha ao parsear data',
            'detalhes': str(e)
        }), 400
        
@sankhya_routes.route('/status-polling', methods=['GET'])
def status_polling():
    """
    Rota para verificar status do polling de notas fiscais
    """
    return jsonify({
        'status': 'Ativo' if polling_thread and polling_thread.is_alive() else 'Inativo',
        'ultimo_processamento': getattr(polling_thread, 'ultimo_processamento', 'Não iniciado'),
        'inicializado_em': getattr(polling_thread, 'inicializado_em', None),
        'ambiente': 'Produção' if rate_limit_per_minute == 1000 else 'Sandbox',
        'limite_requisicoes': f"{rate_limit_per_minute} por minuto"
    }), 200

@sankhya_routes.route('/config', methods=['GET', 'POST'])
def configuracao():
    """
    Rota para configurar parmetros da API
    """
    global rate_limit_per_minute
    
    if request.method == 'POST':
        try:
            data = request.json
            
            # Atualiza configurações
            if 'ambiente' in data:
                if data['ambiente'].lower() == 'producao':
                    rate_limit_per_minute = 1000
                    logger.info("Ambiente configurado para PRODUÇÃO (1000 req/min)")
                else:
                    rate_limit_per_minute = 20
                    logger.info("Ambiente configurado para SANDBOX (20 req/min)")
            
            return jsonify({
                'message': 'Configuração atualizada com sucesso',
                'ambiente': 'Produção' if rate_limit_per_minute == 1000 else 'Sandbox',
                'limite_requisicoes': f"{rate_limit_per_minute} por minuto"
            }), 200
            
        except Exception as e:
            logger.error(f"Erro ao atualizar configuração: {e}")
            return jsonify({
                'error': 'Falha ao atualizar configuração',
                'detalhes': str(e)
            }), 500
    else:
        # Retorna configuração atual
        return jsonify({
            'ambiente': 'Produção' if rate_limit_per_minute == 1000 else 'Sandbox',
            'limite_requisicoes': f"{rate_limit_per_minute} por minuto"
        }), 200

# Variáveis globais para controle de threading
_polling_thread = None
_polling_initialized = threading.Event()
_polling_lock = threading.Lock()

def iniciar_polling_nf():
    global _polling_thread

    with _polling_lock:
        if _polling_initialized.is_set():
            logger.warning("Polling de pedidos já foi inicializado. Ignorando nova tentativa.")
            return _polling_thread

        def job():
            thread = threading.current_thread()
            # Carrega data das notas fiscais (NFs)
            data_nf = carregar_data_nf()
            start_time = time.time()
            horario_execucao = datetime.now(pytz.timezone('America/Sao_Paulo'))
            logger.info(f"⏳ Iniciando ciclo de polling desde NF: {data_nf.isoformat()}")
            thread.ultimo_processamento = horario_execucao.isoformat()

            try:
                # --- POLLING DE NOTAS FISCAIS (TIPMOV=V) ---
                limite_por_lote = 50
                offset = 0
                tem_mais = True
                ciclos = 0
                max_ciclos = 10
                total_nfs = 0

                while tem_mais and ciclos < max_ciclos:
                    resultado = consultar_notas_fiscais_recentes(
                        data_nf,
                        limite=limite_por_lote,
                        offset=offset
                    )
                    if resultado is None or not all(k in resultado for k in ['ultima_data', 'tem_mais']):
                        logger.error("Resultado inválido em notas. Encerrando polling de notas.")
                        break
                    # Atualiza SOMENTE a data de NFs
                    data_nf = parse_date(resultado['ultima_data'])
                    salvar_data_nf(data_nf)
                    total_nfs += resultado.get('total_processado', 0)
                    tem_mais = resultado.get('tem_mais', False)
                    offset = resultado.get('offset_proximo', offset + limite_por_lote)
                    ciclos += 1
                    if tem_mais:
                        time.sleep(1)
                logger.info(f"Fim do polling de NFs: {total_nfs} notas processadas.")

                # --- POLLING DE PEDIDOS PARA COTAÇÃO (TIPMOV=P) ---
                offset_cotacao = 0
                tem_mais_cotacao = True
                ciclos_cotacao = 0
                total_pedidos = 0

                while tem_mais_cotacao and ciclos_cotacao < max_ciclos:
                    resultado_cotacao = consultar_pedidos_para_cotacao(
                        limite=limite_por_lote,
                        offset=offset_cotacao
                    )
                    if resultado_cotacao is None or not all(k in resultado_cotacao for k in ['tem_mais']):
                        logger.error("Resultado inválido em pedidos. Encerrando polling de cotações.")
                        break

                    # LOG DETALHADO DA INTEGRAÇÃO DE CADA PEDIDO
                    try:
                        if "pedidos_agrupados" in resultado_cotacao:
                            for nunota, pedido in resultado_cotacao["pedidos_agrupados"].items():
                                logger.info(f"Pedido {nunota}: processado no ciclo de cotação.")
                        else:
                            logger.info("Nenhum pedido agrupado retornado na integração do ciclo.")
                    except Exception as log_exc:
                        logger.error(f"Erro ao logar detalhes dos pedidos processados: {log_exc}")

                    total_pedidos += resultado_cotacao.get('total_processado', 0)
                    tem_mais_cotacao = resultado_cotacao.get('tem_mais', False)
                    offset_cotacao = resultado_cotacao.get('offset_proximo', offset_cotacao + limite_por_lote)
                    ciclos_cotacao += 1
                    if tem_mais_cotacao:
                        time.sleep(1)
                logger.info(f"Fim do polling de cotações: {total_pedidos} pedidos processados.")

                execution_time = time.time() - start_time
                logger.info(f"Ciclo completo finalizado em {execution_time:.2f}s.")

                # Logout e limpeza de token após ambos
                global bearer_token, token_expiration_time
                if bearer_token:
                    logout_sankhya(bearer_token)
                    bearer_token = None
                    token_expiration_time = None

            except Exception as e:
                if hasattr(thread, 'stats'):
                    thread.stats['erros'] += 1
                logger.error(f"Erro no ciclo de polling: {e}", exc_info=True)

        # Agendamento correto (agenda para rodar a cada 15 minutos)
        schedule.every(15).minutes.do(job)

        def run_schedule():
            logger.info("Thread de polling de notas fiscais iniciada.")
            job()  # executa a primeira vez imediatamente ao iniciar o servidor

            while True:
                schedule.run_pending()
                time.sleep(10)  # Verifica a cada 10 segundos se há jobs para rodar

        _polling_thread = threading.Thread(target=run_schedule, daemon=True)
        _polling_thread.inicializado_em = datetime.now().isoformat()
        _polling_thread.ultimo_processamento = "Inicializando..."
        _polling_thread.stats = {
            'ciclos_completos': 0,
            'notas_processadas': 0,
            'erros': 0,
            'ultima_execucao': None
        }

        _polling_thread.start()
        _polling_initialized.set()

        logger.info(f"Thread de polling de pedidos iniciada em {_polling_thread.inicializado_em}")
        return _polling_thread
