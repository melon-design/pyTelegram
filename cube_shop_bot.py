# -*- coding: utf-8 -*-
"""
CUBE SHOP — Telegram-бот (aiogram 3) + Mini App (Flask) в одном файле.

Что делает:
  - Бот отвечает на /start приветствием и кнопкой "Открыть CUBE SHOP",
    которая открывает Mini App (WebApp) по ссылке WEBAPP_URL.
  - Flask отдаёт саму мини-апку: магазин с единственным товаром —
    Telegram Stars (от 50 до 10 000 шт, курс 1.3₽ за звезду).
  - Оплата ЗАБЛОКИРОВАНА — при попытке купить показывается
    сообщение "функция в разработке".

Запуск:
  1) pip install aiogram flask
  2) Задайте BOT_TOKEN (токен от @BotFather)
  3) Задайте WEBAPP_URL — публичный HTTPS-адрес, на котором крутится
     этот Flask (например, через ngrok/vps/Reverse proxy).
     Telegram WebApp работает ТОЛЬКО по https.
  4) python cube_shop_bot.py

Файл специально сделан "всё в одном" по просьбе — в бою обычно
бота и веб-сервер разносят на разные процессы/деплои.
"""

import asyncio
import logging
import threading

from flask import Flask, render_template_string

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

# ============================== КОНФИГ ==============================

BOT_TOKEN = "8741315992:AAE3VTvOv-fhhaJgIktehEst-IBs3_Y95Rs"  # токен от @BotFather
WEBAPP_URL = "https://bot-1786889103-5553-melon.bothost.tech/" # публичный https-адрес Flask
FLASK_HOST = "0.0.0.0"
FLASK_PORT = 5000

STAR_PRICE_RUB = 1.3   # курс: 1 звезда = 1.3 ₽
MIN_STARS = 50
MAX_STARS = 10000

# ============================ FLASK APP ==============================

app = Flask(__name__)

