import httpx
import logging

logger = logging.getLogger(__name__)

class OllamaEmbedder:
    def __init__(self, model_name: str = "nomic-embed-text", base_url: str = "http://localhost:11434"):
        """По умолчанию используем встроенную модель Ollama для эмбеддингов, которая дает 768 измерений."""
        self.model_name = model_name
        self.base_url = base_url.rstrip('/')
        
    def embed_text(self, text: str) -> list[float]:
        try:
            with httpx.Client() as client:
                res = client.post(
                    f"{self.base_url}/api/embeddings",
                    json={"model": self.model_name, "prompt": text},
                    timeout=30.0
                )
                res.raise_for_status()
                return res.json().get("embedding", [])
        except Exception as e:
            logger.error(f"Failed to fetch embedding from Ollama for model {self.model_name}: {e}")
            return []
