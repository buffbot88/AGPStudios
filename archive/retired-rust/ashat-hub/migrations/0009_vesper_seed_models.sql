-- Seed test models for Vesper Studios
INSERT IGNORE INTO vesper_models (id, name, slug, description, model_type, version, filename, signature, file_size, download_url, platform_rid, min_ram_mb, quantization, is_active) VALUES
-- LLM models
('vm-001', 'Phi-3 Mini', 'phi-3-mini',
 'Microsoft Phi-3 Mini 3.8B — fast, capable small language model for code assistance and general tasks.',
 'llm', '3.8.1', 'phi-3-mini-3.8b-q4_k_m.gguf', 'sig_phi3mini', 2348810240, '', '', 4096, 'q4_k_m', 1),
('vm-002', 'Llama 3.1 8B', 'llama-3.1-8b',
 'Meta Llama 3.1 8B Instruct — strong general-purpose model with good code generation.',
 'llm', '3.1.0', 'llama-3.1-8b-instruct-q4_k_m.gguf', 'sig_llama31', 4915200000, '', '', 8192, 'q4_k_m', 1),
('vm-003', 'CodeLlama 7B', 'codellama-7b',
 'CodeLlama 7B Instruct — fine-tuned for code generation, completion, and transformation.',
 'llm', '0.1.0', 'codellama-7b-instruct-q8_0.gguf', 'sig_codellama', 7340032000, '', '', 8192, 'q8_0', 1),
('vm-004', 'Mistral 7B', 'mistral-7b',
 'Mistral 7B Instruct v0.3 — efficient model with strong reasoning and code capabilities.',
 'llm', '0.3.0', 'mistral-7b-instruct-v0.3-q4_k_m.gguf', 'sig_mistral7', 4194304000, '', '', 6144, 'q4_k_m', 1),

-- Embedding models
('vm-005', 'All-MiniLM-L6-v2', 'all-minilm-l6-v2',
 'Sentence-BERT embedding model — 384-dimensional embeddings for semantic search and retrieval.',
 'embedding', '2.0.0', 'all-minilm-l6-v2.onnx', 'sig_minilm', 9830400, '', '', 512, '', 1),
('vm-006', 'Nomic Embed v1.5', 'nomic-embed-v1.5',
 'Nomic Embed v1.5 — 768-dimensional embeddings with strong retrieval performance.',
 'embedding', '1.5.0', 'nomic-embed-v1.5-q8_0.gguf', 'sig_nomic', 134217728, '', '', 1024, 'q8_0', 1),

-- Vision models
('vm-007', 'Llava 1.6 7B', 'llava-1.6-7b',
 'Llava 1.6 7B — multimodal vision-language model for image understanding and description.',
 'vision', '1.6.0', 'llava-1.6-7b-q4_k_m.gguf', 'sig_llava', 4718592000, '', '', 8192, 'q4_k_m', 1),

-- TTS / STT (placeholder — real models would be larger)
('vm-008', 'Whisper Small', 'whisper-small',
 'OpenAI Whisper Small — speech recognition model supporting 99 languages.',
 'stt', '3.0.0', 'whisper-small.en.bin', 'sig_whisper', 489596928, '', '', 2048, '', 1);
