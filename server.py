from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from dotenv import load_dotenv
import os
import json
import re
import threading
import time
from datetime import datetime

# Cargar variables de entorno
load_dotenv()

app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)

# Configuración desde variables de entorno
SECRET_KEY = os.getenv('SECRET_KEY', 'default-secret-key-change-me')
MAX_WORKERS = int(os.getenv('MAX_WORKERS', '5'))
API_TIMEOUT = int(os.getenv('API_TIMEOUT', '30'))
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')

app.config['SECRET_KEY'] = SECRET_KEY

# Variables globales de estado
checking_status = {
    'active': False,
    'processed': 0,
    'live': 0,
    'decline': 0,
    'threeds': 0,
    'error': 0,
    'current': '',
    'results': [],
    'thread': None
}

class PaymentAnalyzer:
    """Analizador de respuestas de pagos"""
    
    @staticmethod
    def analyze_response(response_data, raw_logs=""):
        """
        Analiza la respuesta del gateway de pago
        Retorna: {'status': 'live'|'decline'|'threeds'|'error', 'evidence': str, 'gate': str}
        """
        if not response_data:
            return {'status': 'error', 'evidence': 'No response data', 'gate': 'unknown'}
        
        # Convertir todo a minúsculas para comparación
        response_str = json.dumps(response_data).lower() if isinstance(response_data, dict) else str(response_data).lower()
        logs_lower = raw_logs.lower()
        
        evidence = []
        gate = "unknown"
        
        # ========== DETECCIÓN DE GATE ==========
        gate_patterns = {
            'stripe': [r'stripe\.com', r'pi_[a-z0-9_]+', r'payment_intent'],
            'square': [r'square\.com', r'sq0idp-[a-z0-9]+'],
            'braintree': [r'braintreegateway\.com', r'braintree'],
            'paypal': [r'paypal\.com', r'pp[a-z0-9]+'],
            'authorize': [r'authorize\.net', r'aim\.auth']
        }
        
        for gate_name, patterns in gate_patterns.items():
            for pattern in patterns:
                if re.search(pattern, response_str + logs_lower, re.IGNORECASE):
                    gate = gate_name
                    break
            if gate != "unknown":
                break
        
        # ========== DETECCIÓN DE STATUS ==========
        # 1. LIVE (Tarjeta válida)
        live_patterns = [
            r'status[\"\':\s]*1',                    # {status: 1}
            r'\"status\"\s*:\s*1',                  # "status": 1
            r'code=51',                            # code=51...
            r'success\.html',                      # success.html
            r'payment.*success',                   # payment success
            r'transaction.*complete',              # transaction complete
            r'approved',                           # approved
            r'\"result\"\s*:\s*\"success\"',       # "result": "success"
        ]
        
        # 2. DECLINE (Tarjeta rechazada)
        decline_patterns = [
            r'402\s*\(payment\s+required\)',       # 402 (Payment Required)
            r'card.*declined',                     # card declined
            r'insufficient.*funds',                # insufficient funds
            r'do.not.honor',                       # do not honor
            r'invalid.*account',                   # invalid account
            r'stolen.*card',                       # stolen card
            r'\"status\"\s*:\s*\"failed\"',        # "status": "failed"
            r'payment.*failed',                    # payment failed
        ]
        
        # 3. 3D SECURE (Requiere autenticación)
        threeds_patterns = [
            r'authentication\.cardinalcommerce\.com',
            r'threedsecure',
            r'creq\?',                             # CReq? (Cardinal Request)
            r'3d.*secure',
            r'cardinalcommerce',
            r'autofocusing.*cross-origin.*subframe',
            r'issuer.*authentication',
            r'acs\.',                              # Access Control Server
            r'redirect.*issuer',
        ]
        
        # Buscar patrones
        status = 'error'
        matched_pattern = ''
        
        # Primero buscar 3DS (tiene prioridad)
        for pattern in threeds_patterns:
            if re.search(pattern, response_str + logs_lower, re.IGNORECASE):
                status = 'threeds'
                matched_pattern = pattern
                evidence.append(f"3DS detected: {pattern}")
                break
        
        # Si no es 3DS, buscar LIVE
        if status == 'error':
            for pattern in live_patterns:
                if re.search(pattern, response_str + logs_lower, re.IGNORECASE):
                    status = 'live'
                    matched_pattern = pattern
                    evidence.append(f"Live detected: {pattern}")
                    break
        
        # Si no es LIVE, buscar DECLINE
        if status == 'error':
            for pattern in decline_patterns:
                if re.search(pattern, response_str + logs_lower, re.IGNORECASE):
                    status = 'decline'
                    matched_pattern = pattern
                    evidence.append(f"Decline detected: {pattern}")
                    break
        
        # Si aún no se detectó, buscar códigos HTTP
        if status == 'error':
            http_codes = re.findall(r'\b(\d{3})\b', response_str + logs_lower)
            for code in http_codes:
                if code == '200':
                    status = 'live'
                    evidence.append(f"HTTP 200 OK")
                    break
                elif code in ['402', '403', '500']:
                    status = 'decline'
                    evidence.append(f"HTTP {code} Error")
                    break
        
        # Si sigue sin detectar, usar análisis heurístico
        if status == 'error':
            if 'error' in response_str or 'failed' in response_str:
                status = 'decline'
                evidence.append("Heuristic: error/failed keywords")
            elif 'success' in response_str or 'complete' in response_str:
                status = 'live'
                evidence.append("Heuristic: success/complete keywords")
        
        return {
            'status': status,
            'evidence': evidence[:3],  # Solo primeros 3 elementos
            'gate': gate,
            'matched_pattern': matched_pattern
        }

