import os
import asyncio
import threading
import requests
import pandas as pd
import yfinance as yf
from flask import Flask
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# --- RENDER PORT DİNLENME SUNUCUSU ---
app = Flask('')

@app.route('/')
def home():
    return "Bot Aktif ve Çalışıyor!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- AYARLAR VE ENVIRONMENT VARIABLE ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FINNHUB_API_KEY = "da0ahvpr01qh1nomg71gda0ahvpr01qh1nomg7m0"

WAITING_STOCK, WAITING_SCORE, WAITING_TOP_N = range(3)

# Global Sonuç Verisi
LATEST_DATA = pd.DataFrame()

# --- VERİ VE ANALİZ MOTORU ---
def get_sp500_tickers():
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        html = requests.get(url, headers=headers, timeout=5).text
        tables = pd.read_html(html)
        return tables[0]['Symbol'].str.replace('.', '-').tolist()
    except Exception as e:
        print(f"S&P 500 Çekilemedi: {e}")
        return []

def get_nasdaq100_tickers():
    url = "https://en.wikipedia.org/wiki/Nasdaq-100"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        html = requests.get(url, headers=headers, timeout=5).text
        tables = pd.read_html(html)
        for table in tables:
            if 'Ticker' in table.columns:
                return table['Ticker'].str.replace('.', '-').tolist()
            elif 'Symbol' in table.columns:
                return table['Symbol'].str.replace('.', '-').tolist()
        return []
    except Exception as e:
        print(f"NASDAQ 100 Çekilemedi: {e}")
        return []

def fetch_finnhub_news_sentiment(ticker):
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        from_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={from_date}&to={today}&token={FINNHUB_API_KEY}"
        res = requests.get(url, timeout=2).json()
        
        if not isinstance(res, list) or len(res) == 0:
            return 0
        
        pos_keywords = ['growth', 'buy', 'upgrade', 'bullish', 'record', 'profit', 'surge', 'beats']
        neg_keywords = ['fall', 'sell', 'downgrade', 'bearish', 'drop', 'loss', 'miss', 'decline']
        
        score = 0
        for article in res[:5]: # Makale taraması RAM tasarrufu için 5'e düşürüldü
            headline = article.get('headline', '').lower()
            if any(w in headline for w in pos_keywords):
                score += 2
            if any(w in headline for w in neg_keywords):
                score -= 2
        return score
    except Exception:
        return 0

def fetch_single_stock_data(ticker):
    try:
        t = yf.Ticker(ticker)
        info = t.info
        
        inst_ownership = info.get('heldPercentInstitutions', 0)
        if inst_ownership is None: inst_ownership = 0
        inst_ownership *= 100
        
        current_price = info.get('currentPrice') or info.get('regularMarketPrice') or 0
        target_price = info.get('targetMeanPrice') or 0
        
        upside = 0
        if current_price > 0 and target_price > 0:
            upside = ((target_price - current_price) / current_price) * 100
            
        earnings_growth = info.get('earningsGrowth', 0)
        if earnings_growth is None: earnings_growth = 0
        earnings_growth *= 100
        
        news_score = fetch_finnhub_news_sentiment(ticker)
        
        data = {
            "ticker": ticker,
            "name": info.get('shortName', ticker),
            "inst_ownership": inst_ownership,
            "upside_potential": upside,
            "earnings_growth": earnings_growth,
            "current_price": current_price,
            "target_price": target_price
        }
        
        score = calculate_score(data, news_score)
        data["growth_score"] = score
        return data
    except Exception:
        return None

def calculate_score(data, news):
    score = 0
    if data["inst_ownership"] >= 70: score += 25
    elif data["inst_ownership"] >= 50: score += 15
    elif data["inst_ownership"] < 0: score -= 10
    
    if data["upside_potential"] >= 20: score += 35
    elif data["upside_potential"] >= 10: score += 20
    elif data["upside_potential"] < 0: score -= 10
    
    if data["earnings_growth"] >= 15: score += 15
    elif data["earnings_growth"] < 0: score -= 10
    
    score += news
    return max(0, min(100, score))

def run_full_scan():
    sp500 = set(get_sp500_tickers())
    nasdaq = set(get_nasdaq100_tickers())
    tickers = sorted(list(sp500.union(nasdaq)))
    results = []
    
    # RAM çökmesini önlemek için max_workers 12 seviyesine çekildi
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(fetch_single_stock_data, t): t for t in tickers}
        for future in as_completed(futures):
            res = future.result()
            if res:
                results.append(res)
                
    df = pd.DataFrame(results).sort_values(by="growth_score", ascending=False).reset_index(drop=True)
    return df

