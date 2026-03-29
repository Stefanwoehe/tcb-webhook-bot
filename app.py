from flask import Flask, request, jsonify
import hashlib
import hmac
import base64
import json
import os
import time
import requests

app = Flask(__name__)

BITGET_API_KEY    = os.environ.get("BITGET_API_KEY")
BITGET_SECRET_KEY = os.environ.get("BITGET_SECRET_KEY")
BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE")
BITGET_BASE_URL   = "https://api.bitget.com"
RR_RATIO          = float(os.environ.get("RR_RATIO", "0.5"))
ORDER_SIZE_USDT   = float(os.environ.get("ORDER_SIZE_USDT", "100"))

def sign(message, secret):
    mac = hmac.new(secret.encode(), message.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def get_timestamp():
    return str(int(time.time() * 1000))

def place_order(symbol, side, entry, sl, tp, size_usdt):
    ts       = get_timestamp()
    path     = "/api/v2/mix/order/place-order"
    qty      = round(size_usdt / entry, 4)
    body     = {
        "symbol":                symbol + "USDT",
        "marginCoin":            "USDT",
        "marginMode":            "crossed",
        "size":                  str(qty),
        "side":                  side,
        "orderType":             "market",
        "presetTakeProfitPrice": str(round(tp, 6)),
        "presetStopLossPrice":   str(round(sl, 6)),
        "productType":           "USDT-FUTURES"
    }
    body_str  = json.dumps(body, separators=(',', ':'))
    msg       = ts + "POST" + path + body_str
    signature = sign(msg, BITGET_SECRET_KEY)
    headers   = {
        "ACCESS-KEY":        BITGET_API_KEY,
        "ACCESS-SIGN":       signature,
        "ACCESS-TIMESTAMP":  ts,
        "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
        "Content-Type":      "application/json",
        "locale":            "en-US"
    }
    resp = requests.post(BITGET_BASE_URL + path, headers=headers, data=body_str)
    return resp.json()

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data   = request.get_json(force=True)
        action = data.get("action", "").lower()
        symbol = data.get("symbol", "").replace("USDT", "").replace("USD", "")
        entry  = float(data.get("entry", 0))
        sl     = float(data.get("sl", 0))
        tp     = float(data.get("tp", 0))

        if entry == 0 or sl == 0:
            return jsonify({"error": "missing entry or sl"}), 400

        if tp == 0:
            risk = abs(entry - sl)
            if action == "buy":
                tp = entry + risk * RR_RATIO
            else:
                tp = entry - risk * RR_RATIO

        side   = "buy" if action == "buy" else "sell"
        result = place_order(symbol, side, entry, sl, tp, ORDER_SIZE_USDT)
        print(f"Order result: {result}")
        return jsonify({"status": "ok", "result": result})

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def health():
    return "TCB Webhook Bot running!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
