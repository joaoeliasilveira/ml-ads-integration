from flask import Flask, request, jsonify, render_template, redirect
import requests
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone, timedelta

app = Flask(__name__)

CLIENT_ID     = os.environ.get("ML_CLIENT_ID")
CLIENT_SECRET = os.environ.get("ML_CLIENT_SECRET")
REDIRECT_URI  = os.environ.get("ML_REDIRECT_URI")
DATABASE_URL  = os.environ.get("DATABASE_URL")

TOKEN_TTL = timedelta(hours=5, minutes=30)

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sellers (
            user_id TEXT PRIMARY KEY,
            nickname TEXT,
            access_token TEXT,
            refresh_token TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

try:
    init_db()
except Exception as e:
    print(f"Erro ao inicializar banco: {e}")

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/auth")
def auth():
    auth_url = (
        "https://auth.mercadolivre.com.br/authorization"
        "?response_type=code"
        "&client_id=" + CLIENT_ID +
        "&redirect_uri=" + REDIRECT_URI +
        "&scope=read_advertising+write_advertising+read_metrics+offline_access"
    )
    return redirect(auth_url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Erro: codigo nao recebido", 400

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
    user_id       = str(token_data.get("user_id"))

    if not access_token:
        return "Erro ao obter token: " + str(token_data), 400

    user_resp = requests.get(
        "https://api.mercadolibre.com/users/" + user_id,
        headers={"Authorization": "Bearer " + access_token}
    )
    nickname = user_resp.json().get("nickname", "Seller " + user_id)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sellers (user_id, nickname, access_token, refresh_token, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (user_id) DO UPDATE
        SET access_token = EXCLUDED.access_token,
            refresh_token = EXCLUDED.refresh_token,
            nickname = EXCLUDED.nickname,
            updated_at = NOW()
    """, (user_id, nickname, access_token, refresh_token))
    conn.commit()
    cur.close()
    conn.close()

    return redirect("/?seller_added=" + nickname)

@app.route("/api/sellers")
def get_sellers():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, nickname FROM sellers ORDER BY nickname")
    sellers = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(s) for s in sellers])

@app.route("/api/sellers/<user_id>", methods=["DELETE"])
def delete_seller(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sellers WHERE user_id = %s", (user_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"success": True})

def refresh_token_if_needed(user_id, access_token, refresh_token, updated_at):
    now = datetime.now(timezone.utc)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    if now - updated_at < TOKEN_TTL:
        return access_token
    resp = requests.post(
        "https://api.mercadolibre.com/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": refresh_token
        }
    )
    new_tokens = resp.json()
    new_access  = new_tokens.get("access_token", access_token)
    new_refresh = new_tokens.get("refresh_token", refresh_token)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE sellers SET access_token=%s, refresh_token=%s, updated_at=NOW()
        WHERE user_id=%s
    """, (new_access, new_refresh, user_id))
    conn.commit()
    cur.close()
    conn.close()
    return new_access

def get_campaigns(user_id, token):
    endpoints = [
        "https://api.mercadolibre.com/advertising/advertisers/" + user_id + "/product_ads/campaigns",
        "https://api.mercadolibre.com/advertising/advertisers/" + user_id + "/campaigns",
    ]
    for url in endpoints:
        resp = requests.get(url, headers={"Authorization": "Bearer " + token})
        data = resp.json()
        if resp.ok and isinstance(data, list):
            return data, url
        if resp.ok and isinstance(data, dict) and "results" in data:
            return data["results"], url
    return [], None

def get_campaign_metrics(user_id, camp_id, token, date_from, date_to, base_url):
    if "product_ads" in base_url:
        url = "https://api.mercadolibre.com/advertising/advertisers/" + user_id + "/product_ads/campaigns/" + str(camp_id) + "/metrics/days"
    else:
        url = "https://api.mercadolibre.com/advertising/advertisers/" + user_id + "/campaigns/" + str(camp_id) + "/metrics/days"
    resp = requests.get(url, params={"date_from": date_from, "date_to": date_to}, headers={"Authorization": "Bearer " + token})
    return resp.json() if resp.ok else []

@app.route("/api/ads/<user_id>")
def get_ads(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sellers WHERE user_id = %s", (user_id,))
    seller = cur.fetchone()
    cur.close()
    conn.close()

    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    token = refresh_token_if_needed(user_id, seller["access_token"], seller["refresh_token"], seller["updated_at"])
    date_from = request.args.get("date_from", "2026-05-01")
    date_to   = request.args.get("date_to",   "2026-05-26")
    campaigns, base_url = get_campaigns(user_id, token)

    result = []
    total_spend = total_revenue = total_clicks = total_impressions = 0

    for camp in campaigns[:10]:
        camp_id = camp.get("id")
        metrics = get_campaign_metrics(user_id, camp_id, token, date_from, date_to, base_url or "")
        spend   = sum(d.get("cost", 0) for d in metrics) if isinstance(metrics, list) else metrics.get("cost", 0)
        revenue = sum(d.get("revenue", 0) for d in metrics) if isinstance(metrics, list) else metrics.get("revenue", 0)
        clicks  = sum(d.get("clicks", 0) for d in metrics) if isinstance(metrics, list) else metrics.get("clicks", 0)
        imps    = sum(d.get("impressions", 0) for d in metrics) if isinstance(metrics, list) else metrics.get("impressions", 0)
        roas = round(revenue / spend, 2) if spend > 0 else 0
        acos = round((spend / revenue) * 100, 1) if revenue > 0 else 0
        total_spend += spend
        total_revenue += revenue
        total_clicks += clicks
        total_impressions += imps
        result.append({
            "id": camp_id,
            "name": camp.get("name", "Campanha " + str(camp_id)),
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
        "nickname": seller["nickname"],
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

@app.route("/api/ads/<user_id>/daily")
def get_ads_daily(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sellers WHERE user_id = %s", (user_id,))
    seller = cur.fetchone()
    cur.close()
    conn.close()

    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    token = refresh_token_if_needed(user_id, seller["access_token"], seller["refresh_token"], seller["updated_at"])
    date_from = request.args.get("date_from", "2026-05-01")
    date_to   = request.args.get("date_to",   "2026-05-26")
    campaigns, base_url = get_campaigns(user_id, token)
    daily_map = {}

    for camp in campaigns[:10]:
        camp_id = camp.get("id")
        metrics = get_campaign_metrics(user_id, camp_id, token, date_from, date_to, base_url or "")
        if isinstance(metrics, list):
            for day in metrics:
                date = day.get("date", "")
                if not date:
                    continue
                if date not in daily_map:
                    daily_map[date] = {"date": date, "cost": 0, "revenue": 0, "clicks": 0, "impressions": 0}
                daily_map[date]["cost"]        += day.get("cost", 0)
                daily_map[date]["revenue"]     += day.get("revenue", 0)
                daily_map[date]["clicks"]      += day.get("clicks", 0)
                daily_map[date]["impressions"] += day.get("impressions", 0)

    days_list = sorted(daily_map.values(), key=lambda x: x["date"])
    for d in days_list:
        d["cost"]    = round(d["cost"], 2)
        d["revenue"] = round(d["revenue"], 2)

    return jsonify({
        "seller_id": user_id,
        "nickname": seller["nickname"],
        "date_from": date_from,
        "date_to": date_to,
        "days": days_list
    })

@app.route("/api/metrics/<user_id>")
def get_metrics(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sellers WHERE user_id = %s", (user_id,))
    seller = cur.fetchone()
    cur.close()
    conn.close()

    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    token = refresh_token_if_needed(user_id, seller["access_token"], seller["refresh_token"], seller["updated_at"])
    date_from = request.args.get("date_from", "2026-05-01")
    date_to   = request.args.get("date_to",   "2026-05-26")

    rep_resp = requests.get(
        "https://api.mercadolibre.com/users/" + user_id,
        headers={"Authorization": "Bearer " + token}
    )
    rep_data = rep_resp.json() if rep_resp.ok else {}
    reputation = rep_data.get("seller_reputation", {})
    metrics_rep = reputation.get("metrics", {})
    transactions = reputation.get("transactions", {})

    sales_resp = requests.get(
        "https://api.mercadolibre.com/orders/search",
        params={
            "seller": user_id,
            "order.date_created.from": date_from + "T00:00:00.000-00:00",
            "order.date_created.to":   date_to + "T23:59:59.000-00:00",
            "order.status": "paid",
            "limit": 50
        },
        headers={"Authorization": "Bearer " + token}
    )
    sales_data   = sales_resp.json() if sales_resp.ok else {}
    orders       = sales_data.get("results", [])
    total_orders = sales_data.get("paging", {}).get("total", 0)
    gmv          = sum(o.get("total_amount", 0) for o in orders)

    visits_resp = requests.get(
        "https://api.mercadolibre.com/users/" + user_id + "/items_visits",
        params={"date_from": date_from, "date_to": date_to},
        headers={"Authorization": "Bearer " + token}
    )
    visits_data  = visits_resp.json() if visits_resp.ok else {}
    total_visits = visits_data.get("total_visits", 0)

    return jsonify({
        "seller_id": user_id,
        "nickname": seller["nickname"],
        "period": {"date_from": date_from, "date_to": date_to},
        "sales": {
            "total_orders": total_orders,
            "gmv": round(gmv, 2),
            "avg_ticket": round(gmv / total_orders, 2) if total_orders > 0 else 0
        },
        "visits": {
            "total": total_visits,
            "conversion": round((total_orders / total_visits) * 100, 2) if total_visits > 0 else 0
        },
        "reputation": {
            "level": reputation.get("level_id", ""),
            "power_seller": rep_data.get("power_seller_status", ""),
            "cancellations": metrics_rep.get("cancellations", {}).get("rate", 0),
            "claims": metrics_rep.get("claims", {}).get("rate", 0),
            "delayed": metrics_rep.get("delayed_handling_time", {}).get("rate", 0),
            "sales_completed": transactions.get("completed", 0),
            "ratings": reputation.get("ratings", {})
        }
    })

@app.route("/api/debug-metrics/<user_id>")
def debug_metrics(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sellers WHERE user_id = %s", (user_id,))
    seller = cur.fetchone()
    cur.close()
    conn.close()

    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    token = refresh_token_if_needed(user_id, seller["access_token"], seller["refresh_token"], seller["updated_at"])

    r_orders = requests.get(
        "https://api.mercadolibre.com/orders/search",
        params={"seller": user_id, "order.status": "paid", "limit": 1},
        headers={"Authorization": "Bearer " + token}
    )
    r_visits = requests.get(
        "https://api.mercadolibre.com/users/" + user_id + "/items_visits",
        params={"date_from": "2026-05-01", "date_to": "2026-05-26"},
        headers={"Authorization": "Bearer " + token}
    )
    r_rep = requests.get(
        "https://api.mercadolibre.com/users/" + user_id,
        headers={"Authorization": "Bearer " + token}
    )

    return jsonify({
        "orders":     {"status": r_orders.status_code, "response": r_orders.json()},
        "visits":     {"status": r_visits.status_code, "response": r_visits.json()},
        "reputation": {"status": r_rep.status_code,    "response": r_rep.json()}
    })

@app.route("/api/debug/<user_id>")
def debug_ads(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sellers WHERE user_id = %s", (user_id,))
    seller = cur.fetchone()
    cur.close()
    conn.close()

    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    token = refresh_token_if_needed(user_id, seller["access_token"], seller["refresh_token"], seller["updated_at"])

    urls = [
        "https://api.mercadolibre.com/advertising/advertisers/" + user_id + "/product_ads/campaigns",
        "https://api.mercadolibre.com/advertising/advertisers/" + user_id + "/campaigns",
        "https://api.mercadolibre.com/advertising/" + user_id + "/campaigns",
        "https://api.mercadolibre.com/advertising/advertisers/" + user_id,
    ]

    results = {}
    for url in urls:
        r = requests.get(url, headers={"Authorization": "Bearer " + token})
        results[url] = {"status": r.status_code, "response": r.json()}

    return jsonify({"user_id": user_id, "endpoints_tested": results})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
