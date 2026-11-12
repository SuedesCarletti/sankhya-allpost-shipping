"""
Módulo para Filtro de Transportadoras baseado em CODLOCAL
==========================================================

CORREÇÃO: Estrutura da requisição loadRecords para API Sankhya

Autor: Suedes Carletti
Data Correção: 2025-12-16
"""

import logging
import requests
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# CONSTANTES
SANKHYA_API_BASE_URL = 'https://api.sankhya.com.br'

# MAPEAMENTO CODLOCAL → FILIAL
MAPEAMENTO_CODLOCAL_FILIAL = {
    '201': '1',  # CODLOCAL 201 → Filial 1 (CD Curitiba)
    '401': '6',  # CODLOCAL 401 → Filial 6 (CD Serra)
}

# FUNÇÃO 1: Consultar CODLOCAL dos Produtos (CORRIGIDA)
def consultar_codlocal_produtos(pedido: Dict[str, Any], sankhya_session) -> Dict[str, str]:
    """
    Consulta o CODLOCAL de cada produto no pedido.
    
    CORREÇÕES APLICADAS:
    - entity agora é uma lista (array) conforme documentação da API
    - Adicionado campo 'path' obrigatório em cada item de entity
    - Adicionados campos includePresentationFields e offsetPage
    
    Args:
        pedido: Dicionário com dados do pedido (incluindo lista de 'itens')
        sankhya_session: Sessão de requests autenticada com headers do Sankhya
                        Deve ter headers com 'Authorization' configurado
    
    Returns:
        Dict mapeando CODPROD → CODLOCAL
        Exemplo: {'434135': '201', '434079': '401'}
    """
    logger.info(f"[FILTRO] Consultando CODLOCAL dos produtos do pedido {pedido.get('NUNOTA')}")
    
    codlocal_por_produto = {}
    
    try:
        for item in pedido.get('itens', []):
            codprod = str(item.get('CODPROD'))
            
            # Consultar estoque do produto
            url = f"{SANKHYA_API_BASE_URL}/gateway/v1/mge/service.sbr"
            params = {
                'serviceName': 'CRUDServiceProvider.loadRecords',
                'outputType': 'json'
            }
            
            # ESTRUTURA CORRIGIDA conforme documentação
            body = {
                "serviceName": "CRUDServiceProvider.loadRecords",
                "requestBody": {
                    "dataSet": {
                        "rootEntity": "Estoque",
                        "includePresentationFields": "N",  # Adicionado
                        "offsetPage": "0",  # Adicionado
                        "criteria": {
                            "expression": {
                                "$": f"CODPROD = {codprod} AND ATIVO = 'S' AND ESTOQUE > 0 AND CODLOCAL IN ('201', '401')"
                            }
                        },
                        # CORREÇÃO PRINCIPAL: entity agora é uma LISTA com path
                        "entity": [
                            {
                                "path": "",  # path vazio para entidade raiz
                                "fieldset": {
                                    "list": "CODPROD,CODLOCAL,ESTOQUE,RESERVADO"
                                }
                            }
                        ]
                    }
                }
            }
            
            # Fazer requisição usando a sessão (que deve ter os headers configurados)
            response = sankhya_session.post(url, params=params, json=body, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            entities = data.get('responseBody', {}).get('entities', {})
            registros = entities.get('entity', [])
            
            if not isinstance(registros, list):
                registros = [registros] if registros else []
            
            # Processar resultados e selecionar o CODLOCAL com maior estoque disponível
            melhor_local = None
            maior_estoque = -1
            
            for registro in registros:
                # Os campos vêm como f0, f1, f2, f3 na ordem definida no fieldset
                # CODPROD, CODLOCAL, ESTOQUE, RESERVADO
                codlocal = str(registro.get('f1', {}).get('$', ''))  # f1 = CODLOCAL
                estoque = float(registro.get('f2', {}).get('$', 0))  # f2 = ESTOQUE
                reservado = float(registro.get('f3', {}).get('$', 0))  # f3 = RESERVADO
                disponivel = estoque - reservado
                
                if disponivel > maior_estoque:
                    maior_estoque = disponivel
                    melhor_local = codlocal
            
            if melhor_local:
                codlocal_por_produto[codprod] = melhor_local
                logger.info(f"[FILTRO] Produto {codprod} → CODLOCAL {melhor_local} (estoque: {maior_estoque})")
            else:
                logger.warning(f"[FILTRO] Produto {codprod} não encontrado em CODLOCAL 201 ou 401")
        
        return codlocal_por_produto
        
    except requests.exceptions.HTTPError as e:
        logger.error(f"[FILTRO] Erro HTTP ao consultar CODLOCAL dos produtos: {e}")
        logger.error(f"[FILTRO] Resposta da API: {e.response.text if hasattr(e, 'response') else 'N/A'}")
        return {}
    except Exception as e:
        logger.error(f"[FILTRO] Erro ao consultar CODLOCAL dos produtos: {e}", exc_info=True)
        return {}


# FUNÇÃO 2: Determinar Filial Permitida
def determinar_filial_permitida(codlocal_produtos: Dict[str, str]) -> Optional[str]:
    """
    Determina qual filial deve ser usada baseado nos CODLOCALs dos produtos.
    
    Regras:
    - Se todos os produtos estão no mesmo CODLOCAL → retorna a filial correspondente
    - Se produtos em CODLOCALs diferentes → retorna None (sem filtro, usar lógica padrão)
    
    Args:
        codlocal_produtos: Dict mapeando CODPROD → CODLOCAL
    
    Returns:
        String com a filialERP permitida ('1' ou '6') ou None se não houver consenso
    """
    if not codlocal_produtos:
        logger.warning("[FILTRO] Nenhum CODLOCAL encontrado. Sem filtro de filial.")
        return None
    
    # Obter conjunto de CODLOCALs únicos
    codlocais_unicos = set(codlocal_produtos.values())
    
    # Caso 1: Todos no mesmo CODLOCAL
    if len(codlocais_unicos) == 1:
        codlocal = list(codlocais_unicos)[0]
        filial_permitida = MAPEAMENTO_CODLOCAL_FILIAL.get(codlocal)
        
        if filial_permitida:
            logger.info(f"[FILTRO] Todos produtos no CODLOCAL {codlocal} → Filial {filial_permitida} obrigatória")
            return filial_permitida
        else:
            logger.warning(f"[FILTRO] CODLOCAL {codlocal} não mapeado para filial")
            return None
    
    # Caso 2: Produtos em CODLOCALs diferentes
    else:
        logger.warning(f"[FILTRO] Produtos em múltiplos CODLOCALs: {codlocais_unicos}. Sem filtro de filial.")
        return None


# FUNÇÃO 3: Filtrar Opções de Entrega
def filtrar_opcoes_por_filial(opcoes_entrega: List[Dict], filial_permitida: str) -> List[Dict]:
    """
    Filtra opções de entrega mantendo apenas as da filial permitida.
    
    Args:
        opcoes_entrega: Lista de opções retornadas pela AllPost
        filial_permitida: String com o código da filial ('1' ou '6')
    
    Returns:
        Lista filtrada de opções
    """
    if not filial_permitida:
        logger.info("ℹ[FILTRO] Sem filial específica. Retornando todas as opções.")
        return opcoes_entrega
    
    logger.info(f"[FILTRO] Filtrando opções para Filial {filial_permitida}")
    
    opcoes_filtradas = []
    
    for opcao in opcoes_entrega:
        dados_frete = opcao.get('dadosFrete', {})
        filial_erp = str(dados_frete.get('filialERP', ''))
        transportadora = dados_frete.get('transportadoraNome', 'N/A')
        
        if filial_erp == filial_permitida:
            opcoes_filtradas.append(opcao)
            logger.info(f"  Aceita: {transportadora} (Filial {filial_erp})")
        else:
            logger.info(f"  Rejeitada: {transportadora} (Filial {filial_erp} ≠ {filial_permitida})")
    
    logger.info(f"[FILTRO] Resultado: {len(opcoes_filtradas)}/{len(opcoes_entrega)} opções válidas")
    
    return opcoes_filtradas


# FUNÇÃO 4: Selecionar Melhor Opção (com Filtro)
def selecionar_melhor_opcao_com_filtro(
    opcoes_entrega: List[Dict],
    codlocal_produtos: Dict[str, str],
    criterio: str = 'frete'  # 'frete' ou 'prazo'
) -> Optional[Dict]:
    """
    Seleciona a melhor opção de entrega aplicando o filtro de CODLOCAL.
    
    Args:
        opcoes_entrega: Lista de opções retornadas pela AllPost
        codlocal_produtos: Dict mapeando CODPROD → CODLOCAL
        criterio: 'frete' para menor valor ou 'prazo' para menor prazo
    
    Returns:
        Dicionário com a melhor opção ou None se não houver opções válidas
    """
    if not opcoes_entrega:
        logger.error("[FILTRO] Nenhuma opção de entrega disponível")
        return None
    
    logger.info(f"[FILTRO] Selecionando melhor opção (critério: {criterio})")
    
    # 1. Determinar filial permitida
    filial_permitida = determinar_filial_permitida(codlocal_produtos)
    
    # 2. Filtrar opções
    opcoes_filtradas = filtrar_opcoes_por_filial(opcoes_entrega, filial_permitida)
    
    # 3. Se não houver opções filtradas, usar todas (fallback)
    if not opcoes_filtradas:
        logger.warning("[FILTRO] Nenhuma opção passou no filtro. Usando todas as opções como fallback.")
        opcoes_filtradas = opcoes_entrega
    
    # 4. Selecionar melhor opção baseado no critério
    if criterio == 'prazo':
        melhor = min(opcoes_filtradas, key=lambda x: int(x.get('prazoEntregaTotal', 999999)))
        logger.info(f"[FILTRO] Melhor prazo: {melhor['prazoEntregaTotal']} dias")
    else:  # 'frete'
        melhor = min(opcoes_filtradas, key=lambda x: float(x.get('freteCobrar', 999999)))
        logger.info(f"[FILTRO] Melhor frete: R$ {melhor['freteCobrar']}")
    
    # 5. Log da opção selecionada
    dados_frete = melhor.get('dadosFrete', {})
    logger.info(f"[FILTRO] Opção selecionada:")
    logger.info(f"  • Transportadora: {dados_frete.get('transportadoraNome')}")
    logger.info(f"  • Filial: {dados_frete.get('filialNome')} (ERP: {dados_frete.get('filialERP')})")
    logger.info(f"  • Frete: R$ {melhor.get('freteCobrar')}")
    logger.info(f"  • Prazo: {melhor.get('prazoEntregaTotal')} dias")
    
    return melhor

def selecionar_segundo_melhor_frete(opcoes_entrega, codlocal_produtos, melhor_opcao):
    """
    Retorna a segunda melhor opção de frete (menor freteCobrar, excluindo a melhor).
    Respeita o mesmo filtro de filial da melhor opção.
    """
    if not opcoes_entrega or not melhor_opcao:
        return None

    try:
        filial_permitida = determinar_filial_permitida(codlocal_produtos)
        opcoes_filtradas = filtrar_opcoes_por_filial(opcoes_entrega, filial_permitida)

        if not opcoes_filtradas:
            opcoes_filtradas = opcoes_entrega  # fallback sem filtro

        # Excluir a melhor opção já selecionada
        id_melhor = (
            melhor_opcao.get('dadosFrete', {}).get('idTransportadora'),
            melhor_opcao.get('dadosFrete', {}).get('idMetodoEnvio')
        )

        outras_opcoes = [
            o for o in opcoes_filtradas
            if (
                o.get('dadosFrete', {}).get('idTransportadora'),
                o.get('dadosFrete', {}).get('idMetodoEnvio')
            ) != id_melhor
        ]

        if not outras_opcoes:
            logger.info("[COT] Não há segunda opção de frete disponível.")
            return None

        segundo = min(outras_opcoes, key=lambda x: float(x.get('freteCobrar', 999999)))
        logger.info(
            f"[COT] 2ª opção: {segundo.get('dadosFrete', {}).get('transportadoraNome')} "
            f"- R$ {segundo.get('freteCobrar')}"
        )
        return segundo

    except Exception as e:
        logger.error(f"[COT] Erro ao selecionar segundo melhor frete: {e}")
        return None

# FUNÇÃO 5: Validar Mapeamento CODLOCAL → FILIAL
def validar_mapeamento() -> bool:
    """
    Valida se o mapeamento CODLOCAL → FILIAL está correto.
    
    Returns:
        True se válido, False caso contrário
    """
    esperado = {
        '201': '1',
        '401': '6'
    }
    
    if MAPEAMENTO_CODLOCAL_FILIAL == esperado:
        logger.info("[FILTRO] Mapeamento CODLOCAL → FILIAL válido")
        return True
    else:
        logger.error("[FILTRO] Mapeamento CODLOCAL → FILIAL incorreto!")
        return False
