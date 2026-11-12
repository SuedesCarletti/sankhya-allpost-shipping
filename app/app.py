import os
from flask import Flask
from flask_cors import CORS
from dotenv import load_dotenv

# Carregar variáveis de ambiente
load_dotenv()

from routes import sankhya_routes, allpost_routes, iniciar_polling_nf
from allpost_sync_service import sync_service_bp, iniciar_worker
from estoque_fila_oracle import iniciar_polling_fila_estoque

# Criar aplicação Flask
app = Flask(__name__)
CORS(app, origins=[o.strip() for o in os.getenv('CORS_ORIGINS', '').split(',') if o.strip()])

# Registrar blueprints
app.register_blueprint(sankhya_routes, url_prefix='/sankhya')
app.register_blueprint(allpost_routes, url_prefix='/allpost')
app.register_blueprint(sync_service_bp) # Registra rotas como /sync/produtos, /health, etc.

# Exibe o mapa de rotas registradas para depuração
with app.app_context():
    print("Rotas registradas:")
    for rule in app.url_map.iter_rules():
        print(f"- Endpoint: {rule.endpoint}, Rota: {rule.rule}, Métodos: {','.join(rule.methods)}")

# Inicializar tarefas em background
iniciar_polling_nf()
iniciar_worker()
iniciar_polling_fila_estoque()

@app.route('/', methods=['GET'])
def health_check_main():
    """
    Rota principal de health check da aplicação unificada
    """
    return {
        'status': 'Aplicação Integrada Sankhya-AllPost',
        'version': '1.1.0',
        'environment': os.getenv('ENVIRONMENT', 'development'),
        'services': {
            'pedidos': 'Ativo',
            'estoque_sync': 'Ativo',
            'fila_estoque_oracle': 'Ativo'
        }
    }, 200

if __name__ == '__main__':
    # Usar a porta 5001 como padrão para alinhar com o Dockerfile
    port = int(os.getenv('PORT', 5001))
    
    # Iniciar aplicação Flask
    app.run(
        host='0.0.0.0', 
        port=port,
        debug=os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    )
