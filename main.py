import gspread
from google.oauth2.service_account import Credentials
import requests
from datetime import datetime, timedelta
import time
import re
import math
from dotenv import load_dotenv
import os

# ============================================
# КОНФИГУРАЦИЯ
# ============================================

load_dotenv()


SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE")
DEPOSIT_RATE = 0.10  # 10%

# Чёрный список тикеров (исключаем из проверки)
BLACKLIST = [
    "RNFT",   # обычка РуссНефти — не платит дивиденды
    "RNFTP",  # префы РуссНефти — не торгуются в открытом доступе
]

# Ручные данные для тикеров, которые не парсятся автоматически
MANUAL_DATA = {
    "URSB": {
        "name": "Банк Уралсиб",
        "price": 0.1515,
        "lot_size": 10000,
        "dividends": [{
            'value': 0.01999252,
            'declared_date': '2026-07-06',
            'record_date': '2026-07-06',
            'payment_date': '2026-07-06'
        }]
    }
}

# ============================================
# 1. РАБОТА С GOOGLE SHEETS
# ============================================

def get_google_sheets_client():
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    return client

def clear_sheet(worksheet):
    try:
        worksheet.batch_clear(["A2:M1000"])
    except Exception as e:
        print(f"Ошибка очистки: {e}")

def write_to_sheet(worksheet, data):
    if not data:
        print("⚠️ Нет данных для записи")
        return

    clear_sheet(worksheet)

    rows = []
    for stock in data:
        price = stock.get('price', 0)
        lot_size = stock.get('lot_size', 1)
        dividend = stock.get('dividend_amount', 0)

        if price <= 0 or dividend <= 0:
            shares_for_1000 = 0
            lots_for_1000 = 0
            cost_for_1000 = 0
        else:
            shares_exact = 1000 / dividend
            lots_for_1000 = math.ceil(shares_exact / lot_size)
            shares_for_1000 = lots_for_1000 * lot_size
            cost_for_1000 = shares_for_1000 * price

        row = [
            stock.get('ticker', ''),
            stock.get('name', ''),
            price,
            lot_size,
            price * lot_size,
            dividend,
            dividend * lot_size,
            f"{stock.get('current_yield', 0):.2%}",
            shares_for_1000,
            lots_for_1000,
            round(cost_for_1000, 2),
            stock.get('record_date', ''),
            stock.get('period', ''),
            stock.get('dividend_years', '')
        ]
        rows.append(row)

    if rows:
        worksheet.update(range_name="A2", values=rows)
        print(f"✅ Записано {len(rows)} выплат в таблицу")

# ============================================
# 2. РАБОТА С API МОСБИРЖИ
# ============================================

