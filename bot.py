import os
import time
import json
import logging
import requests
import hashlib
import hmac
from datetime import datetime
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
POLY_API_KEY = os.environ.get('POLY_API_KEY', '')
POLY_PRIVATE_KEY = os.environ.get('POLY_PRIVATE_KEY', '')
POLY_SECRET = os.environ.get('POLY_SECRET', '')
POLY_PASSPHRASE = os.environ.get('POLY_PASSPHRASE', '')
SCAN_INTERVAL = int(os.environ.get('SCAN_INTERVAL', 300))
MAX_TRADE_AMOUNT = float(os.environ.get('MAX_TRADE_AMOUNT', 10))
MIN_CONFIDENCE = float(os.environ.get('MIN_CONFIDENCE', 55))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

client = Anthropic(api_key=ANTHROPIC_API_KEY)

def get_auth_headers():
    """Build auth headers for CLOB API"""
    timestamp = str(int(time.time()))
    
    if POLY_SECRET and POLY_PASSPHRASE:
        # CLOB auth with secret
        msg = timestamp + "GET" + "/auth/api-key"
        signature = hmac.new(
            POLY_SECRET.encode('utf-8'),
            msg.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return {
            "POLY_ADDRESS": POLY_PRIVATE_KEY,
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": timestamp,
            "POLY_NONCE": "0",
            "POLY_PASSPHRASE": POLY_PASSPHRASE,
            "Content-Type": "application/json"
        }
    else:
        # Simple auth with API key
        return {
            "POLY_ADDRESS": POLY_PRIVATE_KEY,
            "POLY_SIGNATURE": POLY_API_KEY,
            "Content-Type": "application/json"
        }

def get_markets():
    try:
        r = requests.get(
            f"{GAMMA_API}/markets",
            params={"limit": 50, "active": "true", "closed": "false", "order": "volume24hr", "ascending": "false"},
            timeout=10
        )
        if r.status_code == 200:
            markets = r.json()
            logger.info(f"✅ Got {len(markets)} markets")
            return markets
        return []
    except Exception as e:
        logger.error(f"❌ Markets error: {e}")
        return []

def get_best_price(token_id):
    """Get best price from multiple sources"""
    try:
        # Try orderbook
        r = requests.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=10)
        if r.status_code == 200:
            book = r.json()
            asks = book.get('asks', [])
            if asks:
                best = min(asks, key=lambda x: float(x.get('price', 1)))
                price = float(best.get('price', 0))
                if 0.01 <= price <= 0.99:
                    return price

        # Try midpoint
        r2 = requests.get(f"{CLOB_API}/midpoint", params={"token_id": token_id}, timeout=10)
        if r2.status_code == 200:
            price = float(r2.json().get('mid', 0))
            if 0.01 <= price <= 0.99:
                return price

        # Try price endpoint
        r3 = requests.get(f"{CLOB_API}/price", params={"token_id": token_id, "side": "buy"}, timeout=10)
        if r3.status_code == 200:
            price = float(r3.json().get('price', 0))
            if 0.01 <= price <= 0.99:
                return price

        return 0
    except Exception as e:
        logger.error(f"❌ Price error: {e}")
        return 0

