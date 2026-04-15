"""
Tests unitarios para ModelDiscoveryService.
Usa mocks — no requiere claves API reales.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from nexus.core.llm.model_discovery import ModelDiscoveryService

class TestModelDiscovery:

    def test_selects_preferred_model_when_available(self):
        """Si el modelo preferido está en la lista, debe seleccionarlo."""
        svc = ModelDiscoveryService()
        available = ["gemma2-9b-it", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
        selected = svc._select_best_groq(available)
        assert selected == "llama-3.3-70b-versatile"

    def test_fallback_to_first_available_if_no_preferred(self):
        """Si ningún preferido está disponible, usar el primero."""
        svc = ModelDiscoveryService()
        available = ["some-unknown-model", "another-model"]
        selected = svc._select_best_groq(available)
        assert selected == "some-unknown-model"

    def test_preferred_fallback_when_api_fails(self):
        """Si la API falla, retornar el primer modelo preferido."""
        svc = ModelDiscoveryService()
        result = svc._make_fallback_result("groq")
        assert result["source"] == "preferred_fallback"
        assert result["selected"] == ModelDiscoveryService.GROQ_PREFERRED[0]
        assert result["error"] is None

    def test_gemini_filters_generate_content_models(self):
        """Solo modelos que soporten generateContent deben incluirse."""
        svc = ModelDiscoveryService()
        raw_models = [
            {"name": "models/gemini-2.0-flash",
             "supportedGenerationMethods": ["generateContent", "countTokens"]},
            {"name": "models/embedding-001",
             "supportedGenerationMethods": ["embedContent"]},
            {"name": "models/gemini-1.5-pro",
             "supportedGenerationMethods": ["generateContent"]},
        ]
        filtered = svc._filter_gemini_models(raw_models)
        assert "gemini-2.0-flash" in filtered
        assert "gemini-1.5-pro" in filtered
        assert "embedding-001" not in filtered

if __name__ == "__main__":
    asyncio.run(pytest.main([__file__, "-v"]))
