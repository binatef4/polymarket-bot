import os
import time
import json
import logging
import requests
from datetime import datetime
from anthropic import Anthropic

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
POLY_API_KEY = os.environ.get('POLY_API_KEY', '')
POLY_PRIVATE_KEY = os.environ.get('POLY_PRIVATE_KEY', '')
SCAN_INTERVAL = int(os.environ.get('SCAN_INTERVAL', 300))
MAX_TRADE_AMOUNT = float(os.environ.get('MAX_TRADE_AMOUNT', 10))
MIN_CONFIDENCE = float(os.environ.get('MIN_CONFIDENCE', 55))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

client = Anthropic(api_key=ANTHROPIC_API_KEY)

def get_markets():
    try:
        r = requests.get(f"{GAMMA_API}/markets", params={"limit": 20, "active": "true", "closed": "false", "order": "volume24hr", "ascending": "false"}, timeout=10)
        if r.status_code == 200:
            markets = r.json()
            logger.info(f"✅ Got {len(markets)} markets")
            return markets
        return []
    except Exception as e:
        logger.error(f"❌ Markets error: {e}")
        return []

def execute_trade(token_id, outcome, amount):
    try:
        if not POLY_API_KEY or not POLY_PRIVATE_KEY:
            logger.error("❌ Missing API keys")
            return False

        headers = {"POLY_ADDRESS": POLY_PRIVATE_KEY, "POLY_SIGNATURE": POLY_API_KEY, "Content-Type": "application/json"}

        price_r = requests.get(f"{CLOB_API}/price", params={"token_id": token_id, "side": "buy"}, timeout=10)
        if price_r.status_code != 200:
            logger.error(f"❌ Price error: {price_r.status_code}")
            return False

        price = float(price_r.json().get('price', 0))
        if price <= 0 or price >= 1:
            logger.warning(f"⚠️ Bad price: {price}")
            return False

        size = round(amount / price, 2)
        order = {"token_id": token_id, "price": price, "size": size, "side": "BUY", "type": "FOK"}

        logger.info(f"📤 Order: {outcome} at {price} size {size}")
        r = requests.post(f"{CLOB_API}/order", headers=headers, json=order, timeout=15)

        if r.status_code == 200:
            logger.info(f"✅ TRADE EXECUTED: {r.json()}")
            return True
        else:
            logger.error(f"❌ Order failed: {r.status_code} - {r.text}")
            return False
    except Exception as e:
        logger.error(f"❌ Trade error: {e}")
        return False

def analyze_market_with_ai(market):
    try:
        question = market.get('question', '')
        outcomes = market.get('outcomes', '[]')
        if isinstance(outcomes, str):
            try: outcomes = json.loads(outcomes)
            except: outcomes = []
        prices = market.get('outcomePrices', '[]')
        if isinstance(prices, str):
            try: prices = json.loads(prices)
            except: prices = []

        prompt = f"""You are an expert prediction market trader.

Market: {question}
Volume: ${float(market.get('volume', 0) or 0):,.0f}
Outcomes: {outcomes}
Prices: {prices}
End Date: {market.get('endDate', 'Unknown')}

Respond ONLY with valid JSON, no other text:
{{"decision": "BUY_YES" or "BUY_NO" or "HOLD", "confidence": <0-100>, "reasoning": "<brief>", "target_outcome": "YES" or "NO" or null}}"""

        response = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=300, messages=[{"role": "user", "content": prompt}])
        result_text = response.content[0].text.strip()
        if '```' in result_text:
            result_text = result_text.split('```')[1].split('```')[0].replace('json', '').strip()
        result = json.loads(result_text)
        logger.info(f"🧠 {result['decision']} ({result['confidence']}%) for '{question[:50]}'")
        return result
    except Exception as e:
        logger.error(f"❌ AI error: {e}")
        return {"decision": "HOLD", "confidence": 0, "reasoning": str(e), "target_outcome": None}

def find_token_id(market, outcome):
    try:
        clob_token_ids = market.get('clobTokenIds', '[]')
        if isinstance(clob_token_ids, str):
            clob_token_ids = json.loads(clob_token_ids)
        outcomes = market.get('outcomes', '[]')
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        for i, o in enumerate(outcomes):
            if o.upper() == outcome.upper() and i < len(clob_token_ids):
                return clob_token_ids[i]
    except Exception as e:
        logger.error(f"❌ Token error: {e}")
    return None

def run_scan():
    logger.info("=" * 60)
    logger.info(f"🔍 Scan at {datetime.now().strftime('%H:%M:%S')}")
    logger.info("=" * 60)

    markets = get_markets()
    if not markets:
        return

    trades = 0
    decisions = {"BUY_YES": 0, "BUY_NO": 0, "HOLD": 0}

    for i, market in enumerate(markets[:10]):
        logger.info(f"\n📊 {i+1}/10: {market.get('question', '')[:60]}")
        analysis = analyze_market_with_ai(market)
        decision = analysis.get('decision', 'HOLD')
        confidence = analysis.get('confidence', 0)
        decisions[decision] = decisions.get(decision, 0) + 1

        if decision != 'HOLD' and confidence >= MIN_CONFIDENCE:
            outcome = "YES" if decision == "BUY_YES" else "NO"
            token_id = find_token_id(market, outcome)
            if token_id:
                logger.info(f"🚀 Trading: {decision} ({confidence}%) | Amount: ${MAX_TRADE_AMOUNT}")
                if execute_trade(token_id, outcome, MAX_TRADE_AMOUNT):
                    trades += 1
            else:
                logger.warning(f"⚠️ No token ID for {outcome}")
        else:
            logger.info(f"⏸️ HOLD ({confidence}%)")
        time.sleep(3)

    logger.info(f"\n📈 Summary: {decisions} | Executed: {trades}")
    logger.info(f"⏰ Next scan in {SCAN_INTERVAL//60} min")

def main():
    logger.info("🚀 Polymarket AI Bot Started!")
    logger.info(f"⚙️ Max=${MAX_TRADE_AMOUNT} | MinConf={MIN_CONFIDENCE}% | Interval={SCAN_INTERVAL//60}min")
    if not ANTHROPIC_API_KEY:
        logger.error("❌ No ANTHROPIC_API_KEY!")
        return
    while True:
        try:
            run_scan()
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"❌ {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