# --- TELEGRAM BOT ARAYÜZÜ VE BUTONLAR ---
def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("🚀 Yeniden Borsa Taraması Yap", callback_data='rescan')],
        [InlineKeyboardButton("🔝 En Yüksek N Hisse", callback_data='top_n'), InlineKeyboardButton("🔍 Hisse Ara", callback_data='search_stock')],
        [InlineKeyboardButton("🔥 Minimum Skor Filtresi", callback_data='filter_score'), InlineKeyboardButton("📁 CSV Raporu İndir", callback_data='download_csv')]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🚀 *CELAL HOCA BORSA POTANSİYEL BOTU*\n\n"
        "S&P 500 ve NASDAQ 100 hisselerinin kurumsal sahiplik, analist "
        "hedef fiyatları ve haber duygularını analiz eder.\n\n"
        "Lütfen aşağıdaki menüden yapmak istediğiniz işlemi seçin:"
    )
    if update.message:
        await update.message.reply_text(welcome_text, parse_mode='Markdown', reply_markup=main_menu_keyboard())
    elif update.callback_query:
        await update.callback_query.message.reply_text(welcome_text, parse_mode='Markdown', reply_markup=main_menu_keyboard())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LATEST_DATA
    query = update.callback_query
    await query.answer()

    if query.data == 'rescan':
        await query.message.reply_text("⚡ Taraması Başlatıldı...\nLütfen 1-2 dakika bekleyin.")
        
        loop = asyncio.get_running_loop()
        LATEST_DATA = await loop.run_in_executor(None, run_full_scan)
        
        top10 = LATEST_DATA.head(10)
        msg = "🔥 *EN YÜKSEK POTANSİYELLİ İLK 10 HİSSE*\n\n"
        for idx, row in top10.iterrows():
            msg += f"*{idx+1}. {row['ticker']}* - {row['name']}\n"
            msg += f"📊 Skor: `{row['growth_score']}/100` | Kurumsal: `%{row['inst_ownership']:.1f}` | Hedef Artış: `%{row['upside_potential']:.1f}`\n\n"
        
        await query.message.reply_text(msg, parse_mode='Markdown', reply_markup=main_menu_keyboard())

    elif query.data == 'search_stock':
        await query.message.reply_text("🔎 Lütfen analiz etmek istediğiniz hisse sembolünü yazın (Örn: `NVDA`, `AAPL`, `TSLA`):", parse_mode='Markdown')
        return WAITING_STOCK

    elif query.data == 'filter_score':
        await query.message.reply_text("🎯 Minimum kaç puan üzerindeki hisseleri görmek istersiniz? (Örn: `70`):")
        return WAITING_SCORE

    elif query.data == 'top_n':
        await query.message.reply_text("🔝 En yüksek skorlu ilk kaç hisseyi listelemek istersiniz? (Örn: `15`):")
        return WAITING_TOP_N

    elif query.data == 'download_csv':
        if LATEST_DATA.empty:
            await query.message.reply_text("⚠️ Henüz veri taranmadı. Lütfen önce 'Yeniden Borsa Taraması Yap' butonuna basın.")
        else:
            filename = "borsa_analiz_raporu.csv"
            LATEST_DATA.to_csv(filename, index=False)
            with open(filename, 'rb') as doc:
                await query.message.reply_document(document=doc, caption="📊 En güncel borsa analiz raporu CSV formatında ektedir.")

async def process_stock_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ticker = update.message.text.upper().strip()
    await update.message.reply_text(f"🔍 `{ticker}` analizi yapılıyor...", parse_mode='Markdown')
    
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, fetch_single_stock_data, ticker)
    
    if data:
        msg = (
            f"📌 *HİSSE ANALİZ RAPORU: {data['ticker']}*\n"
            f"🏢 Şirket: {data['name']}\n\n"
            f"⭐ *Büyüme/Potansiyel Skoru:* `{data['growth_score']}/100`\n"
            f"🏛️ *Kurumsal Sahiplik:* `%{data['inst_ownership']:.1f}`\n"
            f"🎯 *Analist Hedef Fiyat Artışı:* `%{data['upside_potential']:.1f}`\n"
            f"📈 *Çeyreklik Kazanç Büyümesi:* `%{data['earnings_growth']:.1f}`\n"
            f"💵 *Mevcut Fiyat:* `${data['current_price']}` | *Hedef:* `${data['target_price']}`"
        )
    else:
        msg = f"❌ `{ticker}` sembolüne ait veri bulunamadı veya geçersiz."
        
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=main_menu_keyboard())
    return ConversationHandler.END

async def process_score_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LATEST_DATA
    if LATEST_DATA.empty:
        await update.message.reply_text("⚠️ Önce tarama yapmalısınız.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
        
    try:
        min_score = float(update.message.text.strip())
        filtered = LATEST_DATA[LATEST_DATA['growth_score'] >= min_score]
        
        if filtered.empty:
            await update.message.reply_text(f"❌ Skoru {min_score} üzerinde hisse bulunamadı.", reply_markup=main_menu_keyboard())
        else:
            msg = f"🔥 *SKORU {min_score}+ ÜZERİ HİSSELER ({len(filtered)} Adet)*\n\n"
            for idx, row in filtered.head(15).iterrows():
                msg += f"• *{row['ticker']}*: Skor `{row['growth_score']}` | Yükseliş Potansiyeli: `%{row['upside_potential']:.1f}`\n"
            await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=main_menu_keyboard())
    except ValueError:
        await update.message.reply_text("⚠️ Geçersiz sayı girdiniz.")
        
    return ConversationHandler.END

async def process_top_n(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LATEST_DATA
    if LATEST_DATA.empty:
        await update.message.reply_text("⚠️ Önce tarama yapmalısınız.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
        
    try:
        n = int(update.message.text.strip())
        top_n = LATEST_DATA.head(n)
        
        msg = f"🔝 *EN YÜKSEK SKORLU İLK {n} HİSSE*\n\n"
        for idx, row in top_n.iterrows():
            msg += f"*{idx+1}. {row['ticker']}* - Skor: `{row['growth_score']}` | Hedef: `%{row['upside_potential']:.1f}`\n"
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=main_menu_keyboard())
    except ValueError:
        await update.message.reply_text("⚠️ Geçersiz sayı girdiniz.")
        
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("İşlem iptal edildi.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# --- BOT BAŞLATICI ---
def main():
    threading.Thread(target=run_web, daemon=True).start()

    if not TELEGRAM_BOT_TOKEN:
        print("HATA: TELEGRAM_BOT_TOKEN bulunamadı!")
        return

    app_bot = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler)],
        states={
            WAITING_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_stock_search)],
            WAITING_SCORE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_score_filter)],
            WAITING_TOP_N: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_top_n)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(conv_handler)

    print("🤖 Telegram Botu Başlatıldı...")
    app_bot.run_polling()

if __name__ == '__main__':
    main()
