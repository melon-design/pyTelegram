import os
import sqlite3
import hashlib
import hmac
import secrets
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template_string, redirect, url_for,
    request, flash, session, g
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "funcom.db")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FUNCOM_SECRET_KEY", secrets.token_hex(32))

STAR_PRICE = 1.33  # цена одной звезды в рублях
MIN_STARS = 50      # минимальное количество звёзд к покупке
QUICK_AMOUNTS = [500, 1000, 2500, 5000]        # быстрые суммы пополнения
QUICK_QUANTITIES = [50, 100, 250, 500, 1000, 2500]  # быстрые количества звёзд


# ================= База данных (sqlite3, без ORM) =================

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS user (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            balance REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS "order" (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product TEXT NOT NULL,
            recipient TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            total_price REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES user (id)
        )
    """)
    db.commit()
    db.close()


# ================= Пароли (hashlib, без сторонних либ) =================

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        salt, digest_hex = stored.split("$")
    except ValueError:
        return False
    new_digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return hmac.compare_digest(new_digest.hex(), digest_hex)


# ================= Помощники авторизации =================

def current_user():
    if "user_id" not in session:
        return None
    if "_user_cache" not in g:
        db = get_db()
        g._user_cache = db.execute(
            "SELECT * FROM user WHERE id = ?", (session["user_id"],)
        ).fetchone()
    return g._user_cache


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            flash("Войдите, чтобы продолжить.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_user():
    return {"current_user": current_user()}


# ================= Дизайн-система: общие стили =================

BASE_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{% block title %}Funcom — магазин Telegram Stars{% endblock %}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@500;600;700;800&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#08080a; --bg-soft:#0d0c10;
  --surface:#131218; --surface-alt:#1b1a21; --surface-hover:#201f27;
  --border:#242330; --border-strong:#332f3d;
  --red:#ff3347; --red-dim:#c81f34; --red-glow:rgba(255,51,71,.28); --ember:#ff7a45;
  --text:#f4f2f5; --text-dim:#9d97a6; --text-faint:#615c6b;
  --success:#35d488; --success-bg:rgba(53,212,136,.12);
  --warning:#ffb445; --warning-bg:rgba(255,180,69,.12);
  --danger-bg:rgba(255,51,71,.12);
  --radius-sm:10px; --radius:16px; --radius-lg:24px;
  --font-display:'Unbounded',sans-serif; --font-body:'Inter',sans-serif; --font-mono:'JetBrains Mono',monospace;
}
*{box-sizing:border-box;}
html{scroll-behavior:smooth;}
body{
  margin:0; background:
    radial-gradient(1200px 500px at 15% -10%, rgba(255,51,71,.14), transparent 60%),
    radial-gradient(900px 400px at 100% 0%, rgba(255,122,69,.08), transparent 55%),
    var(--bg);
  color:var(--text); font-family:var(--font-body); line-height:1.5; -webkit-font-smoothing:antialiased;
}
a{color:inherit; text-decoration:none;}
.container{max-width:1120px; margin:0 auto; padding:0 24px;}
::selection{background:var(--red); color:#fff;}
:focus-visible{outline:2px solid var(--red); outline-offset:2px; border-radius:6px;}

/* ---------- Navbar ---------- */
.navbar{position:sticky; top:0; z-index:50; background:rgba(8,8,10,.82); backdrop-filter:blur(14px); border-bottom:1px solid var(--border);}
.navbar-inner{display:flex; align-items:center; justify-content:space-between; height:72px; gap:20px;}
.logo{font-family:var(--font-display); font-weight:700; font-size:21px; letter-spacing:-.01em; color:#fff; display:flex; align-items:center; gap:8px;}
.logo .dot{width:7px; height:7px; border-radius:50%; background:var(--red); box-shadow:0 0 10px var(--red);}
.logo span{color:var(--red);}
.nav-links{display:flex; align-items:center; gap:6px;}
.nav-link{padding:9px 14px; border-radius:var(--radius-sm); color:var(--text-dim); font-size:14.5px; font-weight:500; transition:.15s;}
.nav-link:hover{color:var(--text); background:var(--surface-alt);}
.nav-divider{width:1px; height:24px; background:var(--border); margin:0 6px;}
.balance-pill{display:flex; align-items:center; gap:7px; background:var(--surface-alt); border:1px solid var(--border-strong); padding:8px 14px; border-radius:999px; font-family:var(--font-mono); font-weight:600; font-size:13.5px; color:#fff;}
.balance-pill .star{color:var(--red);}

/* ---------- Buttons ---------- */
.btn{display:inline-flex; align-items:center; justify-content:center; gap:8px; border:none; cursor:pointer; font-family:var(--font-body); font-weight:600; font-size:14.5px; border-radius:var(--radius-sm); padding:12px 20px; transition:.15s ease;}
.btn-primary{background:var(--red); color:#fff; box-shadow:0 0 0 rgba(255,51,71,0); }
.btn-primary:hover{background:#ff4f60; box-shadow:0 6px 24px -6px var(--red-glow); transform:translateY(-1px);}
.btn-primary:active{transform:translateY(0);}
.btn-ghost{background:transparent; color:var(--text-dim); border:1px solid var(--border-strong);}
.btn-ghost:hover{color:#fff; border-color:var(--red); background:rgba(255,51,71,.06);}
.btn-block{width:100%;}
.btn-lg{padding:15px 24px; font-size:15.5px;}
.btn[disabled]{opacity:.45; cursor:not-allowed; transform:none !important; box-shadow:none !important;}

/* ---------- Ticker (фирменный элемент) ---------- */
.ticker{overflow:hidden; background:var(--surface); border-top:1px solid var(--border); border-bottom:1px solid var(--border);}
.ticker-track{display:flex; width:max-content; animation:scroll-ticker 24s linear infinite;}
@keyframes scroll-ticker{from{transform:translateX(0);} to{transform:translateX(-50%);}}
.ticker-item{display:flex; align-items:center; gap:10px; padding:13px 30px; font-family:var(--font-mono); font-size:12.5px; letter-spacing:.06em; text-transform:uppercase; color:var(--text-dim); border-right:1px solid var(--border); white-space:nowrap;}
.ticker-item b{color:#fff; font-weight:700;}
.ticker-item .up{color:var(--success);}
.pulse-dot{width:6px; height:6px; border-radius:50%; background:var(--success); box-shadow:0 0 8px var(--success); animation:pulse 1.6s ease-in-out infinite;}
@keyframes pulse{0%,100%{opacity:1;} 50%{opacity:.35;}}

/* ---------- Hero ---------- */
.hero{padding:76px 0 44px; text-align:center;}
.hero-badge{display:inline-flex; align-items:center; gap:8px; background:var(--surface-alt); border:1px solid var(--border-strong); padding:7px 16px; border-radius:999px; font-size:13px; color:var(--text-dim); margin-bottom:26px;}
.hero-badge b{color:var(--red); font-weight:700;}
.hero h1{font-family:var(--font-display); font-weight:700; font-size:clamp(32px,5vw,54px); line-height:1.08; letter-spacing:-.02em; margin:0 0 18px; color:#fff;}
.hero h1 .accent{color:var(--red);}
.hero p{color:var(--text-dim); font-size:17px; max-width:560px; margin:0 auto 34px;}
.hero-actions{display:flex; gap:12px; justify-content:center; flex-wrap:wrap;}

/* ---------- Trust strip ---------- */
.trust-strip{display:flex; flex-wrap:wrap; justify-content:center; gap:10px; padding:36px 0 8px;}
.trust-item{display:flex; align-items:center; gap:9px; background:var(--surface); border:1px solid var(--border); padding:11px 18px; border-radius:999px; font-size:13.5px; color:var(--text-dim);}
.trust-item .ic{font-size:15px;}

/* ---------- Section headers ---------- */
.section{padding:52px 0;}
.section-head{display:flex; align-items:baseline; justify-content:space-between; margin-bottom:22px; flex-wrap:wrap; gap:8px;}
.section-head h2{font-family:var(--font-display); font-size:22px; font-weight:600; color:#fff; margin:0;}
.section-head .hint{color:var(--text-faint); font-size:13.5px;}

/* ---------- Product cards ---------- */
.products-grid{display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:16px;}
.product-card{position:relative; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); padding:26px; overflow:hidden; transition:.2s ease;}
.product-card::before{content:''; position:absolute; inset:0; background:radial-gradient(400px 140px at 100% 0%, var(--red-glow), transparent 70%); opacity:0; transition:.25s;}
.product-card:hover{border-color:var(--border-strong); transform:translateY(-2px);}
.product-card:hover::before{opacity:1;}
.product-top{display:flex; align-items:center; justify-content:space-between; margin-bottom:18px;}
.product-icon{width:48px; height:48px; border-radius:14px; background:linear-gradient(145deg,var(--red),var(--ember)); display:flex; align-items:center; justify-content:center; font-size:22px; box-shadow:0 8px 20px -8px var(--red-glow);}
.stock-badge{font-size:11.5px; font-weight:600; padding:5px 10px; border-radius:999px; background:var(--success-bg); color:var(--success); letter-spacing:.02em;}
.stock-badge.soon{background:var(--warning-bg); color:var(--warning);}
.product-card h3{font-family:var(--font-display); font-size:17px; font-weight:600; color:#fff; margin:0 0 6px;}
.product-desc{color:var(--text-dim); font-size:13.5px; margin:0 0 18px; line-height:1.5;}
.product-price{font-family:var(--font-mono); font-size:22px; font-weight:700; color:#fff; margin-bottom:2px;}
.product-price .unit{font-size:12.5px; color:var(--text-faint); font-weight:500;}
.product-vendor{font-size:12px; color:var(--text-faint); margin-bottom:18px;}
.product-card .btn{width:100%; margin-top:4px;}
.product-card.soon{opacity:.5;}
.product-card.soon:hover{transform:none;}

/* ---------- Auth ---------- */
.auth-wrap{min-height:calc(100vh - 260px); display:flex; align-items:center; justify-content:center; padding:60px 0;}
.auth-box{width:100%; max-width:400px; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-lg); padding:36px; position:relative;}
.auth-box::before{content:''; position:absolute; top:-1px; left:10%; right:10%; height:1px; background:linear-gradient(90deg,transparent,var(--red),transparent);}
.auth-icon{width:52px; height:52px; border-radius:14px; background:linear-gradient(145deg,var(--red),var(--ember)); display:flex; align-items:center; justify-content:center; font-size:22px; margin-bottom:20px;}
.auth-box h2{font-family:var(--font-display); font-size:22px; font-weight:600; color:#fff; margin:0 0 8px;}
.auth-note{color:var(--text-dim); font-size:13.5px; margin:0 0 22px; line-height:1.5;}
.auth-note.warn{color:var(--warning);}
.auth-switch{text-align:center; margin-top:22px; font-size:14px; color:var(--text-dim);}
.auth-switch a{color:var(--red); font-weight:600;}

form label{display:block; margin:16px 0 7px; font-size:13.5px; font-weight:500; color:var(--text-dim);}
form label:first-of-type{margin-top:0;}
form input{width:100%; padding:12px 14px; border-radius:var(--radius-sm); border:1px solid var(--border-strong); background:var(--bg-soft); color:#fff; font-size:15px; font-family:var(--font-body); transition:.15s;}
form input::placeholder{color:var(--text-faint);}
form input:focus{outline:none; border-color:var(--red); box-shadow:0 0 0 3px rgba(255,51,71,.15);}

.input-prefix{display:flex; align-items:center; background:var(--bg-soft); border:1px solid var(--border-strong); border-radius:var(--radius-sm); overflow:hidden; transition:.15s;}
.input-prefix:focus-within{border-color:var(--red); box-shadow:0 0 0 3px rgba(255,51,71,.15);}
.input-prefix span{padding:0 4px 0 14px; color:var(--text-faint); font-family:var(--font-mono);}
.input-prefix input{border:none; background:transparent; box-shadow:none !important;}

/* ---------- Chips (quick select) ---------- */
.chip-row{display:flex; flex-wrap:wrap; gap:8px; margin-top:4px;}
.chip{padding:8px 14px; border-radius:999px; border:1px solid var(--border-strong); background:var(--bg-soft); color:var(--text-dim); font-family:var(--font-mono); font-size:13px; font-weight:600; cursor:pointer; transition:.15s;}
.chip:hover{border-color:var(--red); color:#fff;}
.chip.active{background:var(--red); border-color:var(--red); color:#fff;}

/* ---------- Product / purchase page ---------- */
.buy-layout{display:grid; grid-template-columns:1.1fr .9fr; gap:20px; align-items:start; max-width:820px; margin:44px auto;}
@media (max-width:760px){.buy-layout{grid-template-columns:1fr;}}
.panel{background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-lg); padding:30px;}
.buy-header{display:flex; align-items:center; gap:14px; margin-bottom:22px;}
.buy-header h2{font-family:var(--font-display); font-size:19px; font-weight:600; color:#fff; margin:0;}
.buy-header .rate{color:var(--text-dim); font-size:13px; font-family:var(--font-mono); margin-top:2px;}
.summary-panel{position:sticky; top:96px;}
.summary-row{display:flex; justify-content:space-between; align-items:center; padding:12px 0; border-bottom:1px solid var(--border); font-size:14px; color:var(--text-dim);}
.summary-row:last-of-type{border-bottom:none;}
.summary-row .val{color:#fff; font-family:var(--font-mono); font-weight:600;}
.summary-total{display:flex; justify-content:space-between; align-items:center; margin:18px 0 20px; padding-top:16px; border-top:1px dashed var(--border-strong);}
.summary-total .label{color:var(--text-dim); font-size:14px;}
.summary-total .amount{font-family:var(--font-mono); font-size:26px; font-weight:700; color:var(--red);}
.balance-note{font-size:13px; color:var(--text-faint); text-align:center; margin-top:14px;}
.balance-note a{color:var(--red); font-weight:600;}

/* ---------- Dashboard ---------- */
.dash-head{display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:16px; margin-bottom:28px;}
.dash-head h1{font-family:var(--font-display); font-size:26px; font-weight:600; color:#fff; margin:0 0 4px;}
.dash-head .sub{color:var(--text-dim); font-size:14px;}
.balance-card{background:linear-gradient(135deg, var(--surface-alt), var(--surface)); border:1px solid var(--border-strong); border-radius:var(--radius-lg); padding:28px 30px; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:20px; margin-bottom:28px; position:relative; overflow:hidden;}
.balance-card::after{content:''; position:absolute; top:-40%; right:-10%; width:260px; height:260px; background:radial-gradient(circle, var(--red-glow), transparent 70%);}
.balance-card .label{color:var(--text-dim); font-size:13px; margin-bottom:6px;}
.balance-card .amount{font-family:var(--font-mono); font-size:34px; font-weight:700; color:#fff;}
.balance-actions{display:flex; gap:10px; position:relative; z-index:1;}

.orders-card{background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-lg); overflow:hidden;}
.orders-table{width:100%; border-collapse:collapse;}
.orders-table th{text-align:left; padding:14px 20px; font-size:12px; text-transform:uppercase; letter-spacing:.05em; color:var(--text-faint); font-weight:600; border-bottom:1px solid var(--border);}
.orders-table td{padding:16px 20px; border-bottom:1px solid var(--border); font-size:14px; color:var(--text-dim);}
.orders-table tr:last-child td{border-bottom:none;}
.orders-table td.recipient{color:#fff; font-weight:500;}
.orders-table td.qty{font-family:var(--font-mono); color:#fff;}
.orders-table td.price{font-family:var(--font-mono); color:#fff; font-weight:600;}
.status-pill{display:inline-flex; align-items:center; gap:6px; padding:5px 12px; border-radius:999px; font-size:12px; font-weight:600;}
.status-completed{background:var(--success-bg); color:var(--success);}
.status-pending{background:var(--warning-bg); color:var(--warning);}

.empty-state{padding:56px 24px; text-align:center;}
.empty-state .ic{font-size:34px; margin-bottom:12px;}
.empty-state p{color:var(--text-dim); margin:0 0 20px; font-size:14.5px;}

/* ---------- Flashes ---------- */
.flashes{margin-top:20px; display:flex; flex-direction:column; gap:10px;}
.flash{padding:13px 16px; border-radius:var(--radius-sm); font-size:13.5px; font-weight:500; border:1px solid transparent;}
.flash-success{background:var(--success-bg); color:var(--success); border-color:rgba(53,212,136,.25);}
.flash-danger{background:var(--danger-bg); color:#ff6b78; border-color:rgba(255,51,71,.3);}
.flash-warning{background:var(--warning-bg); color:var(--warning); border-color:rgba(255,180,69,.25);}

/* ---------- Footer ---------- */
.footer{border-top:1px solid var(--border); margin-top:60px; padding:32px 0;}
.footer-inner{display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;}
.footer p{color:var(--text-faint); font-size:13px; margin:0;}
.footer .foot-links{display:flex; gap:18px;}
.footer .foot-links a{color:var(--text-faint); font-size:13px;}
.footer .foot-links a:hover{color:var(--red);}

@media (prefers-reduced-motion: reduce){
  *{animation-duration:.001ms !important; animation-iteration-count:1 !important; transition-duration:.001ms !important;}
}
@media (max-width:640px){
  .nav-links .nav-link{display:none;}
  .navbar-inner{height:64px;}
  .hero{padding:52px 0 32px;}
}
</style>
</head>
<body>
<header class="navbar">
  <div class="container navbar-inner">
    <a class="logo" href="{{ url_for('index') }}"><span class="dot"></span>Fun<span>com</span></a>
    <nav class="nav-links">
      <a class="nav-link" href="{{ url_for('index') }}">Каталог</a>
      {% if current_user %}
        <a class="nav-link" href="{{ url_for('dashboard') }}">Кабинет</a>
        <div class="nav-divider"></div>
        <a class="balance-pill" href="{{ url_for('topup') }}"><span class="star">⭐</span>{{ "%.2f"|format(current_user['balance']) }} ₽</a>
        <a class="nav-link" href="{{ url_for('logout') }}">Выйти</a>
      {% else %}
        <a class="nav-link" href="{{ url_for('login') }}">Войти</a>
        <a class="btn btn-primary" href="{{ url_for('register') }}">Регистрация</a>
      {% endif %}
    </nav>
  </div>
</header>

<main>
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
      <div class="container"><div class="flashes">
        {% for category, message in messages %}
          <div class="flash flash-{{ category }}">{{ message }}</div>
        {% endfor %}
      </div></div>
    {% endif %}
  {% endwith %}
  {% block content %}{% endblock %}
</main>

<footer class="footer">
  <div class="container footer-inner">
    <p>© {{ 2026 }} Funcom — собственный магазин цифровых товаров. Без сторонних продавцов.</p>
    <div class="foot-links">
      <a href="{{ url_for('index') }}">Каталог</a>
      <a href="{{ url_for('index') }}">Поддержка</a>
    </div>
  </div>
</footer>
</body>
</html>
"""

BASE_TEMPLATE = app.jinja_env.from_string(BASE_HTML)


def render(template, **context):
    return render_template_string(template, base=BASE_TEMPLATE, **context)


# ================= Переиспользуемый блок тикера =================

TICKER_HTML = """
<div class="ticker">
  <div class="ticker-track">
    {% for i in range(2) %}
    <div class="ticker-item"><span class="pulse-dot"></span>⭐ TELEGRAM STARS <b>{{ "%.2f"|format(star_price) }} ₽</b> <span class="up">FIXED</span></div>
    <div class="ticker-item">МАГАЗИН БЕЗ ПРОДАВЦОВ · <b>FUNCOM</b> ОФИЦИАЛЬНО</div>
    <div class="ticker-item">ДОСТАВКА <b>МГНОВЕННО</b> ПОСЛЕ ОПЛАТЫ</div>
    <div class="ticker-item">МИНИМУМ <b>{{ min_stars }}</b> ЗВЁЗД НА ЗАКАЗ</div>
    {% endfor %}
  </div>
</div>
"""


# ================= Шаблоны страниц =================

INDEX_HTML = """
{% extends base %}
{% block content %}
<section class="hero">
  <div class="container">
    <div class="hero-badge">⭐ <b>1 товар</b> в каталоге · растём каждую неделю</div>
    <h1>Telegram Stars<br>без <span class="accent">продавцов</span> и наценок</h1>
    <p>Funcom — не барахолка вроде FunPay: здесь один продавец — сам магазин.
       Фиксированная цена, мгновенная доставка, никакой переписки с незнакомцами.</p>
    <div class="hero-actions">
      <a class="btn btn-primary btn-lg" href="{{ url_for('buy_stars') }}">Купить Stars за {{ "%.2f"|format(star_price) }} ₽</a>
      {% if not current_user %}<a class="btn btn-ghost btn-lg" href="{{ url_for('register') }}">Создать аккаунт</a>{% endif %}
    </div>
  </div>
</section>

""" + TICKER_HTML + """

<div class="container">
  <div class="trust-strip">
    <div class="trust-item"><span class="ic">🏬</span>Официальный магазин, без сторонних продавцов</div>
    <div class="trust-item"><span class="ic">⚡</span>Доставка сразу после оплаты</div>
    <div class="trust-item"><span class="ic">🔒</span>Фиксированная цена — {{ "%.2f"|format(star_price) }} ₽/шт</div>
    <div class="trust-item"><span class="ic">🛟</span>Поддержка 24/7</div>
  </div>

  <section class="section">
    <div class="section-head">
      <h2>Каталог</h2>
      <span class="hint">Новые товары появляются регулярно</span>
    </div>
    <div class="products-grid">
      <div class="product-card">
        <div class="product-top">
          <div class="product-icon">⭐</div>
          <span class="stock-badge">В наличии</span>
        </div>
        <h3>Telegram Stars</h3>
        <p class="product-desc">Пополнение звёзд на любой аккаунт Telegram. Оплата с внутреннего баланса.</p>
        <div class="product-price">{{ "%.2f"|format(star_price) }} ₽ <span class="unit">/ звезда</span></div>
        <div class="product-vendor">Продавец: Funcom (официально)</div>
        <a class="btn btn-primary" href="{{ url_for('buy_stars') }}">Купить</a>
      </div>

      <div class="product-card soon">
        <div class="product-top">
          <div class="product-icon">🎮</div>
          <span class="stock-badge soon">Скоро</span>
        </div>
        <h3>Ключи игр</h3>
        <p class="product-desc">Steam, PlayStation и другие цифровые ключи появятся позже.</p>
        <div class="product-price">—</div>
        <div class="product-vendor">Продавец: Funcom (официально)</div>
      </div>

      <div class="product-card soon">
        <div class="product-top">
          <div class="product-icon">🎁</div>
          <span class="stock-badge soon">Скоро</span>
        </div>
        <h3>Подарочные карты</h3>
        <p class="product-desc">Карты популярных сервисов и площадок.</p>
        <div class="product-price">—</div>
        <div class="product-vendor">Продавец: Funcom (официально)</div>
      </div>
    </div>
  </section>
</div>
{% endblock %}
"""

REGISTER_HTML = """
{% extends base %}
{% block title %}Регистрация — Funcom{% endblock %}
{% block content %}
<div class="container">
  <div class="auth-wrap">
    <div class="auth-box">
      <div class="auth-icon">👤</div>
      <h2>Создать аккаунт</h2>
      <p class="auth-note warn">⚠ Имя пользователя закрепляется навсегда — сменить его после регистрации нельзя.</p>
      <form method="post">
        <label for="name">Имя пользователя</label>
        <input type="text" id="name" name="name" placeholder="например, alex_92" value="{{ name or '' }}" maxlength="32" required>
        <label for="password">Пароль</label>
        <input type="password" id="password" name="password" placeholder="минимум 6 символов" minlength="6" required>
        <label for="password2">Повторите пароль</label>
        <input type="password" id="password2" name="password2" placeholder="ещё раз" minlength="6" required>
        <button type="submit" class="btn btn-primary btn-block btn-lg" style="margin-top:22px;">Зарегистрироваться</button>
      </form>
      <p class="auth-switch">Уже есть аккаунт? <a href="{{ url_for('login') }}">Войти</a></p>
    </div>
  </div>
</div>
{% endblock %}
"""

LOGIN_HTML = """
{% extends base %}
{% block title %}Вход — Funcom{% endblock %}
{% block content %}
<div class="container">
  <div class="auth-wrap">
    <div class="auth-box">
      <div class="auth-icon">🔑</div>
      <h2>Вход в аккаунт</h2>
      <p class="auth-note">Введите имя пользователя и пароль.</p>
      <form method="post">
        <label for="name">Имя пользователя</label>
        <input type="text" id="name" name="name" placeholder="ваш логин" maxlength="32" required>
        <label for="password">Пароль</label>
        <input type="password" id="password" name="password" placeholder="ваш пароль" required>
        <button type="submit" class="btn btn-primary btn-block btn-lg" style="margin-top:22px;">Войти</button>
      </form>
      <p class="auth-switch">Нет аккаунта? <a href="{{ url_for('register') }}">Зарегистрироваться</a></p>
    </div>
  </div>
</div>
{% endblock %}
"""

BUY_STARS_HTML = """
{% extends base %}
{% block title %}Telegram Stars — Funcom{% endblock %}
{% block content %}
<div class="container">
  <div class="buy-layout">
    <div class="panel">
      <div class="buy-header">
        <div class="product-icon">⭐</div>
        <div>
          <h2>Telegram Stars</h2>
          <div class="rate">курс {{ "%.2f"|format(star_price) }} ₽ · минимум {{ min_stars }} шт</div>
        </div>
      </div>

      <form method="post" id="star-form">
        <label for="recipient">Username получателя в Telegram</label>
        <div class="input-prefix">
          <span>@</span>
          <input type="text" id="recipient" name="recipient" placeholder="username" value="{{ recipient or '' }}" required>
        </div>

        <label for="quantity">Количество звёзд</label>
        <input type="number" id="quantity" name="quantity" min="{{ min_stars }}" step="1"
               value="{{ quantity or min_stars }}" required>
        <div class="chip-row" id="qty-chips">
          {% for q in quick_quantities %}
          <div class="chip" data-qty="{{ q }}">{{ q }}</div>
          {% endfor %}
        </div>

        <button type="submit" class="btn btn-primary btn-block btn-lg" style="margin-top:24px;">Оплатить с баланса</button>
      </form>
    </div>

    <div class="panel summary-panel">
      <div class="summary-row"><span>Цена за звезду</span><span class="val">{{ "%.2f"|format(star_price) }} ₽</span></div>
      <div class="summary-row"><span>Количество</span><span class="val" id="summary-qty">{{ quantity or min_stars }}</span></div>
      <div class="summary-total">
        <span class="label">Итого к оплате</span>
        <span class="amount" id="total-price">0.00 ₽</span>
      </div>
      <div class="balance-note">Ваш баланс: {{ "%.2f"|format(current_user['balance']) }} ₽ · <a href="{{ url_for('topup') }}">пополнить</a></div>
    </div>
  </div>
</div>

<script>
  const price = {{ star_price }};
  const qtyInput = document.getElementById('quantity');
  const totalEl = document.getElementById('total-price');
  const summaryQty = document.getElementById('summary-qty');
  const chips = document.querySelectorAll('#qty-chips .chip');

  function updateTotal() {
    const qty = parseInt(qtyInput.value, 10) || 0;
    totalEl.textContent = (qty * price).toFixed(2) + ' ₽';
    summaryQty.textContent = qty;
    chips.forEach(c => c.classList.toggle('active', parseInt(c.dataset.qty, 10) === qty));
  }
  chips.forEach(c => c.addEventListener('click', () => { qtyInput.value = c.dataset.qty; updateTotal(); }));
  qtyInput.addEventListener('input', updateTotal);
  updateTotal();
</script>
{% endblock %}
"""

DASHBOARD_HTML = """
{% extends base %}
{% block title %}Кабинет — Funcom{% endblock %}
{% block content %}
<div class="container">
  <div class="dash-head">
    <div>
      <h1>С возвращением, {{ current_user['name'] }}</h1>
      <div class="sub">Личный кабинет и история заказов</div>
    </div>
  </div>

  <div class="balance-card">
    <div>
      <div class="label">Баланс аккаунта</div>
      <div class="amount">{{ "%.2f"|format(current_user['balance']) }} ₽</div>
    </div>
    <div class="balance-actions">
      <a class="btn btn-ghost" href="{{ url_for('topup') }}">Пополнить</a>
      <a class="btn btn-primary" href="{{ url_for('buy_stars') }}">Купить Stars</a>
    </div>
  </div>

  <div class="section-head">
    <h2>История заказов</h2>
  </div>

  <div class="orders-card">
    {% if orders %}
    <table class="orders-table">
      <thead>
        <tr><th>Дата</th><th>Товар</th><th>Получатель</th><th>Кол-во</th><th>Сумма</th><th>Статус</th></tr>
      </thead>
      <tbody>
        {% for order in orders %}
        <tr>
          <td>{{ order['created_at'] }}</td>
          <td>{{ order['product'] }}</td>
          <td class="recipient">@{{ order['recipient'] }}</td>
          <td class="qty">{{ order['quantity'] }}</td>
          <td class="price">{{ "%.2f"|format(order['total_price']) }} ₽</td>
          <td><span class="status-pill status-{{ order['status'] }}">{{ order['status'] }}</span></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div class="empty-state">
      <div class="ic">🛒</div>
      <p>Заказов пока нет — самое время купить первые звёзды.</p>
      <a class="btn btn-primary" href="{{ url_for('buy_stars') }}">Купить Telegram Stars</a>
    </div>
    {% endif %}
  </div>
</div>
{% endblock %}
"""

TOPUP_HTML = """
{% extends base %}
{% block title %}Пополнение — Funcom{% endblock %}
{% block content %}
<div class="container">
  <div class="auth-wrap">
    <div class="auth-box">
      <div class="auth-icon">💳</div>
      <h2>Пополнить баланс</h2>
      <p class="auth-note">Демо-режим: реальная оплата картой пока не подключена. Сумма зачисляется сразу.</p>
      <form method="post">
        <label for="amount">Сумма, ₽</label>
        <input type="number" id="amount" name="amount" min="1" step="1" placeholder="0" required>
        <div class="chip-row" id="amount-chips">
          {% for a in quick_amounts %}
          <div class="chip" data-amount="{{ a }}">{{ a }} ₽</div>
          {% endfor %}
        </div>
        <button type="submit" class="btn btn-primary btn-block btn-lg" style="margin-top:22px;">Пополнить</button>
      </form>
    </div>
  </div>
</div>
<script>
  const amountInput = document.getElementById('amount');
  const chips = document.querySelectorAll('#amount-chips .chip');
  chips.forEach(c => c.addEventListener('click', () => {
    amountInput.value = c.dataset.amount;
    chips.forEach(x => x.classList.remove('active'));
    c.classList.add('active');
  }));
</script>
{% endblock %}
"""


# ================= Роуты: авторизация =================

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        db = get_db()

        error = None
        if len(name) < 3 or len(name) > 32:
            error = "Имя пользователя должно быть от 3 до 32 символов."
        elif not name.replace("_", "").isalnum():
            error = "Имя может содержать только буквы, цифры и подчёркивание."
        elif len(password) < 6:
            error = "Пароль должен быть не короче 6 символов."
        elif password != password2:
            error = "Пароли не совпадают."
        elif db.execute("SELECT 1 FROM user WHERE name = ?", (name,)).fetchone():
            error = "Это имя уже занято."

        if error:
            flash(error, "danger")
            return render(REGISTER_HTML, name=name)

        db.execute(
            "INSERT INTO user (name, password_hash, balance, created_at) VALUES (?, ?, 0, ?)",
            (name, hash_password(password), datetime.utcnow().isoformat()),
        )
        db.commit()
        user = db.execute("SELECT * FROM user WHERE name = ?", (name,)).fetchone()

        session.clear()
        session["user_id"] = user["id"]
        flash("Регистрация прошла успешно. Добро пожаловать в Funcom!", "success")
        return redirect(url_for("dashboard"))

    return render(REGISTER_HTML)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")

        db = get_db()
        user = db.execute("SELECT * FROM user WHERE name = ?", (name,)).fetchone()
        if user and check_password(password, user["password_hash"]):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard"))

        flash("Неверное имя пользователя или пароль.", "danger")

    return render(LOGIN_HTML)


@app.route("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("index"))


# ================= Роуты: витрина и покупка =================

@app.route("/")
def index():
    return render(INDEX_HTML, star_price=STAR_PRICE, min_stars=MIN_STARS)


@app.route("/product/telegram-stars", methods=["GET", "POST"])
@login_required
def buy_stars():
    user = current_user()
    db = get_db()

    if request.method == "POST":
        recipient = request.form.get("recipient", "").strip().lstrip("@")
        try:
            quantity = int(request.form.get("quantity", 0))
        except ValueError:
            quantity = 0

        error = None
        if not recipient:
            error = "Укажите получателя (username в Telegram)."
        elif quantity < MIN_STARS:
            error = f"Минимальное количество — {MIN_STARS} звёзд."

        total = round(quantity * STAR_PRICE, 2)

        if not error and user["balance"] < total:
            error = "Недостаточно средств на балансе. Пополните счёт."

        if error:
            flash(error, "danger")
            return render(BUY_STARS_HTML, star_price=STAR_PRICE, min_stars=MIN_STARS,
                          quick_quantities=QUICK_QUANTITIES,
                          recipient=recipient, quantity=quantity)

        new_balance = round(user["balance"] - total, 2)
        db.execute("UPDATE user SET balance = ? WHERE id = ?", (new_balance, user["id"]))
        db.execute(
            """INSERT INTO "order" (user_id, product, recipient, quantity, total_price, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user["id"], "Telegram Stars", recipient, quantity, total, "completed",
             datetime.utcnow().strftime("%d.%m.%Y %H:%M")),
        )
        db.commit()

        flash(f"Заказ оформлен: {quantity} ⭐ для @{recipient}.", "success")
        return redirect(url_for("dashboard"))

    return render(BUY_STARS_HTML, star_price=STAR_PRICE, min_stars=MIN_STARS,
                  quick_quantities=QUICK_QUANTITIES)


# ================= Роуты: личный кабинет и баланс =================

@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    db = get_db()
    orders = db.execute(
        'SELECT * FROM "order" WHERE user_id = ? ORDER BY id DESC', (user["id"],)
    ).fetchall()
    return render(DASHBOARD_HTML, orders=orders)


@app.route("/topup", methods=["GET", "POST"])
@login_required
def topup():
    """
    ДЕМО-пополнение баланса.
    В реальном проекте здесь должна быть интеграция с платёжным
    провайдером (ЮKassa, CloudPayments и т.д.) через их API/вебхуки,
    а не прямое зачисление средств.
    """
    user = current_user()
    if request.method == "POST":
        try:
            amount = float(request.form.get("amount", 0))
        except ValueError:
            amount = 0

        if amount <= 0 or amount > 100000:
            flash("Введите корректную сумму пополнения.", "danger")
        else:
            db = get_db()
            new_balance = round(user["balance"] + amount, 2)
            db.execute("UPDATE user SET balance = ? WHERE id = ?", (new_balance, user["id"]))
            db.commit()
            flash(f"Баланс пополнен на {amount:.2f} ₽ (демо-режим).", "success")
            return redirect(url_for("dashboard"))

    return render(TOPUP_HTML, quick_amounts=QUICK_AMOUNTS)


# Инициализируем базу при импорте модуля (нужно и для gunicorn, и для python main.py)
init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
