from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from services.config import DATA_DIR, config
from services.content_filter import request_text
from services.log_service import LOG_TYPE_CALL, log_service
from services.protocol import openai_v1_image_edit, openai_v1_image_generations, openai_v1_response

TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_ERROR = "error"
TERMINAL_STATUSES = {TASK_STATUS_SUCCESS, TASK_STATUS_ERROR}
UNFINISHED_STATUSES = {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING}
TASK_STATUS_LOG_LABELS = {
    TASK_STATUS_QUEUED: "排队中",
    TASK_STATUS_RUNNING: "正在生成",
}


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _timestamp(value: object) -> float:
    if not isinstance(value, str) or not value.strip():
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:26], fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _clean(value: object, default: str = "") -> str:
    return str(value or default).strip()


def _owner_id(identity: dict[str, object]) -> str:
    return _clean(identity.get("id")) or "anonymous"


def _task_key(owner_id: str, task_id: str) -> str:
    return f"{owner_id}:{task_id}"


def _collect_image_urls(data: list[Any]) -> list[str]:
    urls: list[str] = []
    for item in data:
        if isinstance(item, dict):
            url = item.get("url")
            if isinstance(url, str) and url:
                urls.append(url)
    return urls


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    item = {
        "id": task.get("id"),
        "status": task.get("status"),
        "mode": task.get("mode"),
        "model": task.get("model"),
        "size": task.get("size"),
        "quality": task.get("quality"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }
    if task.get("conversation_id"):
        item["conversation_id"] = task.get("conversation_id")
    if task.get("data") is not None:
        item["data"] = task.get("data")
    if task.get("response") is not None:
        item["response"] = task.get("response")
    if task.get("usage") is not None:
        item["usage"] = task.get("usage")
    if task.get("error"):
        item["error"] = task.get("error")
    if task.get("progress"):
        item["progress"] = task.get("progress")
    if task.get("duration_ms") is not None:
        item["duration_ms"] = task.get("duration_ms")
    if task.get("status") in (TASK_STATUS_RUNNING, TASK_STATUS_QUEUED):
        if task.get("status") == TASK_STATUS_RUNNING:
            # RUNNING 状态仅在 started_ts 被设置后（image_stream_resolve_start）才计时
            base_ts = task.get("started_ts")
        else:
            # QUEUED 状态从 created_ts 开始计时（排队等待中）
            base_ts = task.get("created_ts") or task.get("updated_ts")
        if base_ts:
            item["elapsed_secs"] = round(time.time() - base_ts, 1)
    return item


class ImageTaskService:
    def __init__(
        self,
        path: Path,
        *,
        generation_handler: Callable[[dict[str, Any]], dict[str, Any]] = openai_v1_image_generations.handle,
        edit_handler: Callable[[dict[str, Any]], dict[str, Any]] = openai_v1_image_edit.handle,
        response_handler: Callable[[dict[str, Any]], Any] = openai_v1_response.handle,
        retention_days_getter: Callable[[], int] | None = None,
    ):
        self.path = path
        self.generation_handler = generation_handler
        self.edit_handler = edit_handler
        self.response_handler = response_handler
        self.retention_days_getter = retention_days_getter or (lambda: config.image_retention_days)
        self._lock = threading.RLock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._tasks = self._load_locked()
            changed = self._recover_unfinished_locked()
            changed = self._cleanup_locked() or changed
            if changed:
                self._save_locked()

    def submit_generation(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        prompt: str,
        model: str,
        size: str | None,
        n: int = 1,
        quality: str = "auto",
        response_format: str = "url",
        base_url: str = "",
    ) -> dict[str, Any]:
        payload = {
            "prompt": prompt,
            "model": model,
            "n": max(1, min(4, int(n or 1))),
            "size": size,
            "quality": quality,
            "response_format": _clean(response_format, "url"),
            "base_url": base_url,
        }
        return self._submit(identity, client_task_id=client_task_id, mode="generate", payload=payload)

    def submit_edit(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        prompt: str,
        model: str,
        size: str | None,
        quality: str = "auto",
        response_format: str = "url",
        base_url: str = "",
        images: list[tuple[bytes, str, str]] | None = None,
        masks: list[tuple[bytes, str, str]] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "prompt": prompt,
            "images": images or [],
            "mask": masks or [],
            "model": model,
            "n": 1,
            "size": size,
            "quality": quality,
            "response_format": _clean(response_format, "url"),
            "base_url": base_url,
        }
        return self._submit(identity, client_task_id=client_task_id, mode="edit", payload=payload)

    def submit_response(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._submit(identity, client_task_id=client_task_id, mode="response", payload=dict(payload))

    def list_tasks(self, identity: dict[str, object], task_ids: list[str]) -> dict[str, Any]:
        owner = _owner_id(identity)
        requested_ids = [_clean(task_id) for task_id in task_ids if _clean(task_id)]
        with self._lock:
            if self._cleanup_locked():
                self._save_locked()
            items = []
            missing_ids = []
            for task_id in requested_ids:
                task = self._tasks.get(_task_key(owner, task_id))
                if task is None:
                    missing_ids.append(task_id)
                else:
                    items.append(_public_task(task))
            if not requested_ids:
                items = [
                    _public_task(task)
                    for task in self._tasks.values()
                    if task.get("owner_id") == owner
                ]
                items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
                missing_ids = []
            return {"items": items, "missing_ids": missing_ids}

    def list_events(
        self,
        identity: dict[str, object],
        task_id: str,
        after: int = 0,
    ) -> dict[str, Any]:
        owner = _owner_id(identity)
        clean_task_id = _clean(task_id)
        cursor = max(0, int(after or 0))
        with self._lock:
            task = self._tasks.get(_task_key(owner, clean_task_id))
            if task is None:
                raise ValueError("task not found")
            events = task.get("events")
            if not isinstance(events, list):
                events = []
            return {
                "events": events[cursor:],
                "next_cursor": len(events),
                "status": task.get("status"),
            }

    def record_finished(
        self,
        identity: dict[str, object],
        *,
        task_id: str,
        mode: str,
        payload: dict[str, Any],
        result: dict[str, Any] | None = None,
        error: str = "",
        started: float | None = None,
    ) -> dict[str, Any]:
        clean_task_id = _clean(task_id)
        if not clean_task_id:
            raise ValueError("task_id is required")
        owner = _owner_id(identity)
        key = _task_key(owner, clean_task_id)
        started_ts = float(started or time.time())
        now = _now_iso()
        status = TASK_STATUS_ERROR if error else TASK_STATUS_SUCCESS
        result = result if isinstance(result, dict) else {}
        task = {
            "id": clean_task_id,
            "owner_id": owner,
            "key_name": _clean(identity.get("name")),
            "role": _clean(identity.get("role")),
            "status": status,
            "mode": "edit" if mode == "edit" else "generate",
            "model": _clean(payload.get("model"), "gpt-image-2"),
            "size": _clean(payload.get("size")),
            "quality": _clean(payload.get("quality"), "auto"),
            "request_text": request_text(payload.get("prompt")),
            "created_at": datetime.fromtimestamp(started_ts).strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": now,
            "created_ts": started_ts,
            "updated_ts": time.time(),
            "started_ts": started_ts,
            "duration_ms": int((time.time() - started_ts) * 1000),
        }
        data = result.get("data")
        if isinstance(data, list):
            task["data"] = data
        usage = result.get("usage")
        if isinstance(usage, dict):
            task["usage"] = usage
        conversation_id = _clean(result.get("_conversation_id") or result.get("conversation_id"))
        if conversation_id:
            task["conversation_id"] = conversation_id
        if error:
            task["error"] = error
        with self._lock:
            self._tasks[key] = task
            self._save_locked()
        return _public_task(task)

    def _submit(
        self,
        identity: dict[str, object],
        *,
        client_task_id: str,
        mode: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        task_id = _clean(client_task_id)
        if not task_id:
            raise ValueError("client_task_id is required")
        owner = _owner_id(identity)
        key = _task_key(owner, task_id)
        now = _now_iso()
        should_start = False
        with self._lock:
            cleaned = self._cleanup_locked()
            task = self._tasks.get(key)
            if task is not None:
                if cleaned:
                    self._save_locked()
                return _public_task(task)
            task = {
                "id": task_id,
                "owner_id": owner,
                "key_name": _clean(identity.get("name")),
                "role": _clean(identity.get("role")),
                "status": TASK_STATUS_QUEUED,
                "mode": mode,
                "model": _clean(payload.get("model"), "gpt-image-2"),
                "size": _clean(payload.get("size")),
                "quality": _clean(payload.get("quality"), "auto"),
                "request_text": request_text(payload.get("prompt")),
                "created_at": now,
                "updated_at": now,
                "created_ts": time.time(),
            }
            self._tasks[key] = task
            self._save_locked()
            should_start = True

        if should_start:
            self._log_call(
                identity,
                mode,
                _clean(payload.get("model"), "gpt-image-2"),
                time.time(),
                "任务已创建",
                task_id=task_id,
                request_preview=request_text(payload.get("prompt")),
            )
            thread = threading.Thread(
                target=self._run_task,
                args=(key, mode, payload, dict(identity), _clean(payload.get("model"), "gpt-image-2")),
                name=f"image-task-{task_id[:16]}",
                daemon=True,
            )
            thread.start()
        return _public_task(task)

    def _run_task(
        self,
        key: str,
        mode: str,
        payload: dict[str, Any],
        identity: dict[str, object],
        model: str,
    ) -> None:
        started = time.time()
        task_id = key.split(":", 1)[1] if ":" in key else key
        self._update_task(key, status=TASK_STATUS_RUNNING, error="")
        # 创建进度回调，每个步骤完成后更新任务状态
        def progress_callback(step: str) -> None:
            if step == "image_stream_resolve_start":
                self._update_task(key, started_ts=time.time())
            self._update_task(key, progress=step)
        # 将进度回调添加到 payload 中（handler 会提取并传递给 ConversationRequest）
        payload_with_progress = {**payload, "progress_callback": progress_callback}
        try:
            if mode == "response":
                handler = self.response_handler
            elif mode == "edit":
                handler = self.edit_handler
            else:
                handler = self.generation_handler
            result = handler(payload_with_progress)
            if mode == "response":
                if not isinstance(result, dict):
                    final_response = None
                    for event in result:
                        if not isinstance(event, dict):
                            continue
                        self._append_event(key, event)
                        if event.get("type") == "response.completed" and isinstance(event.get("response"), dict):
                            final_response = event["response"]
                    if final_response is None:
                        raise RuntimeError("response stream ended without response.completed")
                    result = final_response
                duration_ms = int((time.time() - started) * 1000)
                self._update_task(
                    key,
                    status=TASK_STATUS_SUCCESS,
                    response=result,
                    error="",
                    duration_ms=duration_ms,
                )
                self._log_call(
                    identity,
                    mode,
                    model,
                    started,
                    "调用完成",
                    task_id=task_id,
                    request_preview=request_text(payload.get("input")),
                )
                return
            if not isinstance(result, dict):
                raise RuntimeError("image task returned streaming result unexpectedly")
            data = result.get("data")
            account_email = _clean(result.get("_account_email") or result.get("account_email"))
            if not isinstance(data, list) or not data:
                upstream = _clean(result.get("message"))
                if upstream:
                    message = upstream
                else:
                    message = "号池中没有可用账号或所有账号均被限流，请检查号池状态（账号额度、是否被封禁、是否到达生图上限）"
                error = RuntimeError(message)
                if account_email:
                    setattr(error, "account_email", account_email)
                raise error
            usage = result.get("usage")
            duration_ms = int((time.time() - started) * 1000)
            self._update_task(key, status=TASK_STATUS_SUCCESS, data=data, usage=usage, error="", duration_ms=duration_ms)
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用完成",
                task_id=task_id,
                request_preview=request_text(payload.get("prompt")),
                urls=_collect_image_urls(data),
                account_email=account_email,
            )
        except Exception as exc:
            error_message = str(exc) or "image task failed"
            account_email = _clean(getattr(exc, "account_email", ""))
            conversation_id = _clean(getattr(exc, "conversation_id", ""))
            duration_ms = int((time.time() - started) * 1000)
            self._update_task(key, status=TASK_STATUS_ERROR, error=error_message, data=[],
                              duration_ms=duration_ms,
                              **({"conversation_id": conversation_id} if conversation_id else {}))
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用失败",
                task_id=task_id,
                request_preview=request_text(payload.get("prompt")),
                status="failed",
                error=error_message,
                account_email=account_email,
            )

    def _log_call(
        self,
        identity: dict[str, object],
        mode: str,
        model: str,
        started: float,
        suffix: str,
        *,
        task_id: str = "",
        request_preview: str = "",
        status: str = "success",
        error: str = "",
        urls: list[str] | None = None,
        account_email: str = "",
    ) -> None:
        endpoint = "/v1/images/edits" if mode == "edit" else "/v1/images/generations"
        summary_prefix = "图生图" if mode == "edit" else "文生图"
        detail = {
            "key_id": identity.get("id"),
            "key_name": identity.get("name"),
            "role": identity.get("role"),
            "endpoint": endpoint,
            "model": model,
            "started_at": datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S"),
            "ended_at": _now_iso(),
            "duration_ms": int((time.time() - started) * 1000),
            "status": status,
        }
        if task_id:
            detail["task_id"] = task_id
        if request_preview:
            detail["request_text"] = request_preview
        if error:
            detail["error"] = error
        if account_email:
            detail["account_email"] = account_email
        if urls:
            detail["urls"] = list(dict.fromkeys(urls))
        try:
            log_service.add(LOG_TYPE_CALL, f"{summary_prefix}{suffix}", detail)
        except Exception:
            pass

    def _update_task(self, key: str, **updates: Any) -> None:
        with self._lock:
            task = self._tasks.get(key)
            if task is None:
                return
            task.update(updates)
            task["updated_at"] = _now_iso()
            task["updated_ts"] = time.time()
            self._save_locked()

    def _append_event(self, key: str, event: dict[str, Any]) -> None:
        with self._lock:
            task = self._tasks.get(key)
            if task is None:
                return
            events = task.setdefault("events", [])
            events.append(event)
            task["updated_at"] = _now_iso()
            task["updated_ts"] = time.time()
            self._save_locked()

    def _load_locked(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        raw_items = raw.get("tasks") if isinstance(raw, dict) else raw
        if not isinstance(raw_items, list):
            return {}
        tasks: dict[str, dict[str, Any]] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            task_id = _clean(item.get("id"))
            owner = _clean(item.get("owner_id"))
            if not task_id or not owner:
                continue
            status = _clean(item.get("status"))
            if status not in {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING, TASK_STATUS_SUCCESS, TASK_STATUS_ERROR}:
                status = TASK_STATUS_ERROR
            task = {
                "id": task_id,
                "owner_id": owner,
                "key_name": _clean(item.get("key_name")),
                "role": _clean(item.get("role")),
                "status": status,
                "mode": item.get("mode") if item.get("mode") in {"generate", "edit", "response"} else "generate",
                "model": _clean(item.get("model"), "gpt-image-2"),
                "size": _clean(item.get("size")),
                "quality": _clean(item.get("quality"), "auto"),
                "request_text": _clean(item.get("request_text")),
                "created_at": _clean(item.get("created_at"), _now_iso()),
                "updated_at": _clean(item.get("updated_at"), _clean(item.get("created_at"), _now_iso())),
                "created_ts": item.get("created_ts"),
                "updated_ts": item.get("updated_ts"),
                "started_ts": item.get("started_ts"),
                "duration_ms": item.get("duration_ms"),
            }
            data = item.get("data")
            if isinstance(data, list):
                task["data"] = data
            usage = item.get("usage")
            if isinstance(usage, dict):
                task["usage"] = usage
            response = item.get("response")
            if isinstance(response, dict):
                task["response"] = response
            events = item.get("events")
            if isinstance(events, list):
                task["events"] = [event for event in events if isinstance(event, dict)]
            error = _clean(item.get("error"))
            if error:
                task["error"] = error
            tasks[_task_key(owner, task_id)] = task
        return tasks

    def _save_locked(self) -> None:
        items = sorted(self._tasks.values(), key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps({"tasks": items}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(self.path)

    def _recover_unfinished_locked(self) -> bool:
        changed = False
        for task in self._tasks.values():
            if task.get("status") in UNFINISHED_STATUSES:
                task["status"] = TASK_STATUS_ERROR
                task["error"] = "服务已重启，未完成的图片任务已中断"
                task["updated_at"] = _now_iso()
                changed = True
        return changed

    def _cleanup_locked(self) -> bool:
        try:
            retention_days = max(1, int(self.retention_days_getter()))
        except Exception:
            retention_days = 30
        cutoff = time.time() - retention_days * 86400
        removed_keys = [
            key
            for key, task in self._tasks.items()
            if task.get("status") in TERMINAL_STATUSES and _timestamp(task.get("updated_at")) < cutoff
        ]
        for key in removed_keys:
            self._tasks.pop(key, None)
        return bool(removed_keys)

    def active_log_items(self, *, start_date: str = "", end_date: str = "") -> list[dict[str, Any]]:
        """Return unfinished image tasks as transient call-log rows."""
        with self._lock:
            tasks = [
                dict(task)
                for task in self._tasks.values()
                if task.get("status") in UNFINISHED_STATUSES
            ]
        items: list[dict[str, Any]] = []
        for task in tasks:
            updated_at = _clean(task.get("updated_at"), _now_iso())
            day = updated_at[:10]
            if start_date and day < start_date:
                continue
            if end_date and day > end_date:
                continue
            mode = "图生图" if task.get("mode") == "edit" else "文生图"
            status = _clean(task.get("status"), TASK_STATUS_RUNNING)
            base_ts = task.get("started_ts") if status == TASK_STATUS_RUNNING else task.get("created_ts")
            try:
                elapsed_base = float(base_ts or time.time())
            except (TypeError, ValueError):
                elapsed_base = time.time()
            elapsed_ms = int(max(0.0, time.time() - elapsed_base) * 1000)
            task_id = _clean(task.get("id"))
            items.append({
                "id": f"active-image-task:{_clean(task.get('owner_id'))}:{task_id}",
                "time": updated_at,
                "type": LOG_TYPE_CALL,
                "summary": f"{mode}{TASK_STATUS_LOG_LABELS.get(status, '正在生成')}",
                "detail": {
                    "key_id": task.get("owner_id"),
                    "key_name": task.get("key_name"),
                    "role": task.get("role"),
                    "endpoint": "/v1/images/edits" if task.get("mode") == "edit" else "/v1/images/generations",
                    "model": task.get("model"),
                    "task_id": task_id,
                    "started_at": task.get("created_at"),
                    "ended_at": updated_at,
                    "duration_ms": elapsed_ms,
                    "status": status,
                    "request_text": task.get("request_text"),
                    "progress": task.get("progress"),
                    "size": task.get("size"),
                    "quality": task.get("quality"),
                },
            })
        items.sort(key=lambda item: str(item.get("time") or ""), reverse=True)
        return items

    def resume_poll(
        self,
        identity: dict[str, object],
        task_id: str,
        extra_timeout_secs: float = 30.0,
    ) -> dict[str, Any]:
        """恢复对已超时任务的轮询，额外等待 extra_timeout_secs 秒。"""
        owner = _owner_id(identity)
        key = _task_key(owner, _clean(task_id))
        with self._lock:
            task = self._tasks.get(key)
            if task is None:
                raise ValueError("task not found")
            if task.get("status") != TASK_STATUS_ERROR:
                raise ValueError("task is not in error state")
            error_msg = _clean(task.get("error"))
            if "超时" not in error_msg:
                raise ValueError("task error is not a timeout error")
            conversation_id = _clean(task.get("conversation_id"))
            if not conversation_id:
                raise ValueError("task has no conversation_id")
            mode = task.get("mode", "generate")
            model = task.get("model", "gpt-image-2")
            # 将任务状态重置为 running
            self._update_task(key, status=TASK_STATUS_RUNNING, error="")

        # 启动新线程继续轮询
        thread = threading.Thread(
            target=self._run_resume_poll,
            args=(key, conversation_id, extra_timeout_secs, dict(identity), mode, model),
            name=f"image-resume-{_clean(task_id)[:16]}",
            daemon=True,
        )
        thread.start()
        return _public_task(task)

    def _run_resume_poll(
        self,
        key: str,
        conversation_id: str,
        extra_timeout_secs: float,
        identity: dict[str, object],
        mode: str,
        model: str,
    ) -> None:
        """后台线程：继续轮询已有 conversation_id 的图片结果。"""
        started = time.time()
        task_id = key.split(":", 1)[1] if ":" in key else key
        backend = None
        try:
            from services.openai_backend_api import OpenAIBackendAPI
            from services.protocol.conversation import format_image_result

            backend = OpenAIBackendAPI(proxy_url=config.proxy_url or None)
            file_ids, sediment_ids = backend._poll_image_results(
                conversation_id,
                extra_timeout_secs,
            )
            if not file_ids and not sediment_ids:
                raise RuntimeError(
                    f"继续等待 {extra_timeout_secs} 秒后仍未找到图片结果。"
                )

            image_urls = backend.resolve_conversation_image_urls(
                conversation_id, file_ids, sediment_ids, poll=False,
            )
            if not image_urls:
                raise RuntimeError("图片 URL 解析失败")

            image_items = [
                {"b64_json": __import__("base64").b64encode(image_data).decode("ascii")}
                for image_data in backend.download_image_bytes(image_urls)
            ]
            # 获取 task 的原始 prompt（从 _public_task 的 mode 判断）
            with self._lock:
                task = self._tasks.get(key)
                quality = _clean(task.get("quality"), "auto") if task else "auto"
                size = _clean(task.get("size")) if task else None
            data = format_image_result(
                image_items,
                "",  # prompt 已不重要，结果已经拿到了
                "b64_json",
                "",
                int(time.time()),
            )["data"]
            self._update_task(key, status=TASK_STATUS_SUCCESS, data=data, error="", duration_ms=int((time.time() - started) * 1000))
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用完成（续轮询）",
                task_id=task_id,
                status="success",
                urls=_collect_image_urls(data),
            )
        except Exception as exc:
            error_message = str(exc) or "resume poll failed"
            duration_ms = int((time.time() - started) * 1000)
            self._update_task(key, status=TASK_STATUS_ERROR, error=error_message, data=[], duration_ms=duration_ms)
            self._log_call(
                identity,
                mode,
                model,
                started,
                "调用失败（续轮询）",
                task_id=task_id,
                status="failed",
                error=error_message,
            )
        finally:
            if backend is not None:
                backend.close()


image_task_service = ImageTaskService(DATA_DIR / "image_tasks.json")
