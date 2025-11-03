import re
import csv
import os
import json
import time
from datetime import datetime
import email.utils
import requests
from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client, Client

# Load environment variables
load_dotenv('key.env', override=True)

# Also manually parse key.env file for variables that dotenv might miss
def parse_key_env():
    """Manually parse key.env file to extract variables"""
    env_vars = {}
    try:
        with open('key.env', 'r') as f:
            for line in f:
                line = line.strip()
                if line and '=' in line and not line.startswith('#'):
                    key, value = line.split('=', 1)
                    key = key.strip()
                    # Remove quotes from value
                    value = value.strip().strip("'").strip('"')
                    env_vars[key] = value
    except Exception as e:
        print(f"Warning: Could not parse key.env file: {e}")
    return env_vars

# Get environment variables
env_vars = parse_key_env()
API_KEY = os.getenv('API_key') or env_vars.get('API_key')
SUPABASE_URL = os.getenv('SUPABASE_URL') or env_vars.get('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY') or env_vars.get('SUPABASE_KEY')

if not API_KEY:
    raise ValueError("API key not found in key.env file. Please ensure API_key is set correctly.")

# Remove any quotes from the API key if present
API_KEY = API_KEY.strip("'").strip('"')

# Configure OpenAI client
try:
    client = OpenAI(api_key=API_KEY)
except Exception as e:
    raise Exception(f"Failed to initialize OpenAI client: {str(e)}")

# Configure Supabase client (optional - will only use if credentials are provided)
supabase: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    SUPABASE_URL = SUPABASE_URL.strip("'").strip('"')
    SUPABASE_KEY = SUPABASE_KEY.strip("'").strip('"')
    try:
        print(f"Initializing Supabase client with URL: {SUPABASE_URL}")
        print(f"Supabase key starts with: {SUPABASE_KEY[:10]}...")
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        # Test connection by trying to read from the table
        test_result = supabase.table('transactions').select('id').limit(1).execute()
        print("✓ Supabase client initialized successfully and connection verified!")
    except Exception as e:
        print(f"✗ Warning: Failed to initialize Supabase client: {str(e)}")
        print("Continuing without Supabase integration...")
        print("Please check:")
        print("  1. SUPABASE_URL is correct")
        print("  2. SUPABASE_KEY is a valid service role key (starts with 'eyJ' for JWT)")
        print("  3. The 'transactions' table exists in your Supabase database")
        supabase = None
else:
    print("Note: Supabase credentials not found. Transactions will only be saved to CSV.")

# Exchange rate cache and functions
exchange_rates = {}

def get_exchange_rate(from_currency, retry_count=0, max_retries=3):
    """Get exchange rate from a currency to USD with retry logic"""
    if from_currency == 'USD':
        return 1.0
        
    # Normalize currency code
    from_currency = from_currency.upper().strip()
    
    if from_currency in exchange_rates:
        return exchange_rates[from_currency]
    
    try:
        # Using ExchangeRate-API's free endpoint
        response = requests.get(f'https://open.er-api.com/v6/latest/USD', timeout=10)
        response.raise_for_status()  # Raise an exception for bad status codes
        data = response.json()
        
        if data.get('result') == 'success':
            # Store all rates for future use
            rates = data.get('rates', {})
            for curr, rate in rates.items():
                if rate > 0:  # Avoid division by zero
                    exchange_rates[curr.upper()] = 1/rate  # Store rate to USD
            
            if from_currency in exchange_rates:
                return exchange_rates[from_currency]
            else:
                print(f"Warning: Currency {from_currency} not found in exchange rates")
                return None
        else:
            raise ValueError(f"API returned unsuccessful result: {data.get('result')}")
    except Exception as e:
        if retry_count < max_retries:
            wait_time = (2 ** retry_count) * 2  # 2s, 4s, 8s
            print(f"Error fetching exchange rate (attempt {retry_count + 1}/{max_retries}): {str(e)}")
            print(f"Retrying in {wait_time} seconds...")
            time.sleep(wait_time)
            return get_exchange_rate(from_currency, retry_count + 1, max_retries)
        else:
            print(f"Error fetching exchange rate after {max_retries} attempts: {str(e)}")
            return None

def convert_to_usd(amount, from_currency):
    """Convert an amount from given currency to USD"""
    if amount is None:
        return None
    
    # If currency is empty or missing, assume USD
    if not from_currency or from_currency.strip() == '':
        return round(float(amount), 2)
        
    # Normalize currency code to uppercase
    from_currency = from_currency.upper().strip()
    
    # If already USD, return as is
    if from_currency == 'USD':
        return round(float(amount), 2)
        
    rate = get_exchange_rate(from_currency)
    if rate is None:
        # If conversion fails, log warning but return None
        # We'll handle this in the calling function
        print(f"Warning: Could not convert {amount} {from_currency} to USD. Exchange rate unavailable.")
        return None
        
    return round(float(amount) * rate, 2)

import asyncio
from tqdm import tqdm