MINI_APP_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>CUBE SHOP</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root{
    --bg:#0b0d12;
    --bg-soft:#12151c;
    --card:#161a23;
    --card-2:#1c212c;
    --border:#242a37;
    --accent:#6e7bff;
    --accent-2:#8f6bff;
    --text:#f2f3f7;
    --muted:#8b93a7;
    --success:#3ddc97;
    --danger:#ff5c72;
    --radius:18px;
  }
  *{box-sizing:border-box;}
  html,body{
    margin:0;padding:0;
    background:var(--bg);
    color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,Arial,sans-serif;
    -webkit-tap-highlight-color: transparent;
  }
  body{
    background:
      radial-gradient(1200px 600px at 100% -10%, rgba(110,123,255,.18), transparent 60%),
      radial-gradient(900px 500px at -10% 0%, rgba(143,107,255,.12), transparent 55%),
      var(--bg);
    min-height:100vh;
    padding-bottom:40px;
  }
  .wrap{max-width:520px;margin:0 auto;padding:18px 16px 8px;}

  /* HEADER */
  .header{
    display:flex;align-items:center;justify-content:space-between;
    margin-bottom:20px;
  }
  .brand{display:flex;align-items:center;gap:10px;}
  .cube-logo{
    width:38px;height:38px;border-radius:11px;
    background:linear-gradient(135deg,var(--accent),var(--accent-2));
    display:flex;align-items:center;justify-content:center;
    font-weight:800;font-size:18px;color:#fff;
    box-shadow:0 6px 18px rgba(110,123,255,.35);
  }
  .brand-name{font-size:19px;font-weight:800;letter-spacing:.3px;}
  .brand-sub{font-size:11px;color:var(--muted);margin-top:1px;}
  .balance-pill{
    background:var(--card);
    border:1px solid var(--border);
    border-radius:999px;
    padding:8px 14px;
    font-size:13px;color:var(--muted);
    display:flex;align-items:center;gap:6px;
  }

  /* HERO */
  .hero{
    background:linear-gradient(135deg, rgba(110,123,255,.16), rgba(143,107,255,.08));
    border:1px solid var(--border);
    border-radius:var(--radius);
    padding:18px;
    margin-bottom:18px;
  }
  .hero-title{font-size:16px;font-weight:700;margin:0 0 4px;}
  .hero-text{font-size:13px;color:var(--muted);margin:0;line-height:1.5;}

  /* SECTION TITLE */
  .section-title{
    font-size:14px;font-weight:700;color:var(--muted);
    text-transform:uppercase;letter-spacing:.06em;
    margin:22px 0 10px 2px;
  }

  /* CATEGORY CHIPS (визуальные, без функционала) */
  .chips{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px;}
  .chip{
    flex:0 0 auto;
    background:var(--card);
    border:1px solid var(--border);
    color:var(--muted);
    font-size:13px;
    padding:8px 14px;
    border-radius:999px;
    white-space:nowrap;
  }
  .chip.active{
    background:linear-gradient(135deg,var(--accent),var(--accent-2));
    color:#fff;border-color:transparent;font-weight:600;
  }

  /* PRODUCT CARD */
  .product-card{
    background:var(--card);
    border:1px solid var(--border);
    border-radius:var(--radius);
    padding:18px;
    margin-top:14px;
  }
  .product-top{display:flex;gap:14px;align-items:center;}
  .product-icon{
    width:56px;height:56px;border-radius:16px;flex:0 0 56px;
    background:radial-gradient(circle at 30% 30%, #ffd76b, #ff9f1c 70%);
    display:flex;align-items:center;justify-content:center;
    font-size:28px;
    box-shadow:0 8px 20px rgba(255,159,28,.3);
  }
  .product-info h3{margin:0 0 4px;font-size:16px;font-weight:700;}
  .product-info p{margin:0;font-size:12.5px;color:var(--muted);}
  .badge-row{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap;}
  .badge{
    font-size:11px;padding:4px 9px;border-radius:999px;
    background:var(--card-2);color:var(--muted);border:1px solid var(--border);
  }
  .badge.rate{color:var(--success);border-color:rgba(61,220,151,.3);background:rgba(61,220,151,.08);}

  .divider{height:1px;background:var(--border);margin:16px 0;}

  /* AMOUNT SELECTOR */
  .amount-label{font-size:13px;color:var(--muted);margin-bottom:8px;display:flex;justify-content:space-between;}
  .amount-box{
    display:flex;align-items:center;gap:10px;
    background:var(--card-2);
    border:1px solid var(--border);
    border-radius:14px;
    padding:6px;
  }
  .amount-btn{
    width:40px;height:40px;border-radius:10px;border:none;
    background:var(--bg-soft);color:var(--text);
    font-size:20px;font-weight:700;cursor:pointer;
    display:flex;align-items:center;justify-content:center;
    user-select:none;
  }
  .amount-btn:active{transform:scale(.94);}
  .amount-input{
    flex:1;background:transparent;border:none;outline:none;
    text-align:center;color:var(--text);font-size:20px;font-weight:800;
    -moz-appearance:textfield;
  }
  .amount-input::-webkit-outer-spin-button,
  .amount-input::-webkit-inner-spin-button{-webkit-appearance:none;margin:0;}

  .quick-amounts{display:flex;gap:8px;margin-top:10px;}
  .quick-amount{
    flex:1;text-align:center;padding:9px 0;border-radius:12px;
    background:var(--card-2);border:1px solid var(--border);
    color:var(--text);font-size:12.5px;font-weight:600;cursor:pointer;
  }
  .quick-amount:active{transform:scale(.96);}

  .slider-wrap{margin-top:14px;}
  input[type=range]{
    width:100%;-webkit-appearance:none;appearance:none;
    height:6px;border-radius:999px;
    background:linear-gradient(90deg,var(--accent),var(--accent-2));
    outline:none;
  }
  input[type=range]::-webkit-slider-thumb{
    -webkit-appearance:none;width:20px;height:20px;border-radius:50%;
    background:#fff;border:3px solid var(--accent);cursor:pointer;
    box-shadow:0 2px 6px rgba(0,0,0,.4);
  }
  .slider-minmax{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:6px;}

  /* PRICE SUMMARY */
  .price-box{
    margin-top:16px;
    background:var(--card-2);
    border:1px solid var(--border);
    border-radius:14px;
    padding:14px 16px;
  }
  .price-row{display:flex;justify-content:space-between;font-size:13px;color:var(--muted);margin-bottom:6px;}
  .price-row:last-child{margin-bottom:0;}
  .price-total{font-size:18px;font-weight:800;color:var(--text);}

  .buy-btn{
    width:100%;margin-top:16px;
    background:linear-gradient(135deg,var(--accent),var(--accent-2));
    color:#fff;border:none;border-radius:14px;
    padding:15px;font-size:15px;font-weight:700;
    cursor:pointer;
    box-shadow:0 10px 24px rgba(110,123,255,.35);
  }
  .buy-btn:active{transform:scale(.98);}

  /* FEATURES */
  .features{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px;}
  .feature{
    background:var(--card);border:1px solid var(--border);
    border-radius:14px;padding:12px;
  }
  .feature-emoji{font-size:18px;margin-bottom:6px;}
  .feature-title{font-size:12.5px;font-weight:700;margin-bottom:2px;}
  .feature-text{font-size:11px;color:var(--muted);line-height:1.4;}

  /* FOOTER NAV (визуальная, декоративная) */
  .bottom-nav{
    display:flex;justify-content:space-around;
    background:var(--card);
    border:1px solid var(--border);
    border-radius:18px;
    padding:10px 6px;
    margin-top:26px;
  }
  .nav-item{
    display:flex;flex-direction:column;align-items:center;gap:3px;
    color:var(--muted);font-size:10.5px;
  }
  .nav-item.active{color:var(--accent-2);}
  .nav-icon{font-size:18px;}

  /* MODAL */
  .overlay{
    position:fixed;inset:0;background:rgba(0,0,0,.6);
    display:none;align-items:flex-end;justify-content:center;
    z-index:50;backdrop-filter: blur(2px);
  }
  .overlay.show{display:flex;}
  .modal{
    width:100%;max-width:520px;
    background:var(--bg-soft);
    border:1px solid var(--border);
    border-radius:22px 22px 0 0;
    padding:22px 20px 26px;
    animation:slideUp .25s ease-out;
  }
  @keyframes slideUp{from{transform:translateY(30px);opacity:0;}to{transform:translateY(0);opacity:1;}}
  .modal-icon{
    width:56px;height:56px;border-radius:16px;margin:0 auto 14px;
    background:rgba(255,92,114,.12);
    display:flex;align-items:center;justify-content:center;font-size:26px;
  }
  .modal-title{text-align:center;font-size:16px;font-weight:800;margin-bottom:8px;}
  .modal-text{text-align:center;font-size:13.5px;color:var(--muted);line-height:1.55;margin-bottom:18px;}
  .modal-btn{
    width:100%;padding:14px;border-radius:14px;border:none;
    background:var(--card-2);color:var(--text);font-weight:700;font-size:14px;
    border:1px solid var(--border);
  }
</style>
</head>
<body>
<div class="wrap">

  <div class="header">
    <div class="brand">
      <div class="cube-logo">▦</div>
      <div>
        <div class="brand-name">CUBE SHOP</div>
        <div class="brand-sub">цифровые товары</div>
      </div>
    </div>
    <div class="balance-pill">⭐ Telegram Stars</div>
  </div>

  <div class="hero">
    <div class="hero-title">Добро пожаловать в CUBE SHOP</div>
    <p class="hero-text">Быстрая покупка Telegram Stars по курсу 1.3 ₽ за звезду. Прозрачные цены, мгновенный расчёт, без скрытых комиссий.</p>
  </div>

  <div class="chips">
    <div class="chip active">⭐ Telegram Stars</div>
    <div class="chip">🎮 Игровая валюта</div>
    <div class="chip">💎 Premium</div>
    <div class="chip">🎁 Подарки</div>
  </div>

  <div class="section-title">Товар</div>

  <div class="product-card">
    <div class="product-top">
      <div class="product-icon">⭐</div>
      <div class="product-info">
        <h3>Telegram Stars</h3>
        <p>Внутренняя валюта Telegram для покупок в ботах и каналах</p>
      </div>
    </div>
    <div class="badge-row">
      <div class="badge rate">Курс: 1 ⭐ = 1.3 ₽</div>
      <div class="badge">от 50 до 10 000 ⭐</div>
      <div class="badge">Мгновенно</div>
    </div>

    <div class="divider"></div>

    <div class="amount-label">
      <span>Количество звёзд</span>
      <span id="rangeHint">50 – 10 000</span>
    </div>

    <div class="amount-box">
      <button class="amount-btn" id="minusBtn">−</button>
      <input class="amount-input" type="number" id="amountInput" value="100" min="50" max="10000" step="1">
      <button class="amount-btn" id="plusBtn">+</button>
    </div>

    <div class="quick-amounts">
      <div class="quick-amount" data-val="50">50</div>
      <div class="quick-amount" data-val="100">100</div>
      <div class="quick-amount" data-val="500">500</div>
      <div class="quick-amount" data-val="1000">1000</div>
      <div class="quick-amount" data-val="5000">5000</div>
    </div>

    <div class="slider-wrap">
      <input type="range" id="amountSlider" min="50" max="10000" value="100" step="1">
      <div class="slider-minmax"><span>50 ⭐</span><span>10 000 ⭐</span></div>
    </div>

    <div class="price-box">
      <div class="price-row"><span>Количество</span><span id="sumStars">100 ⭐</span></div>
      <div class="price-row"><span>Курс</span><span>1.3 ₽ / ⭐</span></div>
      <div class="price-row"><span>Итого к оплате</span><span class="price-total" id="sumPrice">130.00 ₽</span></div>
    </div>

    <button class="buy-btn" id="buyBtn">Купить Telegram Stars</button>
  </div>

  <div class="section-title">Почему CUBE SHOP</div>
  <div class="features">
    <div class="feature">
      <div class="feature-emoji">⚡</div>
      <div class="feature-title">Быстро</div>
      <div class="feature-text">Расчёт суммы в реальном времени</div>
    </div>
    <div class="feature">
      <div class="feature-emoji">🔒</div>
      <div class="feature-title">Безопасно</div>
      <div class="feature-text">Официальная валюта Telegram</div>
    </div>
    <div class="feature">
      <div class="feature-emoji">💬</div>
      <div class="feature-title">Поддержка</div>
      <div class="feature-text">Всегда на связи в боте</div>
    </div>
    <div class="feature">
      <div class="feature-emoji">📈</div>
      <div class="feature-title">Честный курс</div>
      <div class="feature-text">1 ⭐ = 1.3 ₽, без скрытых наценок</div>
    </div>
  </div>

  <div class="bottom-nav">
    <div class="nav-item active"><div class="nav-icon">🏬</div>Магазин</div>
    <div class="nav-item"><div class="nav-icon">🧾</div>Заказы</div>
    <div class="nav-item"><div class="nav-icon">👤</div>Профиль</div>
    <div class="nav-item"><div class="nav-icon">⚙️</div>Настройки</div>
  </div>

</div>

<div class="overlay" id="overlay">
  <div class="modal">
    <div class="modal-icon">🛠️</div>
    <div class="modal-title">Оплата в разработке</div>
    <div class="modal-text">Покупка Telegram Stars временно недоступна — мы дорабатываем систему оплаты. Загляните позже, скоро всё заработает!</div>
    <button class="modal-btn" id="closeModal">Понятно</button>
  </div>
</div>

<script>
  const tg = window.Telegram ? window.Telegram.WebApp : null;
  if (tg) { tg.ready(); tg.expand(); }

  const RATE = 1.3, MIN = 50, MAX = 10000;
  const amountInput = document.getElementById('amountInput');
  const amountSlider = document.getElementById('amountSlider');
  const sumStars = document.getElementById('sumStars');
  const sumPrice = document.getElementById('sumPrice');

  function clamp(v){ return Math.min(MAX, Math.max(MIN, v)); }

  function updateUI(value, fromSlider){
    let v = parseInt(value || 0, 10);
    if (isNaN(v)) v = MIN;
    if (!fromSlider) v = clamp(v);
    amountInput.value = v;
    amountSlider.value = clamp(v);
    sumStars.textContent = v.toLocaleString('ru-RU') + ' ⭐';
    sumPrice.textContent = (v * RATE).toLocaleString('ru-RU', {minimumFractionDigits:2, maximumFractionDigits:2}) + ' ₽';
  }

  amountInput.addEventListener('input', () => updateUI(amountInput.value, false));
  amountInput.addEventListener('blur', () => updateUI(amountInput.value, false));
  amountSlider.addEventListener('input', () => updateUI(amountSlider.value, true));

  document.getElementById('minusBtn').addEventListener('click', () => {
    updateUI(clamp(parseInt(amountInput.value,10) - 10), false);
  });
  document.getElementById('plusBtn').addEventListener('click', () => {
    updateUI(clamp(parseInt(amountInput.value,10) + 10), false);
  });

  document.querySelectorAll('.quick-amount').forEach(el => {
    el.addEventListener('click', () => updateUI(el.dataset.val, false));
  });

  const overlay = document.getElementById('overlay');
  document.getElementById('buyBtn').addEventListener('click', () => {
    overlay.classList.add('show');
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('warning');
  });
  document.getElementById('closeModal').addEventListener('click', () => {
    overlay.classList.remove('show');
  });
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.classList.remove('show');
  });

  updateUI(100, false);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(MINI_APP_HTML)


def run_flask():
    app.run(host=FLASK_HOST, port=FLASK_PORT)


# ============================ AIOGRAM BOT =============================

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="🛍 Открыть CUBE SHOP",
                    web_app=types.WebAppInfo(url=WEBAPP_URL),
                )
            ]
        ]
    )
    text = (
        "👋 Добро пожаловать в <b>CUBE SHOP</b>!\n\n"
        "⭐ Здесь можно купить <b>Telegram Stars</b> по выгодному курсу "
        "<b>1.3 ₽ за звезду</b> — от 50 до 10 000 звёзд.\n\n"
        "Нажмите кнопку ниже, чтобы открыть магазин 👇"
    )
    await message.answer(text, reply_markup=keyboard)


async def run_bot():
    await dp.start_polling(bot)


# =============================== MAIN =================================

def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
