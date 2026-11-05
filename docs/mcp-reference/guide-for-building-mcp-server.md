# RooCode MCP Server Guide

## Overview of Primary Client-Side Logic

1.  **`src/services/mcp/McpHub.ts`**: This file appears to be the central hub for managing connections to MCP servers (both local stdio and remote SSE). It handles discovering servers from configuration, connecting/disconnecting, and provides methods like `callTool` and `readResource` for interacting with connected servers.
2.  **`src/services/mcp/McpServerManager.ts`**: This acts as a singleton manager for the `McpHub` instance, ensuring only one hub runs across different parts of the extension.
3.  **`webview-ui/src/components/mcp/McpView.tsx`**: This React component provides the user interface within the webview for viewing server status, enabling/disabling servers, restarting them, editing configurations, and toggling the ability for the AI to create new servers. It communicates with the extension backend (likely via `McpHub` indirectly) to perform these actions.
4.  **`src/core/prompts/sections/mcp-servers.ts`**: This generates the part of the system prompt that describes currently connected MCP servers, their tools, and resources, using information fetched from `McpHub`.
5.  **`src/core/prompts/instructions/create-mcp-server.ts`**: This contains instructions for the AI on how to guide the user in creating *new* MCP servers, mentioning the client-side tools `use_mcp_tool` and `access_mcp_resource`.

In summary, the core interaction logic is in `McpHub.ts`, managed by `McpServerManager.ts`, with the user interface handled by `McpView.tsx`, and prompt generation utilizing the hub's data.

## How RooCode Starts and Interacts With MCP Servers

**Phase 1: Extension Activation & Hub Initialization**

1.  **VS Code Activation (File: `extension.ts` - *Assumed*):**
    *   When VS Code starts, or when a specific activation event occurs (like opening a workspace or the RooCode panel), the RooCode extension's main `activate` function in `extension.ts` (or similar entry point) is called.
    *   This function typically registers commands and sets up UI elements, including the webview panel provider (likely `ClineProvider`).

2.  **Webview Panel Creation (`ClineProvider.ts` - *Assumed*):**
    *   When the user opens the RooCode chat/tool panel, an instance of `ClineProvider` (or similar class responsible for the webview) is created.
    *   The `ClineProvider` needs access to the MCP functionality to display server status and facilitate interactions.

3.  **Requesting the Singleton Hub (`McpServerManager.ts` - *Conceptual*):**
    *   The `ClineProvider` likely calls a static method like `McpServerManager.getInstance(context, this)` to get the central `McpHub`.
    *   `McpServerManager` ensures only *one* `McpHub` instance exists for the entire extension session. It checks if an instance already exists. If not, it creates a *new* `McpHub` instance, passing the `ClineProvider` (or a reference to it) to the `McpHub` constructor. It also increments a reference counter (`registerClient`).

4.  **`McpHub` Construction (`McpHub.ts:114-121`):**
    *   The `McpHub` constructor is called (`new McpHub(provider)`).
    *   It stores a `WeakRef` to the `ClineProvider` (`115`) to avoid memory leaks while still allowing communication back to the UI.
    *   Crucially, it immediately starts setting up file system watchers and initializing server connections:
        *   `watchMcpSettingsFile()` (`116`): Sets up a listener (`vscode.workspace.onDidSaveTextDocument`) for changes to the *global* settings file (e.g., `~/.roo/cline_mcp_settings.json`). (See `348-357`)
        *   `watchProjectMcpFile()` (`117`): Sets up a listener for changes to the *project-specific* settings file (`<workspace>/.roo/mcp.json`). (See `251-260`)
        *   `setupWorkspaceFoldersWatcher()` (`118`): Watches for workspace folder changes to potentially re-evaluate project-level servers. (See `214-225`)
        *   `initializeGlobalMcpServers()` (`119`): Begins the process of reading the global config and connecting to defined servers. (Calls `initializeMcpServers`: `401-403`)
        *   `initializeProjectMcpServers()` (`120`): Begins the process of reading the project config (if present) and connecting. (Calls `initializeMcpServers`: `423-426`)

**Phase 2: Server Discovery and Connection**