def parse_transaction_with_llm(text, retry_count=0, max_retries=3):
    """
    Parse transaction information using OpenAI's API.
    Returns a dictionary containing the extracted information.
    Includes retry logic with exponential backoff for rate limits.
    """
    system_prompt = """You are a transaction parser that extracts structured information from transaction text.
    You must return a JSON object with specific fields, maintaining exact data types and formats.
    For currency codes, convert symbols to standard codes: $ -> USD, € -> EUR, £ -> GBP, ₽ -> RUB.
    For amounts, extract only the numeric value without currency symbols.
    For dates, convert all formats to YYYY-MM-DD."""
    
    user_prompt = f"""Extract the following information from this transaction text and return it in JSON format:
    - transaction_type: Must be exactly one of: "Payment", "Refund", "Failed Charge", "Charge"
    - name: Full name of the person (string, empty if not found)
    - email: Complete email address (string, empty if not found)
    - amount: Numeric amount without currency symbol (number, null if not found)
    - currency: Standard currency code: "USD", "EUR", "GBP", "RUB", "BRL" (string, empty if not found)
    - date: Date in YYYY-MM-DD format (string, empty if not found)

    Transaction text: {text}

    Return ONLY a JSON object with these exact keys. Example:
    {{"transaction_type": "Payment", "name": "John Smith", "email": "john@example.com", "amount": 42.50, "currency": "USD", "date": "2024-11-02"}}"""

    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo-1106",  # Using the version that supports JSON mode
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.1,  # Low temperature for more consistent output
            response_format={"type": "json_object"}  # Ensure JSON response
        )
        
        if not response.choices:
            raise ValueError("No response received from OpenAI API")
            
        result = response.choices[0].message.content
        if not result:
            raise ValueError("Empty response received from OpenAI API")
            
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as e:
            print(f"Failed to parse JSON response: {str(e)}")
            print(f"Raw response: {result}")
            raise
        
        # Validate and clean parsed data
        valid_transaction_types = {'Payment', 'Refund', 'Failed Charge', 'Charge'}
        valid_currencies = {'USD', 'EUR', 'GBP', 'RUB', 'BRL'}
        
        # Ensure all required fields are present with correct types
        if 'transaction_type' not in parsed or parsed['transaction_type'] not in valid_transaction_types:
            print(f"Warning: Invalid transaction type in response: {parsed.get('transaction_type', 'missing')}")
            parsed['transaction_type'] = 'Unknown'
            
        if 'amount' in parsed and parsed['amount'] is not None:
            try:
                parsed['amount'] = float(parsed['amount'])
            except (ValueError, TypeError):
                print(f"Warning: Invalid amount format: {parsed['amount']}")
                parsed['amount'] = None
                
        if 'currency' in parsed and parsed['currency'] not in valid_currencies:
            print(f"Warning: Invalid currency code: {parsed['currency']}")
            parsed['currency'] = ''
            
        if 'date' in parsed:
            try:
                # Verify date format
                datetime.strptime(parsed['date'], '%Y-%m-%d')
            except ValueError:
                print(f"Warning: Invalid date format: {parsed['date']}")
                parsed['date'] = ''
        
        # Ensure all required fields exist with correct default values
        defaults = {
            'transaction_type': 'Unknown',
            'name': '',
            'email': '',
            'amount': None,
            'currency': '',
            'date': ''
        }
        
        for field, default in defaults.items():
            if field not in parsed or parsed[field] is None:
                parsed[field] = default
        
        return parsed
    except Exception as e:
        if ("429" in str(e) or "rate_limit" in str(e).lower()) and retry_count < max_retries:
            # Rate limit hit - wait with exponential backoff
            wait_time = (2 ** retry_count) * 20  # 20s, 40s, 80s - OpenAI has higher rate limits
            print(f"Rate limit hit, waiting {wait_time} seconds before retry {retry_count + 1}/{max_retries}")
            time.sleep(wait_time)
            return parse_transaction_with_llm(text, retry_count + 1, max_retries)
        else:
            print(f"Error parsing transaction with LLM: {e}")
            return {
                'transaction_type': 'Unknown',
                'name': '',
                'email': '',
                'amount': None,
                'currency': '',
                'date': ''
            }

def save_transaction_to_supabase(transaction_data):
    """Save a transaction to Supabase database"""
    if supabase is None:
        return False
    
    try:
        # Prepare data for Supabase
        supabase_data = {
            'transaction_type': transaction_data['transaction_type'],
            'name': transaction_data['name'] if transaction_data['name'] else None,
            'email': transaction_data['email'] if transaction_data['email'] else None,
            'amount_usd': transaction_data['amount_usd'],
            'original_amount': transaction_data['original_amount'],
            'original_currency': transaction_data['original_currency'] if transaction_data['original_currency'] else None,
            'date': transaction_data['date'] if transaction_data['date'] else None
        }
        
        # Insert into Supabase
        result = supabase.table('transactions').insert(supabase_data).execute()
        if result.data:
            return True
        else:
            print(f"Warning: Supabase insert returned no data for transaction: {transaction_data.get('transaction_type', 'Unknown')}")
            return False
    except Exception as e:
        print(f"\n✗ Error saving transaction to Supabase: {str(e)}")
        print(f"  Transaction data: {json.dumps(supabase_data, indent=2)}")
        return False

