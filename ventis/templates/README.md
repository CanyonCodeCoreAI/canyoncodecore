# My Ventis Project

A distributed agent orchestration project built with [Ventis](https://github.com/ventis).

## Quick Start

```bash
# Build stubs and Docker images
ventis build

# Launch all agents
ventis deploy

# Test with curl
curl -X POST http://localhost:8080/main \
     -H 'Content-Type: application/json' \
     -d '{"name": "World"}'

# Check result
curl http://localhost:8080/status/<request_id>
```

## Project Structure

```
├── agents/               # Agent implementations and YAML definitions
│   ├── example_agent.py
│   └── example_agent.yaml
├── workflows/            # Workflow scripts (deployed as REST APIs)
│   └── example_workflow.py
├── config/
│   ├── global_controller.yaml   # Deployment configuration
│   └── policy.yaml              # Access control rules
├── stubs/                # Generated agent stubs (auto-generated)
├── grpc_stubs/           # Generated gRPC stubs (auto-generated)
└── Makefile
```

## Adding a New Agent

1. Create `agents/my_agent.yaml` with the agent interface definition
2. Create `agents/my_agent.py` with the implementation class
3. Add the agent entry to `config/global_controller.yaml`
4. Run `ventis build` to regenerate stubs and Docker images
5. Run `ventis deploy` to launch

Set the agent's startup queue priority in its YAML:

```yaml
agent:
  name: ExampleAgent
  priority: 0   # lower number = higher priority
```

## Policy Rules

Edit `config/policy.yaml` to control which callers can access which agents.
Pass `_context` in your curl request to set the caller identity:

```bash
curl -X POST http://localhost:8080/main \
     -H 'Content-Type: application/json' \
     -d '{"name": "World", "_context": {"origin": "admin", "session_priority": -5}}'
```

`session_priority` is optional and defaults to `0`. Lower numbers run first.