5.  **Reading Configuration (`McpHub.ts:359-398` - `initializeMcpServers`):**
    *   This method is called for both `"global"` and `"project"` sources.
    *   It determines the correct config file path (`getMcpSettingsFilePath` for global (`325-346`), `getProjectMcpPath` for project (`406-421`)).
    *   Reads the file content using `fs.readFile` (`368`).
    *   Parses the JSON (`369`).
    *   Validates the parsed object against `McpSettingsSchema` using Zod (`370`). Shows errors if invalid (`375-379`).
    *   If valid (or if global and forcing raw config), it calls `updateServerConnections` (`373`, `384`) with the `mcpServers` object from the config and the `source`.

6.  **Updating Connections (`McpHub.ts:716-772` - `updateServerConnections`):**
    *   This method synchronizes the active connections with the configuration for a specific `source` (global or project).
    *   It compares the servers in the `newServers` config object with the `currentConnections` for that source.
    *   **Removal:** Servers present in `currentConnections` but *not* in `newServers` are removed using `deleteConnection` (`730-734`). `deleteConnection` (`693-714`) closes the transport (`701`) and client (`702`) before removing the entry from the `connections` array.
    *   **Addition/Update:** It iterates through `newServers`:
        *   Validates the config entry using `validateServerConfig` (`742-748`).
        *   If the server is new or its config has changed (`deepEqual` check: `758`), it:
            *   Sets up file watchers if `watchPaths` is defined (`setupFileWatcher`: `753`, `761`). `setupFileWatcher` (`774-835`) uses `chokidar` to monitor specified paths (`789-806`) or the default `build/index.js` path (`809-828`) and triggers `restartConnection` on changes.
            *   Calls `deleteConnection` first if updating (`762`).
            *   Calls `connectToServer` (`754`, `763`) to establish the connection.

7.  **Connecting to a Specific Server (`McpHub.ts:428-571` - `connectToServer`):**
    *   Instantiates an MCP `Client` from the SDK (`437-445`).
    *   Checks `config.type` (`449`).
    *   **For `stdio` type (`449-508`):**
        *   Creates an `StdioClientTransport` (`450-459`), providing the `command`, `args`, `cwd`, and `env` (merged with `process.env.PATH`). `stderr` is set to `"pipe"`.
        *   Attaches `onerror` and `onclose` handlers to the transport (`462-478`) to update server status and notify the UI.
        *   **Crucially**, it uses a workaround to capture `stderr` *before* the main connection: calls `transport.start()` (`482`), gets the `stderrStream` (`483`), attaches a listener to log errors/info (`485-504`), and then monkey-patches `transport.start` to prevent the SDK's `client.connect` from starting it again (`508`).
    *   **For `sse` type (`510-537`):**
        *   Creates an `SSEClientTransport` (`522-525`) using the `url` and `headers` from the config. It uses `ReconnectingEventSource`.
        *   Attaches `onerror` handler (`528-536`).
    *   Stores the `client`, `transport`, and server details (initially `status: "connecting"`) in an `McpConnection` object and adds it to the `connections` array (`539-551`).
    *   Calls `client.connect(transport)` (`554`) which performs the MCP handshake over the established transport (stdio pipes or SSE).
    *   If successful, sets `status: "connected"` (`555`).
    *   **Capability Fetching:** Immediately calls methods to fetch capabilities:
        *   `fetchToolsList` (`559`)
        *   `fetchResourcesList` (`560`)
        *   `fetchResourceTemplatesList` (`561`)
    *   Handles connection errors in a `catch` block (`562-570`), updating status to `disconnected` and storing the error message.

8.  **Fetching Capabilities (`McpHub.ts:607-690`):**
    *   Methods like `fetchToolsList` (`607-656`):
        *   Find the relevant `McpConnection` (`610`).
        *   Use `connection.client.request({ method: "tools/list" }, ListToolsResultSchema)` to send the request via the transport (`616`). The SDK handles serialization/deserialization.
        *   Reads `alwaysAllow` settings from the config file (`618-644`).
        *   Processes the response, adding the `alwaysAllow` flag to each tool (`647-651`).
        *   Returns the array of `McpTool` objects (or `[]` on error).
    *   `fetchResourcesList` (`659-670`) and `fetchResourceTemplatesList` (`673-690`) work similarly for their respective list methods.

9.  **Notifying the UI (`McpHub.ts:881-927` - `notifyWebviewOfServerChanges`):**
    *   This method is called after connections are updated, status changes, or capabilities are fetched.
    *   It reads *both* config files again solely to determine the user-defined *order* of servers (`883-899`).
    *   It sorts the current `connections` array, prioritizing project servers and then applying the order from the config files (`901-920`).
    *   It extracts the `server` object from each sorted connection.
    *   It calls `providerRef.deref()?.postMessageToWebview(...)` (`923-926`), sending a message with `type: "mcpServers"` and the sorted array of `McpServer` objects to the `ClineProvider`, which relays it to the React UI (`McpView.tsx`).

