import os
from datetime import datetime

from flask import Flask, render_template_string, redirect, url_for, request, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FUNCOM_SECRET_KEY", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(BASE_DIR, 'funcom.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Войдите, чтобы продолжить."
login_manager.login_message_category = "warning"

STAR_PRICE = 1.33  # цена одной звезды в рублях
MIN_STARS = 50      # минимальное количество звёзд к покупке


# ================= Модели =================

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(32), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    balance = db.Column(db.Float, default=0.0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    orders = db.relationship("Order", backref="user", lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    product = db.Column(db.String(64), nullable=False, default="Telegram Stars")
    recipient = db.Column(db.String(64), nullable=False)  # @username получателя
    quantity = db.Column(db.Integer, nullable=False)
    total_price = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(32), default="pending")  # pending / completed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ================= Шаблоны (все в одном файле) =================

BASE_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}Funcom{% endblock %}</title>
    <style>
        * { box-sizing: border-box; }
        body { margin: 0; font-family: 'Segoe UI', Arial, sans-serif; background: #0f1115; color: #e6e6e6; }
        .container { max-width: 1000px; margin: 0 auto; padding: 0 20px; }
        a { color: inherit; text-decoration: none; }

        .navbar { background: #14171f; border-bottom: 1px solid #232732; }
        .navbar-inner { display: flex; justify-content: space-between; align-items: center; height: 64px; }
        .logo { font-size: 22px; font-weight: 700; color: #fff; }
        .logo span { color: #6c5ce7; }
        .nav-links { display: flex; align-items: center; gap: 18px; }
        .nav-links a { color: #b3b8c3; font-size: 15px; }
        .nav-links a:hover { color: #fff; }
        .balance { background: #1e2230; padding: 6px 12px; border-radius: 8px; font-weight: 600; color: #6c5ce7; }

        .btn-primary { background: #6c5ce7; color: #fff !important; padding: 9px 18px; border-radius: 8px; border: none; cursor: pointer; font-size: 15px; font-weight: 600; }
        .btn-primary:hover { background: #5b4bd6; }
        .btn-outline { border: 1px solid #333849; padding: 8px 16px; border-radius: 8px; color: #b3b8c3 !important; }
        .btn-outline:hover { border-color: #6c5ce7; color: #fff !important; }
        .btn-block { width: 100%; display: block; text-align: center; margin-top: 10px; }

        .hero { text-align: center; padding: 60px 0 30px; }
        .hero h1 { font-size: 40px; margin-bottom: 8px; color: #fff; }
        .hero p { color: #9096a3; font-size: 17px; }

        .catalog { padding: 20px 0 60px; }
        .catalog h2 { margin-bottom: 20px; }
        .products-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 20px; }
        .product-card { background: #14171f; border: 1px solid #232732; border-radius: 14px; padding: 24px; text-align: center; }
        .product-icon { font-size: 36px; margin-bottom: 8px; }
        .product-card h3 { margin: 4px 0; color: #fff; }
        .product-price { color: #6c5ce7; font-weight: 700; margin: 6px 0; }
        .product-desc { color: #9096a3; font-size: 14px; margin-bottom: 16px; }
        .product-card-soon { opacity: 0.55; }

        .auth-box { max-width: 380px; margin: 60px auto; background: #14171f; border: 1px solid #232732; border-radius: 14px; padding: 32px; }
        .auth-box h2 { margin-top: 0; color: #fff; }
        .auth-note { color: #9096a3; font-size: 13px; margin-bottom: 18px; }
        .auth-switch { text-align: center; margin-top: 16px; font-size: 14px; color: #9096a3; }
        .auth-switch a { color: #6c5ce7; font-weight: 600; }

        form label { display: block; margin: 14px 0 6px; font-size: 14px; color: #b3b8c3; }
        form input { width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid #2a2f3d; background: #0f1115; color: #fff; font-size: 15px; }
        form input:focus { outline: none; border-color: #6c5ce7; }

        .input-prefix { display: flex; align-items: center; background: #0f1115; border: 1px solid #2a2f3d; border-radius: 8px; overflow: hidden; }
        .input-prefix span { padding: 0 10px; color: #9096a3; }
        .input-prefix input { border: none; background: transparent; }

        .product-page { max-width: 420px; margin: 50px auto; background: #14171f; border: 1px solid #232732; border-radius: 14px; padding: 32px; }
        .total-box { margin-top: 18px; font-size: 18px; font-weight: 700; color: #fff; }
        .total-box span { color: #6c5ce7; }

        .orders-table { width: 100%; border-collapse: collapse; margin-top: 16px; }
        .orders-table th, .orders-table td { padding: 10px 12px; border-bottom: 1px solid #232732; text-align: left; font-size: 14px; }
        .orders-table th { color: #9096a3; font-weight: 600; }
        .status { padding: 3px 10px; border-radius: 20px; font-size: 12px; }
        .status-completed { background: #1e3a2f; color: #4ade80; }
        .status-pending { background: #3a331e; color: #facc15; }

        .flashes { margin-top: 20px; }
        .flash { padding: 12px 16px; border-radius: 8px; margin-bottom: 10px; font-size: 14px; }
        .flash-success { background: #1e3a2f; color: #4ade80; }
        .flash-danger { background: #3a1e1e; color: #f87171; }
        .flash-warning { background: #3a331e; color: #facc15; }

        .footer { text-align: center; padding: 30px 0; color: #565c6b; font-size: 13px; border-top: 1px solid #232732; margin-top: 40px; }
    </style>
</head>
<body>
    <header class="navbar">
        <div class="container navbar-inner">
            <a class="logo" href="{{ url_for('index') }}">Fun<span>com</span></a>
            <nav class="nav-links">
                <a href="{{ url_for('index') }}">Каталог</a>
                {% if current_user.is_authenticated %}
                    <a href="{{ url_for('dashboard') }}">Кабинет</a>
                    <a href="{{ url_for('topup') }}">Пополнить</a>
                    <span class="balance">{{ "%.2f"|format(current_user.balance) }} ₽</span>
                    <a href="{{ url_for('logout') }}" class="btn-outline">Выйти</a>
                {% else %}
                    <a href="{{ url_for('login') }}">Войти</a>
                    <a href="{{ url_for('register') }}" class="btn-primary">Регистрация</a>
                {% endif %}
            </nav>
        </div>
    </header>

    <main class="container">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                <div class="flashes">
                    {% for category, message in messages %}
                        <div class="flash flash-{{ category }}">{{ message }}</div>
                    {% endfor %}
                </div>
            {% endif %}
        {% endwith %}

        {% block content %}{% endblock %}
    </main>

    <footer class="footer">
        <div class="container">
            <p>Funcom — демо-проект цифрового маркетплейса. Реальные платежи не принимаются.</p>
        </div>
    </footer>
</body>
</html>
"""

INDEX_HTML = """
{% extends base %}
{% block title %}Funcom — цифровые товары{% endblock %}
{% block content %}
<section class="hero">
    <h1>Funcom</h1>
    <p>Маркетплейс цифровых товаров. Быстро, без лишних действий.</p>
</section>
<section class="catalog">
    <h2>Каталог</h2>
    <div class="products-grid">
        <div class="product-card">
            <div class="product-icon">⭐</div>
            <h3>Telegram Stars</h3>
            <p class="product-price">{{ "%.2f"|format(star_price) }} ₽ / звезда</p>
            <p class="product-desc">Пополнение звёзд Telegram на любой аккаунт.</p>
            <a href="{{ url_for('buy_stars') }}" class="btn-primary">Купить</a>
        </div>
        <div class="product-card product-card-soon">
            <div class="product-icon">🎮</div>
            <h3>Ключи игр</h3>
            <p class="product-price">Скоро</p>
            <p class="product-desc">Steam, PlayStation и другие ключи появятся позже.</p>
        </div>
        <div class="product-card product-card-soon">
            <div class="product-icon">🎁</div>
            <h3>Подарочные карты</h3>
            <p class="product-price">Скоро</p>
            <p class="product-desc">Карты популярных сервисов.</p>
        </div>
    </div>
</section>
{% endblock %}
"""

REGISTER_HTML = """
{% extends base %}
{% block title %}Регистрация — Funcom{% endblock %}
{% block content %}
<div class="auth-box">
    <h2>Регистрация</h2>
    <p class="auth-note">Имя пользователя нельзя будет изменить после регистрации.</p>
    <form method="post">
        <label for="name">Имя пользователя</label>
        <input type="text" id="name" name="name" value="{{ name or '' }}" maxlength="32" required>
        <label for="password">Пароль</label>
        <input type="password" id="password" name="password" minlength="6" required>
        <label for="password2">Повторите пароль</label>
        <input type="password" id="password2" name="password2" minlength="6" required>
        <button type="submit" class="btn-primary btn-block">Зарегистрироваться</button>
    </form>
    <p class="auth-switch">Уже есть аккаунт? <a href="{{ url_for('login') }}">Войти</a></p>
</div>
{% endblock %}
"""

LOGIN_HTML = """
{% extends base %}
{% block title %}Вход — Funcom{% endblock %}
{% block content %}
<div class="auth-box">
    <h2>Вход</h2>
    <form method="post">
        <label for="name">Имя пользователя</label>
        <input type="text" id="name" name="name" maxlength="32" required>
        <label for="password">Пароль</label>
        <input type="password" id="password" name="password" required>
        <button type="submit" class="btn-primary btn-block">Войти</button>
    </form>
    <p class="auth-switch">Нет аккаунта? <a href="{{ url_for('register') }}">Зарегистрироваться</a></p>
</div>
{% endblock %}
"""

BUY_STARS_HTML = """
{% extends base %}
{% block title %}Telegram Stars — Funcom{% endblock %}
{% block content %}
<div class="product-page">
    <h2>⭐ Telegram Stars</h2>
    <p class="product-price">{{ "%.2f"|format(star_price) }} ₽ за 1 звезду · минимум {{ min_stars }} шт.</p>
    <form method="post" id="star-form">
        <label for="recipient">Username получателя в Telegram</label>
        <div class="input-prefix">
            <span>@</span>
            <input type="text" id="recipient" name="recipient" value="{{ recipient or '' }}" required>
        </div>
        <label for="quantity">Количество звёзд</label>
        <input type="number" id="quantity" name="quantity" min="{{ min_stars }}" step="1"
               value="{{ quantity or min_stars }}" required>
        <div class="total-box">Итого: <span id="total-price">0.00</span> ₽</div>
        <button type="submit" class="btn-primary btn-block">Оплатить с баланса</button>
    </form>
    <p class="auth-note">Ваш баланс: {{ "%.2f"|format(current_user.balance) }} ₽ · <a href="{{ url_for('topup') }}">пополнить</a></p>
</div>
<script>
    const price = {{ star_price }};
    const qtyInput = document.getElementById('quantity');
    const totalEl = document.getElementById('total-price');
    function updateTotal() {
        const qty = parseInt(qtyInput.value, 10) || 0;
        totalEl.textContent = (qty * price).toFixed(2);
    }
    qtyInput.addEventListener('input', updateTotal);
    updateTotal();
</script>
{% endblock %}
"""

DASHBOARD_HTML = """
{% extends base %}
{% block title %}Кабинет — Funcom{% endblock %}
{% block content %}
<h2>Личный кабинет</h2>
<p class="auth-note">Пользователь: <strong>{{ current_user.name }}</strong> · Баланс: <strong>{{ "%.2f"|format(current_user.balance) }} ₽</strong></p>
<h3>История заказов</h3>
{% if orders %}
<table class="orders-table">
    <thead>
        <tr><th>Дата</th><th>Товар</th><th>Получатель</th><th>Кол-во</th><th>Сумма</th><th>Статус</th></tr>
    </thead>
    <tbody>
        {% for order in orders %}
        <tr>
            <td>{{ order.created_at.strftime('%d.%m.%Y %H:%M') }}</td>
            <td>{{ order.product }}</td>
            <td>@{{ order.recipient }}</td>
            <td>{{ order.quantity }}</td>
            <td>{{ "%.2f"|format(order.total_price) }} ₽</td>
            <td><span class="status status-{{ order.status }}">{{ order.status }}</span></td>
        </tr>
        {% endfor %}
    </tbody>
</table>
{% else %}
<p>У вас пока нет заказов. <a href="{{ url_for('buy_stars') }}">Купить Telegram Stars</a></p>
{% endif %}
{% endblock %}
"""

TOPUP_HTML = """
{% extends base %}
{% block title %}Пополнение — Funcom{% endblock %}
{% block content %}
<div class="auth-box">
    <h2>Пополнение баланса</h2>
    <p class="auth-note">
        Демо-режим: реальная оплата картой не подключена. В боевом проекте
        здесь была бы интеграция с платёжным провайдером.
    </p>
    <form method="post">
        <label for="amount">Сумма, ₽</label>
        <input type="number" id="amount" name="amount" min="1" step="1" required>
        <button type="submit" class="btn-primary btn-block">Пополнить (демо)</button>
    </form>
</div>
{% endblock %}
"""


def render(template, **context):
    return render_template_string(template, base=BASE_HTML, **context)


# ================= Роуты: авторизация =================

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")

        error = None
        if len(name) < 3 or len(name) > 32:
            error = "Имя пользователя должно быть от 3 до 32 символов."
        elif not name.replace("_", "").isalnum():
            error = "Имя может содержать только буквы, цифры и подчёркивание."
        elif len(password) < 6:
            error = "Пароль должен быть не короче 6 символов."
        elif password != password2:
            error = "Пароли не совпадают."
        elif User.query.filter_by(name=name).first():
            error = "Это имя уже занято."

        if error:
            flash(error, "danger")
            return render(REGISTER_HTML, name=name)

        user = User(name=name)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        login_user(user)
        flash("Регистрация прошла успешно. Добро пожаловать в Funcom!", "success")
        return redirect(url_for("dashboard"))

    return render(REGISTER_HTML)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter_by(name=name).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("dashboard"))

        flash("Неверное имя пользователя или пароль.", "danger")

    return render(LOGIN_HTML)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))


# ================= Роуты: витрина и покупка =================

@app.route("/")
def index():
    return render(INDEX_HTML, star_price=STAR_PRICE)


@app.route("/product/telegram-stars", methods=["GET", "POST"])
@login_required
def buy_stars():
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

        if not error and current_user.balance < total:
            error = "Недостаточно средств на балансе. Пополните счёт."

        if error:
            flash(error, "danger")
            return render(BUY_STARS_HTML, star_price=STAR_PRICE, min_stars=MIN_STARS,
                          recipient=recipient, quantity=quantity)

        current_user.balance = round(current_user.balance - total, 2)
        order = Order(
            user_id=current_user.id,
            product="Telegram Stars",
            recipient=recipient,
            quantity=quantity,
            total_price=total,
            status="completed",  # демо: считаем выполненным сразу
        )
        db.session.add(order)
        db.session.commit()

        flash(f"Заказ оформлен: {quantity} ⭐ для @{recipient}.", "success")
        return redirect(url_for("dashboard"))

    return render(BUY_STARS_HTML, star_price=STAR_PRICE, min_stars=MIN_STARS)


# ================= Роуты: личный кабинет и баланс =================

@app.route("/dashboard")
@login_required
def dashboard():
    orders = (
        Order.query.filter_by(user_id=current_user.id)
        .order_by(Order.created_at.desc())
        .all()
    )
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
    if request.method == "POST":
        try:
            amount = float(request.form.get("amount", 0))
        except ValueError:
            amount = 0

        if amount <= 0 or amount > 100000:
            flash("Введите корректную сумму пополнения.", "danger")
        else:
            current_user.balance = round(current_user.balance + amount, 2)
            db.session.commit()
            flash(f"Баланс пополнен на {amount:.2f} ₽ (демо-режим).", "success")
            return redirect(url_for("dashboard"))

    return render(TOPUP_HTML)


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
