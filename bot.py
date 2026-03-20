import os
import time
import json
import logging
import requests
from datetime import datetime
from anthropic import Anthropic
from eth_account import Account
from eth_account.messages import encode_defunct
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', handlers=[logging.StreamHandler()])
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
        r = requests.get(f"{GAMMA_API}/markets", params={"limit": 50, "active": "true", "closed": "false", "order": "volume24hr", "ascending": "false"}, timeout=10)
        if r.status_code == 200:
            markets = r.json()
            logger.info(f"✅ Got {len(markets)} markets")
            return markets
        return []
    except Exception as e:
        logger.error(f"❌ Markets error: {e}")
        return []

def get_best_price(token_id):
    """Get best price from orderbook"""
    try:
        # Try orderbook first
        r = requests.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=10)
        if r.status_code == 200:
            book = r.json()
            asks = book.get('asks', [])
            if asks:
                # Get best ask price
                best_ask = min(asks, key=lambda x: float(x.get('price', 1)))
                price = float(best_ask.get('price', 0))
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
    try:
        if not POLY_API_KEY or not POLY_PRIVATE_KEY:
            logger.error("❌ Missing API keys!")
            return False

        size = round(amount / price, 4)
        headers = {
            "Content-Type": "application/json",
            "POLY_ADDRESS": POLY_PRIVATE_KEY,
            "POLY_SIGNATURE": POLY_API_KEY,
        }
        order = {
            "token_id": token_id,
            "price": str(round(price, 4)),
            "size": str(size),
            "side": "BUY",
            "type": "FOK",
            "fee_rate_bps": "0",
            "nonce": str(int(time.time())),
            "expiration": "0"
        }
        logger.info(f"📤 Order: price={price} size={size} amount=${amount}")
        r = requests.post(f"{CLOB_API}/order", headers=headers, json=order, timeout=15)
        if r.status_code == 200:
            logger.info(f"✅ TRADE EXECUTED! {r.json()}")
            return True
        else:
            logger.error(f"❌ Order failed {r.status_code}: {r.text[:300]}")
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

        # Build token info with prices
        tokens = []
        for i, outcome in enumerate(outcomes):
            token_id = clob_ids[i] if i < len(clob_ids) else None
            market_price = float(prices[i]) if i < len(prices) else 0
            tokens.append({"outcome": outcome, "token_id": token_id, "market_price": market_price})

        prompt = f"""Expert prediction market trader analyzing Polymarket.

Question: {question}
Volume: ${float(market.get('volume', 0) or 0):,.0f}
End: {market.get('endDate', 'Unknown')}
Tokens: {json.dumps(tokens)}

Should I buy YES or NO? Is the price mispriced vs true probability?

Respond ONLY with JSON:
{{"decision": "BUY_YES" or "BUY_NO" or "HOLD", "confidence": <0-100>, "token_index": <0 for YES, 1 for NO, null for HOLD>, "reasoning": "<brief>"}}"""

        response = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=300, messages=[{"role": "user", "content": prompt}])
        text = response.content[0].text.strip()
        if '```' in text:
            text = text.split('```')[1].split('```')[0].replace('json','').strip()
        
        result = json.loads(text)
        logger.info(f"🧠 {result['decision']} ({result['confidence']}%) - {question[:50]}")
        
        # Get token_id and price
        token_index = result.get('token_index')
        if token_index is not None and token_index < len(tokens):
            token_id = tokens[token_index]['token_id']
            result['token_id'] = token_id
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
            else:
                logger.warning(f"⚠️ No price available for token")
        else:
            logger.info(f"⏸️ HOLD ({confidence}%)")
        
        time.sleep(4)

    logger.info(f"\n✅ Done! Executed {executed} trades | Next in {SCAN_INTERVAL//60}min")