**Phase 3: Runtime Interaction**

10. **Calling a Tool (`McpHub.ts:1149-1188` - `callTool`):**
    *   This method is invoked when the AI agent needs to use a tool.
    *   It finds the correct `McpConnection` using `findConnection` (`1155`).
    *   Checks if the server is disabled (`1161-1163`).
    *   Determines the request timeout from the server's config (defaulting to 60s) (`1165-1173`).
    *   Constructs the JSON-RPC request object: `{ method: "tools/call", params: { name: toolName, arguments: toolArguments } }` (`1176-1182`).
    *   Sends the request using `connection.client.request(...)`, passing the request object, the expected result schema (`CallToolResultSchema`), and the timeout (`1175-1187`). The SDK handles sending over the transport and receiving/parsing the response.
    *   Returns the parsed result.

11. **Reading a Resource (`McpHub.ts:1130-1147` - `readResource`):**
    *   Similar flow to `callTool`.
    *   Finds connection (`1131`), checks disabled (`1135-1137`).
    *   Constructs request: `{ method: "resources/read", params: { uri } }` (`1138-1144`).
    *   Sends using `connection.client.request` (`1138`).
    *   Returns the parsed result.

**Phase 4: Shutdown**

12. **Client Unregistration (`McpServerManager.ts` / `McpHub.ts:131-142`):**
    *   When a `ClineProvider` is disposed (e.g., user closes the panel), it calls `McpServerManager.unregisterClient()`.
    *   The manager decrements the reference count.
    *   If the count reaches zero, it calls `McpHub.dispose()`.

13. **Hub Disposal (`McpHub.ts:1271-1292`):**
    *   Sets `isDisposed` flag (`1278`).
    *   Closes all file watchers (`removeAllFileWatchers`: `1279`).
    *   Iterates through all connections, calling `deleteConnection` for each to close transports/clients (`1280-1286`).
    *   Clears the `connections` array (`1287`).
    *   Disposes any VS Code disposables (like file system watchers) (`1288-1291`).

This covers the main lifecycle from starting the extension and discovering servers based on configuration files, to connecting via stdio/sse, fetching capabilities, handling runtime tool calls, and finally cleaning up resources.


## Setting Up MCP Server for RooCode

**Goal:** Create a local server process that RooCode can launch, communicate with via standard input/output, and utilize its capabilities (primarily tools).

**1. Prerequisites**

*   **Node.js and npm/yarn:** You'll need a recent version of Node.js installed, which includes npm (or you can use yarn).
*   **TypeScript (Recommended):** While you can use JavaScript, TypeScript provides better type safety and is generally recommended. The official template uses TypeScript.
*   **VS Code:** For editing and interacting with RooCode.

**2. Workspace Setup (Recommended Method)**

The easiest way to start is by using the official MCP server template generator:

1.  **Navigate:** Open your terminal and navigate to the directory where you want to create your server project. A good practice is to have a dedicated directory for your MCP servers, potentially the one RooCode uses (`~/.roo/mcp_servers/` - you can find the exact path via `mcpHub.getMcpServersPath()` if needed, or just create your own).
2.  **Run the Generator:** Execute the following command, replacing `your-server-name` with a descriptive name (e.g., `github-issue-tracker`):
    ```bash
    npx @modelcontextprotocol/create-server your-server-name
    ```
3.  **Navigate into Project:**
    ```bash
    cd your-server-name
    ```
4.  **Install Dependencies:**
    ```bash
    npm install
    # or
    yarn install
    ```

This command scaffolds a basic TypeScript project structure:

```
your-server-name/
├── node_modules/
├── src/
│   └── index.ts         # <--- Your main server logic goes here
├── build/               # <--- Compiled JavaScript output (after build)
├── package.json         # Project metadata, dependencies, build scripts
└── tsconfig.json        # TypeScript compiler options
```

*   `package.json`: Defines dependencies (`@modelcontextprotocol/sdk`) and scripts (like `build`). Note if it includes `"type": "module"` which means you'll use ES Module syntax (`import`/`export`).
*   `tsconfig.json`: Configures the TypeScript compilation, typically outputting JavaScript to the `build` directory.

