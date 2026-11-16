# alexandrie-mcp

An [MCP](https://modelcontextprotocol.io) server (SSE transport) that gives an AI assistant read/write access to an
[Alexandrie](https://github.com/Smaug6739/Alexandrie) documentation instance: list and search nodes, read and write
Markdown documents, create categories, replace text inside a document, compile Markdown to HTML.

## Security notes (read first)

- **The MCP endpoint has no authentication of its own.** It logs in to Alexandrie with one account and exposes everything
  that account can do. Bind it to localhost or a private network (`MCP_HOST`, default `127.0.0.1`) and never expose it publicly.
- `ALEXANDRIE_USER` and `ALEXANDRIE_PASS` are **required**; there are no defaults.
- Auto-publishing is **off by default**: new pages get Alexandrie's own default accessibility. `ALEXANDRIE_AUTO_PUBLISH=1` makes every node readable without login.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `ALEXANDRIE_API` | `http://alexandrie-backend:8201/api` | Backend API URL |
| `ALEXANDRIE_USER` / `ALEXANDRIE_PASS` | required | Account the server uses |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8202` | Listen address |
| `ALEXANDRIE_COMPILE_INTERVAL` | `300` | Seconds between background Markdown compile passes |
| `ALEXANDRIE_AUTO_PUBLISH` | `0` | `1` starts the publish loop |
| `ALEXANDRIE_PUBLISH_ACCESSIBILITY` | `3` | Accessibility value used for "public" |
| `ALEXANDRIE_PUBLISH_INTERVAL` | `120` | Seconds between publish passes |
| `ALEXANDRIE_PORTAL_PARENT` | empty | Workspace id; when set, a generated index page is maintained there |
| `ALEXANDRIE_PORTAL_START`, `ALEXANDRIE_PORTAL_OVERVIEWS` | empty | Comma-separated node ids pinned in the portal |

## Run

```
docker build -t alexandrie-mcp .
docker run -d --name alexandrie-mcp --network <alexandrie network> -p 127.0.0.1:8202:8202 \
  -e MCP_HOST=0.0.0.0 -e ALEXANDRIE_USER -e ALEXANDRIE_PASS alexandrie-mcp
```

Pass the credentials from your own environment or secret store; do not commit them. Then add `http://127.0.0.1:8202/sse`
as an MCP server in your client.

## License

Apache-2.0, see `LICENSE`.
