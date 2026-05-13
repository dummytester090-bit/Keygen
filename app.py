import os
import time
import threading
import requests
import tempfile
import json
from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync
from bs4 import BeautifulSoup
import speech_recognition as sr
from pydub import AudioSegment

app = Flask(__name__)
CORS(app)

lock = threading.Lock()
running = False
generated_keys = []
password = ""
status_msg = "Idle"

# ---------- Free mail.tm API (no external library) ----------
MAILTM_BASE = "https://api.mail.tm"
MAILTM_HEADERS = {"Content-Type": "application/json"}

def create_temp_email():
    """Create a new mail.tm account and return email address + password."""
    # Get a domain
    domains = requests.get(f"{MAILTM_BASE}/domains").json()
    domain = domains["hydra:member"][0]["domain"]
    # Generate random address
    import random, string
    username = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
    address = f"{username}@{domain}"
    password = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
    # Create account
    payload = {"address": address, "password": password}
    r = requests.post(f"{MAILTM_BASE}/accounts", json=payload, headers=MAILTM_HEADERS)
    if r.status_code != 201:
        raise Exception(f"Mail.tm account creation failed: {r.text}")
    return address, password

def get_inbox(address, password):
    """Login to mail.tm and return list of messages."""
    # Login
    payload = {"address": address, "password": password}
    r = requests.post(f"{MAILTM_BASE}/token", json=payload, headers=MAILTM_HEADERS)
    if r.status_code != 200:
        return []
    token = r.json()["token"]
    auth_headers = {"Authorization": f"Bearer {token}"}
    # Get messages
    r = requests.get(f"{MAILTM_BASE}/messages", headers=auth_headers)
    if r.status_code != 200:
        return []
    return r.json()["hydra:member"]

def get_message_html(address, password, msg_id):
    """Retrieve HTML body of a specific message."""
    payload = {"address": address, "password": password}
    r = requests.post(f"{MAILTM_BASE}/token", json=payload, headers=MAILTM_HEADERS)
    if r.status_code != 200:
        return ""
    token = r.json()["token"]
    auth_headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{MAILTM_BASE}/messages/{msg_id}", headers=auth_headers)
    if r.status_code != 200:
        return ""
    return r.json().get("html", [""])[0] if isinstance(r.json().get("html"), list) else r.json().get("html", "")

# ---------- Audio CAPTCHA solver ----------
def solve_audio_captcha(page):
    try:
        frame = page.frames[1]
        frame.wait_for_selector('#recaptcha-audio-button', timeout=3000).click()
        link = frame.wait_for_selector('.rc-audiochallenge-tdownload-link', timeout=3000)
        audio_url = link.get_attribute('href')
        resp = requests.get(audio_url)
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
            tmp.write(resp.content)
            mp3_path = tmp.name
        wav_path = mp3_path.replace('.mp3', '.wav')
        AudioSegment.from_mp3(mp3_path).export(wav_path, format='wav')
        os.unlink(mp3_path)
        r = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = r.record(source)
        text = r.recognize_google(audio_data)
        os.unlink(wav_path)
        frame.fill('#audio-response', text)
        frame.click('#recaptcha-verify-button')
        return True
    except Exception as e:
        print(f"Audio solver error: {e}")
        return False

# ---------- Main key generation ----------
def generate_key(pw):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        stealth_sync(page)

        # 1. Create temp email (mail.tm)
        email_addr, email_pw = create_temp_email()

        # 2. Magnific signup
        page.goto("https://www.magnific.com/log-in?client_id=magnific&lang=en")
        page.wait_for_load_state("networkidle")
        try:
            page.click('button:has-text("Accept")', timeout=2000)
        except:
            pass
        page.click("text=Create account")
        page.fill("input[name='email']", email_addr)
        page.fill("input[name='password']", pw)

        # 3. Solve CAPTCHA if shown
        time.sleep(2)
        if page.is_visible('iframe[src*="recaptcha"]'):
            solve_audio_captcha(page)

        page.click("button[type='submit']")
        time.sleep(8)

        # 4. Wait for verification email
        verification_link = None
        for _ in range(12):  # poll up to 1 minute
            messages = get_inbox(email_addr, email_pw)
            for msg in messages:
                subject = msg.get("subject", "")
                if "verify" in subject.lower() or "confirm" in subject.lower():
                    html = get_message_html(email_addr, email_pw, msg["id"])
                    soup = BeautifulSoup(html, "html.parser")
                    a = soup.find("a", href=True)
                    if a:
                        verification_link = a["href"]
                        break
            if verification_link:
                break
            time.sleep(5)

        if not verification_link:
            raise Exception("No verification link found")

        # 5. Verify account
        page.goto(verification_link)
        page.wait_for_load_state("networkidle")

        # 6. Get API key
        page.goto("https://www.magnific.com/developers/dashboard/api-key")
        page.wait_for_load_state("networkidle")
        page.click("text=Get free API key")
        page.wait_for_selector('input[class*="api-key"]')
        key = page.input_value('input[class*="api-key"]')

        # 7. Logout
        page.click("text=Log out")
        browser.close()
        return key

def loop():
    global running, password, generated_keys, status_msg
    while running:
        try:
            with lock:
                status_msg = "Creating account..."
            key = generate_key(password)
            with lock:
                generated_keys.append(key)
                status_msg = f"Key generated (#{len(generated_keys)})"
        except Exception as e:
            with lock:
                status_msg = f"Error: {e}"
        time.sleep(15)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/start', methods=['POST'])
def start():
    global running, password, status_msg
    data = request.get_json()
    if not data or 'password' not in data:
        return jsonify({"error": "password required"}), 400
    with lock:
        if running:
            return jsonify({"status": "already running"})
        password = data['password']
        running = True
        status_msg = "Starting..."
        threading.Thread(target=loop, daemon=True).start()
    return jsonify({"status": "started"})

@app.route('/api/stop', methods=['POST'])
def stop():
    global running, status_msg
    with lock:
        running = False
        status_msg = "Stopped"
    return jsonify({"status": "stopped"})

@app.route('/api/state')
def state():
    with lock:
        return jsonify({
            "running": running,
            "status": status_msg,
            "keys": generated_keys,
            "total": len(generated_keys)
        })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
