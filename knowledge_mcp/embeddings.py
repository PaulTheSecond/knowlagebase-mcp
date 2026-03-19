import logging
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

class LocalEmbedder:
    """
    Вычисляет вектора(embeddings) прямо внутри приложения (in-process)
    через CPU или GPU без необходимости скачивать/поднимать внешнюю Ollama.
    """
    def __init__(self, model_name: str = "sentence-transformers/all-mpnet-base-v2"):
        # Эта модель обладает размерностью 768 и отличным качеством поиска, скачивается автоматически
        # из HuggingFace при первом запуске.
        logger.info(f"Loading local embedding model: {model_name}... (this may take a while on first run)")
        self.model = SentenceTransformer(model_name)
        logger.info("Local model loaded successfully.")
        
    def embed_text(self, text: str) -> list[float]:
        try:
            # encode возвращает numpy array, конвертируем в Python list
            vector = self.model.encode(text)
            return vector.tolist()
        except Exception as e:
            logger.error(f"Failed to compute local embedding: {e}")
            return []
