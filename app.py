import os
import time
import threading
import requests
import tempfile
from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync
from smailpro import SmailPro
from bs4 import BeautifulSoup
import speech_recognition as sr
from pydub import AudioSegment

app = Flask(__name__)
CORS(app)

# Shared state
lock = threading.Lock()
running = False
generated_keys = []
password = ""
status_msg = "Idle"

def solve_audio_captcha(page):
    """Free audio challenge solver for reCAPTCHA v2."""
    try:
        frame = page.frames[1]  # the recaptcha iframe
        # Click audio button
        frame.wait_for_selector('#recaptcha-audio-button', timeout=3000).click()
        # Get audio URL
        link = frame.wait_for_selector('.rc-audiochallenge-tdownload-link', timeout=3000)
        audio_url = link.get_attribute('href')

        # Download audio
        resp = requests.get(audio_url)
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
            tmp.write(resp.content)
            mp3_path = tmp.name

        # Convert to wav
        wav_path = mp3_path.replace('.mp3', '.wav')
        AudioSegment.from_mp3(mp3_path).export(wav_path, format='wav')
        os.unlink(mp3_path)

        # Recognize speech
        r = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = r.record(source)
        text = r.recognize_google(audio_data)
        os.unlink(wav_path)

        # Submit answer
        frame.fill('#audio-response', text)
        frame.click('#recaptcha-verify-button')
        return True
    except Exception as e:
        print(f"Audio solver error: {e}")
        return False

def generate_key(pw):
    """Core logic: signup, verify, extract API key, logout."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        stealth_sync(page)

        # 1. Temp email
        sm = SmailPro()
        email = sm.get_email()

        # 2. Open signup form
        page.goto("https://www.magnific.com/log-in?client_id=magnific&lang=en")
        page.wait_for_load_state("networkidle")
        # Accept cookies if popup
        try:
            page.click('button:has-text("Accept")', timeout=2000)
        except:
            pass

        page.click("text=Create account")
        page.fill("input[name='email']", email)
        page.fill("input[name='password']", pw)

        # 3. Solve captcha if appears
        time.sleep(2)
        if page.is_visible('iframe[src*="recaptcha"]'):
            solve_audio_captcha(page)

        # 4. Submit
        page.click("button[type='submit']")
        time.sleep(8)  # wait for email delivery

        # 5. Get verification link
        messages = sm.get_messages()
        vlink = None
        for msg in messages:
            if "verify" in msg.subject.lower() or "confirm" in msg.subject.lower():
                body = msg.html_body if hasattr(msg, 'html_body') else msg.body
                soup = BeautifulSoup(body, "html.parser")
                a = soup.find("a", href=True)
                if a:
                    vlink = a["href"]
                    break
        if not vlink:
            raise Exception("No verification email found")

        # 6. Verify account
        page.goto(vlink)
        page.wait_for_load_state("networkidle")

        # 7. Go to API page and get key
        page.goto("https://www.magnific.com/developers/dashboard/api-key")
        page.wait_for_load_state("networkidle")
        page.click("text=Get free API key")
        page.wait_for_selector('input[class*="api-key"]')
        key = page.input_value('input[class*="api-key"]')

        # 8. Logout
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
        time.sleep(15)  # cooldown to avoid rate limits

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
