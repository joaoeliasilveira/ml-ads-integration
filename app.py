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
    print("Erro ao inicializar banco: " + str(e))

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
        "&scope=read_ads+offline_access"
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

def get_seller_token(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sellers WHERE user_id = %s", (user_id,))
    seller = cur.fetchone()
    cur.close()
    conn.close()
    if not seller:
        return None, None
    token = refresh_token_if_needed(user_id, seller["access_token"], seller["refresh_token"], seller["updated_at"])
    return token, seller

def get_advertiser_id(user_id, token):
    resp = requests.get(
        "https://api.mercadolibre.com/advertising/advertisers",
        params={"product_id": "PADS"},
        headers={"Authorization": "Bearer " + token, "Api-Version": "1"}
    )
    if not resp.ok:
        return None
    data = resp.json()
    advertisers = data if isinstance(data, list) else data.get("advertisers", data.get("results", []))
    if advertisers and len(advertisers) > 0:
        return str(advertisers[0].get("id", advertisers[0].get("advertiser_id", "")))
    return None

def get_campaigns(user_id, token):
    advertiser_id = get_advertiser_id(user_id, token)
    aid = advertiser_id if advertiser_id else user_id

    endpoints = [
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/campaigns",
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/campaigns",
    ]
    for url in endpoints:
        resp = requests.get(url, headers={"Authorization": "Bearer " + token, "Api-Version": "1"})
        try:
            data = resp.json()
        except Exception:
            continue
        if not resp.ok:
            print("[ADS][ERRO] GET " + url + " HTTP " + str(resp.status_code))
            continue
        if isinstance(data, list):
            return data, url, aid
        if isinstance(data, dict) and "results" in data:
            return data["results"], url, aid
    return [], None, aid

def get_campaign_metrics(advertiser_id, camp_id, token, date_from, date_to, base_url):
    url = "https://api.mercadolibre.com/advertising/MLB/product_ads/campaigns/" + str(camp_id)
    resp = requests.get(url, params={
        "date_from": date_from,
        "date_to": date_to,
        "metrics": "clicks,prints,cost,direct_amount,indirect_amount,total_amount"
    }, headers={"Authorization": "Bearer " + token, "Api-Version": "2"})
    if not resp.ok:
        print("[METRICS][ERRO] " + url + " HTTP " + str(resp.status_code) + " " + resp.text[:200])
        return {}
    try:
        return resp.json()
    except Exception:
        return {}

@app.route("/api/ads/<user_id>")
def get_ads(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    date_from = request.args.get("date_from", "2026-05-01")
    date_to   = request.args.get("date_to",   "2026-05-26")
    campaigns, base_url, aid = get_campaigns(user_id, token)

    result = []
    total_spend = total_revenue = total_clicks = total_impressions = 0

    for camp in campaigns[:10]:
        camp_id = camp.get("id")
        metrics = get_campaign_metrics(aid, camp_id, token, date_from, date_to, base_url or "")

        # Formato: response tem objeto "metrics" dentro
        if isinstance(metrics, dict) and "metrics" in metrics:
            m = metrics["metrics"]
        elif isinstance(metrics, list) and len(metrics) > 0:
            m = metrics[0].get("metrics", metrics[0])
        else:
            m = metrics if isinstance(metrics, dict) else {}

        spend   = m.get("cost", 0)
        revenue = m.get("total_amount", m.get("direct_amount", m.get("revenue", 0)))
        clicks  = m.get("clicks", 0)
        imps    = m.get("prints", m.get("impressions", 0))

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
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    date_from = request.args.get("date_from", "2026-05-01")
    date_to   = request.args.get("date_to",   "2026-05-26")
    campaigns, base_url, aid = get_campaigns(user_id, token)
    daily_map = {}

    for camp in campaigns[:10]:
        camp_id = camp.get("id")
        metrics = get_campaign_metrics(aid, camp_id, token, date_from, date_to, base_url or "")
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

@app.route("/api/reputation/<user_id>")
def get_reputation(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    rep_resp = requests.get(
        "https://api.mercadolibre.com/users/" + user_id,
        headers={"Authorization": "Bearer " + token}
    )
    rep_data = rep_resp.json() if rep_resp.ok else {}
    reputation = rep_data.get("seller_reputation", {})
    metrics_rep = reputation.get("metrics", {})
    transactions = reputation.get("transactions", {})

    return jsonify({
        "seller_id": user_id,
        "nickname": seller["nickname"],
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

@app.route("/api/sales/<user_id>")
def get_sales(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    date_from = request.args.get("date_from", "2026-05-01")
    date_to   = request.args.get("date_to",   "2026-05-26")

    from datetime import date as ddate, timedelta as tdelta

    def fetch_orders_by_status(uid, tok, dfrom, dto, status, max_offset=10000):
        all_orders = []
        offset = 0
        limit = 50
        total = None
        while offset <= max_offset:
            r = requests.get(
                "https://api.mercadolibre.com/orders/search",
                params={
                    "seller": uid,
                    "order.date_created.from": dfrom + "T00:00:00.000-00:00",
                    "order.date_created.to":   dto + "T23:59:59.000-00:00",
                    "order.status": status,
                    "limit": limit,
                    "offset": offset,
                    "sort": "date_asc"
                },
                headers={"Authorization": "Bearer " + tok}
            )
            if not r.ok or not r.text:
                break
            data = r.json()
            results = data.get("results", [])
            if total is None:
                total = data.get("paging", {}).get("total", 0)
            all_orders.extend(results)
            offset += limit
            if offset >= (total or 0) or not results:
                break
        return all_orders, total or 0

    def fetch_all_orders(uid, tok, dfrom, dto, max_pages=200):
        # Busca paid + payment_in_process (ambos contam como vendas no ML)
        paid_orders, paid_total = fetch_orders_by_status(uid, tok, dfrom, dto, "paid")
        pip_orders, pip_total   = fetch_orders_by_status(uid, tok, dfrom, dto, "payment_in_process")
        all_orders = paid_orders + pip_orders
        total = paid_total + pip_total
        return all_orders, total

    # Periodo atual
    orders, total_orders = fetch_all_orders(user_id, token, date_from, date_to)
    # total_paid_amount eh o campo mais preciso para vendas brutas
    def order_value(o):
        val = o.get("total_paid_amount") or o.get("paid_amount") or o.get("total_amount") or 0
        return val
    gmv = sum(order_value(o) for o in orders)

    # Periodo anterior
    d_from = ddate.fromisoformat(date_from)
    d_to   = ddate.fromisoformat(date_to)
    delta  = (d_to - d_from).days + 1
    prev_from = (d_from - tdelta(days=delta)).isoformat()
    prev_to   = (d_from - tdelta(days=1)).isoformat()
    prev_orders, prev_total = fetch_all_orders(user_id, token, prev_from, prev_to)
    prev_gmv = sum(order_value(o) for o in prev_orders)

    # Visitas
    visits_resp = requests.get(
        "https://api.mercadolibre.com/users/" + user_id + "/items_visits",
        params={"date_from": date_from, "date_to": date_to},
        headers={"Authorization": "Bearer " + token}
    )
    visits_data  = visits_resp.json() if visits_resp.ok and visits_resp.text else {}
    total_visits = visits_data.get("total_visits", 0)

    # Vendas diarias
    daily_map = {}
    for o in orders:
        day = o.get("date_created", "")[:10]
        if not day:
            continue
        if day not in daily_map:
            daily_map[day] = {"date": day, "gmv": 0, "orders": 0}
        daily_map[day]["gmv"]    += order_value(o)
        daily_map[day]["orders"] += 1
    daily_sales = sorted(daily_map.values(), key=lambda x: x["date"])
    for d in daily_sales:
        d["gmv"] = round(d["gmv"], 2)

    # Ranking de produtos
    products_map = {}
    for o in orders:
        for item in o.get("order_items", []):
            title = item.get("item", {}).get("title", "Produto")
            item_id = item.get("item", {}).get("id", "")
            qty   = item.get("quantity", 1)
            price = item.get("unit_price", 0)
            key   = item_id or title
            if key not in products_map:
                products_map[key] = {"title": title, "qty": 0, "revenue": 0}
            products_map[key]["qty"]     += qty
            products_map[key]["revenue"] += qty * price
    top_products = sorted(products_map.values(), key=lambda x: x["revenue"], reverse=True)[:10]
    for p in top_products:
        p["revenue"] = round(p["revenue"], 2)

    # Metricas de cancelamentos e devolucoes
    cancelled_orders = []
    returned_orders  = []

    r_cancelled = requests.get(
        "https://api.mercadolibre.com/orders/search",
        params={
            "seller": user_id,
            "order.date_created.from": date_from + "T00:00:00.000-00:00",
            "order.date_created.to":   date_to + "T23:59:59.000-00:00",
            "order.status": "cancelled",
            "limit": 50
        },
        headers={"Authorization": "Bearer " + token}
    )
    if r_cancelled.ok and r_cancelled.text:
        cancelled_data   = r_cancelled.json()
        cancelled_orders = cancelled_data.get("results", [])
        total_cancelled  = cancelled_data.get("paging", {}).get("total", 0)
        value_cancelled  = sum(o.get("paid_amount", 0) or o.get("total_amount", 0) for o in cancelled_orders)
    else:
        total_cancelled = 0
        value_cancelled = 0

    # Unidades vendidas e preco medio por unidade
    total_units = 0
    for o in orders:
        for item in o.get("order_items", []):
            total_units += item.get("quantity", 1)

    prev_units = 0
    for o in prev_orders:
        for item in o.get("order_items", []):
            prev_units += item.get("quantity", 1)

    avg_price_per_unit = round(gmv / total_units, 2) if total_units > 0 else 0
    prev_avg_unit      = round(prev_gmv / prev_units, 2) if prev_units > 0 else 0

    # Comparativo
    def var(curr, prev):
        if prev > 0:
            return round(((curr - prev) / prev * 100), 1)
        return 0

    return jsonify({
        "seller_id": user_id,
        "nickname": seller["nickname"],
        "period": {"date_from": date_from, "date_to": date_to},
        "summary": {
            "vendas_brutas":       round(gmv, 2),
            "unidades_vendidas":   total_units,
            "preco_medio_unidade": avg_price_per_unit,
            "qtd_vendas":          total_orders,
            "preco_medio_venda":   round(gmv / total_orders, 2) if total_orders > 0 else 0,
            "total_visits":        total_visits,
            "conversion":          round((total_orders / total_visits) * 100, 2) if total_visits > 0 else 0,
            "qtd_canceladas":      total_cancelled,
            "valor_canceladas":    round(value_cancelled, 2),
        },
        "comparison": {
            "prev_period":        {"date_from": prev_from, "date_to": prev_to},
            "prev_vendas_brutas": round(prev_gmv, 2),
            "prev_qtd_vendas":    prev_total,
            "prev_unidades":      prev_units,
            "prev_avg_unit":      prev_avg_unit,
            "var_vendas_brutas":  var(gmv, prev_gmv),
            "var_qtd_vendas":     var(total_orders, prev_total),
            "var_unidades":       var(total_units, prev_units),
            "var_avg_unit":       var(avg_price_per_unit, prev_avg_unit),
            "var_conversion":     var(
                round((total_orders / total_visits) * 100, 2) if total_visits > 0 else 0,
                0
            )
        },
        "daily_sales":  daily_sales,
        "top_products": top_products
    })

@app.route("/api/promotions/<user_id>")
def get_promotions(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    # Testa multiplos endpoints de promocoes do ML
    promotions = []
    status_info = {}

    # Endpoint 1: promotions/search (mais recente)
    r1 = requests.get(
        "https://api.mercadolibre.com/promotions/search",
        params={"seller_id": user_id, "type": "DEAL", "status": "started"},
        headers={"Authorization": "Bearer " + token}
    )
    status_info["promotions_search"] = r1.status_code
    if r1.ok:
        d = r1.json()
        promotions = d if isinstance(d, list) else d.get("results", d.get("deals", []))

    # Endpoint 2: seller-promotions (se o 1 falhar)
    if not promotions:
        r2 = requests.get(
            "https://api.mercadolibre.com/seller-promotions/users/" + user_id + "/promotions",
            headers={"Authorization": "Bearer " + token}
        )
        status_info["seller_promotions"] = r2.status_code
        if r2.ok:
            d = r2.json()
            promotions = d if isinstance(d, list) else d.get("results", d.get("promotions", []))

    # Endpoint 3: deals (cupons e descontos)
    if not promotions:
        r3 = requests.get(
            "https://api.mercadolibre.com/users/" + user_id + "/deals",
            headers={"Authorization": "Bearer " + token}
        )
        status_info["deals"] = r3.status_code
        if r3.ok:
            d = r3.json()
            promotions = d if isinstance(d, list) else d.get("results", d.get("deals", []))

    result = []
    for p in promotions[:20]:
        result.append({
            "id": str(p.get("id", p.get("deal_id", ""))),
            "name": p.get("name", p.get("description", p.get("deal_print_id", "Promocao"))),
            "type": p.get("type", p.get("deal_type", p.get("promotion_type", ""))),
            "status": p.get("status", ""),
            "discount": p.get("value", p.get("discount_meli_amount", p.get("percent_off", 0))),
            "start_date": str(p.get("start_date", p.get("from_date", p.get("from", ""))))[:10],
            "end_date": str(p.get("finish_date", p.get("to_date", p.get("to", ""))))[:10],
            "items_count": p.get("items_count", p.get("affected_items", 0))
        })

    return jsonify({
        "seller_id": user_id,
        "nickname": seller["nickname"],
        "total": len(result),
        "promotions": result,
        "status_info": status_info
    })

@app.route("/api/metrics/<user_id>")
def get_metrics(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

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

@app.route("/api/debug-advertisers/<user_id>")
def debug_advertisers(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    results = {}
    for product_id in ["PADS", "SPA", "PDA", "DISPLAY"]:
        r = requests.get(
            "https://api.mercadolibre.com/advertising/advertisers",
            params={"product_id": product_id},
            headers={"Authorization": "Bearer " + token, "Api-Version": "1"}
        )
        results[product_id] = {"status": r.status_code, "response": r.json() if r.text else {}}

    r_all = requests.get(
        "https://api.mercadolibre.com/advertising/advertisers",
        headers={"Authorization": "Bearer " + token, "Api-Version": "1"}
    )
    results["sem_product_id"] = {"status": r_all.status_code, "response": r_all.json()}

    return jsonify({
        "user_id": user_id,
        "nickname": seller["nickname"],
        "advertiser_lookup": results
    })

@app.route("/api/debug/<user_id>")
def debug_ads(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    advertiser_id = get_advertiser_id(user_id, token)
    aid = advertiser_id if advertiser_id else user_id

    urls = [
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/campaigns",
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/campaigns",
        "https://api.mercadolibre.com/advertising/advertisers/" + user_id + "/product_ads/campaigns",
        "https://api.mercadolibre.com/advertising/advertisers/" + user_id + "/campaigns",
    ]
    results = {}
    for url in urls:
        r = requests.get(url, headers={"Authorization": "Bearer " + token, "Api-Version": "1"})
        results[url] = {"status": r.status_code, "response": r.json() if r.text else {}}

    return jsonify({
        "user_id": user_id,
        "advertiser_id_found": advertiser_id,
        "endpoints_tested": results
    })

@app.route("/api/debug-metrics/<user_id>")
def debug_metrics(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

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
    r_promos1 = requests.get(
        "https://api.mercadolibre.com/promotions/search",
        params={"seller_id": user_id, "type": "DEAL", "status": "started"},
        headers={"Authorization": "Bearer " + token}
    )
    r_promos2 = requests.get(
        "https://api.mercadolibre.com/seller-promotions/users/" + user_id + "/promotions",
        headers={"Authorization": "Bearer " + token}
    )
    r_promos3 = requests.get(
        "https://api.mercadolibre.com/users/" + user_id + "/deals",
        headers={"Authorization": "Bearer " + token}
    )

    def safe_json(r):
        try:
            return r.json() if r.text and r.text.strip() else {}
        except Exception:
            return {"raw": r.text[:200] if r.text else ""}

    return jsonify({
        "orders":           {"status": r_orders.status_code,  "response": safe_json(r_orders)},
        "visits":           {"status": r_visits.status_code,  "response": safe_json(r_visits)},
        "reputation":       {"status": r_rep.status_code,     "response": safe_json(r_rep)},
        "promotions_search":{"status": r_promos1.status_code, "response": safe_json(r_promos1)},
        "seller_promotions":{"status": r_promos2.status_code, "response": safe_json(r_promos2)},
        "deals":            {"status": r_promos3.status_code, "response": safe_json(r_promos3)}
    })


@app.route("/api/debug-campaign/<user_id>/<camp_id>")
def debug_campaign(user_id, camp_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    advertiser_id = get_advertiser_id(user_id, token)
    aid = advertiser_id if advertiser_id else user_id

    date_from = request.args.get("date_from", "2026-04-01")
    date_to   = request.args.get("date_to",   "2026-05-26")

    results = {}
    tests = [
        ("MLB_v2_metrics", "https://api.mercadolibre.com/advertising/MLB/product_ads/campaigns/" + camp_id, {"date_from": date_from, "date_to": date_to, "metrics": "clicks,prints,cost,direct_amount,indirect_amount,total_amount"}, "2"),
        ("MLB_v1", "https://api.mercadolibre.com/advertising/MLB/product_ads/campaigns/" + camp_id, {"date_from": date_from, "date_to": date_to}, "1"),
        ("aid_v2", "https://api.mercadolibre.com/advertising/" + aid + "/product_ads/campaigns/" + camp_id, {"date_from": date_from, "date_to": date_to, "metrics": "clicks,prints,cost,direct_amount,indirect_amount,total_amount"}, "2"),
        ("aid_campaigns_v2", "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/campaigns/" + camp_id, {"date_from": date_from, "date_to": date_to, "metrics": "clicks,prints,cost,direct_amount,indirect_amount,total_amount"}, "2"),
        ("MLB_no_params", "https://api.mercadolibre.com/advertising/MLB/product_ads/campaigns/" + camp_id, {}, "2"),
    ]
    for name, url, params, version in tests:
        r = requests.get(url, params=params, headers={"Authorization": "Bearer " + token, "Api-Version": version})
        try:
            results[name] = {"status": r.status_code, "url": url, "response": r.json()}
        except Exception:
            results[name] = {"status": r.status_code, "url": url, "response": r.text[:300]}

    return jsonify({
        "user_id": user_id,
        "advertiser_id": aid,
        "camp_id": camp_id,
        "results": results
    })


@app.route("/api/debug-camp-detail/<user_id>")
def debug_camp_detail(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    advertiser_id = get_advertiser_id(user_id, token)
    aid = advertiser_id if advertiser_id else user_id

    # Busca campanhas e retorna JSON completo
    r = requests.get(
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/campaigns",
        headers={"Authorization": "Bearer " + token, "Api-Version": "1"}
    )
    try:
        camp_data = r.json()
    except Exception:
        camp_data = r.text[:500]

    # Testa endpoint de summary de metricas do advertiser
    r2 = requests.get(
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/campaigns/summary",
        params={"date_from": "2026-04-01", "date_to": "2026-05-26"},
        headers={"Authorization": "Bearer " + token, "Api-Version": "1"}
    )
    try:
        summary_data = r2.json()
    except Exception:
        summary_data = r2.text[:500]

    # Testa endpoint de reports
    r3 = requests.get(
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/reports",
        params={"date_from": "2026-04-01", "date_to": "2026-05-26"},
        headers={"Authorization": "Bearer " + token, "Api-Version": "1"}
    )
    try:
        reports_data = r3.json()
    except Exception:
        reports_data = r3.text[:500]

    return jsonify({
        "advertiser_id": aid,
        "campaigns_raw": camp_data,
        "summary_endpoint": {"status": r2.status_code, "response": summary_data},
        "reports_endpoint": {"status": r3.status_code, "response": reports_data}
    })


@app.route("/api/debug-metrics2/<user_id>")
def debug_metrics2(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    advertiser_id = get_advertiser_id(user_id, token)
    aid = advertiser_id if advertiser_id else user_id
    camp_id = "357533684"
    sf_id = "14"
    date_from = "2026-04-01"
    date_to = "2026-05-26"

    urls = [
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/campaigns/" + camp_id + "/metrics/days",
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/campaigns/" + camp_id + "/daily_metrics",
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/metrics",
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/metrics",
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/campaigns/" + camp_id + "/clicks",
        "https://api.mercadolibre.com/advertising/" + aid + "/campaigns/" + camp_id + "/metrics",
        "https://api.mercadolibre.com/pads/advertisers/" + aid + "/campaigns/" + camp_id + "/metrics",
    ]

    results = {}
    for url in urls:
        r = requests.get(url, params={"date_from": date_from, "date_to": date_to}, headers={"Authorization": "Bearer " + token, "Api-Version": "1"})
        try:
            results[url] = {"status": r.status_code, "response": r.json()}
        except Exception:
            results[url] = {"status": r.status_code, "response": r.text[:300]}

    return jsonify({"advertiser_id": aid, "camp_id": camp_id, "results": results})


@app.route("/api/debug-sales/<user_id>")
def debug_sales(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    from datetime import date as ddate, timedelta as tdelta
    today = ddate.today()
    date_from = (today - tdelta(days=7)).isoformat()
    date_to   = today.isoformat()

    # Testa diferentes status
    results = {}
    for status in ["paid", "cancelled", "delivered", "confirmed", "payment_required", "payment_in_process"]:
        r = requests.get(
            "https://api.mercadolibre.com/orders/search",
            params={
                "seller": user_id,
                "order.date_created.from": date_from + "T00:00:00.000-00:00",
                "order.date_created.to":   date_to + "T23:59:59.000-00:00",
                "order.status": status,
                "limit": 1,
                "offset": 0
            },
            headers={"Authorization": "Bearer " + token}
        )
        if r.ok and r.text:
            d = r.json()
            results[status] = {
                "total": d.get("paging", {}).get("total", 0),
                "status": r.status_code
            }
        else:
            results[status] = {"total": 0, "status": r.status_code}

    # Busca paid com paginacao e mostra totais
    all_paid = []
    offset = 0
    limit = 50
    total_api = 0
    pages = []
    while offset <= 500:
        r = requests.get(
            "https://api.mercadolibre.com/orders/search",
            params={
                "seller": user_id,
                "order.date_created.from": date_from + "T00:00:00.000-00:00",
                "order.date_created.to":   date_to + "T23:59:59.000-00:00",
                "order.status": "paid",
                "limit": limit,
                "offset": offset,
                "sort": "date_asc"
            },
            headers={"Authorization": "Bearer " + token}
        )
        if not r.ok or not r.text:
            break
        data = r.json()
        results_page = data.get("results", [])
        total_api = data.get("paging", {}).get("total", 0)
        pages.append({"offset": offset, "count": len(results_page), "total_reported": total_api})
        all_paid.extend(results_page)
        offset += limit
        if offset >= total_api or not results_page:
            break

    gmv = sum(o.get("paid_amount", 0) or o.get("total_amount", 0) for o in all_paid)

    return jsonify({
        "date_range": {"from": date_from, "to": date_to},
        "status_totals": results,
        "paid_pagination": pages,
        "paid_fetched": len(all_paid),
        "paid_total_api": total_api,
        "gmv_calculated": round(gmv, 2),
        "sample_order": all_paid[0] if all_paid else None
    })


@app.route("/api/debug-sales2/<user_id>")
def debug_sales2(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    from datetime import date as ddate, timedelta as tdelta
    today = ddate.today()
    date_from = (today - tdelta(days=30)).isoformat()
    date_to   = today.isoformat()

    # Testa paginacao completa de paid
    pages_paid = []
    offset = 0
    limit = 50
    total_paid = 0
    fetched_paid = 0
    while True:
        r = requests.get(
            "https://api.mercadolibre.com/orders/search",
            params={
                "seller": user_id,
                "order.date_created.from": date_from + "T00:00:00.000-00:00",
                "order.date_created.to":   date_to + "T23:59:59.000-00:00",
                "order.status": "paid",
                "limit": limit,
                "offset": offset,
                "sort": "date_asc"
            },
            headers={"Authorization": "Bearer " + token}
        )
        if not r.ok or not r.text:
            pages_paid.append({"offset": offset, "error": r.status_code})
            break
        data = r.json()
        results = data.get("results", [])
        total_paid = data.get("paging", {}).get("total", 0)
        pages_paid.append({"offset": offset, "fetched": len(results), "total_reported": total_paid})
        fetched_paid += len(results)
        offset += limit
        if offset >= total_paid or not results:
            break

    # Testa paginacao de payment_in_process
    r_pip = requests.get(
        "https://api.mercadolibre.com/orders/search",
        params={
            "seller": user_id,
            "order.date_created.from": date_from + "T00:00:00.000-00:00",
            "order.date_created.to":   date_to + "T23:59:59.000-00:00",
            "order.status": "payment_in_process",
            "limit": 1,
            "offset": 0
        },
        headers={"Authorization": "Bearer " + token}
    )
    pip_total = r_pip.json().get("paging", {}).get("total", 0) if r_pip.ok and r_pip.text else 0

    # Testa cancelled com datas corretas
    r_can = requests.get(
        "https://api.mercadolibre.com/orders/search",
        params={
            "seller": user_id,
            "order.date_created.from": date_from + "T00:00:00.000-00:00",
            "order.date_created.to":   date_to + "T23:59:59.000-00:00",
            "order.status": "cancelled",
            "limit": 1,
            "offset": 0
        },
        headers={"Authorization": "Bearer " + token}
    )
    cancelled_total = r_can.json().get("paging", {}).get("total", 0) if r_can.ok and r_can.text else 0

    return jsonify({
        "date_range": {"from": date_from, "to": date_to},
        "paid": {"total_api": total_paid, "fetched": fetched_paid, "pages": pages_paid},
        "payment_in_process": {"total": pip_total},
        "cancelled": {"total": cancelled_total},
        "combined_total": total_paid + pip_total
    })


@app.route("/api/debug-reports/<user_id>")
def debug_reports(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    from datetime import date as ddate, timedelta as tdelta
    today = ddate.today()
    date_from = (today - tdelta(days=30)).isoformat()
    date_to   = today.isoformat()

    results = {}

    # Endpoint 1: data-reporting/seller_performance
    r1 = requests.get(
        "https://api.mercadolibre.com/data-reporting/seller_performance",
        params={"caller.id": user_id, "date_from": date_from, "date_to": date_to},
        headers={"Authorization": "Bearer " + token}
    )
    results["seller_performance"] = {"status": r1.status_code, "response": r1.json() if r1.ok and r1.text else r1.text[:200]}

    # Endpoint 2: seller_performance/search
    r2 = requests.get(
        "https://api.mercadolibre.com/data-reporting/seller_performance/search",
        params={"caller.id": user_id, "date_from": date_from, "date_to": date_to},
        headers={"Authorization": "Bearer " + token}
    )
    results["seller_performance_search"] = {"status": r2.status_code, "response": r2.json() if r2.ok and r2.text else r2.text[:200]}

    # Endpoint 3: users/{id}/orders_summary
    r3 = requests.get(
        "https://api.mercadolibre.com/users/" + user_id + "/orders_summary",
        params={"date_from": date_from, "date_to": date_to},
        headers={"Authorization": "Bearer " + token}
    )
    results["orders_summary"] = {"status": r3.status_code, "response": r3.json() if r3.ok and r3.text else r3.text[:200]}

    # Endpoint 4: billing/integration/invoices
    r4 = requests.get(
        "https://api.mercadolibre.com/users/" + user_id + "/sales",
        params={"date_from": date_from, "date_to": date_to},
        headers={"Authorization": "Bearer " + token}
    )
    results["user_sales"] = {"status": r4.status_code, "response": r4.json() if r4.ok and r4.text else r4.text[:200]}

    # Endpoint 5: seller_performance via meli API
    r5 = requests.get(
        "https://api.mercadolibre.com/users/" + user_id + "/items_visits/time_window",
        params={"last": "30", "unit": "day", "ending": date_to},
        headers={"Authorization": "Bearer " + token}
    )
    results["items_visits_window"] = {"status": r5.status_code, "response": r5.json() if r5.ok and r5.text else r5.text[:200]}

    # Endpoint 6: highlights
    r6 = requests.get(
        "https://api.mercadolibre.com/highlights/MLB/seller/" + user_id,
        headers={"Authorization": "Bearer " + token}
    )
    results["highlights"] = {"status": r6.status_code, "response": r6.json() if r6.ok and r6.text else r6.text[:200]}

    # Endpoint 7: v2 orders com status=all
    r7 = requests.get(
        "https://api.mercadolibre.com/orders/search",
        params={
            "seller": user_id,
            "order.date_created.from": date_from + "T00:00:00.000-00:00",
            "order.date_created.to":   date_to + "T23:59:59.000-00:00",
            "limit": 1,
            "offset": 0
        },
        headers={"Authorization": "Bearer " + token}
    )
    results["orders_no_status_filter"] = {
        "status": r7.status_code,
        "total": r7.json().get("paging", {}).get("total", 0) if r7.ok and r7.text else 0
    }

    return jsonify({
        "user_id": user_id,
        "date_range": {"from": date_from, "to": date_to},
        "results": results
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