**3. Implementation (`src/index.ts`)**

This is the core file where you define your server's behavior.

```typescript name=src/index.ts
#!/usr/bin/env node // Important for making the built file executable

import {
    Server, // The main MCP Server class from the SDK
    StdioServerTransport, // The transport layer for stdio communication
    ListToolsRequestSchema, // Schema for the tools/list request
    ListToolsResult, // Expected result type for tools/list
    CallToolRequestSchema, // Schema for the tools/call request
    CallToolResult, // Expected result type for tools/call
    McpError, // Custom error class for MCP-specific errors
    ErrorCode, // Standard MCP error codes
    // Import other necessary schemas if implementing resources/templates
} from '@modelcontextprotocol/sdk/server/index.js'; // Use .js extension if package.json has "type": "module"

// --- Optional: Import external libraries ---
// import axios from 'axios'; // Example for making HTTP requests

// --- 1. Environment Variable Handling (Example) ---
// API Keys or secrets MUST come from environment variables set in mcp_settings.json
const API_KEY = process.env.YOUR_SERVICE_API_KEY;
if (!API_KEY) {
    // Throwing here will cause the server to fail startup, visible in RooCode MCP panel
    throw new Error('Required environment variable YOUR_SERVICE_API_KEY is not set.');
}

// --- Type definition for your tool's arguments (using Zod or similar is good practice) ---
// Example: Define expected arguments for a 'createIssue' tool
const CreateIssueArgsSchema = z.object({
    repo: z.string().describe("The owner/repo string (e.g., 'owner/repo-name')"),
    title: z.string().describe("The title of the issue"),
    body: z.string().optional().describe("The optional body content of the issue")
});

type CreateIssueArgs = z.infer<typeof CreateIssueArgsSchema>;

// --- Main Server Class ---
class MyMcpServer {
    private server: Server;
    private transport: StdioServerTransport;

    constructor() {
        // --- 2. Initialize Transport ---
        this.transport = new StdioServerTransport();

        // --- 3. Initialize Server ---
        this.server = new Server(
            {
                // Information about your server
                name: 'your-server-name', // Match the name used in create-server
                version: '0.1.0',
            },
            {
                // Declare the capabilities your server supports
                capabilities: {
                    tools: {}, // Indicates support for tools/list and tools/call
                    // resources: {}, // Uncomment if implementing resources/list and resources/read
                    // resourceTemplates: {}, // Uncomment if implementing resources/templates/list
                },
            }
        );

        // --- 4. Register Capability Handlers ---
        this.registerToolHandlers();
        // this.registerResourceHandlers(); // Call if implementing resources

        // --- 5. Setup Error Handling & Shutdown ---
        this.server.onerror = (error) => {
            console.error('[MCP Server Error]', error); // Log errors to stderr
        };

        process.on('SIGINT', async () => {
            console.log('Received SIGINT, shutting down server...');
            await this.server.close();
            await this.transport.close();
            process.exit(0);
        });
    }

    private registerToolHandlers(): void {
        // --- Handler for tools/list ---
        this.server.setRequestHandler(ListToolsRequestSchema, async (request): Promise<ListToolsResult> => {
            console.log("Received tools/list request"); // Good for debugging
            return {
                tools: [
                    {
                        name: 'createIssue',
                        description: 'Creates a new issue in a GitHub repository.',
                        // Define expected input arguments using JSON Schema
                        inputSchema: {
                            type: 'object',
                            properties: {
                                repo: { type: 'string', description: "The owner/repo string (e.g., 'owner/repo-name')" },
                                title: { type: 'string', description: "The title of the issue" },
                                body: { type: 'string', description: "The optional body content of the issue" },
                            },
                            required: ['repo', 'title'], // Specify required properties
                        },
                    },
                    // Add other tools your server provides here
                ],
            };
        });

        // --- Handler for tools/call ---
        this.server.setRequestHandler(CallToolRequestSchema, async (request): Promise<CallToolResult> => {
            const { name: toolName, arguments: toolArgs } = request.params;
            console.log(`Received tools/call request for tool: ${toolName}`, toolArgs);

            try {
                switch (toolName) {
                    case 'createIssue':
                        // --- Validate Arguments ---
                        // Use Zod or another validator, or manual checks
                        const validatedArgs: CreateIssueArgs = CreateIssueArgsSchema.parse(toolArgs);
                        // Or: if (!isValidCreateIssueArgs(toolArgs)) { throw new McpError(ErrorCode.InvalidParams, "Invalid arguments for createIssue"); }

                        // --- Perform Tool Action ---
                        const issueUrl = await this.handleCreateIssue(validatedArgs);

                        // --- Return Result ---
                        // Result must be JSON-serializable
                        return {
                            result: {
                                message: `Successfully created issue!`,
                                url: issueUrl,
                            },
                        };

                    // Add cases for other tools here

                    default:
                        // Tool not found
                        throw new McpError(ErrorCode.MethodNotFound, `Tool '${toolName}' not found.`);
                }
            } catch (error: any) {
                 console.error(`Error calling tool ${toolName}:`, error);
                 if (error instanceof z.ZodError) {
                     // Provide specific validation errors if possible
                     throw new McpError(ErrorCode.InvalidParams, `Invalid arguments for ${toolName}: ${error.errors.map(e => `${e.path.join('.')}: ${e.message}`).join(', ')}`);
                 }
                 if (error instanceof McpError) {
                     throw error; // Re-throw known MCP errors
                 }
                 // Throw a generic internal error for unexpected issues
                 throw new McpError(ErrorCode.InternalError, `Internal server error calling tool ${toolName}: ${error.message}`);
            }
        });
    }

    // --- Example Implementation for a Tool ---
    private async handleCreateIssue(args: CreateIssueArgs): Promise<string> {
        // Use the API_KEY from process.env
        console.log(`Creating issue in ${args.repo} with title "${args.title}" using API Key: ${API_KEY?.substring(0, 4)}...`);

        // --- Replace with your actual API call logic ---
        // Example using a placeholder function
        // const response = await axios.post(`https://api.github.com/repos/${args.repo}/issues`, {
        //     title: args.title,
        //     body: args.body,
        // }, {
        //     headers: { Authorization: `Bearer ${API_KEY}` }
        // });
        // return response.data.html_url;
        await new Promise(resolve => setTimeout(resolve, 500)); // Simulate async work
        const mockUrl = `https://github.com/${args.repo}/issues/123`;
        console.log(`Mock issue created: ${mockUrl}`);
        return mockUrl; // Placeholder return
        // --- End of actual API call logic ---
    }

    // --- Optional: Implement Resource Handlers ---
    // private registerResourceHandlers(): void {
    //     this.server.setRequestHandler(ListResourcesRequestSchema, async (request) => { /* ... */ });
    //     this.server.setRequestHandler(ReadResourceRequestSchema, async (request) => { /* ... */ });
    //     this.server.setRequestHandler(ListResourceTemplatesRequestSchema, async (request) => { /* ... */ });
    // }

    // --- 6. Start Listening ---
    public async start(): Promise<void> {
        console.log('MCP Server starting to listen...');
        await this.server.listen(this.transport);
        console.log('MCP Server listening on stdio.');
    }
}

