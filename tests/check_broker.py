import os
import sys
from dotenv import load_dotenv

# Add project root to path
sys.path.append(os.getcwd())

# Load .env
load_dotenv()

email = os.getenv("IQ_OPTION_EMAIL")
password = os.getenv("IQ_OPTION_PASSWORD")

print(f"🔍 Testing IQ Option Connectivity for: {email}")

try:
    from iqoptionapi.stable_api import IQ_Option
    api = IQ_Option(email, password)
    
    print("⏳ Connecting to Broker...")
    check, reason = api.connect()
    
    if check:
        print("✅ CONNECTION SUCCESSFUL!")
        print(f"💰 Balance: {api.get_balance()}")
        print(f"💼 Mode: {api.get_balance_mode()}")
        api.api.close()
    else:
        print(f"❌ CONNECTION FAILED: {reason}")
        
except ImportError as e:
    print(f"❌ LIBRARY MISSING: {e}")
except Exception as e:
    print(f"❌ UNEXPECTED ERROR: {e}")
