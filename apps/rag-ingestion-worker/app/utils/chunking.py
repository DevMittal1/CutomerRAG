from typing import List

def perform_text_chunking(
    text: str, 
    chunk_size: int = 512, 
    chunk_overlap: int = 50
) -> List[str]:
    """
    Splits text into chunks of roughly equal size with a specified overlap.
    A production implementation would likely use a smarter splitter (e.g. LangChain or native sentences).
    """
    if not text:
        return []
        
    chunks = []
    start = 0
    text_len = len(text)
    
    while start < text_len:
        end = start + chunk_size
        chunks.append(text[start:end])
        
        # Guard against reaching the end
        if end >= text_len:
            break
            
        start += (chunk_size - chunk_overlap)
        
    return chunks
