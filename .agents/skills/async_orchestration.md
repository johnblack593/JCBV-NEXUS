# Skill: Async Event Loop Orchestration (async_orchestration.md)

This skill manages high-concurrency event loops in Python for HFT applications.

## Capabilities
- **Event Loop Starvation Detection**: Identifying blocking calls in pure `asyncio` code.
- **Thread/Process Offloading**: Offloading CPU-bound tasks (Pandas, ML inference) to worker threads or processes via `asyncio.to_thread` or `ProcessPoolExecutor`.
- **WebSocket Heartbeat Integrity**: Ensuring network-intensive tasks are prioritized to avoid broker timeouts.
- **Locking & Synchronization**: Managing shared state across concurrent coroutines.

## Usage Guidelines
- Never run `time.sleep` or heavy Pandas calculations directly in the main event loop.
- Use `asyncio.gather` for parallel I/O tasks (fetching market data from multiple assets).
- Maintain a stable loop frequency for HFT signal generation.
