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
    handlers=[logging.StreamHandler()]
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

def get_clob_markets():
    """Get markets directly from CLOB with token IDs"""
    try:
        r = requests.get(f"{CLOB_API}/markets", params={"limit": 50, "active": "true"}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            markets = data.get('data', [])
            logger.info(f"✅ Got {len(markets)} CLOB markets")
            return markets
        logger.error(f"❌ CLOB error: {r.status_code}")
        return []
    except Exception as e:
        logger.error(f"❌ CLOB markets error: {e}")
        return []

def get_market_price(token_id, side="buy"):
    """Get current price for a token"""
    try:
        r = requests.get(f"{CLOB_API}/price", params={"token_id": token_id, "side": side}, timeout=10)
        if r.status_code == 200:
            return float(r.json().get('price', 0))
        return 0
    except:
        return 0

def execute_market_order(token_id, amount, price):
    """Execute a market order on Polymarket"""
    try:
        if not POLY_API_KEY or not POLY_PRIVATE_KEY:
            logger.error("❌ Missing POLY_API_KEY or POLY_PRIVATE_KEY")
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

        logger.info(f"📤 Placing order: token={token_id[:20]}... price={price} size={size}")
        
        r = requests.post(f"{CLOB_API}/order", headers=headers, json=order, timeout=15)
        
        if r.status_code == 200:
            result = r.json()
            logger.info(f"✅ ORDER SUCCESS: {result}")
            return True
        else:
            logger.error(f"❌ Order failed {r.status_code}: {r.text[:200]}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Execute error: {e}")
        return False

def analyze_with_ai(market):
    """Use Claude to analyze a CLOB market"""
    try:
        question = market.get('question', '')
        tokens = market.get('tokens', [])
        
        # Get token info
        token_info = []
        for t in tokens:
            price = get_market_price(t.get('token_id', ''))
            token_info.append({
                "outcome": t.get('outcome', ''),
                "token_id": t.get('token_id', ''),
                "price": price
            })

        prompt = f"""You are an expert prediction market trader on Polymarket.

Question: {question}
End Date: {market.get('end_date_iso', 'Unknown')}
Token Info: {json.dumps(token_info, indent=2)}

Analyze and decide which outcome to buy (if any).
Consider if current prices reflect true probabilities.

Respond ONLY with this exact JSON, no other text:
{{"decision": "BUY" or "HOLD", "outcome": "YES" or "NO" or null, "token_id": "<token_id>" or null, "confidence": <0-100>, "reasoning": "<brief>"}}"""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )

        result_text = response.content[0].text.strip()
        if '```' in result_text:
            result_text = result_text.split('```')[1].split('```')[0].replace('json','').strip()
        
        result = json.loads(result_text)
        logger.info(f"🧠 {result.get('decision')} ({result.get('confidence')}%) - {question[:50]}")
        return result, token_info

    except Exception as e:
        logger.error(f"❌ AI error: {e}")
        return {"decision": "HOLD", "confidence": 0, "token_id": None}, []

def run_scan():
    logger.info("=" * 60)
    logger.info(f"🔍 Scan started at {datetime.now().strftime('%H:%M:%S')}")
    logger.info("=" * 60)

    markets = get_clob_markets()
    if not markets:
        logger.warning("⚠️ No markets")
        return

    # Sort by volume and take top 10
    markets = sorted(markets, key=lambda x: float(x.get('volume', 0) or 0), reverse=True)[:10]
    
    executed = 0
    for i, market in enumerate(markets):
        question = market.get('question', '')[:60]
        logger.info(f"\n📊 {i+1}/10: {question}")

        analysis, token_info = analyze_with_ai(market)
        
        decision = analysis.get('decision', 'HOLD')
        confidence = analysis.get('confidence', 0)
        token_id = analysis.get('token_id')

        if decision == 'BUY' and confidence >= MIN_CONFIDENCE and token_id:
            price = get_market_price(token_id)
            
            if 0.01 <= price <= 0.99:
                logger.info(f"🚀 TRADING: {analysis.get('outcome')} at {price} | Confidence: {confidence}%")
                logger.info(f"   Reason: {analysis.get('reasoning', '')[:100]}")
                
                success = execute_market_order(token_id, MAX_TRADE_AMOUNT, price)
                if success:
                    executed += 1
                    logger.info(f"✅ TRADE #{executed} EXECUTED!")
            else:
                logger.warning(f"⚠️ Bad price: {price}")
        else:
            logger.info(f"⏸️ HOLD - Confidence: {confidence}%")

        time.sleep(4)

    logger.info(f"\n🏁 Done! Executed {executed} trades | Next scan in {SCAN_INTERVAL//60} min")

def main():
    logger.info("🚀 Polymarket AI Bot v2 Started!")
    logger.info(f"⚙️ Max=${MAX_TRADE_AMOUNT} | MinConf={MIN_CONFIDENCE}% | Every {SCAN_INTERVAL//60}min")
    
    if not ANTHROPIC_API_KEY:
        logger.error("❌ No ANTHROPIC_API_KEY!")
        return
    
    if not POLY_API_KEY:
        logger.warning("⚠️ No POLY_API_KEY - will analyze only, no trades!")

    while True:
        try:
            run_scan()
            logger.info(f"😴 Sleeping {SCAN_INTERVAL//60} min...")
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            logger.info("👋 Stopped")
            break
        except Exception as e:
            logger.error(f"❌ Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
