# MCP Configuration Templates

Quick reference for adding Z-Library MCP server to your projects.

---

## Shared Checkout (System-Wide Installation)

**Example Installation Location**: `/home/your-user/mcp-servers/zlibrary-mcp/`.
Replace it with your checkout's absolute path. The account variables shown below
are needed only for Z-Library operations; omit the `env` block for LibGen-only use.

### Copy-Paste Configuration

**Copy this into your project's `.mcp.json`**:

```json
{
  "mcpServers": {
    "zlibrary": {
      "command": "node",
      "args": ["/home/your-user/mcp-servers/zlibrary-mcp/dist/index.js"],
      "env": {
        "ZLIBRARY_EMAIL": "your@email.com",
        "ZLIBRARY_PASSWORD": "your_password"
      }
    }
  }
}
```

**That's it!** Restart Claude Code and you'll have all five acquisition tools.

---

## When to Add This Config

### ✅ DO Add to .mcp.json For:

**Research Projects**:

- Academic paper searches
- Literature reviews
- Citation analysis
- Bibliography building

**RAG/AI Projects**:

- Knowledge base creation
- Training data collection
- Document corpus building
- Semantic search preparation

**Learning Projects**:

- Finding study materials
- Course development
- Reference libraries

### ❌ DON'T Add to .mcp.json For:

**Pure Coding Projects**:

- Web development (React, Node.js, etc.)
- API development
- Tool building
- General software development

**Data Science (Usually)**:

- Working with existing datasets
- Model training
- Data analysis
- (Unless doing literature review)

**Best Practice**: Only configure servers you actually use in that project!

---

## Quick Test

After adding config and restarting Claude Code:

```
Ask Claude: "What zlibrary tools are available?"

Expected: Five tools:
- search
- get_book
- download
- get_limits
- get_history
```

---

## Example: Using in a Research Project

**Project**: `~/projects/philosophy-research/`

**Step 1**: Create `.mcp.json`

```bash
cd ~/projects/philosophy-research
cat > .mcp.json << 'EOF'
{
  "mcpServers": {
    "zlibrary": {
      "command": "node",
      "args": ["/home/your-user/mcp-servers/zlibrary-mcp/dist/index.js"],
      "env": {
        "ZLIBRARY_EMAIL": "your@email.com",
        "ZLIBRARY_PASSWORD": "your_password"
      }
    }
  }
}
EOF
```

**Step 2**: Restart Claude Code

**Step 3**: Use it!

```
"Search Z-Library for books on phenomenology by Hegel"
"Inspect the first result with get_book using its ref"
"Download that ref to server disk as the original file"
```

---

## Multiple Servers in One Project

You can combine multiple MCP servers:

```json
{
  "mcpServers": {
    "zlibrary": {
      "command": "node",
      "args": ["/home/your-user/mcp-servers/zlibrary-mcp/dist/index.js"],
      "env": {
        "ZLIBRARY_EMAIL": "your@email.com",
        "ZLIBRARY_PASSWORD": "your_password"
      }
    },
    "sqlite": {
      "command": "sqlite-mcp",
      "args": ["--db", "research.db"]
    },
    "playwright": {
      "command": "playwright-mcp"
    }
  }
}
```

Each server provides its own tools. Use what you need!

---

## Troubleshooting

### "Tools not showing up"

1. Verify .mcp.json has correct absolute path
2. Restart Claude Code
3. Check the MCP client's captured server stderr

### "ENOENT: no such file"

- Path in .mcp.json must be absolute
- Use: `/home/your-user/mcp-servers/...`
- Not: `~/mcp-servers/...` (~ doesn't expand in JSON)

### "Rate limit detected"

- Z-Library limits login attempts
- Wait 10-15 minutes
- Normal behavior, handled gracefully

---

## Summary

**Example Location**: `/home/your-user/mcp-servers/zlibrary-mcp/`

**How to Use**:

1. Copy JSON config above
2. Paste into project's `.mcp.json`
3. Restart Claude Code
4. Use five acquisition tools!

**When to Use**: Research, RAG, learning projects

**When NOT to Use**: Pure coding projects (keep it clean!)

Restart the client after changing its configuration, then check the five-tool list.