def execute_trade(token_id, amount, price):
    """Execute trade using L1 auth (private key signing)"""
    try:
        size = round(amount / price, 4)
        nonce = str(int(time.time()))
        
        order = {
            "token_id": token_id,
            "price": str(round(price, 4)),
            "size": str(size),
            "side": "BUY",
            "type": "FOK",
            "fee_rate_bps": "0",
            "nonce": nonce,
            "expiration": "0"
        }

        headers = get_auth_headers()
        
        logger.info(f"📤 Order: price={price} size={size} amount=${amount}")
        r = requests.post(f"{CLOB_API}/order", headers=headers, json=order, timeout=15)
        
        if r.status_code == 200:
            result = r.json()
            logger.info(f"✅ TRADE EXECUTED! {result}")
            return True
        else:
            logger.error(f"❌ Order failed {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"❌ Execute error: {e}")
        return False

def analyze_market(market):
    try:
        question = market.get('question', '')
        outcomes = market.get('outcomes', '[]')
        if isinstance(outcomes, str):
            try: outcomes = json.loads(outcomes)
            except: outcomes = []

        clob_ids = market.get('clobTokenIds', '[]')
        if isinstance(clob_ids, str):
            try: clob_ids = json.loads(clob_ids)
            except: clob_ids = []

        prices = market.get('outcomePrices', '[]')
        if isinstance(prices, str):
            try: prices = json.loads(prices)
            except: prices = []

        tokens = []
        for i, outcome in enumerate(outcomes):
            token_id = clob_ids[i] if i < len(clob_ids) else None
            market_price = float(prices[i]) if i < len(prices) else 0
            tokens.append({"outcome": outcome, "token_id": token_id, "price": market_price})

        prompt = f"""Expert prediction market trader on Polymarket.

Question: {question}
Volume: ${float(market.get('volume', 0) or 0):,.0f}
End: {market.get('endDate', 'Unknown')}
Tokens: {json.dumps(tokens)}

Is this market mispriced? Should I buy YES or NO?

Respond ONLY with JSON:
{{"decision": "BUY_YES" or "BUY_NO" or "HOLD", "confidence": <0-100>, "token_index": <0 for YES, 1 for NO, null>, "reasoning": "<brief>"}}"""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        if '```' in text:
            text = text.split('```')[1].split('```')[0].replace('json','').strip()

        result = json.loads(text)
        logger.info(f"🧠 {result['decision']} ({result['confidence']}%) - {question[:50]}")

        token_index = result.get('token_index')
        if token_index is not None and token_index < len(tokens):
            result['token_id'] = tokens[token_index]['token_id']
            result['outcome'] = tokens[token_index]['outcome']

        return result
    except Exception as e:
        logger.error(f"❌ AI error: {e}")
        return {"decision": "HOLD", "confidence": 0, "token_id": None}

def run_scan():
    logger.info("=" * 50)
    logger.info(f"🔍 Scan at {datetime.now().strftime('%H:%M:%S')}")
    
    markets = get_markets()
    if not markets:
        return

    executed = 0
    for i, market in enumerate(markets[:10]):
        logger.info(f"\n📊 {i+1}/10: {market.get('question','')[:55]}")

        analysis = analyze_market(market)
        decision = analysis.get('decision', 'HOLD')
        confidence = analysis.get('confidence', 0)
        token_id = analysis.get('token_id')

        if decision != 'HOLD' and confidence >= MIN_CONFIDENCE and token_id:
            price = get_best_price(token_id)
            logger.info(f"💰 Price: {price} | Outcome: {analysis.get('outcome')}")

            if price > 0:
                logger.info(f"🚀 EXECUTING: {decision} at {price} for ${MAX_TRADE_AMOUNT}")
                if execute_trade(token_id, MAX_TRADE_AMOUNT, price):
                    executed += 1
                    logger.info(f"🎉 TRADE #{executed} DONE!")
            else:
                logger.warning(f"⚠️ No price for token")
        else:
            logger.info(f"⏸️ HOLD ({confidence}%)")

        time.sleep(4)

    logger.info(f"\n✅ Done! Executed {executed} | Next in {SCAN_INTERVAL//60}min")

def main():
    logger.info("🚀 Polymarket AI Bot v4 - FINAL!")
    logger.info(f"⚙️ Max=${MAX_TRADE_AMOUNT} | MinConf={MIN_CONFIDENCE}% | Every {SCAN_INTERVAL//60}min")
    
    if not ANTHROPIC_API_KEY:
        logger.error("❌ No ANTHROPIC_API_KEY!")
        return

    logger.info(f"🔑 Auth: {'CLOB+Secret' if POLY_SECRET else 'API Key'}")

    while True:
        try:
            run_scan()
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"❌ Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
