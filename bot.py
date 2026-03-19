import os
import time
import json
import logging
import requests
from datetime import datetime
from anthropic import Anthropic

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Config
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
SCAN_INTERVAL = int(os.environ.get('SCAN_INTERVAL', 1800))  # 30 minutes
MAX_TRADE_AMOUNT = float(os.environ.get('MAX_TRADE_AMOUNT', 10))
MIN_CONFIDENCE = float(os.environ.get('MIN_CONFIDENCE', 70))
POLYMARKET_API = "https://gamma-api.polymarket.com"

client = Anthropic(api_key=ANTHROPIC_API_KEY)

def get_markets():
    """Get top active markets from Polymarket"""
    try:
        response = requests.get(
            f"{POLYMARKET_API}/markets",
            params={
                "limit": 20,
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false"
            },
            timeout=10
        )
        if response.status_code == 200:
            markets = response.json()
            logger.info(f"✅ Got {len(markets)} markets")
            return markets
        else:
            logger.error(f"❌ API Error: {response.status_code}")
            return []
    except Exception as e:
        logger.error(f"❌ Error getting markets: {e}")
        return []

def analyze_market_with_ai(market):
    """Use Claude AI to analyze a market and make a trading decision"""
    try:
        question = market.get('question', '')
        description = market.get('description', '')
        volume = market.get('volume', 0)
        
        # Get current prices
        outcomes = market.get('outcomes', '[]')
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except:
                outcomes = []
        
        prices = market.get('outcomePrices', '[]')
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except:
                prices = []

        # Build market info
        market_info = f"""
Market Question: {question}
Description: {description[:500] if description else 'N/A'}
Volume: ${float(volume or 0):,.0f}
Outcomes: {outcomes}
Current Prices: {prices}
End Date: {market.get('endDate', 'Unknown')}
"""

        prompt = f"""You are an expert prediction market trader analyzing Polymarket.

{market_info}

Analyze this market and decide:
1. Should I BUY YES, BUY NO, or HOLD?
2. What is your confidence level (0-100)?
3. What is your reasoning?

Consider:
- Current prices vs true probability
- Recent news and events
- Market liquidity and volume
- Time until resolution

Respond in this exact JSON format:
{{
  "decision": "BUY_YES" or "BUY_NO" or "HOLD",
  "confidence": <number 0-100>,
  "reasoning": "<brief explanation>",
  "target_outcome": "YES" or "NO" or null
}}"""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        
        result_text = response.content[0].text.strip()
        
        # Parse JSON
        if '```json' in result_text:
            result_text = result_text.split('```json')[1].split('```')[0]
        elif '```' in result_text:
            result_text = result_text.split('```')[1].split('```')[0]
            
        result = json.loads(result_text.strip())
        logger.info(f"🧠 AI Decision for '{question[:50]}': {result['decision']} ({result['confidence']}% confidence)")
        return result
        
    except Exception as e:
        logger.error(f"❌ AI analysis error: {e}")
        return {"decision": "HOLD", "confidence": 0, "reasoning": str(e), "target_outcome": None}

def log_trade_decision(market, analysis):
    """Log trade decision to file"""
    try:
        trade_log = {
            "timestamp": datetime.now().isoformat(),
            "market": market.get('question', ''),
            "decision": analysis.get('decision'),
            "confidence": analysis.get('confidence'),
            "reasoning": analysis.get('reasoning'),
            "max_amount": MAX_TRADE_AMOUNT
        }
        
        with open('trades.json', 'a') as f:
            f.write(json.dumps(trade_log) + '\n')
            
        logger.info(f"📝 Logged: {trade_log['decision']} on '{trade_log['market'][:50]}'")
    except Exception as e:
        logger.error(f"❌ Logging error: {e}")

def run_scan():
    """Main scanning function"""
    logger.info("=" * 60)
    logger.info(f"🔍 Starting market scan at {datetime.now().strftime('%H:%M:%S')}")
    logger.info("=" * 60)
    
    markets = get_markets()
    
    if not markets:
        logger.warning("⚠️ No markets found")
        return
    
    decisions = {"BUY_YES": 0, "BUY_NO": 0, "HOLD": 0}
    
    for i, market in enumerate(markets[:10]):  # Analyze top 10
        question = market.get('question', 'Unknown')
        logger.info(f"\n📊 Analyzing market {i+1}/10: {question[:60]}")
        
        analysis = analyze_market_with_ai(market)
        decision = analysis.get('decision', 'HOLD')
        confidence = analysis.get('confidence', 0)
        
        decisions[decision] = decisions.get(decision, 0) + 1
        
        # Only log trades with high enough confidence
        if decision != 'HOLD' and confidence >= MIN_CONFIDENCE:
            log_trade_decision(market, analysis)
            logger.info(f"✅ TRADE SIGNAL: {decision} with {confidence}% confidence")
            logger.info(f"   Reason: {analysis.get('reasoning', '')[:100]}")
        else:
            logger.info(f"⏸️ HOLD - Confidence too low ({confidence}%) or HOLD decision")
        
        # Wait between API calls
        time.sleep(2)
    
    logger.info("\n" + "=" * 60)
    logger.info(f"📈 Scan Summary: BUY_YES={decisions['BUY_YES']} | BUY_NO={decisions['BUY_NO']} | HOLD={decisions['HOLD']}")
    logger.info(f"⏰ Next scan in {SCAN_INTERVAL//60} minutes")
    logger.info("=" * 60)

def main():
    logger.info("🚀 Polymarket AI Trading Bot Started!")
    logger.info(f"⚙️ Settings: Max Trade=${MAX_TRADE_AMOUNT} | Min Confidence={MIN_CONFIDENCE}% | Scan Every {SCAN_INTERVAL//60}min")
    
    if not ANTHROPIC_API_KEY:
        logger.error("❌ ANTHROPIC_API_KEY not set!")
        return
    
    while True:
        try:
            run_scan()
            logger.info(f"😴 Sleeping {SCAN_INTERVAL//60} minutes...")
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            logger.info("👋 Bot stopped by user")
            break
        except Exception as e:
            logger.error(f"❌ Unexpected error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
