import os
import sys

# Añadir el path del proyecto al sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nexus.config.settings import GOOGLE_API_KEYS
import google.generativeai as genai

def test_gemini():
    print(f"Listando modelos disponibles para API Key: {GOOGLE_API_KEYS[0][:10]}...")
    try:
        genai.configure(api_key=GOOGLE_API_KEYS[0])
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                print(m.name)
        return True
    except Exception as e:
        print(f"Error listando modelos: {e}")
        return False

if __name__ == "__main__":
    test_gemini()