def process_transactions(input_filename, output_filename):
    fieldnames = ['transaction_type', 'name', 'email', 'amount_usd', 'original_amount', 'original_currency', 'date']
    processed_count = 0
    
    print("\nReading transaction file...")
    with open(input_filename, 'r') as file:
        # Read all non-empty lines first
        lines = [line.strip() for line in file if line.strip()]
    
    total_transactions = len(lines)
    print(f"Found {total_transactions} transactions to process")
    print("\nStarting transaction processing with OpenAI API...")
    
    # Create progress bar
    pbar = tqdm(total=total_transactions, desc="Processing transactions", 
                bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]')
    
    # Create/overwrite CSV file with headers
    with open(output_filename, 'w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
    
    # Process each transaction and append to CSV
    for i, line in enumerate(lines):
        try:
            # Remove line number prefix if present
            line = re.sub(r'^\s*\d+\|', '', line).strip()
            
            # Add delay between transactions to avoid rate limits
            if i > 0:
                time.sleep(2)  # Wait 2 seconds between transactions - OpenAI allows higher throughput
            
            # Parse transaction using LLM
            transaction = parse_transaction_with_llm(line)
            
            # Convert amount to USD and prepare transaction for CSV
            # Always ensure we have a USD amount
            amount_usd = None
            if transaction['amount'] is not None:
                # Try to convert to USD
                amount_usd = convert_to_usd(transaction['amount'], transaction['currency'])
                
                # If conversion failed, handle fallback cases
                if amount_usd is None:
                    currency = transaction.get('currency', '').upper().strip() if transaction.get('currency') else ''
                    # If currency is empty or seems like USD, use original amount
                    if not currency or currency == 'USD' or currency == '':
                        amount_usd = round(float(transaction['amount']), 2)
                    else:
                        # For other currencies where conversion failed, we still need USD
                        # Try to get exchange rate one more time (the get_exchange_rate already has retries)
                        retry_rate = get_exchange_rate(currency)
                        if retry_rate is not None:
                            amount_usd = round(float(transaction['amount']) * retry_rate, 2)
                        else:
                            # Last resort: log error but still provide a value
                            # This shouldn't happen often, but ensures we always have USD amount
                            print(f"ERROR: Could not convert {transaction['amount']} {currency} to USD after all retries. "
                                  f"Transaction will use original amount as fallback.")
                            amount_usd = round(float(transaction['amount']), 2)
            
            csv_transaction = {
                'transaction_type': transaction['transaction_type'],
                'name': transaction['name'],
                'email': transaction['email'],
                'amount_usd': amount_usd,
                'original_amount': transaction['amount'],
                'original_currency': transaction['currency'],
                'date': transaction['date']
            }
            
            # Write transaction to CSV
            with open(output_filename, 'a', newline='') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writerow(csv_transaction)
            
            # Save to Supabase if configured
            if supabase:
                supabase_saved = save_transaction_to_supabase(csv_transaction)
                if not supabase_saved:
                    print(f"\n⚠ Warning: Failed to save transaction {i+1} to Supabase")
            
            processed_count += 1
            
            # Update progress bar
            pbar.update(1)
            # Add transaction details to progress bar description
            pbar.set_postfix_str(
                f"Last: {transaction.get('transaction_type', 'Unknown')} - " +
                (f"${amount_usd:.2f} USD" if amount_usd is not None else "N/A") +
                (f" (Original: {transaction.get('amount', 'N/A')} {transaction.get('currency', '')})" if transaction.get('amount') is not None else "")
            )
            
        except Exception as e:
            print(f"\nError processing transaction {i+1}: {str(e)}")
            print(f"Original text: {line}")
            continue
    
    pbar.close()
    print("\nTransaction processing completed!")
    return processed_count

def main():
    input_file = 'data.md'
    output_file = 'transactions.csv'
    
    print("\n=== Transaction Processing Script ===")
    print(f"Input file: {input_file}")
    print(f"Output file: {output_file}")
    print("Using OpenAI API for processing")
    if supabase:
        print("✓ Supabase integration enabled")
    else:
        print("Note: Supabase integration disabled (add SUPABASE_URL and SUPABASE_KEY to key.env)")
    print("================================\n")
    
    start_time = time.time()
    processed_count = process_transactions(input_file, output_file)
    end_time = time.time()
    processing_time = end_time - start_time
    
    print("\n=== Processing Summary ===")
    print(f"Total transactions processed: {processed_count}")
    print(f"Total processing time: {processing_time:.2f} seconds")
    if processed_count > 0:
        print(f"Average time per transaction: {processing_time/processed_count:.2f} seconds")
    print(f"Output saved to: {output_file}")
    print("=========================")

if __name__ == "__main__":
    main()
