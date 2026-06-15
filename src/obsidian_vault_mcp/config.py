import os
from pathlib import Path

# Vault configuration
VAULT_PATH = Path(os.environ.get("VAULT_PATH", os.path.expanduser("~/Obsidian/MyVault")))
VAULT_MCP_TOKEN = os.environ.get("VAULT_MCP_TOKEN", "")
VAULT_MCP_PORT = int(os.environ.get("VAULT_MCP_PORT", "8420"))

# OAuth 2.0 client credentials (for Claude app integration)
VAULT_OAUTH_CLIENT_ID = os.environ.get("VAULT_OAUTH_CLIENT_ID", "vault-mcp-client")
VAULT_OAUTH_CLIENT_SECRET = os.environ.get("VAULT_OAUTH_CLIENT_SECRET", "")

# Safety limits
MAX_CONTENT_SIZE = 1_000_000  # 1MB max write size
MAX_BATCH_SIZE = 20           # Max files per batch operation
MAX_SEARCH_RESULTS = 50       # Max results per search
DEFAULT_SEARCH_RESULTS = 20
MAX_LIST_DEPTH = 5            # Max directory recursion depth
CONTEXT_LINES = 2             # Default lines of context in search results

# Directories to never expose or modify
EXCLUDED_DIRS = {".obsidian", ".trash", ".git", ".DS_Store"}

# Frontmatter index refresh interval (seconds)
FRONTMATTER_INDEX_DEBOUNCE = 5.0

# --- Semantic (vector) search ---
# Multilingual model (Bulgarian + English in one space), 384-dim, CPU-friendly.
SEMANTIC_MODEL = os.environ.get(
    "SEMANTIC_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
# Set SEMANTIC_ENABLED=0 to skip building the vector index entirely.
SEMANTIC_ENABLED = os.environ.get("SEMANTIC_ENABLED", "1") not in ("0", "false", "False")
# Debounce for re-embedding changed files (seconds).
SEMANTIC_INDEX_DEBOUNCE = 5.0
# Reciprocal Rank Fusion constant for hybrid search.
RRF_K = 60

# Rate limiting (requests per minute) -- track in-memory, enforce per-token
RATE_LIMIT_READ = 100
RATE_LIMIT_WRITE = 30
