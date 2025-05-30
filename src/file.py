from src.logging import logger
from src.config import Config
from typing import Any, List, Optional, Set, Tuple
import numpy as np 
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from sklearn.metrics.pairwise import cosine_similarity
from tqdm import tqdm
import os
import re
from src.utils import create_batches

class FileIndexer:
    """Indexes files with text embedding for search"""

    def __init__(self, text_processor, embeddings_cache_manager, content_cache_manager, content_extractor, lock):
        """Initialize the file indexer"""
        self.text_processor = text_processor
        self.embeddings_cache_manager = embeddings_cache_manager
        self.content_cache_manager = content_cache_manager
        self.content_extractor = content_extractor  # Inject ContentExtractor
        self.lock = lock  # Inject the lock

    def index_files(
        self,
        file_paths: List[str],
        max_workers: int,
        batch_size: int = Config.DEFAULT_BATCH_SIZE,
        max_batch_memory: int = 100 * 1024 * 1024,  # 100MB per batch (adjustable)
        timeout: int = Config.DEFAULT_TIMEOUT,
    ) -> int:
        """
        Index files by extracting content and creating embeddings with a two-phase approach.
        Now using adaptive batching based on file sizes and memory.
        """
                
        # Filter out already processed files
        new_files = [fp for fp in file_paths if not self.embeddings_cache_manager.contains(fp)]
        
        if not new_files:
            logger.info("All files already indexed.")
            return 0
        
        logger.info(f"Indexing {len(new_files)} new files...")
        logger.info(f"Current cache size: {self.embeddings_cache_manager.get_size()} files")
        
        successful_files = 0
        
        # PHASE 1: Extract content using ThreadPoolExecutor (I/O bound)
        logger.info("Phase 1: Extracting text from files...")
        extracted_contents = {}

        # Create batches using the utility function
        batches = create_batches(new_files, batch_size, max_batch_memory)
        
        # Now process each batch of files (I/O bound)
        for batch_idx, batch_files in enumerate(batches):
            logger.info(f"Processing batch {batch_idx + 1}/{len(batches)}")
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit extraction tasks
                future_to_file = {
                    executor.submit(self.content_extractor.extract_text, file_path): file_path
                    for file_path in batch_files
                }
                
                # Process completed tasks with a progress bar
                for future in tqdm(
                    as_completed(future_to_file),
                    total=len(batch_files),
                    desc="Extracting content"
                ):
                    file_path = future_to_file[future]
                    try:
                        # Get the result with timeout
                        result = future.result(timeout=timeout)
                        
                        # filter meaningless content
                        cleaned = self.text_processor.clean_text(result)
                        if len(cleaned) < 10:
                            logger.warning(f"Content too short after cleaning for {file_path}: '{result}'")
                            continue
                        if result:
                            extracted_contents[file_path] = result

                            if self.content_cache_manager:
                                with self.lock:
                                    self.content_cache_manager.add_item(file_path, result)

                    except TimeoutError:
                        logger.warning(f"Content extraction from {file_path} timed out")
                    except Exception as e:
                        logger.error(f"Exception extracting content from {file_path}: {e}")
            
            with self.lock:
                self.content_cache_manager.save_cache()

        
        logger.info(f"Successfully extracted content from {len(extracted_contents)} of {len(new_files)} files")

        if not extracted_contents:
            logger.warning("No content extracted from any files. Aborting embedding phase.")
            return 0

        # PHASE 2: Create embeddings in batches (no multiprocessing)
        logger.info("Phase 2: Creating embeddings using batch processing...")

        # Wrap batches with tqdm
        for batch_idx, batch_files in enumerate(tqdm(batches, desc="Embedding Batches")):
            logger.info(f"Processing batch {batch_idx + 1}/{len(batches)}")

            texts_to_embed = []
            valid_files = []

            for file_path in batch_files:
                text = extracted_contents.get(file_path, "")
                cleaned = self.text_processor.clean_text(text)
                if cleaned:
                    texts_to_embed.append(cleaned)
                    valid_files.append(file_path)

            if not texts_to_embed:
                continue

            embeddings = self.text_processor.encode_text(texts_to_embed)

            for file_path, emb in zip(valid_files, embeddings):
                if emb is not None:
                    with self.lock:
                        self.embeddings_cache_manager.add_item(file_path, emb)
                    successful_files += 1

        with self.lock:
            self.embeddings_cache_manager.save_cache()

        # Save the final cache
        logger.info(f"Successfully processed {successful_files} new files")
        logger.info(f"New cache size: {self.embeddings_cache_manager.get_size()} files")
        with self.lock:
            self.embeddings_cache_manager.save_cache(force=True)

        return successful_files



