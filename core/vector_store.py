"""L5: 轻量语义搜索引擎 — 字符 bigram + TF-IDF + 余弦相似度，纯 numpy 零额外依赖。"""
import re
import numpy as np

def _char_bigrams(text: str) -> set[str]:
    """提取中文文本的字符 bigram 集合，用于语义相似度计算。"""
    text = re.sub(r'[\s\d\W]+', '', text)  # 去除非中文/字母
    return {text[i:i+2] for i in range(len(text)-1)} if len(text) >= 2 else {text}

def _bigram_vector(text: str, vocabulary: dict[str, int]) -> np.ndarray:
    """将文本转为基于字符 bigram 的 TF-IDF 向量。"""
    bigrams = _char_bigrams(text)
    vec = np.zeros(len(vocabulary))
    if not bigrams:
        return vec
    for bg in bigrams:
        if bg in vocabulary:
            vec[vocabulary[bg]] += 1
    # 归一化
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec

def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """余弦相似度。"""
    if a.ndim == 1:
        a = a.reshape(1, -1)
    if b.ndim == 1:
        b = b.reshape(1, -1)
    return float(np.dot(a, b.T).flatten()[0])

class VectorStore:
    """轻量向量存储，支持添加文档和语义搜索。"""

    def __init__(self):
        self.documents: list[dict] = []       # [{id, text, meta}]
        self.vocabulary: dict[str, int] = {}  # bigram → index
        self.vectors: np.ndarray | None = None

    def build_vocabulary(self, texts: list[str]) -> None:
        """从文本集合构建 bigram 词汇表。"""
        all_bigrams: set[str] = set()
        for t in texts:
            all_bigrams |= _char_bigrams(t)
        self.vocabulary = {bg: i for i, bg in enumerate(sorted(all_bigrams))}

    def add(self, doc_id: str, text: str, meta: dict = None) -> None:
        """添加文档到向量存储。"""
        old_vocab_size = len(self.vocabulary)
        # 更新词汇表
        new_bigrams = _char_bigrams(text) - set(self.vocabulary.keys())
        for bg in sorted(new_bigrams):
            self.vocabulary[bg] = len(self.vocabulary)

        # 如果词汇表增长了，需要扩展已有向量
        if len(self.vocabulary) > old_vocab_size and self.vectors is not None:
            padding = np.zeros((self.vectors.shape[0],
                               len(self.vocabulary) - old_vocab_size))
            self.vectors = np.hstack([self.vectors, padding])

        vec = _bigram_vector(text, self.vocabulary)

        # 更新已有文档还是新增
        for i, doc in enumerate(self.documents):
            if doc["id"] == doc_id:
                self.documents[i] = {"id": doc_id, "text": text, "meta": meta or {}}
                self.vectors[i] = vec
                return

        self.documents.append({"id": doc_id, "text": text, "meta": meta or {}})
        if self.vectors is None:
            self.vectors = vec.reshape(1, -1)
        else:
            self.vectors = np.vstack([self.vectors, vec])

    def search(self, query: str, top_k: int = 5, min_score: float = 0.05) -> list[dict]:
        """语义搜索，返回相似度排序的结果。"""
        if self.vectors is None or len(self.documents) == 0:
            return []
        query_vec = _bigram_vector(query, self.vocabulary)
        scores = np.dot(self.vectors, query_vec)
        # 取 top-k
        indices = np.argsort(scores)[::-1][:top_k]
        results = []
        for idx in indices:
            score = float(scores[idx])
            if score < min_score:
                continue
            doc = self.documents[idx]
            results.append({
                "id": doc["id"],
                "text": doc["text"][:300],
                "score": round(score, 4),
                "meta": doc.get("meta", {}),
            })
        return results

    def to_dict(self) -> dict:
        """序列化为 JSON。"""
        return {
            "documents": self.documents,
            "vocabulary": self.vocabulary,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VectorStore":
        """从 JSON 反序列化。"""
        store = cls()
        store.documents = data.get("documents", [])
        store.vocabulary = data.get("vocabulary", {})
        # 重建向量矩阵
        if store.documents and store.vocabulary:
            vecs = []
            for doc in store.documents:
                vecs.append(_bigram_vector(doc.get("text", ""), store.vocabulary))
            store.vectors = np.vstack(vecs)
        return store
