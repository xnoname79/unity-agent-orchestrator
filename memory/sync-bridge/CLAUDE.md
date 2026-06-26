Remember always fetch all tools from subscribed MCP server before starting the chat.

This project is a **SOURCE|TARGET** for sync-bridge MCP server.

## sync-bridge MCP Server Rules (MANDATORY)

### Status Lifecycle
Specs flow through these statuses during collaboration:

```
pending → discuss → confirmed → done
```

- **pending**: Mới tạo, chờ phía đối tác xử lý
- **discuss**: Đang trao đổi qua lại giữa SOURCE và TARGET (cần thêm thông tin, góp ý, chỉnh sửa)
- **confirmed**: Cả hai bên đã thống nhất spec, sẵn sàng implement
- **done**: Đã implement và integrate xong

Use `get_pending_requirements()` (không param) để lấy tất cả specs chưa done.
Use `get_pending_requirements(status="discuss")` để lọc theo status cụ thể.

### Role: SOURCE (API provider — e.g. BE, API server)
After EVERY code change that affects an API (new endpoint, updated request/response, changed behavior):
1. Call `add_api_requirement` or `update_api_requirement` to sync the change with target.
2. Include in description: request format, response format, and status ([NEW], [UPDATED], [DONE]).
3. At the end of a task, call `list_api_requirements` to verify all specs are up to date.
4. When target marks items as done, do NOT revert their status.
5. When discussing a spec, set status to "discuss" and update description with feedback/questions.
6. When both sides agree, set status to "confirmed".

### Role: TARGET (API consumer — e.g. FE, client, external service)
When needing a new API or changes to an existing API:
1. Call `add_api_requirement` with endpoint, method, and detailed description of what is needed.
2. Include in description: expected request/response format, which component will use it.
3. Call `get_pending_requirements` to check all active (non-done) specs.
4. When integration is done for a spec, call `update_api_requirement` to set status to "done".
5. When a spec needs discussion, set status to "discuss" and update description with questions/feedback.

### Auto-sync (Real-time notification)
The sync-bridge server runs as a shared HTTP server. Both SOURCE and TARGET connect to the same instance.
Both sides can trigger updates AND wait for updates from the other side — the flow is **symmetric**.

**Workflow for BOTH roles:**
1. At the start of a session, call `watch_for_changes()` to check for any pending updates from the other side.
2. Process the returned changes (update code, respond to questions, etc.).
3. When you make a change (add/update spec), the other side's `watch_for_changes` is automatically triggered.
4. After processing, call `watch_for_changes(since="<last_timestamp>")` to block and wait for the next change from the other side.
5. When `watch_for_changes` returns new changes, process them and loop back to step 4.

Any call to `add_api_requirement`, `update_api_requirement`, or `reset_api_requirements` from either side automatically wakes up the other side's `watch_for_changes`.

This is a BLOCKING requirement — do not consider an API task complete until specs are synced.
