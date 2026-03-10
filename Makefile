.PHONY: help ollama-install ollama-start ollama-pull ingest-books ingest-book

OLLAMA_MODEL ?= nomic-embed-text
BOOKS_DIR    ?= books

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Ollama (local embedding model)
# ---------------------------------------------------------------------------

ollama-install: ## Install Ollama via Homebrew
	@command -v ollama >/dev/null 2>&1 && echo "Ollama already installed" || brew install ollama

ollama-pull: ## Pull the embedding model (nomic-embed-text)
	ollama pull $(OLLAMA_MODEL)

ollama-start: ## Start Ollama server (foreground)
	ollama serve

ollama-setup: ollama-install ollama-pull ## Install Ollama and pull the embedding model

# ---------------------------------------------------------------------------
# Knowledge base ingestion
# ---------------------------------------------------------------------------

ingest-books: ## Ingest all PDFs from books/ directory
	python3 scripts/ingest_books.py --dir $(BOOKS_DIR)

ingest-book: ## Ingest a single PDF: make ingest-book FILE=path/to/book.pdf
	@test -n "$(FILE)" || (echo "Usage: make ingest-book FILE=path/to/book.pdf" && exit 1)
	python3 scripts/ingest_books.py --file $(FILE)
