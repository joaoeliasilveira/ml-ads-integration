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
    user_id       = str(token_data.get("user_id"))

    if not access_token:
        return f"Erro ao obter token: {token_data}", 400

    user_resp = requests.get(
        f"https://api.mercadolibre.com/users/{user_id}",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    nickname = user_resp.json().get("nickname", f"Seller {user_id}")

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

    return redirect(f"/?seller_added={nickname}")

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

@app.route("/api/ads/<user_id>")
def get_ads(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sellers WHERE user_id = %s", (user_id,))
    seller = cur.fetchone()
    cur.close()
    conn.close()

    if not seller:
        return jsonify({"error": "Seller não autorizado"}), 404

    token = refresh_token_if_needed(user_id, seller["access_token"], seller["refresh_token"], seller["updated_at"])

    date_from = request.args.get("date_from", "2026-05-01")
    date_to   = request.args.get("date_to",   "2026-05-26")

    camps_resp = requests.get(
        f"https://api.mercadolibre.com/advertising/advertisers/{user_id}/campaigns",
        headers={"Authorization": f"Bearer {token}"}
    )
    campaigns = camps_resp.json() if camps_resp.ok else []

    result = []
    total_spend = total_revenue = total_clicks = total_impressions = 0

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

            total_spend       += spend
            total_revenue     += revenue
            total_clicks      += clicks
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
        return jsonify({"error": "Seller não autorizado"}), 404

    token = refresh_token_if_needed(user_id, seller["access_token"], seller["refresh_token"], seller["updated_at"])

    date_from = request.args.get("date_from", "2026-05-01")
    date_to   = request.args.get("date_to",   "2026-05-26")

    camps_resp = requests.get(
        f"https://api.mercadolibre.com/advertising/advertisers/{user_id}/campaigns",
        headers={"Authorization": f"Bearer {token}"}
    )
    campaigns = camps_resp.json() if camps_resp.ok else []

    daily_map = {}

    if isinstance(campaigns, list):
        for camp in campaigns[:10]:
            camp_id = camp.get("id")
            metrics_resp = requests.get(
                f"https://api.mercadolibre.com/advertising/advertisers/{user_id}/campaigns/{camp_id}/metrics/days",
                params={"date_from": date_from, "date_to": date_to},
                headers={"Authorization": f"Bearer {token}"}
            )
            metrics = metrics_resp.json() if metrics_resp.ok else []

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

@app.route("/api/debug/<user_id>")
def debug_ads(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sellers WHERE user_id = %s", (user_id,))
    seller = cur.fetchone()
    cur.close()
    conn.close()

    if not seller:
        return jsonify({"error": "Seller não encontrado"}), 404

    token = refresh_token_if_needed(user_id, seller["access_token"], seller["refresh_token"], seller["updated_at"])

    camps_resp = requests.get(
        f"https://api.mercadolibre.com/advertising/advertisers/{user_id}/campaigns",
        headers={"Authorization": f"Bearer {token}"}
    )

    metrics_raw = None
    campaigns = camps_resp.json()
    if isinstance(campaigns, list) and len(campaigns) > 0:
        first_camp = campaigns[0]
        m = requests.get(
            f"https://api.mercadolibre.com/advertising/advertisers/{user_id}/campaigns/{first_camp.get('id')}/metrics/days",
            params={"date_from": "2026-05-01", "date_to": "2026-05-26"},
            headers={"Authorization": f"Bearer {token}"}
        )
        metrics_raw = m.json()

    return jsonify({
        "campaigns_status": camps_resp.status_code,
        "campaigns_raw": campaigns,
        "first_campaign_metrics": metrics_raw
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
