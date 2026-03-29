import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from nexus.core.iqoption_engine import IQOptionManager

async def probe_assets():
    manager = IQOptionManager.get_instance()
    connected = await manager.connect()
    
    if not connected:
        print("Failed to connect.")
        return
        
    api = manager.api
    
    # Get all profit data
    profit_data = await asyncio.to_thread(api.get_all_profit)
    
    active_turbo = []
    active_binary = []
    
    for active, data in profit_data.items():
        turbo_payout = data.get("turbo", 0) * 100
        binary_payout = data.get("binary", 0) * 100
        
        if turbo_payout >= 80:
            active_turbo.append({"asset": active, "payout": turbo_payout})
        if binary_payout >= 80:
            active_binary.append({"asset": active, "payout": binary_payout})
            
    # Sort by payout descending
    active_turbo.sort(key=lambda x: x["payout"], reverse=True)
    active_binary.sort(key=lambda x: x["payout"], reverse=True)
    
    print("=== HIGH PAYOUT TURBO ASSETS (>= 80%) ===")
    for a in active_turbo:
        print(f"{a['asset']:<15} | {a['payout']}%")
        
    print("\n=== HIGH PAYOUT BINARY ASSETS (>= 80%) ===")
    for a in active_binary:
        print(f"{a['asset']:<15} | {a['payout']}%")

if __name__ == "__main__":
    asyncio.run(probe_assets())