def main():
    logger.info("🚀 Polymarket AI Bot v3 - FIXED!")
    logger.info(f"⚙️ Max=${MAX_TRADE_AMOUNT} | MinConf={MIN_CONFIDENCE}% | Every {SCAN_INTERVAL//60}min")
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
            logger.error(f"❌ Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
import os
import time
import json
import logging
import requests
from datetime import datetime
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', handlers=[logging.StreamHandler()])
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
        r = requests.get(f"{GAMMA_API}/markets", params={"limit": 50, "active": "true", "closed": "false", "order": "volume24hr", "ascending": "false"}, timeout=10)
        if r.status_code == 200:
            markets = r.json()
            logger.info(f"✅ Got {len(markets)} markets")
            return markets
        return []
    except Exception as e:
        logger.error(f"❌ Markets error: {e}")
        return []

def get_best_price(token_id):
    """Get best price from orderbook"""
    try:
        # Try orderbook first
        r = requests.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=10)
        if r.status_code == 200:
            book = r.json()
            asks = book.get('asks', [])
            if asks:
                # Get best ask price
                best_ask = min(asks, key=lambda x: float(x.get('price', 1)))
                price = float(best_ask.get('price', 0))
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
    try:
        if not POLY_API_KEY or not POLY_PRIVATE_KEY:
            logger.error("❌ Missing API keys!")
            return False

        size = round(amount / price, 4)
account = Account.from_key(POLY_PRIVATE_KEY)

headers = {
    "Content-Type": "application/json",
    "POLY_ADDRESS": account.address,
}
        }
        order = {
            "token_id": token_id,
            "price": str(round(price, 4)),
            "size": str(size),
            "side": "BUY",
            "type": "FOK",
            "fee_rate_bps": "0",
            "nonce": str(int(time.time())),
            "expiration": "0"
        }
        logger.info(f"📤 Order: price={price} size={size} amount=${amount}")
message = json.dumps(order, separators=(',', ':'))

message_encoded = encode_defunct(text=message)
signed = account.sign_message(message_encoded)
headers["POLY_SIGNATURE"] = signed.signature.hex()
        r = requests.post(f"{CLOB_API}/order", headers=headers, json=order, timeout=15)
        if r.status_code == 200:
            logger.info(f"✅ TRADE EXECUTED! {r.json()}")
            return True
        else:
            logger.error(f"❌ Order failed {r.status_code}: {r.text[:300]}")
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

        # Build token info with prices
        tokens = []
        for i, outcome in enumerate(outcomes):
            token_id = clob_ids[i] if i < len(clob_ids) else None
            market_price = float(prices[i]) if i < len(prices) else 0
            tokens.append({"outcome": outcome, "token_id": token_id, "market_price": market_price})

        prompt = f"""Expert prediction market trader analyzing Polymarket.

Question: {question}
Volume: ${float(market.get('volume', 0) or 0):,.0f}
End: {market.get('endDate', 'Unknown')}
Tokens: {json.dumps(tokens)}

Should I buy YES or NO? Is the price mispriced vs true probability?

Respond ONLY with JSON:
{{"decision": "BUY_YES" or "BUY_NO" or "HOLD", "confidence": <0-100>, "token_index": <0 for YES, 1 for NO, null for HOLD>, "reasoning": "<brief>"}}"""

        response = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=300, messages=[{"role": "user", "content": prompt}])
        text = response.content[0].text.strip()
        if '```' in text:
            text = text.split('```')[1].split('```')[0].replace('json','').strip()
        
        result = json.loads(text)
        logger.info(f"🧠 {result['decision']} ({result['confidence']}%) - {question[:50]}")
        
        # Get token_id and price
        token_index = result.get('token_index')
        if token_index is not None and token_index < len(tokens):
            token_id = tokens[token_index]['token_id']
            result['token_id'] = token_id
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
            else:
                logger.warning(f"⚠️ No price available for token")
        else:
            logger.info(f"⏸️ HOLD ({confidence}%)")
        
        time.sleep(4)

    logger.info(f"\n✅ Done! Executed {executed} trades | Next in {SCAN_INTERVAL//60}min")

def main():
    logger.info("🚀 Polymarket AI Bot v3 - FIXED!")
    logger.info(f"⚙️ Max=${MAX_TRADE_AMOUNT} | MinConf={MIN_CONFIDENCE}% | Every {SCAN_INTERVAL//60}min")
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
            logger.error(f"❌ Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
