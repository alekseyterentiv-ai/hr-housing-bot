from flask import Flask, request
import os
import requests

app = Flask(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
TG_API = f"https://api.telegram.org/bot{TOKEN}"

@app.get("/")
def index():
    return "ok", 200

@app.post("/webhook")
def webhook():
    data = request.get_json(force=True)
    message = data.get("message")
    if not message:
        return "ok", 200

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    requests.post(
        f"{TG_API}/sendMessage",
        json={"chat_id": chat_id, "text": f"✅ Бот жив. Ты написал: {text}"}
    )

    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