// --- Entry Point ---
const serverInstance = new MyMcpServer();
serverInstance.start().catch((error) => {
    console.error('Failed to start MCP server:', error);
    process.exit(1);
});
```

**4. Defining Capabilities (Schema)**

*   **Tools (`tools/list` response):**
    *   `name` (string): Unique identifier for the tool.
    *   `description` (string): Human-readable explanation of what the tool does (shown to the AI).
    *   `inputSchema` (JSON Schema object, optional): Defines the structure, types, and descriptions of the arguments the tool expects. This is *highly recommended* for clarity and validation. Use standard [JSON Schema](https://json-schema.org/) format. RooCode uses this schema to help the AI formulate correct calls.
*   **Resources (`resources/list` response):** Static pieces of data identified by a URI (e.g., `my-data://item/1`). Includes `uri`, `name`, `description`, `mimeType`.
*   **Resource Templates (`resources/templates/list` response):** URI templates (RFC 6570) for accessing dynamic data (e.g., `weather://{city}/current`). Includes `uriTemplate`, `name`, `description`, `mimeType`.

**5. Building the Server**

*   Run the build script defined in your `package.json`:
    ```bash
    npm run build
    # or
    yarn build
    ```
*   This typically runs `tsc` and outputs the compiled JavaScript files (like `index.js` and any others) into the `build/` directory.

**6. Configuration (`mcp_settings.json` or `.roo/mcp.json`)**

You need to tell RooCode how to run your built server. Edit the appropriate MCP settings file:

*   **Global:** `~/.roo/cline_mcp_settings.json` (path from `getMcpSettingsFilePath`)
*   **Project:** `<your_workspace_folder>/.roo/mcp.json` (path from `getProjectMcpPath`)

