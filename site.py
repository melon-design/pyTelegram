#!/usr/bin/env python3
"""
Лёгкий сайт на Python — без внешних зависимостей.
Запуск: python3 site.py
Открыть в браузере: http://localhost:8000
"""

from http.server import BaseHTTPRequestHandler, HTTPServer

HTML_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Мой сайт</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            background: #f4f4f9;
            margin: 0;
            padding: 0;
            display: flex;
            flex-direction: column;
            align-items: center;
            min-height: 100vh;
        }
        header {
            background: #4a4ae0;
            color: white;
            width: 100%;
            padding: 20px 0;
            text-align: center;
        }
        main {
            max-width: 600px;
            margin-top: 40px;
            background: white;
            padding: 30px;
            border-radius: 10px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            text-align: center;
        }
        button {
            padding: 10px 20px;
            font-size: 16px;
            border: none;
            border-radius: 6px;
            background: #4a4ae0;
            color: white;
            cursor: pointer;
        }
        button:hover {
            background: #3737b3;
        }
        #result {
            margin-top: 15px;
            font-weight: bold;
        }
    </style>
</head>
<body>
    <header>
        <h1>Добро пожаловать!</h1>
    </header>
    <main>
        <p>Это простой сайт на чистом Python — без Flask и Django, работает через встроенный http.server.</p>
        <button onclick="sayHello()">Нажми меня</button>
        <p id="result"></p>
    </main>
    <script>
        function sayHello() {
            document.getElementById('result').innerText = 'Привет! Сайт работает 🎉';
        }
    </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
        else:
            self.send_response(404)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h1>404 - stranica ne naidena</h1>")

    def log_message(self, format, *args):
        # Отключаем лишние логи в консоли
        pass


def run(port=8000):
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Сайт запущен: http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
