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
RR_RATIO          = float(os.environ.get("RR_RATIO", "1.0"))
ORDER_SIZE_USDT   = float(os.environ.get("ORDER_SIZE_USDT", "100"))
LEVERAGE          = int(os.environ.get("LEVERAGE", "3"))

tick_cache    = {}
setup_cache   = set()
one_way_cache = set()  # Symbole die One-Way sind

def sign(message, secret):
    mac = hmac.new(secret.encode(), message.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def get_timestamp():
    return str(int(time.time() * 1000))

def signed_post(path, body_dict):
    ts       = get_timestamp()
    body_str = json.dumps(body_dict, separators=(',', ':'))
    msg      = ts + "POST" + path + body_str
    headers  = {
        "ACCESS-KEY":        BITGET_API_KEY,
        "ACCESS-SIGN":       sign(msg, BITGET_SECRET_KEY),
        "ACCESS-TIMESTAMP":  ts,
        "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
        "Content-Type":      "application/json",
        "locale":            "en-US"
    }
    resp = requests.post(BITGET_BASE_URL + path, headers=headers, data=body_str)
    return resp.json()

def get_tick_size(symbol):
    if symbol in tick_cache:
        return tick_cache[symbol]
    try:
        url    = f"{BITGET_BASE_URL}/api/v2/mix/market/contracts"
        params = {"symbol": symbol + "USDT", "productType": "USDT-FUTURES"}
        resp   = requests.get(url, params=params, timeout=5)
        data   = resp.json()
        place  = int(data["data"][0]["pricePlace"])
        tick   = 10 ** (-place)
        tick_cache[symbol] = tick
        print(f"Tick-Size {symbol}: {tick} (pricePlace: {place})")
        return tick
    except Exception as e:
        print(f"Tick-Size Fehler {symbol}: {e}")
        return 0.00001

def format_price(price, tick):
    if tick is None or tick == 0:
        return str(price)
    decimals = 0
    t = tick
    while t < 1:
        t *= 10
        decimals += 1
    return str(round(price, decimals))

def setup_symbol(symbol, side):
    cache_key = f"{symbol}_{side}"
    if cache_key in setup_cache:
        return

    full_symbol = symbol + "USDT"

    # 1. Margin Mode → Isolated
    r1 = signed_post("/api/v2/mix/account/set-margin-mode", {
        "symbol":      full_symbol,
        "productType": "USDT-FUTURES",
        "marginCoin":  "USDT",
        "marginMode":  "isolated"
    })
    print(f"Margin Mode {symbol}: {r1}")

    # 2. Hebel setzen
    for hold_side in ["long", "short"]:
        r2 = signed_post("/api/v2/mix/account/set-leverage", {
            "symbol":      full_symbol,
            "productType": "USDT-FUTURES",
            "marginCoin":  "USDT",
            "leverage":    str(LEVERAGE),
            "holdSide":    hold_side
        })
        print(f"Hebel {symbol} {hold_side}: {r2}")

        if r2.get("code") in ["40774", "400172"]:
            # One-Way Symbol → ohne holdSide
            one_way_cache.add(symbol)
            r2b = signed_post("/api/v2/mix/account/set-leverage", {
                "symbol":      full_symbol,
                "productType": "USDT-FUTURES",
                "marginCoin":  "USDT",
                "leverage":    str(LEVERAGE)
            })
            print(f"Hebel {symbol} (one-way fallback): {r2b}")
            break

    setup_cache.add(cache_key)

def place_order(symbol, side, entry, sl, tp, size_usdt):
    setup_symbol(symbol, side)

    tick = get_tick_size(symbol)
    qty  = round(size_usdt / entry, 4)

    is_one_way = symbol in one_way_cache

    if is_one_way:
        # One-Way: buy/sell direkt
        order_side = side
    else:
        # Hedge: open_long/open_short
        order_side = "open_long" if side == "buy" else "open_short"

    # Basis Body
    body = {
        "symbol":                symbol + "USDT",
        "marginCoin":            "USDT",
        "size":                  str(qty),
        "side":                  order_side,
        "orderType":             "market",
        "presetTakeProfitPrice": format_price(tp, tick),
        "presetStopLossPrice":   format_price(sl, tick),
        "productType":           "USDT-FUTURES"
    }

    # marginMode nur für Hedge Symbole
    if not is_one_way:
        body["marginMode"] = "isolated"

    result = signed_post("/api/v2/mix/order/place-order", body)
    print(f"Order result: {result}")

    # Falls immer noch 40774 → anderes Format versuchen
    if result.get("code") == "40774":
        print(f"Fallback 1: marginMode entfernen für {symbol}")
        body.pop("marginMode", None)
        body["side"] = side  # buy/sell
        result = signed_post("/api/v2/mix/order/place-order", body)
        print(f"Fallback 1 result: {result}")

    # Falls immer noch Fehler → open_long/open_short versuchen
    if result.get("code") == "40774":
        print(f"Fallback 2: open_long/open_short für {symbol}")
        body["side"] = "open_long" if side == "buy" else "open_short"
        result = signed_post("/api/v2/mix/order/place-order", body)
        print(f"Fallback 2 result: {result}")

    return result

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data   = request.get_json(force=True)
        action = data.get("action", "").lower()
        symbol = data.get("symbol", "").replace("USDT", "").replace("USD", "")
        entry  = float(data.get("entry", 0))
        sl_raw = data.get("sl", 0)
        tp_raw = data.get("tp", 0)

        # null-safe conversion
        try:
            sl = float(sl_raw) if sl_raw not in (None, "null", "", 0, "0") else 0.0
        except (ValueError, TypeError):
            sl = 0.0

        try:
            tp = float(tp_raw) if tp_raw not in (None, "null", "", 0, "0") else 0.0
        except (ValueError, TypeError):
            tp = 0.0

        print(f"Signal: {action} {symbol} entry={entry} sl={sl} tp={tp}")

        if entry == 0 or entry == 1.0:
            return jsonify({"error": "invalid entry price"}), 400

        if sl == 0:
            return jsonify({"error": "missing sl"}), 400

        if tp == 0:
            risk = abs(entry - sl)
            tp   = entry + risk * RR_RATIO if action == "buy" else entry - risk * RR_RATIO

        # Plausibilitätsprüfung
        if action == "buy" and (sl >= entry or tp <= entry):
            return jsonify({"error": f"buy: sl={sl} muss < entry={entry}, tp={tp} muss > entry"}), 400
        if action == "sell" and (sl <= entry or tp >= entry):
            return jsonify({"error": f"sell: sl={sl} muss > entry={entry}, tp={tp} muss < entry"}), 400

        side   = "buy" if action == "buy" else "sell"
        result = place_order(symbol, side, entry, sl, tp, ORDER_SIZE_USDT)
        return jsonify({"status": "ok", "result": result})

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def health():
    return "TCB Webhook Bot running!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
