# System-Wide Installation Guide

Use one source checkout from multiple MCP client projects. The paths below are
examples: replace `/home/your-user` with your own absolute home path.

---

## This Installation

Configure each project to launch the same installed checkout. Each server process
has its own session and row refs; sharing a checkout does not share those refs.

**Location**: `/home/your-user/mcp-servers/zlibrary-mcp/`

**Credentials**: Optional for startup and LibGen. Set `ZLIBRARY_EMAIL` and
`ZLIBRARY_PASSWORD` for Z-Library operations, or `ANNAS_SECRET_KEY` for Anna's
Archive downloads. Anna's search needs no key.

---

## Using This Server in Your Projects

### For Projects That Need Z-Library

**When to use**:

- Research projects (literature reviews, citations)
- Research projects (corpus acquisition)
- Learning projects (finding study materials)
- Document analysis projects

**How to configure**:

Create or edit `.mcp.json` in your project root:

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

**Then**:

1. Restart Claude Code in that project
2. The five acquisition tools will be available
3. Start using: search, download, and metadata retrieval.

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
uv sync      # Keep the Python environment in sync too
npm run build
# Restart each MCP client so it launches the updated server.
```

### Checking Status

```bash
cd ~/mcp-servers/zlibrary-mcp
git log --oneline -5  # See recent commits
npm test              # Run tests
git status            # Check for local changes
```

### Viewing Logs

Diagnostics go to stderr, which the MCP client normally captures. Inspect the
client's server logs; there is no default `logs/nodejs_debug.log` file.
Use `LOG_LEVEL=debug` only when needed, and scrub queries before sharing logs.

---

## Available Tools (5 Total)

| Tool          | Purpose                                                        |
| ------------- | -------------------------------------------------------------- |
| `search`      | Catalogue metadata search with an ordered `sources` list       |
| `get_book`    | Inspect the current row's `ref`                                |
| `download`    | Acquire the selected original file to server disk              |
| `get_limits`  | Local capabilities; `mode: "live"` for Z-Library account quota |
| `get_history` | Z-Library account history                                      |

Use the current row's `ref` for `get_book` and `download`; raw book records and
IDs are not download inputs. See [interface migration](../interface-migration.md).

---

## Example Project Configurations

### Research Project

```json
// ~/projects/my-research/.mcp.json
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

### Corpus Acquisition Project

```json
// ~/projects/research-corpus/.mcp.json
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
# Restart each MCP client to use the updated build.
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
# Should see: "Book acquisition MCP server is running via Stdio."
# Press Ctrl+C to stop

# Test in a project
cd ~/projects/some-project
# Create .mcp.json with config above
# Restart Claude Code
# Ask: "What zlibrary tools are available?"
# Should see five tools ✅
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
lint tools. Text processing is removed in 2.0; see [migration](../text-processing-migration.md).

### "Path not found" in .mcp.json

- Verify path is absolute: `/home/your-user/mcp-servers/zlibrary-mcp/dist/index.js`
- Not relative: `~/mcp-servers/...` (expand ~ to full path)

### Server not loading

- Check .mcp.json syntax (valid JSON)
- Restart Claude Code
- Check the MCP client's captured server stderr

---

## Summary

**Example Installation Location**: `/home/your-user/mcp-servers/zlibrary-mcp/`

**How to Use**:

- Projects that need Z-Library: Add to `.mcp.json` with absolute path
- Projects that don't: Don't configure it

**Maintenance**: Update in one place, all projects benefit

Restart each client after updating the shared checkout, then check its tool list.
