import os
import logging
import requests
from datetime import datetime

class AllPostIntegration:
    def __init__(self, app_token=None, api_token=None):
        """
        Initialize AllPost integration with configurable tokens
        """
        self.app_token = app_token or os.getenv('ALLPOST_APP_TOKEN')
        self.api_token = api_token or os.getenv('ALLPOST_API_TOKEN')
        self.base_url = os.getenv('ALLPOST_BASE_URL', 'https://www.allpost.com.br')
        self.api_endpoint = os.getenv('ALLPOST_API_ENDPOINT', '/api/v1/pedido/novo')
        self.logger = logging.getLogger(__name__)
    
    def validate_payload(self, payload):
        """
        Validates the payload structure before sending
        """
        required_keys = ['numeroPedido', 'dataEmissao', 'valorTotalProdutos', 'produtos', 'cliente']
        for key in required_keys:
            if key not in payload:
                raise ValueError(f"Payload está incompleto. Chave '{key}' ausente.")
    
    def send_order(self, payload=None, nota=None, endpoint=None):
        """
        Envio de pedido com tratamento robusto de respostas
        """
        try:
            # Usar o payload convertido ou o payload direto
            if nota:
                payload = self.convert_sankhya_payload(nota)
            
            # Validar payload
            self.validate_payload(payload)
            
            # Configurar endpoint
            endpoint = endpoint or self.api_endpoint
            full_url = f"{self.base_url}{endpoint}"
            
            # Configurar headers
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {self.api_token}',
                'X-App-Token': self.app_token
            }
            
            # Fazer requisição
            response = requests.post(
                full_url, 
                json=payload, 
                headers=headers,
                timeout=10  # Timeout de 10 segundos
            )
            
            # Verificar resposta
            if response.status_code in [200, 201]:
                response_data = response.json() if response.content else {}
                
                # Logs detalhados
                self.logger.info(f"Resposta completa: {response_data}")
                
                # Tratamento flexível de diferentes formatos de resposta
                chave_pedido = (
                    response_data.get('chavePedido') or 
                    response_data.get('chave_pedido') or 
                    response_data.get('pedido', {}).get('chave')
                )
                
                numero_pedido = (
                    response_data.get('numeroPedido') or 
                    response_data.get('numero_pedido') or 
                    response_data.get('pedido', {}).get('numero') or 
                    payload.get('numeroPedido')
                )
                
                # Mensagem de sucesso flexível
                sucesso = (
                    response_data.get('mensagem', '').lower() == 'sucesso' or
                    response_data.get('status', '').lower() == 'sucesso' or
                    response.status_code in [200, 201]
                )
                
                if sucesso:
                    return {
                        'status': 'sucesso',
                        'chavePedido': chave_pedido,
                        'numeroPedido': numero_pedido,
                        'dadosResposta': response_data,
                        'payload': payload
                    }
            
            # Tratamento de erros mais detalhado
            error_data = response.json() if response.content else {}
            error_message = (
                error_data.get('mensagem') or 
                error_data.get('message') or 
                f"Erro HTTP {response.status_code}"
            )
            
            self.logger.error(f"Erro na integração: {error_message}")
            
            return {
                'status': 'erro',
                'mensagem': error_message,
                'codigoHttp': response.status_code,
                'detalhes': error_data
            }
        
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Erro de conexão: {e}")
            return {
                'status': 'erro',
                'mensagem': f"Erro de conexão: {e}",
            }
        except Exception as e:
            self.logger.error(f"Erro inesperado: {e}")
            raise

    def convert_sankhya_payload(self, nota):
        """
        Converts Sankhya nota fiscal payload to AllPost format.
        """
        try:
            produtos = []
            valor_total_produtos = 0
            
            for item in nota.get('itens', []):
                quantidade = int(float(item.get('QTDNEG', 0)))
                valor_unitario = (
                    float(item.get('VLRUNIT', 0)) / quantidade 
                    if quantidade > 0 else 0
                )
                valor_item = quantidade * valor_unitario
                
                produtos.append({
                    'codigo': str(item.get('CODPROD', '')),
                    'nome': str(item.get('DESCRPROD', '')),
                    'quantidade': quantidade,
                    'valorUnitario': f"{valor_unitario:.2f}".replace(',', '.'),
                    'valorTotal': f"{valor_item:.2f}".replace(',', '.')
                })
                
                valor_total_produtos += valor_item
            
            payload = {
                'numeroPedido': str(nota.get('NUNOTA', '')),
                'dataEmissao': datetime.strptime(
                    nota.get('DTFATUR', ''), '%d/%m/%Y'
                ).strftime('%Y-%m-%d %H:%M:%S'),
                'valorTotalProdutos': f"{valor_total_produtos:.2f}".replace(',', '.'),
                'valorFrete': f"{float(nota.get('AD_VLRFRETEATACADO', 0)):.2f}".replace(',', '.'),
                'produtos': produtos,
                'cliente': {
                    'nome': str(nota.get('NOMEPARC', '')),
                    'cpfCnpj': str(nota.get('CGC_CPF', '')),
                    'email': str(nota.get('EMAIL', '')),
                    'telefone': str(nota.get('TELEFONE', '')),
                    'endereco': {
                        'logradouro': str(nota.get('ENDER', '')),
                        'numero': str(nota.get('NUMEND', '')),
                        'complemento': '',
                        'bairro': str(nota.get('BAIRRO', '')),
                        'cidade': str(nota.get('NOMECID', '')),
                        'estado': str(nota.get('UF_DESTINO', '')),
                        'cep': str(nota.get('CEP', ''))
                    }
                }
            }
            
            return payload
        
        except Exception as e:
            self.logger.error(f"Erro ao converter payload Sankhya: {e}")
            raise

    def process_tracking_webhook(self, data):
        """
        Processa webhooks de rastreamento de pedidos
        """
        try:
            # Log do rastreamento
            self.logger.info(f"Rastreamento de pedido: {data}")
            
            # Extrair informações importantes
            order_number = data.get('numeroPedido')
            tracking_status = data.get('situacao')
            
            return {
                'status': 'sucesso',
                'numeroPedido': order_number,
                'situacao': tracking_status
            }
        
        except Exception as e:
            self.logger.error(f"Erro no processamento de rastreamento: {e}")
            return {
                'status': 'erro',
                'mensagem': str(e)
            }

    def process_tracking_events(self, data):
        """
        Processa eventos de rastreamento detalhados
        """
        try:
            # Log do evento de rastreamento
            self.logger.info(f"Evento de rastreamento: {data}")
            
            # Extrair informações do evento
            order_number = data.get('numeroPedido')
            event_message = data.get('mensagem')
            
            return {
                'status': 'sucesso',
                'numeroPedido': order_number,
                'mensagem': event_message
            }
        
        except Exception as e:
            self.logger.error(f"Erro no processamento de eventos de rastreamento: {e}")
            return {
                'status': 'erro',
                'mensagem': str(e)
            }

    def process_additional_deadlines(self, data):
        """
        Processa alterações de prazos adicionais
        """
        try:
            # Log de mudanças de prazos
            self.logger.info(f"Alteração de prazos: {data}")
            
            # Extrair informações de prazos
            maior_prazo = data.get('maior', {})
            menor_prazo = data.get('menor', {})
            
            return {
                'status': 'sucesso',
                'prazos': {
                    'maior': maior_prazo,
                    'menor': menor_prazo
                }
            }
        
        except Exception as e:
            self.logger.error(f"Erro no processamento de prazos adicionais: {e}")
            return {
                'status': 'erro',
                'mensagem': str(e)
            }

    def process_app_uninstall(self, data):
        """
        Processa desinstalação de aplicativo
        """
        try:
            # Log de desinstalação
            self.logger.info(f"Desinstalação de aplicativo: {data}")
            
            # Extrair informações do aplicativo
            app_id = data.get('idApp')
            app_name = data.get('nomeApp')
            
            return {
                'status': 'sucesso',
                'aplicativo': {
                    'id': app_id,
                    'nome': app_name
                }
            }
        
        except Exception as e:
            self.logger.error(f"Erro no processamento de desinstalação: {e}")
            return {
                'status': 'erro',
                'mensagem': str(e)
            }

    def process_shipping_method(self, data):
        """
        Processa alterações de método de envio
        """
        try:
            # Log de mudança de método de envio
            self.logger.info(f"Alteração de método de envio: {data}")
            
            # Extrair informações do método
            method_status = data.get('situacao')
            carrier_id = data.get('idTransportadora')
            method_id = data.get('idMetodoEnvio')
            
            return {
                'status': 'sucesso',
                'metodo_envio': {
                    'situacao': method_status,
                    'transportadora_id': carrier_id,
                    'metodo_id': method_id
                }
            }
        
        except Exception as e:
            self.logger.error(f"Erro no processamento de método de envio: {e}")
            return {
                'status': 'erro',
                'mensagem': str(e)
            }

def integrar_allpost(notas, app_token=None, api_token=None):
    """
    Função de alto nível para integração de múltiplas notas
    """
    integrador = AllPostIntegration(app_token, api_token)
    
    resultados = []
    
    for nota in notas:
        try:
            resultado = integrador.send_order(nota=nota)
            resultados.append({
                'numero_nota': nota.get('NUNOTA'),
                'status': resultado.get('status', 'Sucesso'),
                'resposta_api': resultado
            })
        except Exception as e:
            resultados.append({
                'numero_nota': nota.get('NUNOTA'),
                'status': 'Erro',
                'erro': str(e)
            })
    
    return resultados