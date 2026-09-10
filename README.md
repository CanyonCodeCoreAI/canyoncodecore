<p align="center">
  <img src="images/canyonos-banner.gif" alt="CanyonOS" width="600">
</p>

## CanyonOS turns plain Python into a running, distributed workflow — without changing a line of code.

CanyonOS is a control plane that takes your agentic workflow and deploys it, providing observability and managing distributed deployment.  Maintained by [Canyon Code](https://canyoncode.ai/).

## Difference

<table>
<tr>
<th width="50%"> Normal Workflow Deployment</th>
<th width="50%"> canyonos deploy</th>
</tr>
<tr>
<td valign="top">

Orchestration: Install Kubernetes/Docker Compose for distributed deployment and management

Observability: Install Langfuse/Arize Phoenix for LLM Observability

Execution: Install Ray or Kuberay to manage async task execution

</td>
<td valign="top">

uv tool install canyonos

canyonos build

canyonos deploy

</td>
</tr>
</table>

Same deployment. Same managament. Same observability.

## Core Features
- **Easy deployment**: Developers write agents in python as if writing completely localized code. CanyonOS takes care of distributed deployment of agents and workflows.
- **Complete Observability**: All metrics, logs, and traces from your runtime are collected and visualized, with OTel compatability allowing connection to any OTel-compatable frontend
- **Fully Asynchronous**: Asynchronous execution built in, without any user workflow modification.
- **Non-invasive**: None of your existing code is changed, a new folder (.car) is created when building on top of a existing workflow

---

## Requirements
- [Docker](https://docs.docker.com/desktop/) with Compose v2 — used to manage everything
- Optional: A coding agent in terminal (used only by canyonos build to convert workflow to canyonos compatible format) — [Claude Code CLI](https://code.claude.com/docs/en/overview) or [Codex CLI](https://learn.chatgpt.com/docs/codex/cli#getting-started)

## Installation

Use any of the following package managers to install the canyonos CLI (curl, brew, uv, pip):

```bash
curl -fsSL https://raw.githubusercontent.com/CanyonCodeCoreAI/canyoncodecore/main/cli/install.sh | sh
# OR
brew tap CanyonCodeCoreAI/canyonos https://github.com/CanyonCodeCoreAI/canyoncodecore
brew install canyonos
# OR
uv tool install canyonos
# OR
pipx install canyonos
```

Run canyonos doctor to verify all prerequisites are set up before your first deploy:

```bash
canyonos doctor
```

---

## Commands

### Essentials

| Command | What it does |
|---|---|
| `build` | Convert your project to CanyonOS format using your coding agent |
| `deploy` | Build images, launch the workflow, start the dashboard |
| `config` | View or edit the project config |

### Utility

| Command | What it does |
|---|---|
| `status` | Show live workflow endpoints |
| `test` | Send a test prompt to the running workflow |
| `logs` | Re-attach to the deploy log stream |
| `serve` | Start the local dashboard separately |
| `stop` | Stop the running workflow, keep the container |
| `quit` | Full teardown — remove the container and workspace |
| `clean` | Remove generated build artifacts |
| `doctor` | Check that your environment is ready |
| `new-app` | Scaffold a new project |
| `version` | Print the installed version |

---

## Usage

### Overview:
Our framework involves creating a global controller that is responsible for managing all config changes, deployment, and any orchestration that happens with your workflow. Running deploy will spawn this controller in the same machine that you run canyonos deploy in.
For each agent being deployed, they all get created with their own local controller, which handles requests being sent in/out of the agent it manages. This controller gets spawned alongside every agent in the same agent container.

For all steps below, commands should be ran in the directory of your project folder
```bash
cd my-project
```

### 1. Build your project

`canyonos build` installs the CanyonOS skill into your coding agent and launches it with a prompt to convert your project into `.car/` — CanyonOS's deploy-ready format.

```bash
canyonos build
```
The agent runs, reads your code, and produces a `.car/` folder. When it's done, exit back into the terminal, you're now ready to deploy.
The agent will also periodically ask questions to configure your deployment file for you. If you want to change configuration details afterwards, go to step 5. If the config suits your taste, continue.

### 2. Test

Verify the workflow works by deploying everything locally and sending test queries:

```bash
canyonos test {input query}
```

For CI, use `--json` to get a single result object and exit with a non-zero code on failure:

```bash
canyonos test "Hello World!" --json
```

### 3. Deploy

Deploy the project fully, configured by the config files.
On deploy success, a `POST` endpoint will be returned, in which you can send your workflow queries to.

```bash
canyonos deploy
```

Example Success Message:
```
 ┌─ Deploy is live ─────────────────────────────────┐
 │  Dashboard   http://localhost:8080               │
 │  POST        http://localhost:8000/main          │
 └──────────────────────────────────────────────────┘
```
- If you forgot any endpoint, type `canyonos status` to get the endpoints
- The dashboard opens automatically. If you want the raw build output instead of the progress summary, add a `-v` flag to the end of canyonos deploy:
- Every `canyonos deploy` automatically tears down any previous workflow too, so you can also just redeploy directly.

### 4. Sending requests to the workflow

Upon running the deploy command, canyonos automatically generates a REST API endpoint for the workflow. Send requests to this endpoint to trigger the workflow:

```bash
curl -X POST http://localhost:8000/main \
  -H "Content-Type: application/json" \
  -d '{
    "query": "AAPL"
  }'
```
You should get a `request_id`, this request_id is async, and will be updated with the answer when complete.
To get the result, use:

```bash
curl http://localhost:8000/status/<request_id>
```

### 5. Edit config

```bash
canyonos config
```

Config-only changes (no code edits) reload in place — no redeploy needed. If you change workflow source files, you'll need to redeploy from scratch.

#### Configuring the Global Controller (in progress)

Edit `.car/config/global_controller.yaml` to list the agents you want to deploy, their `provider`, `replicas`, and resource limits. Add a per-agent `requirements: [pkg, ...]` list for any extra pip packages that agent's code imports — only a small base list (grpc, redis, pyyaml, psutil, etc.) is installed by default.

Agents that need API keys read them from environment variables. Point `env_file` at a `.env` file to have CanyonOS inject it into every agent container:

```yaml
# .car/config/global_controller.yaml
env_file: .env
```

If you are deploying agents and tools to multiple hosts, make sure the hosts are reachable from the machine running the deploy command and that SSH key-based access is already configured. A guide to set that up can be found [here](https://www.redhat.com/en/blog/passwordless-ssh).

### 6. Stop or quit

```bash
canyonos stop   # stop the workflow, keeps the global controller container and files, but stops all local controllers
canyonos quit   # full teardown — removes everything
```

### Clean generated files

Removes the .car folder:

```bash
canyonos clean
```

---

## Dashboard

`canyonos serve` starts the local dashboard as a separate compose stack. Deploy starts it automatically, but you can also launch it on its own:

```bash
canyonos serve
```

The dashboard shows OTLP traces emitted by your running workflow and is available at `http://127.0.0.1:{dashboard_port}`.
- dashboard_port is automatically 8081, but you can manually configure your own in config

For more details, please refer to our paper - [Nalar: An agent serving framework](https://arxiv.org/abs/2601.05109)

## Future Work
- **Dynamic Policy Updates**: Currently, policies are loaded as static yaml files at startup. We are actively working on adding mechanisms to dynamically update policies based on custom user code, allowing developers more flexible and dynamic policy management.

- **Agent Thread Safety**: The Local Controller now executes agent methods in a `ThreadPoolExecutor`. This means multiple requests can run concurrently on the same agent instance. Currently, agents are assumed to be stateless or thread-safe. If an agent has mutable shared state, concurrent calls could cause data corruption. Future improvements could include per-thread agent instances, a locking mechanism, or a configurable concurrency mode (e.g., serial vs. parallel execution per agent).

- **Stale Future Detection**: If an agent process crashes mid-execution, a Future's result may never be available, causing indefinite waiting for the result. We currently have a time-out based mechanism; in future we will add customizable retry policies.

### Citation
If you find CanyonOS (Nalar) useful for your research, please cite our paper:
```bibtex
@misc{laju2026nalar,
      title={Nalar: An agent serving framework}, 
      author={Marco Laju and Donghyun Son and Saurabh Agarwal and Nitin Kedia and Myungjin Lee and Jayanth Srinivasa and Aditya Akella},
      year={2026},
      eprint={2601.05109},
      archivePrefix={arXiv},
      primaryClass={cs.DC},
      url={https://arxiv.org/abs/2601.05109}, 
}
```

## License

This project is licensed under the GNU Affero General Public License v3.0 - see the [LICENSE](LICENSE) file for details.
