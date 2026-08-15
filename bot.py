import os
import io
import time
import asyncio
import pandas as pd
import requests
import yfinance as yf
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# --- AYARLAR ---
TELEGRAM_BOT_TOKEN = "BURAYA_BOT_TOKEN_YAZIN"
FINNHUB_API_KEY = "da0ahvpr01qh1nomg7lgda0ahvpr01qh1nomg7m0"

WAITING_STOCK, WAITING_SCORE, WAITING_TOP_N = range(3)

# Global Sonuç Verisi
LATEST_DATA = pd.DataFrame()

# --- VERİ VE ANALİZ MOTORU ---
def get_sp500_tickers():
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers)
        tables = pd.read_html(io.StringIO(res.text))
        return tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
    except Exception:
        return []

def get_nasdaq100_tickers():
    url = "https://en.wikipedia.org/wiki/Nasdaq-100"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers)
        tables = pd.read_html(io.StringIO(res.text))
        for table in tables:
            if "Ticker" in table.columns:
                return table["Ticker"].str.replace(".", "-", regex=False).tolist()
            elif "Symbol" in table.columns:
                return table["Symbol"].str.replace(".", "-", regex=False).tolist()
        return []
    except Exception:
        return []

def fetch_single_stock_data(ticker):
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        inst_ownership = info.get("heldPercentInstitutions", 0)
        current_price = info.get("currentPrice", info.get("previousClose", 0))
        target_price = info.get("targetMeanPrice", 0)

        upside_potential = 0
        if current_price and target_price and current_price > 0:
            upside_potential = ((target_price - current_price) / current_price) * 100

        earnings_growth = info.get("earningsGrowth", 0)

        data = {
            "symbol": ticker,
            "name": info.get("shortName", ticker),
            "current_price": current_price,
            "target_price": target_price,
            "upside_potential": round(upside_potential, 2),
            "inst_ownership": round(inst_ownership * 100, 2) if inst_ownership else 0,
            "earnings_growth": round(earnings_growth * 100, 2) if earnings_growth else 0
        }

        news_score = fetch_stock_news_sentiment(ticker)
        growth_score = calculate_growth_score(data, news_score)
        data["growth_score"] = growth_score
        data["news_score"] = news_score

        return data
    except Exception:
        return None

def fetch_stock_news_sentiment(ticker):
    if not FINNHUB_API_KEY:
        return 0
    today_dt = datetime.now()
    today = today_dt.strftime("%Y-%m-%d")
    month_ago = (today_dt - timedelta(days=30)).strftime("%Y-%m-%d")
    url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={month_ago}&to={today}&token={FINNHUB_API_KEY}"
    try:
        res = requests.get(url, timeout=3)
        news_list = res.json()
        if not news_list or not isinstance(news_list, list):
            return 0
        pos = ["growth", "buy", "profit", "bull", "upgrade", "record", "beats", "high", "partner", "expand"]
        neg = ["loss", "sell", "drop", "bear", "downgrade", "miss", "lawsuit", "risk", "investigation", "cut"]
        score = 0
        for news in news_list[:15]:
            headline = news.get("headline", "").lower()
            if any(w in headline for w in pos): score += 2
            if any(w in headline for w in neg): score -= 2
        return max(-15, min(15, score))
    except Exception:
        return 0

def calculate_growth_score(data, news):
    score = 50
    if data["inst_ownership"] >= 80: score += 15
    elif data["inst_ownership"] >= 65: score += 10
    
    if data["upside_potential"] >= 20: score += 20
    elif data["upside_potential"] >= 10: score += 10
    elif data["upside_potential"] < 0: score -= 10

    if data["earnings_growth"] >= 15: score += 15
    elif data["earnings_growth"] < 0: score -= 10

    score += news
    return max(0, min(100, score))

def run_full_scan():
    tickers = sorted(list(set(get_sp500_tickers().union(set(get_nasdaq100_tickers())))))
    results = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(fetch_single_stock_data, t): t for t in tickers}
        for future in as_completed(futures):
            res = future.result()
            if res: results.append(res)
    df = pd.DataFrame(results).sort_values(by="growth_score", ascending=False).reset_index(drop=True)
    return df

