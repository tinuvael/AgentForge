# Local inference with llama.cpp

The primary local setup uses `llama-server` through AgentForge's existing
`openai_compatible` adapter. This changes operator examples, not runtime routing.
Ollama remains supported with `config/workers.ollama.example.toml`; existing
private configurations are never rewritten automatically.

## Install and launch on Windows

Download a Windows x64 CUDA build from the official
[llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases), following
that release's runtime prerequisites. Keep the executable and its required DLLs
together. Verify `llama-server.exe --version`, `--help` and `--list-devices`.
Record the build/release, GPU/driver and actual command line.

Select a tool-capable model in GGUF format from its publisher or a verified
conversion source. Record the repository revision, exact filenames, quantization
and SHA-256 of every shard. The earlier `gpt-oss:20b` is an Ollama tag, not a
GGUF filename. GPT-OSS 20B remains a candidate, but its GGUF/template and memory
use must be checked on the target PC. No model download is performed by AgentForge.

Example PowerShell command (replace both absolute paths):

```powershell
& 'C:\llama.cpp\llama-server.exe' -m 'C:\Models\selected-model.gguf' --alias local-model --jinja --host 127.0.0.1 --port 8080 -c 8192 -ngl 99
```

Keep this terminal running separately from Gateway. `-ngl 99` requests GPU layers;
it does not prove that all weights/context fit. Check the installed build's help,
startup offload logs and `nvidia-smi` under load. Record actual context, slots and
VRAM. Adjust deliberately on OOM; update the Worker context to match. Start with
one request at a time. The 8192 context is an initial smoke setting, not a claim
that larger requests fit or that this is the best performance configuration.

`--jinja` enables the tool-use template path. Some models require a reviewed
`--chat-template-file`; preserve the chosen template/revision. See upstream
[function calling](https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md)
and [server options](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
Only model inference runs in llama-server; AgentForge executes its own allowlisted
repository tools. Do not enable a separate server-side agent/tool execution layer.

## Bind Worker and Director

Copy `config/workers.example.toml` to private state. It selects connection
`local-llama`, base URL `http://127.0.0.1:8080/v1`, Worker `local-4080`, model alias
`local-model`. The adapter appends `chat/completions`; do not append it to base_url.
Worker identity stays explicit. Grant this Worker to the intended Gateway clients.

Local Director uses the same endpoint and alias with its own configuration in
[local-director.md](local-director.md). Both share server resources; measure
queueing and contention. No automatic model selection or fallback is introduced.
Keep inference bound to loopback for the initial setup. Remote clients connect
to the authenticated AgentForge Gateway. A separate Mac-local Application using
Windows inference needs an explicitly secured/reachable endpoint and trust policy.

## Acceptance before declaring readiness

1. Validate configuration and list Workers using the installed CLI. Compatible
   `worker check` reports `not_probed`; this is not evidence of failure or success.
2. Use existing worker diagnostics (`worker probe --help`) to check actual
   generation, streaming and native tool capability; retain observed results.
3. Through authenticated MCP, run Explorer on a disposable committed Project:
   actual `read_file`/search call, tool execution, then a second model turn citing
   the known source. A plausible text-only answer is insufficient.
4. Exercise isolated Coder plus a real required validator, then Local Director.
   Record failures, model/build/template/hash and real Task/workflow IDs in #31.
5. Measure cold/warm latency, GPU/CPU offload and VRAM. For ComfyUI switching,
   stop the owned llama-server process and confirm VRAM release before image
   generation; restart the identical command afterward. Do not assume simultaneous
   residency. Stopping inference during a Task can fail that Task; do this only
   at a deliberate idle boundary or as an explicitly recorded failure test.

Mocked OpenAI-compatible tests verify protocol contracts. They do not establish
real llama.cpp/model compatibility, tool quality or target-PC performance.
