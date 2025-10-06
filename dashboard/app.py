from flask import Flask, render_template, jsonify
import json, os

app = Flask(__name__, template_folder="templates")
LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "trades.json")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/data")
def api_data():
    if not os.path.exists(LOG_PATH):
        return jsonify({"timestamps": [], "profit": [], "positions": {}, "trades": []})
    with open(LOG_PATH, "r") as f:
        data = json.load(f)
    timestamps = [t.get("closed_at") or t.get("ts") for t in data.get("trades", [])]
    profit = []
    total = 0.0
    for t in data.get("trades", []):
        rp = t.get("realized_pnl")
        if rp is None:
            profit.append(total)
            continue
        total += float(rp)
        profit.append(total)
    return jsonify({
        "timestamps": timestamps,
        "profit": profit,
        "positions": data.get("positions", {}),
        "trades": data.get("trades", [])
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
