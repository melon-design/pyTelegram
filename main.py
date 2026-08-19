from flask import Flask, render_template_string

app = Flask(__name__)

TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Мой сайт на Flask</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            background: #f5f5f5;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
        }
        .card {
            background: white;
            padding: 40px 60px;
            border-radius: 12px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
            text-align: center;
        }
        h1 { color: #2c3e50; }
        p { color: #555; }
        a { color: #3498db; text-decoration: none; }
    </style>
</head>
<body>
    <div class="card">
        <h1>Привет, мир! 👋</h1>
        <p>Это простой сайт на Flask.</p>
        <p><a href="/about">О сайте</a></p>
    </div>
</body>
</html>
"""

ABOUT_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>О сайте</title>
</head>
<body style="font-family: Arial, sans-serif; text-align:center; margin-top: 50px;">
    <h1>О сайте</h1>
    <p>Это учебный пример на Flask.</p>
    <p><a href="/">На главную</a></p>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(TEMPLATE)

@app.route("/about")
def about():
    return render_template_string(ABOUT_TEMPLATE)

if __name__ == "__main__":
    app.run(debug=True)
