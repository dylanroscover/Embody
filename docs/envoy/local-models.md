# Local Models & Open Clients

Envoy is a standard MCP server — any MCP-compatible client can drive TouchDesigner through it, including open clients running **local models**. This page covers the two clients that matter most for that workflow — **OpenCode** and **LM Studio** — plus practical model recommendations and the settings that make small models reliable against Envoy's tool surface.

Everything runs on your machine: TouchDesigner, the model, and the agent. No account, no API key, no cloud.

## OpenCode

[OpenCode](https://opencode.ai/) is the most widely adopted open coding agent, with first-class support for local models through LM Studio, Ollama, llama.cpp, or any OpenAI-compatible endpoint.

### Automatic setup (recommended)

Set the **AI Client** parameter on the Embody COMP to `opencode` (or pick OpenCode in the setup wizard). Embody generates `opencode.json` in your project root with:

- **`mcp.envoy`** — spawns the same STDIO bridge Claude Code uses, so OpenCode gets the bridge meta-tools (`get_td_status`, `launch_td`, `restart_td`, `switch_instance`), a cached tool list while TouchDesigner is closed, automatic reconnection, and instance-registry identity checks.
- **`instructions`** — loads the generated `.claude/rules/*.md` alongside `AGENTS.md`. (OpenCode reads `AGENTS.md` natively and discovers `.claude/skills/` through its Claude-compatibility layer, so the full rule-and-skill set carries over with one copy on disk.)
- A **`permission`** block matching your **Tool Permissions** posture (only when the file is created fresh — an existing `opencode.json` is merged into, never overwritten).

`opencode.json` embeds absolute paths, so Embody gitignores it and mirrors it into sibling `*-wt-*` worktrees, exactly like `.mcp.json`.

### Manual setup

Point OpenCode at a running session directly (simplest, but no bridge features — the connection dies when TouchDesigner closes):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "envoy": {
      "type": "remote",
      "url": "http://127.0.0.1:9870/mcp",
      "enabled": true
    }
  }
}
```

Use `127.0.0.1`, not `localhost` — on Windows, `localhost` can resolve to IPv6 first and stall every request for ~2 seconds.

### Connecting a local model

OpenCode configures local providers in the same `opencode.json`. LM Studio example (from the [OpenCode docs](https://opencode.ai/docs/providers/)):

```json
{
  "provider": {
    "lmstudio": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "LM Studio",
      "options": { "baseURL": "http://127.0.0.1:1234/v1" },
      "models": {
        "qwen/qwen3-coder-30b": { "name": "Qwen3 Coder 30B" }
      }
    }
  }
}
```

Ollama is the same pattern with `baseURL: http://127.0.0.1:11434/v1`; llama.cpp's `llama-server` with `:8080/v1`.

## LM Studio as an MCP host

If you'd rather not use a terminal at all: [LM Studio](https://lmstudio.ai/) (0.3.17+) can host MCP servers directly in its chat UI — a local model with Envoy's tools, no CLI. Add this to LM Studio's `mcp.json` (Program tab → Install → Edit mcp.json):

```json
{
  "mcpServers": {
    "envoy": {
      "url": "http://127.0.0.1:9870/mcp"
    }
  }
}
```

LM Studio asks for confirmation before each tool call by default. Note its `mcp.json` is global to LM Studio (not per-project), so this connects whichever TouchDesigner project's Envoy is on that port.

## Which model? (snapshot: July 2026)

Tool-calling quality matters more than raw parameter count, and **MoE models with small active-parameter counts are the key to good speed on modest hardware** — a dense 27–31B model that spills into system RAM crawls, while an 80B MoE with 3B active parameters stays fast. Current picks:

| Hardware | Models that work well |
|---|---|
| 16 GB VRAM laptop | gpt-oss-20b (fits fully, ~60K context), GLM-4.7-Flash (30B-A3B), Gemma 4 26B-A4B |
| 16 GB VRAM + 64 GB RAM | gpt-oss-120b or **Qwen3-Coder-Next (80B-A3B)** via llama.cpp `--n-cpu-moe` — big-model quality at ~15–25 tok/s |
| 24–32 GB VRAM | Qwen3.6-35B-A3B or Qwen3-Coder-30B-A3B fully resident; Devstral Small 2 (24B dense) |
| 96 GB (RTX PRO 6000-class) | gpt-oss-120b fully in VRAM (~130+ tok/s), Qwen3-Coder-Next at high quants |
| Apple Silicon 64–128 GB | GLM-4.5-Air, gpt-oss-120b (MLX) |

Qwen3-Coder-Next is the current local benchmark leader for agentic coding (70.6% SWE-bench Verified, Apache 2.0). For creative TouchDesigner direction — as opposed to mechanical code edits — bigger models are noticeably better; 12B-class models handle Python well but plateau on aesthetic judgment.

**Cheap cloud fallbacks** that pair well with a local setup: GLM-4.7-Flash is free on Z.ai's API, DeepSeek V4 is $0.30/M input tokens, and OpenRouter's `:free` models allow 50 requests/day (1,000/day once you've ever bought $10 of credit).

## Settings that make small models reliable

These four fix the vast majority of "my local model won't use the tools" reports:

1. **Context window ≥ 16K, ideally 32K.** Ollama's default context is 4,096 tokens and it **truncates silently** — the system prompt plus tool schemas already exceed that, so the model literally never sees Envoy's tools. Set `num_ctx`/`OLLAMA_CONTEXT_LENGTH` (or the equivalent in LM Studio) to 16–32K minimum.
2. **Quantization Q5/Q6 or better.** Q4 quants degrade tool-call JSON validity and multi-step consistency faster than chat quality suggests. KV cache at q8 is essentially free; avoid q4 V-cache for agent work.
3. **llama.cpp needs `--jinja`.** Without it, tool-call delimiters pass through as literal text.
4. **Trim the tool surface.** Envoy exposes 50+ tools ≈ 15–25K tokens of schemas — on a 32K local context that's most of the window gone, and research shows tool *selection* accuracy degrades sharply as tool count grows. In OpenCode, the top-level `tools` map controls which tool schemas reach the model (the `permission` map only governs call approval — a denied tool's schema still occupies context). Disable the full surface with a glob, then re-enable a lean core:

```json
{
  "tools": {
    "envoy_*": false,
    "envoy_get_td_status": true,
    "envoy_query_network": true,
    "envoy_read_tdn": true,
    "envoy_get_op": true,
    "envoy_get_op_errors": true,
    "envoy_get_parameter": true,
    "envoy_set_parameter": true,
    "envoy_create_op": true,
    "envoy_connect_ops": true,
    "envoy_delete_op": true,
    "envoy_batch_operations": true,
    "envoy_execute_python": true,
    "envoy_get_network_layout": true,
    "envoy_set_op_position": true,
    "envoy_capture_top": true,
    "envoy_get_dat_content": true,
    "envoy_edit_dat_content": true,
    "envoy_get_docs": true
  }
}
```

That 18-tool core covers inspect, build, wire, position, verify, and capture — enough for real work while leaving the context to the model.

## Expectations

A well-configured local setup handles operator work, parameter edits, Python, and network building competently. Long multi-step autonomy and creative direction still favor larger models — many people run local for iteration and switch to a hosted model for the hard steps. Both connect to the same Envoy; nothing about the project changes.