Add an entry for your server within the `mcpServers` object:

```json name=cline_mcp_settings.json
{
  "mcpServers": {
    "your-server-name": {
      "type": "stdio", // Explicitly state type (optional if command is present)
      "command": "node", // Command to execute
      "args": [
        // --- CRITICAL ---
        // Path to your BUILT index.js file.
        // Use absolute paths or paths relative to the settings file location or workspace root.
        // Example assuming settings file is in ~/.roo/ and server is in ~/.roo/mcp_servers/your-server-name/
        "/Users/your_username/.roo/mcp_servers/your-server-name/build/index.js"
        // Or potentially relative if cwd is set or known
        // "../mcp_servers/your-server-name/build/index.js"
      ],
      "env": {
        // Pass secrets/config needed by your server here
        "YOUR_SERVICE_API_KEY": "your_actual_api_key_or_token",
        "LOG_LEVEL": "debug"
      },
      "timeout": 60, // Optional: Request timeout in seconds (default: 60)
      "alwaysAllow": [], // Optional: List of tool names that don't require user confirmation
      "watchPaths": [
        // Optional: Paths RooCode should watch for changes to auto-restart server
        // Point to SOURCE files, not build output!
        "/Users/your_username/.roo/mcp_servers/your-server-name/src/index.ts",
        "/Users/your_username/.roo/mcp_servers/your-server-name/src/api-calls.ts"
      ]
    }
    // Add other servers here
  }
}
```

**Key Configuration Points:**

*   **`command`:** Usually `"node"`.
*   **`args`:** Must contain the path to the *compiled* JavaScript entry point (e.g., `build/index.js`). Make sure the path is correct relative to how RooCode will execute it. Absolute paths are often safest.
*   **`env`:** The *only* way to securely pass secrets (like API keys) to your server. Do not hardcode secrets in your source code. Your server reads these using `process.env.YOUR_VARIABLE_NAME`.
*   **`watchPaths`:** Extremely useful during development. Point these to your *source* files (`.ts`). When you save changes to these files, RooCode will automatically kill and restart the server process.

**7. Testing and Debugging**

1.  **Build:** Run `npm run build`.
2.  **Configure:** Add/update the server entry in your `mcp_settings.json`.
3.  **Restart RooCode/VS Code:** Ensure RooCode re-reads the settings (or it might pick it up automatically if watching the settings file).
4.  **Check RooCode MCP Panel:** Open the RooCode settings/panel where MCP servers are listed.
    *   Look for your server name.
    *   Check its status: `connecting`, `connected`, or `disconnected`.
    *   If `disconnected`, check the error message displayed in the RooCode UI. This often comes from `stderr` or connection failures.
5.  **Server Logs:** Add `console.log` and `console.error` statements in your `index.ts` handlers. RooCode's `McpHub` captures `stderr` output (see `McpHub.ts:485-504`) and often displays it in the UI when errors occur.
6.  **Manual Test (Optional):** You can try running the built server directly from your terminal (`node build/index.js`) to see if it starts without immediate errors, although it won't receive MCP requests this way.
7.  **Interact via RooCode:** Ask the RooCode AI to use one of your server's tools. Check if the request reaches your server (via logs) and if the response is processed correctly by RooCode.

**Must-Haves for Successful Integration:**

1.  **Correct Configuration:** RooCode must be able to find and execute your *built* server using the `command` and `args` in `mcp_settings.json`. Pathing is critical.
2.  **SDK Usage:** Use `@modelcontextprotocol/sdk/server` correctly to instantiate `Server` and `StdioServerTransport`.
3.  **Capability Handlers:** Implement request handlers for the capabilities you declare (e.g., `tools/list` and `tools/call` if you declare `tools: {}`).
4.  **Listen Call:** Your server must call `server.listen(transport)` to start accepting connections/requests over stdio.
5.  **Valid Responses:** Handlers must return results matching the expected MCP schema (e.g., `ListToolsResult`, `CallToolResult`). Errors should ideally be thrown as `McpError` with appropriate codes.
6.  **Environment Variables:** Rely on `process.env` (populated via the `env` field in `mcp_settings.json`) for any required configuration or secrets.

By following these steps, using the template, and carefully managing the configuration, you can create robust MCP servers to extend RooCode's capabilities. Remember to check the RooCode MCP panel for status and errors during development!