# PM Studio test server

From the repository root, activate the project Python environment and run:

```powershell
.\start_pm_studio.ps1
```

Open the Studio link printed for port 2025 and select `pm_agent_studio`.
This is a separate development server with a separate working directory and
an in-memory project and memory store. Each Studio thread receives its own
reserved project scope. Project-state writes, ACP workers, background memory
jobs and production trajectory recording are disabled in this test graph.
The model still makes real calls using the configured EasyCLI endpoint.
Store data is ephemeral; local Studio thread/checkpoint files stay under this
directory. The standard `pm_agent` on the original server continues to require
an explicit project ID and cannot use the reserved `studio-thread:` prefix.