# --- TELEGRAM BOT ARAYÜZÜ VE BUTONLAR ---
def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("🚀 Yeniden Borsa Taraması Yap", callback_data="rescan")],
        [InlineKeyboardButton("🎯 En Yüksek N Hisse", callback_data="top_n"), InlineKeyboardButton("🔍 Hisse Ara", callback_data="search_stock")],
        [InlineKeyboardButton("🔥 Minimum Skor Filtresi", callback_data="filter_score"), InlineKeyboardButton("📁 CSV Raporu İndir", callback_data="download_csv")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🚀 *CELAL HOCA BORSA POTANSİYEL BOTU*\n\n"
        "S&P 500 ve NASDAQ 100 hisselerinin kurumsal sahiplik, analist hedef fiyatları ve haber duygularını analiz eder.\n\n"
        "Lütfen aşağıdaki menüden yapmak istediğiniz işlemi seçin:"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())

async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LATEST_DATA
    query = update.callback_query
    await query.answer()

    if query.data == "rescan":
        await query.edit_message_text("⚡ *S&P 500 + NASDAQ 100 Taraması Başlatıldı...*\nBu işlem yaklaşık 30-40 saniye sürer.", parse_mode="Markdown")
        loop = asyncio.get_running_loop()
        LATEST_DATA = await loop.run_in_executor(None, run_full_scan)
        
        await query.message.reply_text(
            f"✅ *Tarama Tamamlandı!* Toplam {len(LATEST_DATA)} hisse analiz edildi.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )

    elif query.data == "download_csv":
        if LATEST_DATA.empty:
            await query.message.reply_text("⚠️ Önce tarama yapmalısınız!", reply_markup=main_menu_keyboard())
            return
        csv_path = "sp500_nasdaq_analiz.csv"
        LATEST_DATA.to_csv(csv_path, index=False, encoding="utf-8-sig")
        with open(csv_path, "rb") as f:
            await query.message.reply_document(document=f, filename=csv_path, caption="📊 Anlık Analiz Raporu")

    elif query.data == "search_stock":
        await query.message.reply_text("🔍 Lütfen detayını görmek istediğiniz *Hisse Kodunu* yazın (Örn: NVDA, AAPL):", parse_mode="Markdown")
        return WAITING_STOCK

    elif query.data == "filter_score":
        await query.message.reply_text("🎯 Görmek istediğiniz *Minimum Skoru* girin (Örn: 80):", parse_mode="Markdown")
        return WAITING_SCORE

    elif query.data == "top_n":
        await query.message.reply_text("📊 Kaç hisse listelemek istersiniz? (Örn: 15):", parse_mode="Markdown")
        return WAITING_TOP_N

async def process_stock_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LATEST_DATA
    symbol = update.message.text.strip().upper()
    if LATEST_DATA.empty:
        await update.message.reply_text("⚠️ Henüz veri yok. Önce tarama yapın.")
        return ConversationHandler.END

    row = LATEST_DATA[LATEST_DATA["symbol"] == symbol]
    if row.empty:
        await update.message.reply_text(f"❌ {symbol} bulunamadı.")
    else:
        r = row.iloc[0]
        msg = (
            f"📌 *{r['symbol']} - {r['name']}*\n"
            f"• **Potansiyel Skoru:** `{r['growth_score']}/100`\n"
            f"• **Hedef Fiyat Primi:** `%{r['upside_potential']}`\n"
            f"• **Kurumsal Sahiplik:** `%{r['inst_ownership']}`\n"
            f"• **Kâr Büyüme Beklentisi:** `%{r['earnings_growth']}`\n"
            f"• **Son 30 Gün Haber Skoru:** `{r['news_score']}`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

async def process_score_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LATEST_DATA
    score_input = update.message.text.strip()
    min_score = int(score_input) if score_input.isdigit() else 80
    filtered = LATEST_DATA[LATEST_DATA["growth_score"] >= min_score]
    
    msg = f"🔥 *{min_score}+ Skora Sahip Hisseler ({len(filtered)} Adet):*\n\n"
    for _, r in filtered.head(15).iterrows():
        msg += f"• *{r['symbol']}* | Skor: `{r['growth_score']}` | Prim: `%{r['upside_potential']}`\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

async def process_top_n(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LATEST_DATA
    n_input = update.message.text.strip()
    n = int(n_input) if n_input.isdigit() else 10
    
    msg = f"🎯 *En Yüksek Potansiyelli İlk {n} Hisse:*\n\n"
    for _, r in LATEST_DATA.head(n).iterrows():
        msg += f"• *{r['symbol']}* | Skor: `{r['growth_score']}` | Prim: `%{r['upside_potential']}`\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return ConversationHandler.END

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_click_handler)],
        states={
            WAITING_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_stock_search)],
            WAITING_SCORE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_score_filter)],
            WAITING_TOP_N: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_top_n)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(conv_handler)
    
    print("🤖 Telegram Botu Başlatıldı...")
    app.run_polling()

if __name__ == "__main__":
    main()