class MoexClient:
    BASE_URL = "https://iss.moex.com/iss"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

    def get_last_price(self, ticker):
        url = f"{self.BASE_URL}/engines/stock/markets/shares/boards/TQBR/securities/{ticker}.json"
        params = {
            'iss.meta': 'off',
            'iss.only': 'marketdata',
            'marketdata.columns': 'LAST'
        }
        try:
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get('marketdata', {}).get('data'):
                last_price = data['marketdata']['data'][0][0]
                if last_price is not None:
                    return float(last_price)
            return None
        except Exception as e:
            print(f"  Ошибка цены {ticker}: {e}")
            return None

    def get_lot_size(self, ticker):
        url = f"{self.BASE_URL}/engines/stock/markets/shares/boards/TQBR/securities/{ticker}.json"
        params = {
            'iss.meta': 'off',
            'iss.only': 'securities',
            'securities.columns': 'LOTSIZE'
        }
        try:
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get('securities', {}).get('data'):
                lot_size = data['securities']['data'][0][0]
                return int(lot_size) if lot_size else 1
        except:
            pass
        return 1

    def get_dividends(self, ticker):
        if ticker in MANUAL_DATA:
            return MANUAL_DATA[ticker]["dividends"]

        dividends = self._get_dividends_from_api(ticker)
        if dividends:
            return dividends

        dividends = self._get_dividends_from_dohod(ticker)
        return dividends

    def _get_dividends_from_api(self, ticker):
        url = f"{self.BASE_URL}/engines/stock/markets/shares/boards/TQBR/securities/{ticker}/dividends.json"
        try:
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if not data.get('dividends', {}).get('data'):
                return []
            columns = data['dividends']['columns']
            rows = data['dividends']['data']
            dividends = []
            for row in rows:
                div_dict = dict(zip(columns, row))
                value = div_dict.get('value', 0)
                if value and float(value) > 0:
                    dividends.append({
                        'value': float(value),
                        'declared_date': div_dict.get('declared_date', ''),
                        'record_date': div_dict.get('record_date', ''),
                        'payment_date': div_dict.get('payment_date', '')
                    })
            return dividends
        except Exception as e:
            print(f"  Ошибка дивидендов {ticker}: {e}")
            return []

    def _get_dividends_from_dohod(self, ticker):
        from bs4 import BeautifulSoup

        url = f"https://www.dohod.ru/ik/analytics/dividend/{ticker.lower()}"
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            resp = self.session.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            resp.encoding = 'utf-8'
            soup = BeautifulSoup(resp.text, 'html.parser')

            tables = soup.find_all('table')
            for table in tables:
                header_text = table.get_text()
                if 'Дата закрытия реестра' in header_text and 'Дивиденд' in header_text:
                    rows = table.find_all('tr')
                    
                    header_cells = rows[0].find_all('th') if rows else []
                    col_index = {}
                    for idx, cell in enumerate(header_cells):
                        text = cell.get_text().strip()
                        if 'Дата закрытия реестра' in text:
                            col_index['record_date'] = idx
                        elif 'Дивиденд' in text:
                            col_index['dividend'] = idx
                        elif 'Год для учета дивиденда' in text:
                            col_index['period'] = idx
                    
                    for row in rows[1:]:
                        cells = row.find_all('td')
                        if len(cells) >= 3:
                            record_date = cells[col_index.get('record_date', 1)].get_text().strip()
                            dividend_value = cells[col_index.get('dividend', 3)].get_text().strip()
                            period_value = cells[col_index.get('period', 2)].get_text().strip() if 'period' in col_index else ''
                            
                            if record_date and dividend_value and dividend_value != 'n/a':
                                try:
                                    date_obj = datetime.strptime(record_date, '%d.%m.%Y')
                                    date_formatted = date_obj.strftime('%Y-%m-%d')
                                    dividend_clean = dividend_value.replace(',', '.')
                                    dividend_float = float(dividend_clean)
                                    
                                    if period_value and period_value != 'n/a':
                                        period_text = period_value
                                    else:
                                        period_text = f"{date_obj.year} (прогноз)"
                                    
                                    print(f"  ✅ Нашли на dohod.ru: {dividend_float} ₽, дата {date_formatted}, период {period_text}")
                                    
                                    return [{
                                        'value': dividend_float,
                                        'declared_date': date_formatted,
                                        'record_date': date_formatted,
                                        'payment_date': date_formatted,
                                        'period': period_text
                                    }]
                                except:
                                    continue

            # Специальная проверка для DOMRF
            if ticker.upper() == "DOMRF":
                page_text = soup.get_text()
                amount_match = re.search(r'(\d+\.\d+)\s*руб\.?\s*\([^)]*\)', page_text)
                if not amount_match:
                    amount_match = re.search(r'(\d+\.\d+)\s*₽', page_text)
                if amount_match:
                    try:
                        dividend_float = float(amount_match.group(1))
                        date_match = re.search(r'(\d{2}\.\d{2}\.\d{4})', page_text)
                        if date_match:
                            date_obj = datetime.strptime(date_match.group(1), '%d.%m.%Y')
                            date_formatted = date_obj.strftime('%Y-%m-%d')
                            print(f"  ✅ Нашли DOMRF вручную: {dividend_float} ₽, дата {date_formatted}")
                            return [{
                                'value': dividend_float,
                                'declared_date': date_formatted,
                                'record_date': date_formatted,
                                'payment_date': date_formatted,
                                'period': f"{date_obj.year} (прогноз)"
                            }]
                    except:
                        pass

            return []
        except Exception as e:
            if "404" not in str(e):
                print(f"  ⚠️ Ошибка парсинга dohod.ru для {ticker}: {e}")
            return []

    def get_fundamental_data(self, ticker):
        """
        Парсит фундаментальные показатели с dohod.ru:
        ROE, P/E, Чистый долг/EBITDA, Дивидендная история (лет), Отрасль
        """
        from bs4 import BeautifulSoup
        import re
        
        # Если есть ручные данные, возвращаем их
        if ticker in MANUAL_DATA:
            return {
                'roe': None,
                'pe': None,
                'debt_ebitda': None,
                'sector': None,
                'dividend_years': None
            }
        
        url = f"https://www.dohod.ru/ik/analytics/dividend/{ticker.lower()}"
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            resp = self.session.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            resp.encoding = 'utf-8'
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            page_text = soup.get_text()
            
            # Ищем ROE
            roe_match = re.search(r'ROE\s*:\s*([\d.]+)%', page_text, re.IGNORECASE)
            roe = float(roe_match.group(1)) if roe_match else None
            
            # Ищем P/E
            pe_match = re.search(r'P/E\s*:\s*([\d.]+)', page_text, re.IGNORECASE)
            pe = float(pe_match.group(1)) if pe_match else None
            
            # Ищем Чистый долг/EBITDA
            debt_match = re.search(r'Чистый долг/EBITDA\s*:\s*([\d.]+)', page_text, re.IGNORECASE)
            debt_ebitda = float(debt_match.group(1)) if debt_match else None
            
            # Ищем Отрасль
            sector_match = re.search(r'Отрасль\s*:\s*([^\n]+)', page_text, re.IGNORECASE)
            sector = sector_match.group(1).strip() if sector_match else None
            
            # Ищем Дивидендную историю (количество лет выплат)
            tables = soup.find_all('table')
            dividend_years = 0
            for table in tables:
                header_text = table.get_text()
                if 'Дивиденд' in header_text and 'Дата закрытия реестра' in header_text:
                    rows = table.find_all('tr')
                    dividend_years = len(rows) - 1
                    break
            
            return {
                'roe': roe,
                'pe': pe,
                'debt_ebitda': debt_ebitda,
                'sector': sector,
                'dividend_years': dividend_years
            }
        except Exception as e:
            # Не выводим ошибку для каждого тикера, чтобы не захламлять консоль
            return {
                'roe': None,
                'pe': None,
                'debt_ebitda': None,
                'sector': None,
                'dividend_years': None
            }

    def get_company_name(self, ticker):
        if ticker in MANUAL_DATA:
            return MANUAL_DATA[ticker]["name"]

        url = f"{self.BASE_URL}/engines/stock/markets/shares/boards/TQBR/securities/{ticker}.json"
        params = {
            'iss.meta': 'off',
            'iss.only': 'securities',
            'securities.columns': 'SECID,SHORTNAME'
        }
        try:
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get('securities', {}).get('data'):
                return data['securities']['data'][0][1]
        except:
            pass
        return ticker

    def get_all_shares(self):
        url = f"{self.BASE_URL}/engines/stock/markets/shares/boards/TQBR/securities.json"
        params = {
            'iss.meta': 'off',
            'iss.only': 'securities',
            'securities.columns': 'SECID,SHORTNAME,ISIN'
        }
        try:
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get('securities', {}).get('data'):
                columns = data['securities']['columns']
                rows = data['securities']['data']
                shares = []
                for row in rows:
                    share_dict = dict(zip(columns, row))
                    shares.append({
                        'ticker': share_dict.get('SECID'),
                        'name': share_dict.get('SHORTNAME')
                    })
                return shares
            return []
        except Exception as e:
            print(f"Ошибка получения списка акций: {e}")
            return []

