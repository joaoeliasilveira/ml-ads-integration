from flask import Flask, request, jsonify, render_template, redirect
import requests
import os
import json

app = Flask(__name__)

CLIENT_ID     = os.environ.get("ML_CLIENT_ID")
CLIENT_SECRET = os.environ.get("ML_CLIENT_SECRET")
REDIRECT_URI  = os.environ.get("ML_REDIRECT_URI")

# Armazena tokens em memória (em produção use banco de dados)
tokens_store = {}

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/auth")
def auth():
    auth_url = (
        f"https://auth.mercadolivre.com.br/authorization"
        f"?response_type=code"
        f"&client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
    )
    return redirect(auth_url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Erro: código não recebido", 400

    response = requests.post(
        "https://api.mercadolibre.com/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "redirect_uri": REDIRECT_URI
        }
    )

    token_data = response.json()
    access_token  = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    user_id       = token_data.get("user_id")

    if not access_token:
        return f"Erro ao obter token: {token_data}", 400

    # Salva o token
    tokens_store[str(user_id)] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user_id": user_id
    }

    # Busca nome do seller
    user_resp = requests.get(
        f"https://api.mercadolibre.com/users/{user_id}",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    user_data = user_resp.json()
    tokens_store[str(user_id)]["nickname"] = user_data.get("nickname", f"Seller {user_id}")

    return redirect(f"/?seller_added={user_data.get('nickname', user_id)}")

@app.route("/api/sellers")
def get_sellers():
    return jsonify(list(tokens_store.values()))

@app.route("/api/ads/<user_id>")
def get_ads(user_id):
    if user_id not in tokens_store:
        return jsonify({"error": "Seller não autorizado"}), 404

    token = tokens_store[user_id]["access_token"]
    date_from = request.args.get("date_from", "2026-05-01")
    date_to   = request.args.get("date_to",   "2026-05-26")

    # Busca campanhas
    camps_resp = requests.get(
        f"https://api.mercadolibre.com/advertising/advertisers/{user_id}/campaigns",
        headers={"Authorization": f"Bearer {token}"}
    )
    campaigns = camps_resp.json() if camps_resp.ok else []

    result = []
    total_spend = 0
    total_revenue = 0
    total_clicks = 0
    total_impressions = 0

    if isinstance(campaigns, list):
        for camp in campaigns[:10]:
            camp_id = camp.get("id")
            metrics_resp = requests.get(
                f"https://api.mercadolibre.com/advertising/advertisers/{user_id}/campaigns/{camp_id}/metrics/days",
                params={"date_from": date_from, "date_to": date_to},
                headers={"Authorization": f"Bearer {token}"}
            )
            metrics = metrics_resp.json() if metrics_resp.ok else {}

            spend   = sum(d.get("cost", 0) for d in metrics) if isinstance(metrics, list) else metrics.get("cost", 0)
            revenue = sum(d.get("revenue", 0) for d in metrics) if isinstance(metrics, list) else metrics.get("revenue", 0)
            clicks  = sum(d.get("clicks", 0) for d in metrics) if isinstance(metrics, list) else metrics.get("clicks", 0)
            imps    = sum(d.get("impressions", 0) for d in metrics) if isinstance(metrics, list) else metrics.get("impressions", 0)

            roas = round(revenue / spend, 2) if spend > 0 else 0
            acos = round((spend / revenue) * 100, 1) if revenue > 0 else 0

            total_spend     += spend
            total_revenue   += revenue
            total_clicks    += clicks
            total_impressions += imps

            result.append({
                "id": camp_id,
                "name": camp.get("name", f"Campanha {camp_id}"),
                "status": camp.get("status", "unknown"),
                "spend": round(spend, 2),
                "revenue": round(revenue, 2),
                "clicks": clicks,
                "impressions": imps,
                "roas": roas,
                "acos": acos
            })

    return jsonify({
        "seller_id": user_id,
        "nickname": tokens_store[user_id].get("nickname"),
        "summary": {
            "spend": round(total_spend, 2),
            "revenue": round(total_revenue, 2),
            "clicks": total_clicks,
            "impressions": total_impressions,
            "roas": round(total_revenue / total_spend, 2) if total_spend > 0 else 0,
            "acos": round((total_spend / total_revenue) * 100, 1) if total_revenue > 0 else 0
        },
        "campaigns": result
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
