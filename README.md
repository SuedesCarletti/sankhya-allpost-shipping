# integracaoallpost

Dois serviços Flask que ligam um ERP **Sankhya/Oracle** à plataforma de frete **AllPost**:

1. **Pedidos e NF-e** (`routes.py`, `allpost_integration.py`): *polling* a cada 15 min por notas faturadas e pedidos no Sankhya, envio ao AllPost e recebimento de webhooks de cotação. Guarda o último cursor processado (`ultima_data_nf.txt`, `ultima_nunota_pedido.txt`) para não reprocessar.
2. **Estoque** (`allpost_sync_service.py`, `estoque_fila_oracle.py`): fila de alterações de estoque consumida por um *worker* com buffer (agrupa produtos antes de enviar); endpoints de webhook, sincronização manual, `/health` e estatísticas.

Outros pontos: filtro de transportadora por local de estoque (`filtro_transportadora_codlocal.py`), cache e controle de taxa para o token do Sankhya, autenticação Basic nos webhooks.

Configuração por variáveis de ambiente (`.env`, não incluído). Docker e `start.sh` para execução.
