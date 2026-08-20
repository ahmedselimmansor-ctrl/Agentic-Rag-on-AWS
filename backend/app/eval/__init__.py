"""RAG evaluation harness — golden datasets, retrieval metrics, LLM-as-judge."""

from app.eval.dataset import Dataset, DatasetError, GoldenCase, load
from app.eval.metrics import RetrievalScores, aggregate, score_retrieval

__all__ = [
    "Dataset",
    "DatasetError",
    "GoldenCase",
    "RetrievalScores",
    "aggregate",
    "load",
    "score_retrieval",
]