# ========== ENDPOINTS API ==========

@app.route('/')
def index():
    """Servir la página principal"""

@app.route('/api/status', methods=['GET'])
def get_status():
    """Obtener estado actual del checker"""
    return jsonify({
        'active': checking_status['active'],
        'processed': checking_status['processed'],
        'live': checking_status['live'],
        'decline': checking_status['decline'],
        'threeds': checking_status['threeds'],
        'error': checking_status['error'],
        'current': checking_status['current'],
        'total': len(checking_status['results'])
    })

@app.route('/api/check', methods=['POST'])
def check_cards():
    """Iniciar verificación de tarjetas"""
    global checking_status
    
    if checking_status['active']:
        return jsonify({'error': 'Ya hay un chequeo en progreso'}), 400
    
    data = request.json
    cards = data.get('cards', [])
    amount = data.get('amount', 50)
    stop_on_live = data.get('stop_on_live', False)
    
    if not cards:
        return jsonify({'error': 'No hay tarjetas para verificar'}), 400
    
    # Inicializar estado
    checking_status = {
        'active': True,
        'processed': 0,
        'live': 0,
        'decline': 0,
        'threeds': 0,
        'error': 0,
        'current': '',
        'results': [],
        'thread': None
    }
    
    # Iniciar thread de verificación
    thread = threading.Thread(
        target=process_cards_worker,
        args=(cards, amount, stop_on_live)
    )
    thread.daemon = True
    thread.start()
    checking_status['thread'] = thread
    
    return jsonify({
        'message': f'Verificación iniciada para {len(cards)} tarjetas',
        'total': len(cards)
    })

@app.route('/api/results', methods=['GET'])
def get_results():
    """Obtener resultados del chequeo"""
    return jsonify({
        'results': checking_status['results'][-100:],  # Últimos 100 resultados
        'stats': {
            'total': len(checking_status['results']),
            'live': checking_status['live'],
            'decline': checking_status['decline'],
            'threeds': checking_status['threeds'],
            'error': checking_status['error']
        }
    })

@app.route('/api/cancel', methods=['POST'])
def cancel_check():
    """Cancelar chequeo en curso"""
    global checking_status
    checking_status['active'] = False
    return jsonify({'message': 'Chequeo cancelado'})

@app.route('/api/analyze', methods=['POST'])
def analyze_response():
    """Analizar una respuesta de pago específica"""
    data = request.json
    response_data = data.get('response', {})
    logs = data.get('logs', '')
    
    analyzer = PaymentAnalyzer()
    result = analyzer.analyze_response(response_data, logs)
    
    return jsonify(result)

