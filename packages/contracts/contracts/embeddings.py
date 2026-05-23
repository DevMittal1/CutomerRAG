from typing import TypedDict

class InlineEmbeddingPart(TypedDict):
    text: str

class InlineEmbeddingContent(TypedDict):
    parts: list[InlineEmbeddingPart]

class InlineEmbeddingRequest(TypedDict, total=False):
    output_dimensionality: int
    content: InlineEmbeddingContent
    metadata: dict[str, str]
