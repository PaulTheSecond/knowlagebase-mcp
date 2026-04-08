import logging

logger = logging.getLogger(__name__)

class LocalEmbedder:
    """
    Вычисляет вектора(embeddings) прямо внутри приложения (in-process)
    через CPU или GPU без необходимости скачивать/поднимать внешнюю Ollama.
    
    ВАЖНО: import sentence_transformers (который тянет за собой torch ~30 сек)
    выполняется лениво — только при первом вызове, а не при старте процесса.
    Это критически важно для MCP-режима, где handshake должен пройти мгновенно.
    """
    def __init__(self, model_name: str = "sentence-transformers/all-mpnet-base-v2"):
        logger.info(f"Loading local embedding model: {model_name}... (this may take a while on first run)")
        # Ленивый импорт: sentence_transformers тянет за собой torch (~30 сек на импорт),
        # поэтому импортируем только когда реально нужна модель.
        from sentence_transformers import SentenceTransformer
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

    def embed_batch(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        """Пакетная векторизация для резкого ускорения через параллелизм Pytorch/ONNX.
        При ошибке батча автоматически откатывается на поштучную обработку,
        чтобы не терять весь пакет из-за одного проблемного текста.
        """
        if not texts:
            return []
        try:
            vectors = self.model.encode(texts, batch_size=batch_size, show_progress_bar=False)
            return vectors.tolist()
        except Exception as e:
            logger.warning(f"Batch embedding failed ({e}), falling back to per-item processing...")
            # Fallback: обрабатываем каждый текст отдельно, чтобы не потерять весь батч
            results = []
            for i, text in enumerate(texts):
                try:
                    vector = self.model.encode(text)
                    results.append(vector.tolist())
                except Exception as item_err:
                    logger.error(f"Failed to embed item {i}: {item_err}")
                    results.append([])
            return results
