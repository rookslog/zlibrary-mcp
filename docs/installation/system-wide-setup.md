# System-Wide Installation Guide

**Location**: `/home/rookslog/mcp-servers/zlibrary-mcp/`
**Status**: ✅ Installed and ready for use

---

## This Installation

This Z-Library MCP server is installed **system-wide** and can be used by any project on this computer.

**Location**: `/home/rookslog/mcp-servers/zlibrary-mcp/`

**Credentials**: Configured in environment variables
- `ZLIBRARY_EMAIL`: your@email.com
- `ZLIBRARY_PASSWORD`: (set in environment)

---

## Using This Server in Your Projects

### For Projects That Need Z-Library

**When to use**:
- Research projects (literature reviews, citations)
- RAG/AI projects (knowledge base building)
- Learning projects (finding study materials)
- Document analysis projects

**How to configure**:

Create or edit `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "zlibrary": {
      "command": "node",
      "args": ["/home/rookslog/mcp-servers/zlibrary-mcp/dist/index.js"],
      "env": {
        "ZLIBRARY_EMAIL": "your@email.com",
        "ZLIBRARY_PASSWORD": "your_password"
      }
    }
  }
}
```

**Then**:
1. Restart Claude Code in that project
2. The 11 Z-Library tools will be available
3. Start using: search, download, process, metadata extraction, etc.

---

### For Projects That DON'T Need Z-Library

**When NOT to use**:
- Pure software development (web apps, APIs, tools)
- Data science with existing datasets
- General coding projects
- Projects with no research/document needs

**How to configure**:
- **Don't create `.mcp.json` file**
- Or create `.mcp.json` without zlibrary config
- Server won't load (cleaner, less overhead)

**Example** `.mcp.json` for a coding project:
```json
{
  "mcpServers": {
    // Other servers you need, but not zlibrary
    "sqlite": {
      "command": "..."
    }
    // No zlibrary config = not loaded ✅
  }
}
```

---

## Maintenance

### Updating the Server

```bash
cd ~/mcp-servers/zlibrary-mcp
git pull origin master
npm install  # If dependencies changed
npm run build
# All projects using this server now get the update!
```

### Checking Status

```bash
cd ~/mcp-servers/zlibrary-mcp
git log --oneline -5  # See recent commits
npm test              # Run tests
git status            # Check for local changes
```

### Viewing Logs

```bash
cd ~/mcp-servers/zlibrary-mcp
tail -f logs/nodejs_debug.log  # Watch MCP server logs
```

---

## Available Tools (11 Total)

When a project configures this server, it gets access to:

### Search (6 methods)
1. `search_books` - Basic search with filters
2. `full_text_search` - Search within content
3. `search_by_term` - Conceptual navigation (60 terms/book)
4. `search_by_author` - Advanced author search
5. `search_advanced` - Fuzzy match detection
6. `fetch_booklist` - Collection exploration (954 books)

### Metadata (1 method)
7. `get_book_metadata` - Extract 60 terms, 11 booklists, complete data

### Download/Process (2 methods)
8. `download_book_to_file` - Download PDF/EPUB
9. `process_document_for_rag` - Extract clean text

### Utility (2 methods)
10. `get_download_limits` - Check quota
11. `get_download_history` - View downloads

---

## Example Project Configurations

### Research Project

```json
// ~/projects/my-research/.mcp.json
{
  "mcpServers": {
    "zlibrary": {
      "command": "node",
      "args": ["/home/rookslog/mcp-servers/zlibrary-mcp/dist/index.js"],
      "env": {
        "ZLIBRARY_EMAIL": "your@email.com",
        "ZLIBRARY_PASSWORD": "your_password"
      }
    }
  }
}
```

### RAG System Project

```json
// ~/projects/rag-knowledge-base/.mcp.json
{
  "mcpServers": {
    "zlibrary": {
      "command": "node",
      "args": ["/home/rookslog/mcp-servers/zlibrary-mcp/dist/index.js"],
      "env": {
        "ZLIBRARY_EMAIL": "your@email.com",
        "ZLIBRARY_PASSWORD": "your_password"
      }
    },
    "sqlite": {
      // Other servers as needed
    }
  }
}
```

### Web Development Project (No Z-Library Needed)

```json
// ~/projects/web-app/.mcp.json
{
  "mcpServers": {
    // No zlibrary - don't need it for web development
    "sqlite": {
      "command": "..."
    }
  }
}
```

Or simply don't create `.mcp.json` at all if no MCP servers needed.

---

## Advantages of This Setup

✅ **Single Installation**
- Only one copy of the server
- Only one Python venv to maintain
- Only one place to update

✅ **Selective Use**
- Only loaded in projects that need it
- Other projects stay clean
- No unnecessary overhead

✅ **Easy Updates**
```bash
cd ~/mcp-servers/zlibrary-mcp
git pull && npm run build
# All projects get the update!
```

✅ **Consistent Behavior**
- Same version across all projects
- Same credentials
- Same configuration

---

## Testing the Installation

```bash
# Test the server works
cd ~/mcp-servers/zlibrary-mcp
node dist/index.js
# Should see: "Z-Library MCP server (ESM/TS) is running via Stdio..."
# Press Ctrl+C to stop

# Test in a project
cd ~/projects/some-project
# Create .mcp.json with config above
# Restart Claude Code
# Ask: "What zlibrary tools are available?"
# Should see 11 tools ✅
```

---

## Troubleshooting

### "Module not found" errors
```bash
cd ~/mcp-servers/zlibrary-mcp
npm install
bash setup-uv.sh --no-dev
```

This installs the lightweight search/download core without contributor test and
lint tools. Add PDF/EPUB processing with `uv sync --no-dev --extra rag`, or the
complete scholarly/OCR tier with `uv sync --no-dev --extra scholar`.

### "Path not found" in .mcp.json
- Verify path is absolute: `/home/rookslog/mcp-servers/zlibrary-mcp/dist/index.js`
- Not relative: `~/mcp-servers/...` (expand ~ to full path)

### Server not loading
- Check .mcp.json syntax (valid JSON)
- Restart Claude Code
- Check logs in ~/mcp-servers/zlibrary-mcp/logs/

---

## Summary

**Installation Location**: `/home/rookslog/mcp-servers/zlibrary-mcp/` ✅

**How to Use**:
- Projects that need Z-Library: Add to `.mcp.json` with absolute path
- Projects that don't: Don't configure it

**Maintenance**: Update in one place, all projects benefit

**Status**: Ready for use across all your projects! 🚀