# ========== WORKER FUNCTIONS ==========

def process_cards_worker(cards, amount, stop_on_live):
    """Worker que procesa las tarjetas"""
    global checking_status
    
    analyzer = PaymentAnalyzer()
    
    for i, card_line in enumerate(cards):
        if not checking_status['active']:
            break
        
        # Parsear tarjeta
        try:
            parts = card_line.strip().split('|')
            if len(parts) < 4:
                continue
            
            card_number = parts[0].strip()
            month = parts[1].strip()
            year = parts[2].strip()
            cvv = parts[3].strip()
            
            last4 = card_number[-4:]
            checking_status['current'] = f"****{last4}"
            
            # Simular verificación (EN PRODUCCIÓN AQUÍ IRÍA LA LÓGICA REAL DE PAGO)
            time.sleep(1)  # Simular delay
            
            # Generar respuesta simulada basada en el tipo de tarjeta
            result = simulate_payment_response(card_number, amount)
            analysis = analyzer.analyze_response(result['response'], result['logs'])
            
            # Crear resultado
            card_result = {
                'id': i + 1,
                'card': f"****{last4}",
                'full_card': card_line,
                'status': analysis['status'],
                'gate': analysis['gate'],
                'evidence': analysis['evidence'],
                'amount': amount,
                'timestamp': datetime.now().isoformat(),
                'response': result['response'],
                'logs': result['logs']
            }
            
            # Actualizar estadísticas
            checking_status['processed'] += 1
            checking_status['results'].append(card_result)
            
            if analysis['status'] == 'live':
                checking_status['live'] += 1
                if stop_on_live:
                    checking_status['active'] = False
                    break
            elif analysis['status'] == 'decline':
                checking_status['decline'] += 1
            elif analysis['status'] == 'threeds':
                checking_status['threeds'] += 1
            else:
                checking_status['error'] += 1
            
            # Pequeño delay entre tarjetas
            time.sleep(0.5)
            
        except Exception as e:
            print(f"Error processing card: {e}")
            continue
    
    checking_status['active'] = False

def simulate_payment_response(card_number, amount):
    """Simular diferentes respuestas de pago para testing"""
    last_digit = int(card_number[-1])
    
    # Basado en el último dígito, generar diferentes respuestas
    if last_digit % 3 == 0:  # LIVE (33%)
        response = {
            "status": 1,
            "data": {
                "id": f"tx_{int(time.time())}",
                "amount": amount,
                "currency": "MXN",
                "status": "succeeded",
                "payment_method": "card"
            },
            "message": "Payment successful"
        }
        logs = f"success.html?code=51...\n{{status: 1, data: {{...}}}}"
    
    elif last_digit % 3 == 1:  # DECLINE (33%)
        response = {
            "error": {
                "code": "payment_intent_payment_failed",
                "message": "Your card was declined.",
                "type": "card_error",
                "decline_code": "insufficient_funds"
            },
            "status": "failed"
        }
        logs = "POST https://api.stripe.com/v1/payment_intents/pi_XXXX/confirm 402 (Payment Required)"
    
    else:  # 3D SECURE (33%)
        response = {
            "status": "requires_action",
            "next_action": {
                "type": "redirect_to_url",
                "redirect_to_url": {
                    "url": f"https://authentication.cardinalcommerce.com/ThreeDSecure/V2_1_0/CReq?oid={int(time.time())}",
                    "return_url": "https://your-site.com/return"
                }
            }
        }
        logs = "authentication.cardinalcommerce.com/ThreeDSecure/V2_1_0/CReq?... Blocked autofocusing on a <input> element in a cross-origin subframe"
    
    return {
        'response': response,
        'logs': logs
    }

if __name__ == '__main__':
    port = int(os.getenv('PORT', 8080))
    debug = os.getenv('FLASK_ENV', 'production') == 'development'
    
    print(f"🚀 Server starting on port {port}")
    print(f"🔧 Config: MAX_WORKERS={MAX_WORKERS}, API_TIMEOUT={API_TIMEOUT}")
    print(f"📊 Log level: {LOG_LEVEL}")
    
    app.run(host='0.0.0.0', port=port, debug=debug)