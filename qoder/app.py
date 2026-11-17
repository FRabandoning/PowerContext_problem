"""StudyLoop: a review coach whose memory lives in PowerContext."""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import planner
import powercontext_client as pc

BASE = Path(__file__).resolve().parent
QUESTIONS: list[dict] = json.loads(
    (BASE / "data" / "questions.json").read_text(encoding="utf-8")
)["questions"]
BY_ID = {question["id"]: question for question in QUESTIONS}
SESSIONS: dict[str, dict[str, Any]] = {}

app = FastAPI(title="StudyLoop", version="0.1.0")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


class StartRequest(BaseModel):
    learner: str = Field(min_length=1, max_length=24)
    weak_only: bool = False


class AnswerIn(BaseModel):
    id: str
    choice: int


class SubmitRequest(BaseModel):
    learner: str = Field(min_length=1, max_length=24)
    session_id: str
    answers: list[AnswerIn]


def clean_name(raw: str) -> str:
    name = re.sub(r"[^\w\u4e00-\u9fff· -]", "", raw).strip()
    if not name:
        raise HTTPException(status_code=422, detail="学习者名称不能为空")
    return name[:24]


def public_question(question: dict) -> dict:
    return {
        "id": question["id"],
        "topic": question["topic"],
        "difficulty": question["difficulty"],
        "question": question["question"],
        "options": question["options"],
    }


def recall(scope_id: str, entries: list[dict]) -> list[dict]:
    recalled: dict[str, dict] = {}
    for entry in entries:
        kind = entry.get("kind", "memory")
        if kind not in ("weakness", "progress", "preference"):
            continue
        entry_id = entry["citation"]["entry_id"]
        recalled[entry_id] = {
            "entry_id": entry_id,
            "kind": kind,
            "text": entry.get("text", ""),
            "matched_by": [],
        }
    for query in ("薄弱点", "复习进度", "复习偏好"):
        try:
            hits = pc.search(scope_id, query, limit=10).get("hits", [])
        except pc.PowerContextError:
            hits = []
        for hit in hits:
            entry_id = hit["citation"]["entry_id"]
            if entry_id in recalled:
                recalled[entry_id]["matched_by"] = hit.get("matched_by", [])
                recalled[entry_id]["score"] = round(hit["score"], 4)
                continue
            recalled[entry_id] = {
                "entry_id": entry_id,
                "kind": "memory",
                "text": hit["text"],
                "score": round(hit["score"], 4),
                "matched_by": hit.get("matched_by", []),
            }
    return list(recalled.values())


def memory_write(scope_id: str, kind: str, text: str, reason: str) -> dict:
    result = pc.remember(scope_id, kind, text, reason)
    entry = result.get("entry") or {}
    return {
        "entry_id": entry.get("citation", {}).get("entry_id", ""),
        "kind": kind,
        "text": text,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/bootstrap")
def bootstrap(learner: str) -> dict:
    name = clean_name(learner)
    status = pc.health()
    if not status["ok"]:
        return {"pc_ok": False, "learner": name, "detail": status["detail"]}
    scope_id = pc.ensure_scope(name)
    entries = pc.list_entries(scope_id)
    return {
        "pc_ok": True,
        "learner": name,
        "scope_id": scope_id,
        "weak_topics": planner.weakness_counts(entries),
        "prefers_weak_only": planner.prefers_weak_only(entries),
        "memory_count": len(entries),
    }


@app.get("/api/memory")
def memory(learner: str) -> dict:
    name = clean_name(learner)
    scope_id = pc.ensure_scope(name)
    entries = pc.list_entries(scope_id)
    return {
        "scope_id": scope_id,
        "entries": [
            {
                "entry_id": entry["citation"]["entry_id"],
                "kind": entry.get("kind", "memory"),
                "text": entry.get("text", ""),
                "state": entry.get("state", "active"),
                "version": entry.get("version", 1),
            }
            for entry in entries
        ],
    }


@app.post("/api/session/start")
def start_session(request: StartRequest) -> dict:
    name = clean_name(request.learner)
    try:
        scope_id = pc.ensure_scope(name)
        entries = pc.list_entries(scope_id)
        plan, counts = planner.build_plan(QUESTIONS, entries, request.weak_only)
        recalled = recall(scope_id, entries)
        if request.weak_only and not planner.prefers_weak_only(entries):
            memory_write(
                scope_id,
                "preference",
                "复习偏好：只复习薄弱点，优先安排历史错题",
                "学习者在本轮选择只复习薄弱点",
            )
    except pc.PowerContextError as exc:
        raise HTTPException(status_code=503, detail=f"PowerContext 调用失败：{exc}") from exc

    session_id = "ses_" + secrets.token_hex(6)
    SESSIONS[session_id] = {
        "learner": name,
        "scope_id": scope_id,
        "question_ids": [question["id"] for question in plan],
    }
    return {
        "session_id": session_id,
        "learner": name,
        "weak_only": request.weak_only,
        "weak_topics": counts,
        "recalled": recalled,
        "questions": [public_question(question) for question in plan],
    }


@app.post("/api/session/submit")
def submit_session(request: SubmitRequest) -> dict:
    name = clean_name(request.learner)
    session = SESSIONS.get(request.session_id)
    if not session or session["learner"] != name:
        raise HTTPException(status_code=404, detail="复习会话不存在，请重新开始")
    scope_id = session["scope_id"]
    choices = {answer.id: answer.choice for answer in request.answers}
    details = []
    wrong_topics: list[str] = []
    wrong_items: list[dict] = []
    correct = 0
    for question_id in session["question_ids"]:
        question = BY_ID[question_id]
        selected = choices.get(question_id)
        ok = selected == question["answer"]
        correct += int(ok)
        details.append(
            {
                "id": question_id,
                "topic": question["topic"],
                "question": question["question"],
                "options": question["options"],
                "selected": selected,
                "answer": question["answer"],
                "correct": ok,
                "explanation": question["explanation"],
            }
        )
        if not ok:
            wrong_topics.append(question["topic"])
            wrong_items.append(question)

    try:
        entries = pc.list_entries(scope_id)
        existing = "\n".join(entry.get("text", "") for entry in entries)
        writes = []
        for question in wrong_items:
            marker = f"{question['id']} 错题"
            if marker in existing:
                continue
            writes.append(
                memory_write(
                    scope_id,
                    "weakness",
                    f"[{question['topic']}] {marker}：{question['question']} "
                    f"正确选项：{question['options'][question['answer']]}",
                    "本轮答错，记录为薄弱点",
                )
            )
        topics = "、".join(sorted(set(wrong_topics))) if wrong_topics else "无"
        writes.append(
            memory_write(
                scope_id,
                "progress",
                f"复习进度：{time.strftime('%Y-%m-%d %H:%M')} 完成一轮 "
                f"{len(details)} 题，答对 {correct} 题，待加强：{topics}",
                "每轮复习结束后更新进度",
            )
        )
    except pc.PowerContextError as exc:
        raise HTTPException(status_code=503, detail=f"PowerContext 写入失败：{exc}") from exc

    SESSIONS.pop(request.session_id, None)
    return {
        "score": correct,
        "total": len(details),
        "details": details,
        "writes": writes,
    }


@app.get("/api/health")
def health() -> dict:
    return pc.health()


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("APP_HOST", "127.0.0.1"),
        port=int(os.environ.get("APP_PORT", "7860")),
    )