class FileSearcher:
    """Searches indexed files using text embeddings"""
    
    def __init__(self, text_processor, embeddings_cache_manager, content_cache_manager):
        """Initialize the file searcher"""
        self.text_processor = text_processor
        self.embeddings_cache_manager = embeddings_cache_manager
        self.content_cache_manager = content_cache_manager
        self.bm25_searcher = None
        
    def initialize_bm25(self):
        """Initialize BM25 search if content cache is available"""
        if self.content_cache_manager and self.content_cache_manager.get_size() > 0:
            from src.bm25 import BM25Searcher
            self.bm25_searcher = BM25Searcher(self.content_cache_manager)
            self.bm25_searcher.build_index()
            return True
        else:
            logger.warning("Content cache not available or empty. BM25 search will not be available.")
            return False
    
    def _compute_batch_similarities(self, query_embedding, file_paths_batch):
        """Compute similarities for a batch of files"""
        embeddings = [self.embeddings_cache_manager.get_item(fp) for fp in file_paths_batch]
        # Filter out None values
        valid_indices = [i for i, emb in enumerate(embeddings) if emb is not None]
        if not valid_indices:
            logger.warning("No valid embeddings found in this batch")
            return []
            
        valid_paths = [file_paths_batch[i] for i in valid_indices]
        valid_embeddings = [embeddings[i] for i in valid_indices]
        
        # Stack embeddings and compute similarities in one go
        embeddings_matrix = np.vstack(valid_embeddings)
        if embeddings_matrix.shape[1] != query_embedding.shape[0]:
            logger.error(f"Embedding dimension mismatch: query={query_embedding.shape[0]}, files={embeddings_matrix.shape[1]}")
            raise ValueError("Embedding dimension mismatch. Consider clearing the cache or using consistent models.")
        similarities = cosine_similarity([query_embedding], embeddings_matrix)[0]
        
        return list(zip(valid_paths, similarities))
        
    def _generate_preview(self, file_path, query, max_length=None):
        """
        Generate a preview snippet from the file content that's relevant to the query
        """
        if max_length is None:
            max_length = Config.PREVIEW_LENGTH

        try:
            # Get content using a single retrieval path
            content = None
            if self.content_cache_manager and self.content_cache_manager.contains(file_path):
                content = self.content_cache_manager.get_item(file_path)
            else:
                content = ContentExtractor.extract_text(file_path)
                # Store in cache if available
                if self.content_cache_manager:
                    self.content_cache_manager.add_item(file_path, content)
                
            if not content:
                return "No preview available"
            
            # Convert to lowercase once for case-insensitive matching
            content_lower = content.lower()
            
            # Split query into keywords once
            query_words = self.text_processor.clean_text(query).split()
            
            # Find the best paragraph with a single pass
            paragraphs = re.split(r'\n\s*\n', content)
            best_para = None
            best_score = -1
            best_positions = []
            
            for para in paragraphs:
                para_lower = para.lower()
                # Track positions of all matches for better snippet selection
                positions = []
                score = 0
                
                for word in query_words:
                    word_pos = para_lower.find(word)
                    while word_pos >= 0:
                        positions.append(word_pos)
                        score += 1
                        # Find next occurrence
                        word_pos = para_lower.find(word, word_pos + 1)
                
                if score > best_score:
                    best_score = score
                    best_para = para
                    best_positions = positions
            
            # If no match found, return start of document
            if best_score <= 0:
                return content[:max_length].strip() + "..." if len(content) > max_length else content.strip()
                
            # Single snippet generation with all context    
            if len(best_para) > max_length and best_positions:
                # Center snippet around average position of matches
                center_pos = sum(best_positions) // len(best_positions)
                start = max(0, center_pos - max_length // 2)
                end = min(len(best_para), start + max_length)
                
                # Adjust boundaries to avoid cutting words
                if start > 0:
                    start = best_para.rfind(' ', 0, start) + 1 if best_para.rfind(' ', 0, start) >= 0 else start
                
                if end < len(best_para):
                    end = best_para.find(' ', end) if best_para.find(' ', end) >= 0 else end
                
                snippet = best_para[start:end].strip()
                return f"...{snippet}..." if len(snippet) < len(best_para) else snippet
            
            return best_para.strip()
                
        except Exception as e:
            logger.error(f"Error generating preview from {file_path}: {e}")
            return "Preview generation failed"
    
    def search(self, query: str, top_k: int = 5, include_preview: bool = True) -> List[Tuple[str, float, Optional[str]]]:
        """
        Search indexed files based on a text query using embedding similarity
        
        """
        cache_size = self.embeddings_cache_manager.get_size()
        logger.info(f"Cache size before search: {cache_size} files")
        
        if cache_size == 0:
            logger.warning("No files have been indexed yet. Please index files first.")
            return []
        
        # Create query embedding
        query_embedding = self.text_processor.encode_text(query)
        
        # Get all file paths
        file_paths = self.embeddings_cache_manager.get_all_keys()
        
        logger.info(f"Computing similarities for {len(file_paths)} files...")
        
        # Process in batches for better memory efficiency
        batch_size = 1000
        results = []
        
        for i in range(0, len(file_paths), batch_size):
            batch_end = min(i + batch_size, len(file_paths))
            batch_paths = file_paths[i:batch_end]
            
            batch_results = self._compute_batch_similarities(query_embedding, batch_paths)
            results.extend(batch_results)
        
        # Sort all results by similarity (descending)
        results.sort(key=lambda x: x[1], reverse=True)
        
        logger.info(f"Found {len(results)} files with similarity scores")
        
        # Get top_k results
        top_results = results[:top_k]
        
        # Add previews if requested
        if include_preview:
            logger.info("Generating previews for top results...")
            results_with_preview = []
            for file_path, similarity in top_results:
                preview = self._generate_preview(file_path, query)
                results_with_preview.append((file_path, similarity, preview))
            return results_with_preview
        
        # If previews not requested, add None as preview placeholder
        return [(file_path, similarity, None) for file_path, similarity in top_results]
        
    def hybrid_search(self, query: str, top_k: int = 5, vector_weight: float = 0.7, include_preview: bool = True) -> List[Tuple[str, float, Optional[str]]]:
        """
        Perform hybrid search combining vector and BM25 scores
        
        """
        # Initialize BM25 if not already initialized
        if self.bm25_searcher is None and self.content_cache_manager:
            self.initialize_bm25()
            
        # Get vector similarity results
        vector_results = self.search(query, top_k=top_k*2, include_preview=False)
        vector_dict = {path: score for path, score, _ in vector_results}
        
        # Get BM25 results if available
        if self.bm25_searcher and self.bm25_searcher.bm25:
            logger.info("Running BM25 search...")
            bm25_results = self.bm25_searcher.search(query, top_k=top_k*2)
            bm25_dict = {path: score for path, score in bm25_results}
            
            # Normalize BM25 scores
            if bm25_dict:
                max_bm25 = max(bm25_dict.values()) if bm25_dict else 1.0
                if max_bm25 > 0:
                    bm25_dict = {k: v/max_bm25 for k, v in bm25_dict.items()}
            logger.info(f"Found {len(bm25_dict)} results with BM25")
        else:
            bm25_dict = {}
            logger.info("BM25 search not available, using vector search only")
        
        # Combine scores
        combined_scores = {}
        all_paths = set(vector_dict.keys()) | set(bm25_dict.keys())
        
        for path in all_paths:
            vector_score = vector_dict.get(path, 0.0)
            bm25_score = bm25_dict.get(path, 0.0)
            combined_scores[path] = (vector_score * vector_weight) + (bm25_score * (1-vector_weight))
        
        # Sort by combined score
        sorted_results = sorted(combined_scores.items(), key=lambda x: x[1], reverse=True)
        
        # Add previews if requested
        if include_preview:
            results_with_preview = []
            logger.info("Generating previews for top hybrid results...")
            for file_path, score in sorted_results[:top_k]:
                preview = self._generate_preview(file_path, query)
                results_with_preview.append((file_path, score, preview))
            return results_with_preview
        
        return [(path, score, None) for path, score in sorted_results[:top_k]]