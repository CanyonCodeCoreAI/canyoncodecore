# Epigenomics Example

A synthetic, LLM-free workflow modeled on the
[WfCommons Epigenomics recipe](https://docs.wfcommons.org/en/latest/generating_workflows.html):
a split → fan-out (filter → align → sort) → fan-in (dedup) → index pipeline.
Every stage does deterministic SHA-256 work sized off chunk byte counts, so
results are reproducible and the fan-out width scales with `num_chunks` --
useful for exercising scheduling/replica behavior locally without any real
model calls.

## Pipeline

```
SplitAgent  --> FilterAgent --> MapAgent --> SortAgent --\
 (1 call)      (fan-out)       (fan-out)     (fan-out)    --> DedupAgent --> IndexAgent
                                                           (fan-in barrier)   (1 call)
```

- **SplitAgent** — splits `input_size` bytes into `num_chunks` equal chunks.
- **FilterAgent** — per-chunk contaminant filter (light cost).
- **MapAgent** — per-chunk alignment, the heaviest stage.
- **SortAgent** — per-chunk sort (moderate cost).
- **DedupAgent** — merges every sorted chunk into one digest (the barrier).
- **IndexAgent** — builds the final index from the merged digest.

## Quick Start

```bash
# Build stubs and Docker images
ventis build

# Launch all agents
ventis deploy

# Test with curl
curl -X POST http://<workflow_host_ip>:8080/main \
     -H 'Content-Type: application/json' \
     -d '{"input_size": 65536, "num_chunks": 4}'

# Check result
curl http://<workflow_host_ip>:8080/status/<request_id>
```

## Project Structure

```
├── agents/               # Agent implementations and YAML definitions
│   ├── split_agent.py/.yaml
│   ├── filter_agent.py/.yaml
│   ├── map_agent.py/.yaml
│   ├── sort_agent.py/.yaml
│   ├── dedup_agent.py/.yaml
│   └── index_agent.py/.yaml
├── workflow/              # Workflow script (deployed as a REST API)
│   └── epigenomics_workflow.py
└── config/
    ├── global_controller.yaml   # Deployment configuration (provider: local)
    └── policy.yaml               # Access control rules
```

## Policy Rules

Edit `config/policy.yaml` to control which callers can access which agents.
Pass `_context` in your curl request to set the caller identity:

```bash
curl -X POST http://localhost:8080/main \
     -H 'Content-Type: application/json' \
     -d '{"input_size": 65536, "num_chunks": 4, "_context": {"origin": "admin"}}'
```
