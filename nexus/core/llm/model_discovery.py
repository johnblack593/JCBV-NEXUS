import asyncio
import logging
from typing import Dict, Any, Optional, List

import aiohttp

logger = logging.getLogger("nexus.llm.discovery")

class ModelDiscoveryService:
    """
    Consulta la API de cada proveedor LLM para obtener la lista
    de modelos disponibles y seleccionar el mejor automáticamente.
    Cachea el resultado por sesión (no consulta en cada llamada).
    """

    GROQ_PREFERRED = [
        "llama-3.3-70b-versatile",
        "llama-3.1-70b-versatile",
        "llama-3.1-8b-instant",
        "mixtral-8x7b-32768",
        "gemma2-9b-it",
    ]

    GEMINI_PREFERRED = [
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
    ]

    def _select_best_groq(self, available: List[str]) -> str:
        for pref in self.GROQ_PREFERRED:
            if pref in available:
                return pref
        # Si ninguno, usar el primero disponible (fallback)
        return available[0] if available else self.GROQ_PREFERRED[0]

    def _select_best_gemini(self, available: List[str]) -> str:
        for pref in self.GEMINI_PREFERRED:
            if pref in available:
                return pref
        return available[0] if available else self.GEMINI_PREFERRED[0]

    def _make_fallback_result(self, provider: str, error: Optional[Exception] = None) -> dict:
        pref = self.GROQ_PREFERRED[0] if provider.lower() == "groq" else self.GEMINI_PREFERRED[0]
        return {
            "available": [],
            "selected": pref,
            "source": "preferred_fallback",
            "error": str(error) if error else None
        }

    async def discover_groq_models(self, api_key: str, timeout: float = 6.0) -> dict:
        url = "https://api.groq.com/openai/v1/models"
        try:
            connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(), ttl_dns_cache=300)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        models = [m["id"] for m in data.get("data", [])]
                        
                        selected = self._select_best_groq(models)
                        source = "api" if selected in self.GROQ_PREFERRED else "api_first_available"
                        if source == "api_first_available":
                            logger.info(f"Groq API: Ningún modelo preferido. Usando primero disponible: {selected}")
                        return {
                            "available": models,
                            "selected": selected,
                            "source": source,
                            "error": None
                        }
                    else:
                        text = await resp.text()
                        logger.warning(f"Groq discovery failed (HTTP {resp.status}): {text[:200]}")
                        return self._make_fallback_result("groq")
        except Exception as e:
            logger.warning(f"Groq discovery exception: {e}")
            return self._make_fallback_result("groq", e)

    def _filter_gemini_models(self, models_data: List[dict]) -> List[str]:
        filtered = []
        for m in models_data:
            name = m.get("name", "").replace("models/", "")
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" in methods:
                filtered.append(name)
        return filtered

    async def discover_gemini_models(self, api_key: str, timeout: float = 6.0) -> dict:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
        try:
            connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(), ttl_dns_cache=300)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        raw_models = data.get("models", [])
                        models = self._filter_gemini_models(raw_models)
                        
                        selected = self._select_best_gemini(models)
                        source = "api" if selected in self.GEMINI_PREFERRED else "api_first_available"
                        if source == "api_first_available":
                            logger.info(f"Gemini API: Ningún modelo preferido. Usando primero disponible: {selected}")
                        return {
                            "available": models,
                            "selected": selected,
                            "source": source,
                            "error": None
                        }
                    else:
                        text = await resp.text()
                        logger.warning(f"Gemini discovery failed (HTTP {resp.status}): {text[:200]}")
                        return self._make_fallback_result("gemini")
        except Exception as e:
            logger.warning(f"Gemini discovery exception: {e}")
            return self._make_fallback_result("gemini", e)

    async def discover_all(self, groq_key: Optional[str] = None, gemini_key: Optional[str] = None) -> dict:
        res = {"groq": self._make_fallback_result("groq"), "gemini": self._make_fallback_result("gemini")}
        coros = []
        
        if groq_key:
            coros.append(self.discover_groq_models(groq_key))
        else:
            coros.append(asyncio.sleep(0, result=self._make_fallback_result("groq")))
            
        if gemini_key:
            coros.append(self.discover_gemini_models(gemini_key))
        else:
            coros.append(asyncio.sleep(0, result=self._make_fallback_result("gemini")))
            
        groq_result, gemini_result = await asyncio.gather(*coros, return_exceptions=True)
        
        if isinstance(groq_result, Exception):
            logger.error(f"Error fatal en Groq discovery: {groq_result}")
            groq_result = self._make_fallback_result("groq", groq_result)
        if isinstance(gemini_result, Exception):
            logger.error(f"Error fatal en Gemini discovery: {gemini_result}")
            gemini_result = self._make_fallback_result("gemini", gemini_result)
            
        res["groq"] = groq_result
        res["gemini"] = gemini_result
        return res