# ============================================
# 3. ОСНОВНАЯ ЛОГИКА
# ============================================

def main():
    print("🚀 Запуск парсера дивидендных акций...")
    print(f"📊 Ставка по депозитам: {DEPOSIT_RATE:.0%}")

    moex = MoexClient()
    print("✅ Подключение к MOEX установлено")

    gs_client = get_google_sheets_client()
    sheet = gs_client.open_by_key(SPREADSHEET_ID)
    worksheet = sheet.worksheet("Список")
    print("✅ Подключение к Google Sheets установлено")

    # Заголовки

    headers = [
        "Тикер", "Название", "Цена", "Лотность", "Цена лота",
        "Сумма дивиденда", "Дивиденды на лот", "Доходность",
        "Акций для 1000 ₽", "Лотов для 1000 ₽", "Стоимость лотов",
        "Дата отсечки", "Период выплаты", "Див. история (лет)"
    ]

    worksheet.update(range_name="A1", values=[headers])


    # Получаем список акций
    all_shares = moex.get_all_shares()
    if not all_shares:
        print("❌ Не удалось получить список акций")
        return

    print(f"📈 Получен список из {len(all_shares)} акций")

    # Добавляем ручные тикеры
    manual_tickers = ['DOMRF', 'URSB']
    existing_tickers = [s['ticker'] for s in all_shares]
    for ticker in manual_tickers:
        if ticker not in existing_tickers:
            name = MANUAL_DATA.get(ticker, {}).get("name", ticker)
            all_shares.append({'ticker': ticker, 'name': name})
            print(f"  ➕ Добавлен тикер {ticker} вручную")

    print(f"📈 Итоговый список: {len(all_shares)} акций")

    profitable_payments = []
    today = datetime.now().date()
    one_year_later = today + timedelta(days=365)

    for i, share in enumerate(all_shares, 1):
        ticker = share['ticker']

        # Пропускаем чёрный список
        if ticker in BLACKLIST:
            continue

        print(f"  [{i}/{len(all_shares)}] {ticker}...", end=" ")

        # Получаем данные

        price = moex.get_last_price(ticker)
        dividends = moex.get_dividends(ticker)
        name = moex.get_company_name(ticker)
        lot_size = moex.get_lot_size(ticker)

        # === ПОЛУЧАЕМ ДИВИДЕНДНУЮ ИСТОРИЮ ===
        try:
            fundamental = moex.get_fundamental_data(ticker)
        except:
            fundamental = {'dividend_years': None}

        # Ручные данные для URSB (если API не дал)
        if ticker == "URSB":
            if price is None:
                price = MANUAL_DATA["URSB"]["price"]
                print(f"  🔧 Ручная цена: {price}")
            if not dividends:
                dividends = MANUAL_DATA["URSB"]["dividends"]
                print(f"  🔧 Ручные дивиденды: {dividends[0]['value']} ₽")
            if name == "URSB":
                name = MANUAL_DATA["URSB"]["name"]
            if lot_size == 1:
                lot_size = MANUAL_DATA["URSB"]["lot_size"]

        if price is None:
            print("⚠️ Нет цены")
            continue

        if not dividends:
            print("⚠️ Нет дивидендов")
            continue

        # Собираем будущие выплаты
        future_payments = []
        total_annual_dividend = 0.0

        for div in dividends:
            record_date_str = div.get('record_date', '')
            if not record_date_str:
                continue
            try:
                record_date = datetime.strptime(record_date_str, '%Y-%m-%d').date()
            except:
                continue

            if today <= record_date <= one_year_later:
                div_value = div.get('value', 0)
                if div_value > 0:
                    future_payments.append({
                        'value': div_value,
                        'record_date': record_date,
                        'payment_date': div.get('payment_date', ''),
                        'declared_date': div.get('declared_date', '')
                    })
                    total_annual_dividend += div_value

        if not future_payments:
            print("⚠️ Нет будущих дивидендов в ближайший год")
            continue

        future_payments.sort(key=lambda x: x['record_date'])
        annual_yield = total_annual_dividend / price if price > 0 else 0

        # Фильтруем аномалии
        if annual_yield > 0.50:
            print(f"⚠️ Аномальная доходность {annual_yield:.2%} — пропускаем")
            continue

        if annual_yield <= DEPOSIT_RATE:
            print(f"ℹ️ Доходность {annual_yield:.2%} ниже {DEPOSIT_RATE:.0%}%")
            continue

        # Добавляем выплаты
        for payment in future_payments:
            payment_yield = payment['value'] / price if price > 0 else 0
            profitable_payments.append({
                'ticker': ticker,
                'name': name,
                'price': price,
                'dividend_amount': payment['value'],
                'current_yield': payment_yield,
                'annual_yield': annual_yield,
                'record_date': payment['record_date'].strftime('%Y-%m-%d'),
                'period': payment.get('period', f"{payment['record_date'].month}-{payment['record_date'].year}"),
                'lot_size': lot_size,
                'dividend_years': fundamental.get('dividend_years')
            })

        print(f"✅ {len(future_payments)} выплат, годовая: {annual_yield:.2%}")
        time.sleep(0.3)

    # Сортировка и вывод
    profitable_payments.sort(key=lambda x: x['record_date'])

    print(f"\n📊 Найдено {len(profitable_payments)} будущих выплат с годовой доходностью выше {DEPOSIT_RATE:.0%}%")

    for p in profitable_payments[:10]:
        print(f"  • {p['ticker']}: {p['dividend_amount']} ₽ → {p['record_date']} (годовая {p['annual_yield']:.2%})")

    if profitable_payments:
        write_to_sheet(worksheet, profitable_payments)
        
        # === ДАТА ОБНОВЛЕНИЯ ПОД ТАБЛИЦЕЙ ===

        now = datetime.now().strftime("%d.%m.%Y %H:%M")

        # Находим последнюю заполненную строку и ставим дату на 2 строки ниже

        last_row = len(profitable_payments) + 2  # +2 для отступа
        worksheet.update(range_name=f"A{last_row + 1}", values=[[f"🔄 Обновлено: {now}"]])
        print(f"📅 Дата обновления: {now}")
        
        print(f"\n✅ Таблица обновлена! Ссылка: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")
    else:
        print("\n⚠️ Нет выплат для записи")
        clear_sheet(worksheet)

if __name__ == "__main__":
    main()