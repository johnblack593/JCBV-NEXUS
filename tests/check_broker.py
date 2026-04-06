"""
NEXUS v4.0 — IQ Option Broker Connectivity Test
=================================================
Validates connection to IQ Option using the JCBV Modernized API.
Powered by: https://github.com/johnblack593/IQOP-API-JOHNBARZOLA

Usage:
    $env:IQ_OPTION_EMAIL="your_email"
    $env:IQ_OPTION_PASSWORD="your_password"
    python tests/check_broker.py
"""

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
    from iqoptionapi.version_control import api_version

    print(f"📦 JCBV API Version: {api_version}")

    api = IQ_Option(email, password)

    print("⏳ Connecting to Broker...")
    check, reason = api.connect()

    if check:
        print("✅ CONNECTION SUCCESSFUL!")
        print(f"💰 Balance: {api.get_balance()}")
        print(f"💼 Mode: {api.get_balance_mode()}")

        # Clean WebSocket shutdown (JCBV API feature)
        try:
            api.api.close()
            print("🔌 WebSocket closed cleanly.")
        except Exception:
            pass
    else:
        print(f"❌ CONNECTION FAILED: {reason}")

except ImportError as e:
    print(f"❌ LIBRARY MISSING: {e}")
    print("   Install: pip install git+https://github.com/johnblack593/IQOP-API-JOHNBARZOLA.git@main")
except Exception as e:
    print(f"❌ UNEXPECTED ERROR: {e}")
